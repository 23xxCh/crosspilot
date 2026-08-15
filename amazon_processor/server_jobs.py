"""Durable file-queue state for the unattended Windows worker.

This module owns input stability checks, content-hash acceptance, job-state
persistence, and restart reconciliation.  It deliberately has no knowledge of
the Amazon business pipeline or HTTP API.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Iterable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(
        path.suffix
        + f".{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt >= 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class JobState:
    source_path: str
    sha256: str
    status: str
    submitted_at: str = ""
    row_count: int = 0
    attempt: int = 0
    started_at: str = ""
    finished_at: str = ""
    next_retry_at: str = ""
    exit_code: int | None = None
    log_path: str = ""
    output_path: str = ""
    review_path: str = ""
    delivery_path: str = ""
    operator_delivery_path: str = ""
    failure_kind: str = ""
    pending_product_ids: list[str] | None = None
    isolated_product_ids: list[str] | None = None
    exception_path: str = ""
    source_name: str = ""
    accepted_at: str = ""
    updated_at: str = ""
    stage: str = "queued"
    progress_current: int = 0
    progress_total: int = 0
    queue_position: int = 0
    blocker_reason: str = ""
    outcome_path: str = ""
    error: str = ""


def begin_attempt(
    source: Path,
    file_hash: str,
    *,
    previous: JobState | None,
    logs_root: Path,
    outcomes_root: Path,
    started_at: str | None = None,
) -> JobState:
    """Create one isolated processing attempt while preserving intake metadata."""
    attempt = (previous.attempt if previous else 0) + 1
    log_path = Path(logs_root) / f"{file_hash}_{attempt:02d}.log"
    outcome_path = (
        Path(outcomes_root)
        / file_hash
        / f"attempt_{attempt:02d}"
        / "任务结果.json"
    )
    return JobState(
        source_path=str(Path(source).resolve()),
        source_name=(previous.source_name if previous else "") or source.name,
        sha256=file_hash,
        status="running",
        submitted_at=previous.submitted_at if previous else "",
        accepted_at=previous.accepted_at if previous else "",
        row_count=previous.row_count if previous else 0,
        attempt=attempt,
        started_at=started_at or utc_now(),
        log_path=str(log_path),
        outcome_path=str(outcome_path),
        stage="processing",
    )


class StabilityTracker:
    """Observe file stability across polls without sleeping per file."""

    def __init__(self) -> None:
        self._seen: dict[Path, tuple[int, int, float]] = {}

    def ready(
        self,
        path: Path,
        *,
        stable_seconds: float,
        now_monotonic: float | None = None,
    ) -> bool:
        try:
            stat = path.stat()
        except OSError:
            self._seen.pop(path, None)
            return False
        now_value = time.monotonic() if now_monotonic is None else now_monotonic
        signature = (stat.st_size, stat.st_mtime_ns)
        previous = self._seen.get(path)
        if not previous or previous[:2] != signature:
            self._seen[path] = (*signature, now_value)
            return stable_seconds <= 0 and stat.st_size > 0
        return bool(
            stat.st_size > 0
            and now_value - previous[2] >= max(0.0, stable_seconds)
        )

    def forget(self, path: Path) -> None:
        self._seen.pop(path, None)


def is_input_file(path: Path, ignored_prefixes: tuple[str, ...]) -> bool:
    if path.suffix.lower() != ".json" or path.name.startswith((".", "~$")):
        return False
    return not path.stem.startswith(ignored_prefixes)


def iter_input_files(
    input_dir: Path,
    *,
    ignored_prefixes: tuple[str, ...],
) -> Iterable[Path]:
    if not input_dir.exists():
        return ()
    candidates = [
        path
        for path in input_dir.glob("*.json")
        if is_input_file(path, ignored_prefixes) and path.is_file()
    ]

    def received_order(path: Path) -> tuple[int, str]:
        try:
            return path.stat().st_mtime_ns, path.name
        except OSError:
            return 2**63 - 1, path.name

    return iter(sorted(candidates, key=received_order))


def intake_files(
    input_dir: Path,
    *,
    default_inbox: Path,
    legacy_inbox: Path,
    ignored_prefixes: tuple[str, ...],
) -> list[Path]:
    input_path = Path(input_dir).expanduser().resolve()
    roots = [input_path]
    if input_path == default_inbox.resolve():
        legacy = legacy_inbox.resolve()
        if legacy != input_path:
            roots.append(legacy)
    files: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        for source in iter_input_files(root, ignored_prefixes=ignored_prefixes):
            resolved = source.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(source)
    return files


def is_file_stable(path: Path, stable_seconds: float = 5.0) -> bool:
    """Return true only when size and mtime stay unchanged during the window."""
    try:
        first = path.stat()
    except OSError:
        return False
    if stable_seconds > 0:
        time.sleep(stable_seconds)
    try:
        second = path.stat()
    except OSError:
        return False
    return (
        first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and second.st_size > 0
    )


def state_path(file_hash: str, *, jobs_root: Path) -> Path:
    return jobs_root / f"{file_hash}.json"


def load_state(file_hash: str, *, jobs_root: Path) -> JobState | None:
    path = state_path(file_hash, jobs_root=jobs_root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return JobState(**{
            key: payload.get(key, default)
            for key, default in asdict(JobState("", "", "")).items()
        })
    except (OSError, TypeError, ValueError):
        try:
            damaged = path.with_suffix(
                path.suffix + f".corrupt_{time.strftime('%Y%m%d_%H%M%S')}"
            )
            os.replace(path, damaged)
        except OSError:
            pass
        return None


def save_state(state: JobState, *, jobs_root: Path) -> None:
    state.updated_at = utc_now()
    atomic_json(state_path(state.sha256, jobs_root=jobs_root), asdict(state))


def safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", value).strip(" ._")
    return cleaned[:80] or "Amazon任务"


def unique_archive_path(
    root: Path,
    source: Path,
    file_hash: str,
) -> Path:
    month = datetime.now().strftime("%Y-%m")
    folder = root / month
    folder.mkdir(parents=True, exist_ok=True)
    stem = safe_name(source.stem)
    candidate = folder / f"{stem}_{file_hash[:12]}.json"
    suffix = 1
    while candidate.exists():
        try:
            if sha256_file(candidate) == file_hash:
                return candidate
        except OSError:
            pass
        candidate = folder / f"{stem}_{file_hash[:12]}_{suffix:02d}.json"
        suffix += 1
    return candidate


def accept_input(
    source: Path,
    *,
    accepted_root: Path,
    jobs_root: Path,
    terminal_statuses: set[str],
) -> JobState:
    """Atomically accept one stable inbox file and create its durable job."""
    source = Path(source).resolve()
    file_hash = sha256_file(source)
    previous = load_state(file_hash, jobs_root=jobs_root)
    root = Path(accepted_root)
    previous_source = Path(previous.source_path) if previous else None
    previous_is_archived = False
    if previous_source and previous_source.is_file():
        try:
            previous_source.resolve().relative_to(root.resolve())
            previous_is_archived = True
        except ValueError:
            previous_is_archived = False
    target = (
        previous_source
        if previous_source and previous_is_archived
        else unique_archive_path(root, source, file_hash)
    )
    submitted_at = previous.submitted_at if previous else utc_now()
    state = previous or JobState(
        source_path=str(source),
        source_name=source.name,
        sha256=file_hash,
        status="queued",
        submitted_at=submitted_at,
        stage="queued",
    )
    if not state.source_name:
        state.source_name = source.name
    if target.exists():
        if source != target and source.exists():
            source.unlink()
    else:
        os.replace(source, target)
    state.source_path = str(target.resolve())
    state.accepted_at = state.accepted_at or utc_now()
    if not previous or previous.status not in terminal_statuses:
        state.status = "queued"
        state.stage = "queued"
        state.next_retry_at = ""
    save_state(state, jobs_root=jobs_root)
    return state


def find_archived_source(
    file_hash: str,
    *,
    accepted_root: Path,
) -> Path | None:
    root = Path(accepted_root)
    if not root.exists():
        return None
    for candidate in root.rglob(f"*_{file_hash[:12]}*.json"):
        if "历史文件" in candidate.parts:
            continue
        try:
            if candidate.is_file() and sha256_file(candidate) == file_hash:
                return candidate.resolve()
        except OSError:
            continue
    return None


def reconcile_jobs(
    *,
    jobs_root: Path,
    accepted_root: Path,
    active_statuses: set[str],
    processor_active: bool,
    now: datetime | None = None,
) -> list[JobState]:
    """Repair interrupted acceptance/running states after a restart."""
    now_value = now or datetime.now(timezone.utc)
    accepted = Path(accepted_root)
    repaired: list[JobState] = []
    jobs_root.mkdir(parents=True, exist_ok=True)
    if accepted.exists():
        for source in accepted.rglob("*.json"):
            if "历史文件" in source.parts or not source.is_file():
                continue
            try:
                file_hash = sha256_file(source)
            except OSError:
                continue
            if load_state(file_hash, jobs_root=jobs_root):
                continue
            recovered = JobState(
                source_path=str(source.resolve()),
                source_name=source.name,
                sha256=file_hash,
                status="queued",
                submitted_at=utc_now(),
                accepted_at=utc_now(),
                stage="recovering",
                blocker_reason="已恢复受理后未写完状态的任务",
            )
            save_state(recovered, jobs_root=jobs_root)
    for path in sorted(jobs_root.glob("*.json")):
        state = load_state(path.stem, jobs_root=jobs_root)
        if not state:
            continue
        source = Path(state.source_path) if state.source_path else None
        if not source or not source.is_file():
            recovered = find_archived_source(
                state.sha256,
                accepted_root=accepted,
            )
            if recovered:
                state.source_path = str(recovered)
                state.blocker_reason = ""
            elif state.status in active_statuses:
                state.status = "invalid_input"
                state.stage = "stopped"
                state.failure_kind = "input"
                state.blocker_reason = "已受理输入文件缺失"
                state.error = state.blocker_reason
        if state.status == "running" and not processor_active:
            state.status = "retry_wait"
            state.stage = "recovering"
            state.failure_kind = "transient"
            state.next_retry_at = now_value.isoformat(timespec="seconds")
            state.blocker_reason = "检测到中断任务，将从缓存续跑"
        save_state(state, jobs_root=jobs_root)
        repaired.append(state)
    return repaired


def retry_allowed(
    state: JobState,
    now: datetime,
    *,
    retry_terminal: bool = False,
) -> bool:
    if state.status == "blocked":
        if retry_terminal:
            return True
        retry_at = parse_time(state.next_retry_at)
        return bool(retry_at and retry_at <= now)
    if state.status == "failed":
        return retry_terminal
    if state.status in {
        "published",
        "published_with_warnings",
        "invalid_input",
    }:
        return False
    if state.status == "pending_review":
        return retry_terminal or not state.delivery_path
    if state.status == "running":
        started = parse_time(state.started_at)
        return bool(started and started < now - timedelta(hours=2))
    retry_at = parse_time(state.next_retry_at)
    return retry_at is None or retry_at <= now


__all__ = [
    "JobState",
    "StabilityTracker",
    "accept_input",
    "atomic_json",
    "begin_attempt",
    "find_archived_source",
    "intake_files",
    "is_file_stable",
    "iter_input_files",
    "load_state",
    "parse_time",
    "reconcile_jobs",
    "retry_allowed",
    "safe_name",
    "save_state",
    "sha256_file",
    "state_path",
    "unique_archive_path",
    "utc_now",
]
