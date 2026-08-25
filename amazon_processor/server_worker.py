"""Windows-friendly one-job-at-a-time worker for unattended Amazon runs.

The worker deliberately stays outside the business pipeline.  It watches a
directory, claims each stable JSON input by content hash, starts one isolated
``amazon_processor run`` child process, and records a durable state file.  A
crashed child cannot take down the watcher.  Operationally unknown pending
results resume from cache, while deterministic review failures remain paused.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import time
from typing import Callable, Iterable

from .config.locking import ProcessLock, processor_is_running
from .delivery import REFILL_NAME, STATUS_NAME
from . import (
    operator_workspace,
    server_delivery,
    server_health,
    server_jobs,
    server_process,
    server_retention,
    server_state,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUT_ROOT = PROJECT_ROOT / "01_输入采集表"
LEGACY_INBOX = INPUT_ROOT / "待处理"
OPERATOR_ROOT = PROJECT_ROOT / "Amazon日常操作"
DEFAULT_INBOX = OPERATOR_ROOT / operator_workspace.INBOX_NAME
ACCEPTED_ROOT = INPUT_ROOT / "已接收"
RUNTIME_ROOT = PROJECT_ROOT / ".runtime" / "server"
JOBS_ROOT = RUNTIME_ROOT / "jobs"
LOGS_ROOT = RUNTIME_ROOT / "logs"
OUTCOMES_ROOT = RUNTIME_ROOT / "outcomes"
WORKER_LOCK = RUNTIME_ROOT / "worker.lock"
MAINTENANCE_PATH = RUNTIME_ROOT / "maintenance.json"
RETENTION_STATE_PATH = RUNTIME_ROOT / "retention.json"
CACHE_RETENTION_DAYS = 2
DELIVERIES_ROOT = PROJECT_ROOT / "02_处理结果" / "服务器交付"
FORMAL_LATEST_ROOT = PROJECT_ROOT / "02_处理结果" / "最新"

ACTIVE_STATUSES = {
    "queued",
    "running",
    "retry_wait",
    "delivery_retry",
    "blocked",
}
TERMINAL_STATUSES = {
    "published",
    "published_with_warnings",
    "pending_review",
    "invalid_input",
    "failed",
}

_IGNORED_PREFIXES = (
    "审核",
    "跨境电商自动化回填表",
    "终审",
    "运行状态",
    "待定",
)

JobState = server_jobs.JobState
StabilityTracker = server_jobs.StabilityTracker
_classify_failure = server_process.classify_failure
_classify_outcome_failure = server_process.classify_outcome_failure
_error_tail = server_process.error_tail
_extract_result = server_process.extract_result
_read_outcome = server_process.read_outcome


def _utc_now() -> str:
    return server_jobs.utc_now()


def _parse_time(value: str | None) -> datetime | None:
    return server_jobs.parse_time(value)


def sha256_file(path: Path) -> str:
    return server_jobs.sha256_file(path)


def _atomic_json(path: Path, value: dict) -> None:
    server_jobs.atomic_json(path, value)


def _is_input_file(path: Path) -> bool:
    return server_jobs.is_input_file(path, _IGNORED_PREFIXES)


def iter_input_files(input_dir: Path) -> Iterable[Path]:
    return server_jobs.iter_input_files(
        input_dir,
        ignored_prefixes=_IGNORED_PREFIXES,
    )


def intake_files(input_dir: Path) -> list[Path]:
    """Return new operator files plus pending files from the legacy inbox."""
    return server_jobs.intake_files(
        input_dir,
        default_inbox=DEFAULT_INBOX,
        legacy_inbox=LEGACY_INBOX,
        ignored_prefixes=_IGNORED_PREFIXES,
    )


def is_file_stable(path: Path, stable_seconds: float = 5.0) -> bool:
    """Return true only when size and mtime stay unchanged during the window."""
    return server_jobs.is_file_stable(path, stable_seconds)


def _state_path(file_hash: str) -> Path:
    return server_jobs.state_path(file_hash, jobs_root=JOBS_ROOT)


def _load_state(file_hash: str) -> JobState | None:
    return server_jobs.load_state(file_hash, jobs_root=JOBS_ROOT)


def _save_state(state: JobState) -> None:
    server_jobs.save_state(state, jobs_root=JOBS_ROOT)


def _unique_archive_path(root: Path, source: Path, file_hash: str) -> Path:
    return server_jobs.unique_archive_path(root, source, file_hash)


def accept_input(
    source: Path,
    *,
    accepted_root: Path | None = None,
) -> JobState:
    """Atomically accept one stable inbox file and create its durable job."""
    return server_jobs.accept_input(
        source,
        accepted_root=Path(accepted_root or ACCEPTED_ROOT),
        jobs_root=JOBS_ROOT,
        terminal_statuses=TERMINAL_STATUSES,
    )


def _find_archived_source(
    file_hash: str,
    *,
    accepted_root: Path | None = None,
) -> Path | None:
    return server_jobs.find_archived_source(
        file_hash,
        accepted_root=Path(accepted_root or ACCEPTED_ROOT),
    )


def reconcile_jobs(
    *,
    now: datetime | None = None,
    accepted_root: Path | None = None,
) -> list[JobState]:
    """Repair interrupted acceptance/running states after a restart."""
    return server_jobs.reconcile_jobs(
        jobs_root=JOBS_ROOT,
        accepted_root=Path(accepted_root or ACCEPTED_ROOT),
        active_statuses=ACTIVE_STATUSES,
        processor_active=processor_is_running(),
        now=now,
    )


def _retry_allowed(
    state: JobState,
    now: datetime,
    *,
    retry_terminal: bool = False,
) -> bool:
    return server_jobs.retry_allowed(
        state,
        now,
        retry_terminal=retry_terminal,
    )




def _load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层不是对象: {path}")
    return value


def validate_published_output(path: Path) -> dict:
    return server_delivery.validate_published_output(
        path,
        status_name=STATUS_NAME,
    )


def _hardlink_or_copy(source: str, target: str) -> str:
    return server_delivery.hardlink_or_copy(source, target)


def _safe_name(value: str) -> str:
    return server_jobs.safe_name(value)


def snapshot_delivery(
    state: JobState,
    *,
    category: str,
    artifact_dir: Path | None = None,
) -> Path:
    return server_delivery.snapshot_delivery(
        state,
        category=category,
        deliveries_root=DELIVERIES_ROOT,
        refill_name=REFILL_NAME,
        artifact_dir=artifact_dir,
    )


def _write_delivery_state(state: JobState) -> None:
    server_delivery.write_delivery_state(state, atomic_json=_atomic_json)


def _write_health(status: str, **details: object) -> None:
    server_health.write_health(
        RUNTIME_ROOT,
        status,
        pid=os.getpid(),
        updated_at=_utc_now(),
        atomic_json=_atomic_json,
        **details,
    )


def worker_health(max_age_seconds: float = 120.0) -> dict:
    """Return a machine-readable liveness result for Task Scheduler."""
    return server_health.worker_health(
        RUNTIME_ROOT,
        max_age_seconds,
        load_json=_load_json,
        parse_time=_parse_time,
        now=datetime.now(timezone.utc),
    )


def preflight(input_dir: Path, *, min_free_gb: float = 1.0) -> dict:
    """Fail fast before accepting jobs, without making paid API requests."""
    return server_health.preflight(
        input_dir,
        project_root=PROJECT_ROOT,
        runtime_root=RUNTIME_ROOT,
        jobs_root=JOBS_ROOT,
        logs_root=LOGS_ROOT,
        deliveries_root=DELIVERIES_ROOT,
        min_free_gb=min_free_gb,
        environ=os.environ,
    )




def retry_delay_seconds(
    attempt: int,
    *,
    retry_base_seconds: float = 30.0,
    jitter: bool = True,
) -> float:
    return server_state.retry_delay_seconds(
        attempt,
        retry_base_seconds=retry_base_seconds,
        jitter=jitter,
        random_uniform=random.uniform,
    )




def _apply_success_outcome(state: JobState, outcome: dict) -> None:
    server_state.apply_success_outcome(state, outcome)




def _pending_review_is_retryable(review_path: Path) -> bool:
    return server_delivery.pending_review_is_retryable(review_path)


def _disk_free_gb(path: Path = PROJECT_ROOT) -> float:
    return server_retention.disk_free_gb(path)


def _iter_states() -> list[JobState]:
    states: list[JobState] = []
    if not JOBS_ROOT.exists():
        return states
    for path in JOBS_ROOT.glob("*.json"):
        state = _load_state(path.stem)
        if state:
            states.append(state)
    return sorted(
        states,
        key=lambda item: (
            _parse_time(item.submitted_at) or datetime.max.replace(
                tzinfo=timezone.utc
            ),
            item.sha256,
        ),
    )


def _publish_operator_success(
    state: JobState,
    *,
    artifact_dir: Path,
) -> Path:
    return operator_workspace.publish_success(
        state,
        artifact_dir,
        root=OPERATOR_ROOT,
    )


def _publish_operator_attention(state: JobState) -> Path:
    return operator_workspace.publish_attention(state, root=OPERATOR_ROOT)


def refresh_operator_status(*, healthy: bool | None = None) -> Path | None:
    """Refresh the static operator page without exposing technical details."""
    try:
        if healthy is None:
            try:
                healthy = bool(worker_health(120.0).get("healthy"))
            except Exception:
                healthy = False
        if (RUNTIME_ROOT / "restart_required.json").is_file():
            healthy = False
        return operator_workspace.write_status_page(
            operator_workspace.summarize_jobs(_iter_states()),
            healthy=bool(healthy),
            path=operator_workspace.paths_for(OPERATOR_ROOT).status,
        )
    except Exception as exc:
        print(
            "[WORKER] 操作员状态页暂时无法更新: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return None


def repair_operator_deliveries() -> int:
    """Expose existing results after an upgrade without reprocessing inputs."""
    repaired = server_delivery.repair_operator_deliveries(
        _iter_states(),
        formal_latest_root=FORMAL_LATEST_ROOT,
        refill_name=REFILL_NAME,
        operator_root=OPERATOR_ROOT,
        save_state=_save_state,
        publish_success=lambda state, artifact_dir: _publish_operator_success(
            state,
            artifact_dir=artifact_dir,
        ),
        publish_attention=_publish_operator_attention,
    )
    refresh_operator_status()
    return repaired


def _active_queue() -> list[JobState]:
    queue = [state for state in _iter_states() if state.status in ACTIVE_STATUSES]
    for position, state in enumerate(queue, start=1):
        if state.queue_position != position:
            state.queue_position = position
            _save_state(state)
    return queue


def _maintenance_enabled() -> bool:
    if not MAINTENANCE_PATH.is_file():
        return False
    try:
        return bool(_load_json(MAINTENANCE_PATH).get("enabled"))
    except (OSError, TypeError, ValueError):
        return True


_remove_old_entries = server_retention.remove_old_entries


def _prune_cache_until(
    cache_root: Path,
    *,
    target_free_gb: float,
    disk_free: Callable[[], float],
) -> int:
    return server_retention.prune_cache_until(
        cache_root,
        target_free_gb=target_free_gb,
        disk_free=disk_free,
        processor_active=processor_is_running,
    )


def run_retention(
    *,
    now: datetime | None = None,
    disk_free: Callable[[], float] | None = None,
    accepted_root: Path | None = None,
    deliveries_root: Path | None = None,
    cache_root: Path | None = None,
) -> dict:
    """Compatibility entry for the isolated runtime-retention policy."""
    free_reader = disk_free or (lambda: _disk_free_gb(PROJECT_ROOT))
    paths = server_retention.RetentionPaths(
        project_root=PROJECT_ROOT,
        accepted_root=Path(accepted_root or ACCEPTED_ROOT),
        deliveries_root=Path(deliveries_root or DELIVERIES_ROOT),
        jobs_root=JOBS_ROOT,
        outcomes_root=OUTCOMES_ROOT,
        logs_root=LOGS_ROOT,
        cache_root=Path(
            cache_root or (PROJECT_ROOT / ".runtime" / "cache")
        ),
        report_path=RETENTION_STATE_PATH,
    )
    return server_retention.run_retention(
        paths=paths,
        states=_iter_states(),
        active_statuses=ACTIVE_STATUSES,
        processor_active=processor_is_running,
        now=now,
        disk_free=free_reader,
        cache_retention_days=CACHE_RETENTION_DAYS,
    )




def run_child(
    source: Path,
    log_path: Path,
    *,
    outcome_path: Path | None = None,
    timeout_hours: float = 24.0,
    stall_minutes: float = 45.0,
    heartbeat: Callable[[], None] | None = None,
) -> tuple[int, str]:
    """Compatibility wrapper around the isolated child supervisor."""
    return server_process.run_child(
        source,
        log_path,
        outcome_path=outcome_path,
        timeout_hours=timeout_hours,
        stall_minutes=stall_minutes,
        heartbeat=heartbeat,
        project_root=PROJECT_ROOT,
        python_executable=sys.executable,
        environment=os.environ,
        popen_factory=subprocess.Popen,
        timeout_error=subprocess.TimeoutExpired,
        monotonic=time.monotonic,
        sleep=time.sleep,
    )




def _schedule_operator_delivery_retry(
    state: JobState,
    exc: Exception,
    *,
    retry_base_seconds: float,
) -> JobState:
    return server_state.schedule_operator_delivery_retry(
        state,
        exc,
        retry_base_seconds=retry_base_seconds,
        retry_delay=retry_delay_seconds,
    )


def _retry_operator_delivery(
    state: JobState,
    *,
    retry_base_seconds: float,
) -> JobState:
    """Retry only local result packaging; never rerun paid processing."""
    try:
        output = Path(state.output_path)
        validate_published_output(output)
        target = _publish_operator_success(
            state,
            artifact_dir=output.parent,
        )
        state.operator_delivery_path = str(target)
        server_state.complete_operator_delivery(state)
    except Exception as exc:
        _schedule_operator_delivery_retry(
            state,
            exc,
            retry_base_seconds=retry_base_seconds,
        )
    _save_state(state)
    refresh_operator_status()
    return state


def process_one(
    source: Path,
    *,
    stable_seconds: float = 5.0,
    max_retries: int = 3,
    retry_base_seconds: float = 30.0,
    timeout_hours: float = 24.0,
    stall_minutes: float = 45.0,
    blocked_retry_hours: float = 6.0,
    retry_terminal: bool = False,
) -> JobState | None:
    if not is_file_stable(source, stable_seconds):
        return None
    file_hash = sha256_file(source)
    previous = _load_state(file_hash)
    now = datetime.now(timezone.utc)
    if previous and not _retry_allowed(
        previous,
        now,
        retry_terminal=retry_terminal,
    ):
        return previous
    if previous and previous.status == "delivery_retry":
        return _retry_operator_delivery(
            previous,
            retry_base_seconds=retry_base_seconds,
        )
    if previous and previous.status == "running" and processor_is_running():
        # A restarted watcher must not launch a second child while the old
        # child still owns the processor lock.
        return previous

    outcome_root = OUTCOMES_ROOT
    if LOGS_ROOT.parent != RUNTIME_ROOT:
        outcome_root = LOGS_ROOT.parent / "outcomes"
    state = server_jobs.begin_attempt(
        source,
        file_hash,
        previous=previous,
        logs_root=LOGS_ROOT,
        outcomes_root=outcome_root,
        started_at=_utc_now(),
    )
    attempt = state.attempt
    log_path = Path(state.log_path)
    outcome_path = Path(state.outcome_path)
    _save_state(state)
    refresh_operator_status(healthy=True)
    print(f"[WORKER] 开始处理: {source.name} ({file_hash[:12]})", flush=True)
    heartbeat_running = server_health.RunningJobHeartbeat(
        state=state,
        log_path=log_path,
        save_state=_save_state,
        refresh_status=lambda: refresh_operator_status(healthy=True),
        write_health=_write_health,
        telemetry=lambda: {
            "queue_depth": len(_active_queue()),
            "free_disk_gb": round(_disk_free_gb(), 2),
        },
    )

    exit_code, output = run_child(
        source,
        log_path,
        outcome_path=outcome_path,
        timeout_hours=timeout_hours,
        stall_minutes=stall_minutes,
        heartbeat=heartbeat_running,
    )
    state.exit_code = exit_code
    state.finished_at = _utc_now()
    outcome = _read_outcome(outcome_path)
    server_state.apply_child_result(
        state,
        outcome=outcome,
        exit_code=exit_code,
        output=output,
        attempt=attempt,
        max_retries=max_retries,
        classify_failure=_classify_failure,
        classify_outcome_failure=_classify_outcome_failure,
        extract_result=_extract_result,
    )

    if state.status in {"published", "published_with_warnings"}:
        try:
            status = validate_published_output(Path(state.output_path))
            pending_ids = [
                str(value)
                for value in status.get("pending_product_ids") or []
                if str(value)
            ]
            isolated_ids = [
                str(value)
                for value in status.get("isolated_product_ids") or pending_ids
                if str(value)
            ]
            state.pending_product_ids = pending_ids
            state.isolated_product_ids = (
                state.isolated_product_ids or isolated_ids
            )
            if state.isolated_product_ids:
                state.status = "published_with_warnings"
            state.delivery_path = str(snapshot_delivery(
                state,
                category="成功",
                artifact_dir=Path(state.output_path).parent,
            ))
            state.failure_kind = ""
            state.error = ""
            state.blocker_reason = ""
            state.stage = "completed"
        except Exception as exc:
            state.output_path = ""
            message = (
                "PublishedArtifactValidationError: "
                f"{type(exc).__name__}: {exc}"
            )
            state.status, state.error, state.failure_kind = (
                _classify_failure(message, attempt, max_retries)
            )
        else:
            try:
                state.operator_delivery_path = str(
                    _publish_operator_success(
                        state,
                        artifact_dir=Path(state.output_path).parent,
                    )
                )
            except Exception as exc:
                _schedule_operator_delivery_retry(
                    state,
                    exc,
                    retry_base_seconds=retry_base_seconds,
                )
    elif state.status == "pending_review":
        review = Path(state.review_path)
        pending_action = server_state.transition_pending_review(
            state,
            retryable=(
                review.is_file() and _pending_review_is_retryable(review)
            ),
        )
        if pending_action.delivery_category:
            state.delivery_path = str(snapshot_delivery(
                state,
                category=pending_action.delivery_category,
                artifact_dir=review.parent if review.is_file() else None,
            ))
            if pending_action.needs_operator_attention:
                try:
                    state.operator_delivery_path = str(
                        _publish_operator_attention(state)
                    )
                except Exception:
                    pass
    failure_action = server_state.finalize_failure_state(
        state,
        output=output,
        attempt=attempt,
        retry_base_seconds=retry_base_seconds,
        blocked_retry_hours=blocked_retry_hours,
        retry_delay=retry_delay_seconds,
        error_tail=_error_tail,
    )
    if failure_action.delivery_category:
        state.delivery_path = str(snapshot_delivery(
            state,
            category=failure_action.delivery_category,
        ))
    if failure_action.needs_operator_attention:
        try:
            state.operator_delivery_path = str(
                _publish_operator_attention(state)
            )
        except Exception:
            pass
    _save_state(state)
    _write_delivery_state(state)
    _write_health(
        (
            "degraded"
            if state.status in {"retry_wait", "delivery_retry"}
            else "idle"
        ),
        last_job=file_hash,
        last_job_status=state.status,
        delivery_path=state.delivery_path,
        next_retry_at=state.next_retry_at,
        isolated_count=len(state.isolated_product_ids or []),
        free_disk_gb=round(_disk_free_gb(), 2),
    )
    refresh_operator_status()
    print(
        f"[WORKER] {state.status}: {source.name} (exit={exit_code})",
        flush=True,
    )
    return state


def run_worker(
    *,
    input_dir: str | Path = DEFAULT_INBOX,
    poll_seconds: float = 15.0,
    stable_seconds: float = 5.0,
    max_retries: int = 3,
    retry_base_seconds: float = 30.0,
    timeout_hours: float = 24.0,
    stall_minutes: float = 45.0,
    blocked_retry_hours: float = 6.0,
    retry_terminal: bool = False,
    once: bool = False,
) -> int:
    """Watch the reliable inbox and process accepted jobs in FIFO order."""
    input_path = Path(input_dir).expanduser().resolve()
    default_inbox = DEFAULT_INBOX.resolve()
    accepted_root = (
        ACCEPTED_ROOT
        if input_path == default_inbox
        else input_path.parent / "已接收"
    )
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    with ProcessLock(WORKER_LOCK):
        operator_workspace.ensure_workspace(OPERATOR_ROOT)
        try:
            readiness = preflight(input_path)
        except Exception as exc:
            readiness = {
                "input_dir": str(input_path),
                "image_processing_mode": "unknown",
                "free_disk_gb": round(_disk_free_gb(), 2),
                "missing_operations": ["preflight"],
                "blocker_reason": f"{type(exc).__name__}: {exc}",
            }
            print(
                f"[WORKER] 启动预检需要处理: {type(exc).__name__}: {exc}",
                flush=True,
            )
        print(f"[WORKER] 监控目录: {input_path}", flush=True)
        accepted_root.mkdir(parents=True, exist_ok=True)
        reconcile_jobs(accepted_root=accepted_root)
        repair_operator_deliveries()
        tracker = StabilityTracker()
        last_retention = 0.0
        last_preflight = time.monotonic()
        configuration_blocked = bool(readiness.get("missing_operations"))
        once_observed = False
        _write_health("idle", queue_depth=len(_active_queue()), **readiness)
        refresh_operator_status(healthy=not configuration_blocked)
        while True:
            if (
                configuration_blocked
                and time.monotonic() - last_preflight >= 6 * 3600
            ):
                try:
                    readiness = preflight(input_path)
                except Exception as exc:
                    readiness = {
                        "input_dir": str(input_path),
                        "image_processing_mode": "unknown",
                        "free_disk_gb": round(_disk_free_gb(), 2),
                        "missing_operations": ["preflight"],
                        "blocker_reason": f"{type(exc).__name__}: {exc}",
                    }
                configuration_blocked = bool(
                    readiness.get("missing_operations")
                )
                last_preflight = time.monotonic()
            free_gb = _disk_free_gb()
            maintenance = _maintenance_enabled()
            if time.monotonic() - last_retention >= 24 * 3600:
                try:
                    run_retention(
                        accepted_root=accepted_root,
                        disk_free=lambda: _disk_free_gb(),
                    )
                except Exception as exc:
                    print(
                        f"[WORKER] 保留策略执行失败: {type(exc).__name__}: {exc}",
                        flush=True,
                    )
                last_retention = time.monotonic()
                free_gb = _disk_free_gb()

            inbox_files = intake_files(input_path)
            intake_control = server_state.evaluate_worker_control(
                maintenance=maintenance,
                configuration_blocked=configuration_blocked,
                configuration_blocker=str(
                    readiness.get("blocker_reason") or ""
                ),
                free_disk_gb=free_gb,
                current=None,
            )
            if intake_control.accepts_inputs:
                for source in inbox_files:
                    if not tracker.ready(
                        source,
                        stable_seconds=stable_seconds,
                    ):
                        continue
                    try:
                        accepted = accept_input(
                            source,
                            accepted_root=accepted_root,
                        )
                        tracker.forget(source)
                        print(
                            f"[WORKER] 已受理: {accepted.source_name} "
                            f"({accepted.sha256[:12]})",
                            flush=True,
                        )
                        refresh_operator_status(healthy=True)
                    except Exception as exc:
                        print(
                            f"[WORKER] 受理失败 {source.name}: "
                            f"{type(exc).__name__}: {exc}",
                            flush=True,
                        )

            queue = _active_queue()
            current = queue[0] if queue else None
            now = datetime.now(timezone.utc)
            control = server_state.evaluate_worker_control(
                maintenance=maintenance,
                configuration_blocked=configuration_blocked,
                configuration_blocker=str(
                    readiness.get("blocker_reason") or ""
                ),
                free_disk_gb=free_gb,
                current=current,
            )
            if (
                current
                and control.runs_jobs
                and _retry_allowed(
                    current,
                    now,
                    retry_terminal=retry_terminal,
                )
            ):
                source = Path(current.source_path)
                try:
                    process_one(
                        source,
                        stable_seconds=0,
                        max_retries=max_retries,
                        retry_base_seconds=retry_base_seconds,
                        timeout_hours=timeout_hours,
                        stall_minutes=stall_minutes,
                        blocked_retry_hours=blocked_retry_hours,
                        retry_terminal=retry_terminal,
                    )
                except Exception as exc:
                    # process_one may have persisted a terminal state before
                    # raising (e.g. a disk error while writing the delivery
                    # snapshot).  Re-read the durable state so a late failure
                    # cannot clobber a finished job and rerun paid processing.
                    saved = _load_state(current.sha256)
                    if saved and saved.status in TERMINAL_STATUSES:
                        continue
                    current.status = "retry_wait"
                    current.stage = "recovering"
                    current.failure_kind = "internal"
                    current.error = f"{type(exc).__name__}: {exc}"
                    current.blocker_reason = current.error
                    if current.attempt >= max(1, max_retries):
                        current.status = "failed"
                        current.stage = "stopped"
                    else:
                        delay = retry_delay_seconds(
                            max(1, current.attempt),
                            retry_base_seconds=retry_base_seconds,
                        )
                        current.next_retry_at = (
                            now + timedelta(seconds=delay)
                        ).isoformat(timespec="seconds")
                    _save_state(current)

            queue = _active_queue()
            current = queue[0] if queue else None
            control = server_state.evaluate_worker_control(
                maintenance=maintenance,
                configuration_blocked=configuration_blocked,
                configuration_blocker=str(
                    readiness.get("blocker_reason") or ""
                ),
                free_disk_gb=free_gb,
                current=current,
            )
            successful = [
                state for state in _iter_states()
                if state.status in {"published", "published_with_warnings"}
            ]
            last_success = max(
                (state.finished_at for state in successful if state.finished_at),
                default="",
            )
            _write_health(
                control.health_status,
                input_dir=str(input_path),
                accepted_dir=str(accepted_root),
                queue_depth=len(queue),
                inbox_depth=len(inbox_files),
                current_job=current.sha256 if current else "",
                stage=(
                    current.stage if current else control.health_status
                ),
                progress_current=current.progress_current if current else 0,
                progress_total=current.progress_total if current else 0,
                next_retry_at=current.next_retry_at if current else "",
                blocker_reason=control.blocker_reason,
                free_disk_gb=round(free_gb, 2),
                last_success_at=last_success,
            )
            refresh_operator_status(
                healthy=control.operator_healthy,
            )
            if once and (once_observed or stable_seconds <= 0):
                _write_health("stopped", input_dir=str(input_path))
                return 0
            once_observed = True
            sleep_seconds = (
                max(0.01, stable_seconds)
                if once
                else max(1.0, poll_seconds)
            )
            time.sleep(sleep_seconds)


__all__ = [
    "ACCEPTED_ROOT",
    "DEFAULT_INBOX",
    "JobState",
    "StabilityTracker",
    "accept_input",
    "intake_files",
    "iter_input_files",
    "is_file_stable",
    "process_one",
    "preflight",
    "reconcile_jobs",
    "retry_delay_seconds",
    "run_child",
    "run_worker",
    "run_retention",
    "refresh_operator_status",
    "repair_operator_deliveries",
    "sha256_file",
    "snapshot_delivery",
    "validate_published_output",
    "worker_health",
]
