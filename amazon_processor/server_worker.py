"""Windows-friendly one-job-at-a-time worker for unattended Amazon runs.

The worker deliberately stays outside the business pipeline.  It watches a
directory, claims each stable JSON input by content hash, starts one isolated
``amazon_processor run`` child process, and records a durable state file.  A
crashed child cannot take down the watcher, and a pending/manual-review result
is never retried automatically.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Iterable

from .config.locking import ProcessBusyError, ProcessLock, processor_is_running


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = PROJECT_ROOT / ".runtime" / "server"
JOBS_ROOT = RUNTIME_ROOT / "jobs"
LOGS_ROOT = RUNTIME_ROOT / "logs"
WORKER_LOCK = RUNTIME_ROOT / "worker.lock"

_IGNORED_PREFIXES = (
    "审核",
    "跨境电商自动化回填表",
    "终审",
    "运行状态",
    "待定",
)
_AUTH_MARKERS = (
    "鉴权",
    "api key",
    "api_key",
    "401",
    "403",
    "余额",
    "额度",
    "quota",
    "billing",
    "insufficient",
)
_PUBLISHED_MARKER = "正式表已更新:"
_PENDING_MARKER = "待人工审核包:"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: str | None) -> datetime | None:
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


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _is_input_file(path: Path) -> bool:
    if path.suffix.lower() != ".json" or path.name.startswith((".", "~$")):
        return False
    return not path.stem.startswith(_IGNORED_PREFIXES)


def iter_input_files(input_dir: Path) -> Iterable[Path]:
    if not input_dir.exists():
        return ()
    return (
        path
        for path in sorted(input_dir.glob("*.json"), key=lambda item: item.name)
        if _is_input_file(path) and path.is_file()
    )


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


@dataclass
class JobState:
    source_path: str
    sha256: str
    status: str
    attempt: int = 0
    started_at: str = ""
    finished_at: str = ""
    next_retry_at: str = ""
    exit_code: int | None = None
    log_path: str = ""
    output_path: str = ""
    review_path: str = ""
    error: str = ""


def _state_path(file_hash: str) -> Path:
    return JOBS_ROOT / f"{file_hash}.json"


def _load_state(file_hash: str) -> JobState | None:
    path = _state_path(file_hash)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return JobState(**{
            key: payload.get(key, default)
            for key, default in asdict(JobState("", "", "")).items()
        })
    except (OSError, TypeError, ValueError):
        return None


def _save_state(state: JobState) -> None:
    _atomic_json(_state_path(state.sha256), asdict(state))


def _retry_allowed(
    state: JobState,
    now: datetime,
    *,
    retry_terminal: bool = False,
) -> bool:
    if state.status in {"blocked", "failed"}:
        return retry_terminal
    if state.status in {
        "published",
        "pending_review",
        "succeeded",
    }:
        return False
    if state.status == "running":
        started = _parse_time(state.started_at)
        return bool(started and started < now - timedelta(hours=2))
    retry_at = _parse_time(state.next_retry_at)
    return retry_at is None or retry_at <= now


def _classify_failure(text: str, attempt: int, max_retries: int) -> tuple[str, str]:
    lowered = text.lower()
    if any(marker.lower() in lowered for marker in _AUTH_MARKERS):
        return "blocked", "鉴权或余额错误，已停止自动重试"
    if attempt >= max_retries:
        return "failed", f"已达到最大自动重试次数 ({max_retries})"
    return "retry_wait", "任务异常退出，等待下一轮重试"


def _extract_result(text: str) -> tuple[str, str, str]:
    published = re.search(r"正式表已更新:\s*(.+)", text)
    if published:
        return "published", published.group(1).strip(), ""
    pending = re.search(r"待人工审核包:\s*(.+)", text)
    if pending:
        return "pending_review", "", pending.group(1).strip()
    return "succeeded", "", ""


def run_child(
    source: Path,
    log_path: Path,
    *,
    timeout_hours: float = 24.0,
) -> tuple[int, str]:
    """Run one isolated CLI child and return its exit code and captured output."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "amazon_processor", "run", str(source)]
    try:
        completed = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(0.1, timeout_hours * 3600),
            check=False,
        )
        output = completed.stdout or ""
        log_path.write_text(output, encoding="utf-8")
        return int(completed.returncode), output
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        output += "\n[WORKER] 单个任务超过最大运行时间，已终止\n"
        log_path.write_text(output, encoding="utf-8")
        return 124, output


def process_one(
    source: Path,
    *,
    stable_seconds: float = 5.0,
    max_retries: int = 3,
    retry_base_seconds: float = 30.0,
    timeout_hours: float = 24.0,
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
    if previous and previous.status == "running" and processor_is_running():
        # A restarted watcher must not launch a second child while the old
        # child still owns the processor lock.
        return previous

    attempt = (previous.attempt if previous else 0) + 1
    log_path = LOGS_ROOT / f"{file_hash}_{attempt:02d}.log"
    state = JobState(
        source_path=str(source.resolve()),
        sha256=file_hash,
        status="running",
        attempt=attempt,
        started_at=_utc_now(),
        log_path=str(log_path),
    )
    _save_state(state)
    print(f"[WORKER] 开始处理: {source.name} ({file_hash[:12]})", flush=True)
    exit_code, output = run_child(
        source,
        log_path,
        timeout_hours=timeout_hours,
    )
    state.exit_code = exit_code
    state.finished_at = _utc_now()
    if exit_code == 0:
        state.status, state.output_path, state.review_path = _extract_result(output)
        state.error = ""
    else:
        state.status, state.error = _classify_failure(
            output,
            attempt,
            max_retries,
        )
        if state.status == "retry_wait":
            delay = min(
                retry_base_seconds * (2 ** max(0, attempt - 1)),
                3600,
            )
            state.next_retry_at = (
                datetime.now(timezone.utc) + timedelta(seconds=delay)
            ).isoformat(timespec="seconds")
    _save_state(state)
    print(
        f"[WORKER] {state.status}: {source.name} (exit={exit_code})",
        flush=True,
    )
    return state


def run_worker(
    *,
    input_dir: str | Path = PROJECT_ROOT / "01_输入采集表",
    poll_seconds: float = 15.0,
    stable_seconds: float = 5.0,
    max_retries: int = 3,
    retry_base_seconds: float = 30.0,
    timeout_hours: float = 24.0,
    retry_terminal: bool = False,
    once: bool = False,
) -> int:
    """Watch the input directory until interrupted, or process one pass."""
    input_path = Path(input_dir).expanduser().resolve()
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    with ProcessLock(WORKER_LOCK):
        print(f"[WORKER] 监控目录: {input_path}", flush=True)
        while True:
            for source in iter_input_files(input_path):
                try:
                    process_one(
                        source,
                        stable_seconds=stable_seconds,
                        max_retries=max_retries,
                        retry_base_seconds=retry_base_seconds,
                        timeout_hours=timeout_hours,
                        retry_terminal=retry_terminal,
                    )
                except Exception as exc:
                    print(
                        f"[WORKER] 跳过 {source.name}: {type(exc).__name__}: {exc}",
                        flush=True,
                    )
            if once:
                return 0
            time.sleep(max(1.0, poll_seconds))


__all__ = [
    "JobState",
    "iter_input_files",
    "is_file_stable",
    "process_one",
    "run_child",
    "run_worker",
    "sha256_file",
]
