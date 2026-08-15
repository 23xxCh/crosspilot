"""Offline fault-injection soak test for the unattended file queue.

The harness uses an isolated temporary directory and never imports the Amazon
pipeline or a Provider.  It is therefore safe to run for hours without API
costs or changes to production input, cache, state, or output directories.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time

from . import server_jobs


_TERMINAL_STATUSES = {
    "published",
    "published_with_warnings",
    "invalid_input",
}
_ACTIVE_STATUSES = {"queued", "retry_wait", "running", "blocked"}


def _record_failure(failures: list[str], message: str) -> None:
    if len(failures) < 20:
        failures.append(message)


def _finish_state(
    state: server_jobs.JobState,
    *,
    jobs_root: Path,
) -> None:
    state.status = "published"
    state.stage = "completed"
    state.finished_at = server_jobs.utc_now()
    state.next_retry_at = ""
    state.failure_kind = ""
    state.blocker_reason = ""
    server_jobs.save_state(state, jobs_root=jobs_root)


def run_soak(
    *,
    cycles: int = 100,
    duration_seconds: float = 0.0,
    interval_seconds: float = 0.0,
    report_path: Path | None = None,
    unique_job_limit: int = 64,
) -> dict:
    """Exercise idempotency, restart recovery, and corrupt-state isolation."""
    if cycles <= 0 and duration_seconds <= 0:
        raise ValueError("cycles 和 duration_seconds 至少一个必须大于 0")
    unique_limit = max(2, unique_job_limit)
    started_wall = datetime.now(timezone.utc)
    started = time.monotonic()
    deadline = started + duration_seconds if duration_seconds > 0 else None
    failures: list[str] = []
    stats = {
        "cycles": 0,
        "submissions": 0,
        "unique_jobs": 0,
        "duplicate_submissions": 0,
        "interrupted_jobs_recovered": 0,
        "orphan_states_recovered": 0,
        "corrupt_states_quarantined": 0,
    }

    with tempfile.TemporaryDirectory(prefix="amazon_processor_soak_") as raw:
        root = Path(raw)
        inbox = root / "inbox"
        accepted = root / "accepted"
        jobs = root / "jobs"
        inbox.mkdir()
        known_hashes: list[str] = []
        iteration = 0

        while True:
            if cycles > 0 and iteration >= cycles:
                break
            if deadline is not None and time.monotonic() >= deadline:
                break

            slot = iteration % unique_limit
            source = inbox / f"task_{iteration:08d}.json"
            source.write_text(
                json.dumps({"商品id": [f"soak-{slot:04d}"]}),
                encoding="utf-8",
            )
            state = server_jobs.accept_input(
                source,
                accepted_root=accepted,
                jobs_root=jobs,
                terminal_statuses=_TERMINAL_STATUSES,
            )
            stats["submissions"] += 1
            if state.sha256 in known_hashes:
                stats["duplicate_submissions"] += 1
                if state.status != "published":
                    _record_failure(
                        failures,
                        f"重复任务状态被重置: {state.sha256[:12]}",
                    )
            else:
                known_hashes.append(state.sha256)
                stats["unique_jobs"] += 1
                state.status = "running"
                state.stage = "processing"
                state.started_at = server_jobs.utc_now()
                server_jobs.save_state(state, jobs_root=jobs)
                _finish_state(state, jobs_root=jobs)

            if source.exists():
                _record_failure(failures, f"受理后收件箱文件仍存在: {source.name}")

            if known_hashes and iteration % 3 == 0:
                interrupted_hash = known_hashes[iteration % len(known_hashes)]
                interrupted = server_jobs.load_state(
                    interrupted_hash,
                    jobs_root=jobs,
                )
                if interrupted:
                    interrupted.status = "running"
                    interrupted.stage = "processing"
                    interrupted.started_at = server_jobs.utc_now()
                    server_jobs.save_state(interrupted, jobs_root=jobs)
                    repaired = server_jobs.reconcile_jobs(
                        jobs_root=jobs,
                        accepted_root=accepted,
                        active_statuses=_ACTIVE_STATUSES,
                        processor_active=False,
                    )
                    recovered = next(
                        (
                            item
                            for item in repaired
                            if item.sha256 == interrupted_hash
                        ),
                        None,
                    )
                    if recovered and recovered.status == "retry_wait":
                        stats["interrupted_jobs_recovered"] += 1
                        _finish_state(recovered, jobs_root=jobs)
                    else:
                        _record_failure(
                            failures,
                            f"中断任务未进入 retry_wait: {interrupted_hash[:12]}",
                        )

            iteration += 1
            stats["cycles"] = iteration
            if interval_seconds > 0:
                time.sleep(interval_seconds)

        orphan = accepted / "2099-01" / "orphan_feed.json"
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_text('{"商品id":["orphan"]}', encoding="utf-8")
        orphan_hash = server_jobs.sha256_file(orphan)
        repaired = server_jobs.reconcile_jobs(
            jobs_root=jobs,
            accepted_root=accepted,
            active_statuses=_ACTIVE_STATUSES,
            processor_active=False,
        )
        orphan_state = next(
            (item for item in repaired if item.sha256 == orphan_hash),
            None,
        )
        if orphan_state and orphan_state.status == "queued":
            stats["orphan_states_recovered"] = 1
            _finish_state(orphan_state, jobs_root=jobs)
        else:
            _record_failure(failures, "已归档但未写状态的任务没有恢复")

        corrupt_hash = "f" * 64
        corrupt_path = jobs / f"{corrupt_hash}.json"
        corrupt_path.write_text("{broken", encoding="utf-8")
        if server_jobs.load_state(corrupt_hash, jobs_root=jobs) is not None:
            _record_failure(failures, "损坏状态文件被错误加载")
        quarantined = list(jobs.glob(f"{corrupt_hash}.json.corrupt_*"))
        if quarantined and not corrupt_path.exists():
            stats["corrupt_states_quarantined"] = 1
        else:
            _record_failure(failures, "损坏状态文件没有隔离")

        expected_jobs = len(known_hashes) + 1
        valid_states = []
        for state_file in jobs.glob("*.json"):
            state = server_jobs.load_state(state_file.stem, jobs_root=jobs)
            if state:
                valid_states.append(state)
        if len(valid_states) != expected_jobs:
            _record_failure(
                failures,
                f"状态数量不一致: expected={expected_jobs}, actual={len(valid_states)}",
            )
        for state in valid_states:
            if state.status != "published":
                _record_failure(
                    failures,
                    f"任务未收敛到 published: {state.sha256[:12]}={state.status}",
                )
            if not Path(state.source_path).is_file():
                _record_failure(
                    failures,
                    f"任务归档文件丢失: {state.sha256[:12]}",
                )

    finished = datetime.now(timezone.utc)
    report = {
        "version": 1,
        "mode": "offline_fault_injection",
        "provider_requests": 0,
        "started_at": started_wall.isoformat(timespec="seconds"),
        "finished_at": finished.isoformat(timespec="seconds"),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        **stats,
        "invariant_failures": len(failures),
        "failures": failures,
        "passed": not failures,
    }
    if report_path is not None:
        server_jobs.atomic_json(Path(report_path), report)
    return report


__all__ = ["run_soak"]
