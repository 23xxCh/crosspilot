"""Offline Worker readiness checks and machine-readable heartbeat health."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil

from .config import credentials, env, models
from . import server_jobs


JsonLoader = Callable[[Path], dict]
TimeParser = Callable[[str | None], datetime | None]
AtomicJsonWriter = Callable[[Path, dict], None]
StateSaver = Callable[[server_jobs.JobState], None]
StatusRefresher = Callable[[], object]
HealthWriter = Callable[..., None]
TelemetryProvider = Callable[[], Mapping[str, object]]


def _load_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("heartbeat payload must be an object")
    return payload


def progress_from_log(path: Path) -> tuple[str, int, int]:
    """Read the latest complete ``[stage] current/total`` progress marker."""
    if not path.is_file():
        return "processing", 0, 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[-12000:]
    except OSError:
        return "processing", 0, 0
    matches = list(re.finditer(r"\[([^\]]+)]\s*(\d+)\s*/\s*(\d+)", text))
    if not matches:
        return "processing", 0, 0
    match = matches[-1]
    return match.group(1).strip(), int(match.group(2)), int(match.group(3))


@dataclass
class RunningJobHeartbeat:
    """Persist changed job progress while refreshing liveness every poll."""

    state: server_jobs.JobState
    log_path: Path
    save_state: StateSaver
    refresh_status: StatusRefresher
    write_health: HealthWriter
    telemetry: TelemetryProvider
    _saved_progress: tuple[str, int, int] | None = field(
        default=None,
        init=False,
    )

    def __call__(self) -> None:
        stage, current, total = progress_from_log(self.log_path)
        self.state.stage = stage
        self.state.progress_current = current
        self.state.progress_total = total
        progress = (stage, current, total)
        if progress != self._saved_progress:
            self.save_state(self.state)
            self.refresh_status()
            self._saved_progress = progress
        self.write_health(
            "running",
            current_job=self.state.sha256,
            source_path=self.state.source_path,
            attempt=self.state.attempt,
            log_path=str(self.log_path),
            stage=stage,
            progress_current=current,
            progress_total=total,
            **dict(self.telemetry()),
        )


def write_health(
    runtime_root: Path,
    status: str,
    *,
    pid: int | None = None,
    updated_at: str | None = None,
    atomic_json: AtomicJsonWriter = server_jobs.atomic_json,
    **details: object,
) -> None:
    """Atomically write one Worker heartbeat without exposing credentials."""
    payload = {
        "version": 1,
        "status": status,
        "pid": os.getpid() if pid is None else int(pid),
        "updated_at": updated_at or server_jobs.utc_now(),
        **details,
    }
    atomic_json(Path(runtime_root) / "heartbeat.json", payload)


def worker_health(
    runtime_root: Path,
    max_age_seconds: float = 120.0,
    *,
    load_json: JsonLoader = _load_json,
    parse_time: TimeParser = server_jobs.parse_time,
    now: datetime | None = None,
) -> dict:
    """Return separate liveness and readiness signals for watchdog callers."""
    path = Path(runtime_root) / "heartbeat.json"
    if not path.is_file():
        return {
            "healthy": False,
            "status": "missing",
            "message": "Worker 尚未写入心跳",
        }
    try:
        payload = load_json(path)
        updated = parse_time(str(payload.get("updated_at") or ""))
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        return {
            "healthy": False,
            "status": "invalid",
            "message": f"心跳文件损坏: {exc}",
        }
    current = now or datetime.now(timezone.utc)
    age = (current - updated).total_seconds() if updated else float("inf")
    payload["age_seconds"] = round(max(0.0, age), 1)
    payload["healthy"] = bool(
        updated
        and age <= max(1.0, max_age_seconds)
        and payload.get("status") not in {"stopped"}
    )
    payload["ready"] = bool(
        payload["healthy"]
        and payload.get("status")
        not in {"needs_attention", "blocked_disk", "maintenance"}
    )
    return payload


def preflight(
    input_dir: Path,
    *,
    project_root: Path,
    runtime_root: Path,
    jobs_root: Path,
    logs_root: Path,
    deliveries_root: Path,
    min_free_gb: float = 1.0,
    environ: Mapping[str, str] | None = None,
) -> dict:
    """Check credentials, writable directories and disk without paid calls."""
    registry = models.get_model_registry()
    store = credentials.CredentialStore(
        registry,
        env_path=env.ENV_PATH,
        environ=os.environ if environ is None else environ,
    )
    missing: list[str] = []
    for operation in ("text", "vision"):
        usable = any(
            target.provider == "ollama"
            or bool(store.value(target.credential))
            for target in registry.routes(operation)
        )
        if not usable:
            missing.append(operation)

    directories = (
        Path(input_dir),
        Path(runtime_root),
        Path(jobs_root),
        Path(logs_root),
        Path(deliveries_root),
    )
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    probe = Path(runtime_root) / f".write_probe_{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
    finally:
        probe.unlink(missing_ok=True)

    free_gb = shutil.disk_usage(project_root).free / (1024**3)
    if free_gb < max(0.1, min_free_gb):
        raise OSError(
            f"项目磁盘剩余空间不足: {free_gb:.2f} GB，"
            f"至少需要 {min_free_gb:.2f} GB"
        )
    return {
        "input_dir": str(input_dir),
        "image_processing_mode": "select_existing",
        "free_disk_gb": round(free_gb, 2),
        "missing_operations": missing,
        "blocker_reason": (
            "以下处理阶段没有可用模型凭据: " + ", ".join(missing)
            if missing
            else ""
        ),
    }


__all__ = [
    "RunningJobHeartbeat",
    "preflight",
    "progress_from_log",
    "worker_health",
    "write_health",
]
