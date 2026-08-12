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
import random
import re
import shutil
import subprocess
import sys
import time
from typing import Callable, Iterable

from .config.locking import ProcessLock, processor_is_running
from .delivery import STATUS_NAME
from .schema import AMAZON_JSON_OUTPUT_FIELDS, validate_columnar_payload


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUT_ROOT = PROJECT_ROOT / "01_输入采集表"
DEFAULT_INBOX = INPUT_ROOT / "待处理"
ACCEPTED_ROOT = INPUT_ROOT / "已接收"
RUNTIME_ROOT = PROJECT_ROOT / ".runtime" / "server"
JOBS_ROOT = RUNTIME_ROOT / "jobs"
LOGS_ROOT = RUNTIME_ROOT / "logs"
OUTCOMES_ROOT = RUNTIME_ROOT / "outcomes"
WORKER_LOCK = RUNTIME_ROOT / "worker.lock"
MAINTENANCE_PATH = RUNTIME_ROOT / "maintenance.json"
RETENTION_STATE_PATH = RUNTIME_ROOT / "retention.json"
DELIVERIES_ROOT = PROJECT_ROOT / "02_处理结果" / "服务器交付"

ACTIVE_STATUSES = {"queued", "running", "retry_wait", "blocked"}
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
_TRANSIENT_MARKERS = (
    "providerunavailable",
    "timeout",
    "timed out",
    "connection",
    "network",
    "dns",
    "load failed",
    "gateway",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "status 429",
    "status 500",
    "status 502",
    "status 503",
    "status 504",
    " 429",
    " 503",
    "无日志进展",
)
_INPUT_MARKERS = (
    "jsondecodeerror",
    "文件不是有效的 amazon json",
    "amazon json 顶层必须是对象",
    "采集表不存在",
    "仅支持 amazon json",
    "缺少字段",
    "数组长度必须一致",
    "必须是数组",
    "没有商品数据",
    "未知 amazon 站点",
    "超过安全上限",
)
_PUBLISHED_MARKER = "正式表已更新:"
_PENDING_MARKER = "待人工审核包:"
_SECRET_RE = re.compile(
    r"(?i)(bearer\s+|(?:sk|cpk)-)[A-Za-z0-9._:/+\-]+"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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
    candidates = [
        path
        for path in input_dir.glob("*.json")
        if _is_input_file(path) and path.is_file()
    ]

    def received_order(path: Path) -> tuple[int, str]:
        try:
            return path.stat().st_mtime_ns, path.name
        except OSError:
            return 2**63 - 1, path.name

    return iter(sorted(candidates, key=received_order))


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
        try:
            damaged = path.with_suffix(
                path.suffix + f".corrupt_{time.strftime('%Y%m%d_%H%M%S')}"
            )
            os.replace(path, damaged)
        except OSError:
            pass
        return None


def _save_state(state: JobState) -> None:
    state.updated_at = _utc_now()
    _atomic_json(_state_path(state.sha256), asdict(state))


def _unique_archive_path(root: Path, source: Path, file_hash: str) -> Path:
    month = datetime.now().strftime("%Y-%m")
    folder = root / month
    folder.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(source.stem)
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
    accepted_root: Path | None = None,
) -> JobState:
    """Atomically accept one stable inbox file and create its durable job."""
    source = Path(source).resolve()
    file_hash = sha256_file(source)
    previous = _load_state(file_hash)
    root = Path(accepted_root or ACCEPTED_ROOT)
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
        else _unique_archive_path(root, source, file_hash)
    )
    submitted_at = previous.submitted_at if previous else _utc_now()
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
        # Same bytes were accepted before. Remove only this verified duplicate
        # inbox copy; the immutable archived original remains the source.
        if source != target and source.exists():
            source.unlink()
    else:
        os.replace(source, target)
    state.source_path = str(target.resolve())
    state.accepted_at = state.accepted_at or _utc_now()
    if not previous or previous.status not in TERMINAL_STATUSES:
        state.status = "queued"
        state.stage = "queued"
        state.next_retry_at = ""
    _save_state(state)
    return state


def _find_archived_source(
    file_hash: str,
    *,
    accepted_root: Path | None = None,
) -> Path | None:
    root = Path(accepted_root or ACCEPTED_ROOT)
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
    now: datetime | None = None,
    accepted_root: Path | None = None,
) -> list[JobState]:
    """Repair interrupted acceptance/running states after a restart."""
    now_value = now or datetime.now(timezone.utc)
    accepted = Path(accepted_root or ACCEPTED_ROOT)
    repaired: list[JobState] = []
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    if accepted.exists():
        for source in accepted.rglob("*.json"):
            if "历史文件" in source.parts or not source.is_file():
                continue
            try:
                file_hash = sha256_file(source)
            except OSError:
                continue
            if _load_state(file_hash):
                continue
            recovered = JobState(
                source_path=str(source.resolve()),
                source_name=source.name,
                sha256=file_hash,
                status="queued",
                submitted_at=_utc_now(),
                accepted_at=_utc_now(),
                stage="recovering",
                blocker_reason="已恢复受理后未写完状态的任务",
            )
            _save_state(recovered)
    processor_active = processor_is_running()
    for path in sorted(JOBS_ROOT.glob("*.json")):
        state = _load_state(path.stem)
        if not state:
            continue
        source = Path(state.source_path) if state.source_path else None
        if not source or not source.is_file():
            recovered = _find_archived_source(
                state.sha256,
                accepted_root=accepted,
            )
            if recovered:
                state.source_path = str(recovered)
                state.blocker_reason = ""
            elif state.status in ACTIVE_STATUSES:
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
        _save_state(state)
        repaired.append(state)
    return repaired


def _retry_allowed(
    state: JobState,
    now: datetime,
    *,
    retry_terminal: bool = False,
) -> bool:
    if state.status == "blocked":
        if retry_terminal:
            return True
        retry_at = _parse_time(state.next_retry_at)
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
        started = _parse_time(state.started_at)
        return bool(started and started < now - timedelta(hours=2))
    retry_at = _parse_time(state.next_retry_at)
    return retry_at is None or retry_at <= now


def _classify_failure(
    text: str,
    attempt: int,
    max_retries: int,
) -> tuple[str, str, str]:
    lowered = text.lower()
    if any(marker.lower() in lowered for marker in _AUTH_MARKERS):
        return "blocked", "鉴权或余额错误，进入低频自动复查", "auth"
    if any(marker in lowered for marker in _INPUT_MARKERS):
        return "invalid_input", "输入格式或源数据错误，不进行无效重试", "input"
    transient = any(marker in lowered for marker in _TRANSIENT_MARKERS)
    if transient and max_retries == 0:
        return "retry_wait", "临时服务异常，将持续断点续跑", "transient"
    retry_limit = max_retries if max_retries > 0 else 3
    if attempt >= retry_limit:
        return (
            "failed",
            f"已达到最大自动重试次数 ({retry_limit})",
            "transient" if transient else "unknown",
        )
    return (
        "retry_wait",
        "临时服务异常，等待下一轮断点续跑"
        if transient
        else "任务异常退出，等待下一轮重试",
        "transient" if transient else "unknown",
    )


def _extract_result(text: str) -> tuple[str, str, str]:
    published = re.search(r"正式表已更新:\s*(.+)", text)
    if published:
        return "published", published.group(1).strip(), ""
    pending = re.search(r"待人工审核包:\s*(.+)", text)
    if pending:
        return "pending_review", "", pending.group(1).strip()
    return "invalid_result", "", ""


def _load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层不是对象: {path}")
    return value


def validate_published_output(path: Path) -> dict:
    """Reject a false-positive publish before the worker marks a job done."""
    output = Path(path)
    if not output.is_file():
        raise FileNotFoundError(f"正式回填表不存在: {output}")
    payload = _load_json(output)
    if tuple(payload) != AMAZON_JSON_OUTPUT_FIELDS:
        raise ValueError("正式回填表字段名称或顺序不符合 14 字段契约")
    row_count = validate_columnar_payload(
        payload,
        required_fields=AMAZON_JSON_OUTPUT_FIELDS,
    )
    required_text = (
        "商品id",
        "产品站点",
        "产品标题",
        "副标题",
        "产品描述",
        "Bullet Point1",
        "Bullet Point2",
        "Bullet Point3",
        "Bullet Point4",
        "Bullet Point5",
        "关键词信息",
    )
    for field in required_text:
        if any(not str(value or "").strip() for value in payload[field]):
            raise ValueError(f"正式回填表字段“{field}”仍有空值")
    if any(not images for images in payload["产品图片链接"]):
        raise ValueError("正式回填表存在没有产品主图的商品")
    status_path = output.parent / STATUS_NAME
    if not status_path.is_file():
        raise FileNotFoundError(f"正式结果缺少 {STATUS_NAME}")
    status = _load_json(status_path)
    if status.get("published") is not True:
        raise ValueError("运行状态没有确认正式发布")
    status["validated_rows"] = row_count
    return status


def _hardlink_or_copy(source: str, target: str) -> str:
    try:
        os.link(source, target)
        return target
    except OSError:
        return shutil.copy2(source, target)


def _safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", value).strip(" ._")
    return cleaned[:80] or "Amazon任务"


def snapshot_delivery(
    state: JobState,
    *,
    category: str,
    artifact_dir: Path | None = None,
) -> Path:
    """Create one immutable per-job delivery package using hard links."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    stem = _safe_name(Path(state.source_path).stem)
    target = (
        DELIVERIES_ROOT
        / category
        / f"{stem}_{state.sha256[:12]}_{stamp}"
    )
    suffix = 1
    while target.exists():
        target = target.with_name(f"{target.name}_{suffix:02d}")
        suffix += 1
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.{os.getpid()}.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    if artifact_dir and artifact_dir.is_dir():
        shutil.copytree(
            artifact_dir,
            staging,
            copy_function=_hardlink_or_copy,
        )
    else:
        staging.mkdir(parents=True)
    source = Path(state.source_path)
    if source.is_file():
        _hardlink_or_copy(str(source), str(staging / source.name))
    log = Path(state.log_path)
    if log.is_file():
        _hardlink_or_copy(str(log), str(staging / log.name))
    os.replace(staging, target)
    return target


def _write_delivery_state(state: JobState) -> None:
    if not state.delivery_path:
        return
    _atomic_json(
        Path(state.delivery_path) / "任务状态.json",
        asdict(state),
    )


def _write_health(status: str, **details: object) -> None:
    payload = {
        "version": 1,
        "status": status,
        "pid": os.getpid(),
        "updated_at": _utc_now(),
        **details,
    }
    _atomic_json(RUNTIME_ROOT / "heartbeat.json", payload)


def worker_health(max_age_seconds: float = 120.0) -> dict:
    """Return a machine-readable liveness result for Task Scheduler."""
    path = RUNTIME_ROOT / "heartbeat.json"
    if not path.is_file():
        return {
            "healthy": False,
            "status": "missing",
            "message": "Worker 尚未写入心跳",
        }
    try:
        payload = _load_json(path)
        updated = _parse_time(str(payload.get("updated_at") or ""))
    except (OSError, TypeError, ValueError) as exc:
        return {
            "healthy": False,
            "status": "invalid",
            "message": f"心跳文件损坏: {exc}",
        }
    now = datetime.now(timezone.utc)
    age = (now - updated).total_seconds() if updated else float("inf")
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


def preflight(input_dir: Path, *, min_free_gb: float = 1.0) -> dict:
    """Fail fast before accepting jobs, without making paid API requests."""
    from .config.credentials import CredentialStore
    from .config.env import ENV_PATH, load_config
    from .config.models import get_model_registry

    config = load_config()
    registry = get_model_registry()
    credentials = CredentialStore(
        registry,
        env_path=ENV_PATH,
        environ=os.environ,
    )
    mode = str(config.get("IMAGE_PROCESSING_MODE") or "select_existing")
    required_operations = ["text", "vision"]
    if mode != "select_existing":
        required_operations.append("image")
    missing = []
    for operation in required_operations:
        usable = any(
            target.provider == "ollama"
            or bool(credentials.value(target.credential))
            for target in registry.routes(operation)
        )
        if not usable:
            missing.append(operation)
    directories = (
        input_dir,
        RUNTIME_ROOT,
        JOBS_ROOT,
        LOGS_ROOT,
        DELIVERIES_ROOT,
    )
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    probe = RUNTIME_ROOT / f".write_probe_{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
    finally:
        probe.unlink(missing_ok=True)
    free_gb = shutil.disk_usage(PROJECT_ROOT).free / (1024 ** 3)
    if free_gb < max(0.1, min_free_gb):
        raise OSError(
            f"项目磁盘剩余空间不足: {free_gb:.2f} GB，"
            f"至少需要 {min_free_gb:.2f} GB"
        )
    return {
        "input_dir": str(input_dir),
        "image_processing_mode": mode,
        "free_disk_gb": round(free_gb, 2),
        "missing_operations": missing,
        "blocker_reason": (
            "以下处理阶段没有可用模型凭据: " + ", ".join(missing)
            if missing
            else ""
        ),
    }


def _error_tail(text: str, limit: int = 1200) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    tail = "\n".join(lines[-12:])[-limit:]
    return _SECRET_RE.sub(lambda match: f"{match.group(1)}***", tail)


def retry_delay_seconds(
    attempt: int,
    *,
    retry_base_seconds: float = 30.0,
    jitter: bool = True,
) -> float:
    """Return the agreed 30s/2m/5m/10m retry schedule."""
    if retry_base_seconds <= 0:
        return 0.0
    schedule = (30.0, 120.0, 300.0)
    base = schedule[min(max(1, attempt), 4) - 1] if attempt <= 3 else 600.0
    if retry_base_seconds != 30.0:
        base *= retry_base_seconds / 30.0
    return max(0.0, base * random.uniform(0.85, 1.15)) if jitter else base


def _read_outcome(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        payload = _load_json(path)
    except (OSError, TypeError, ValueError):
        return None
    return payload if int(payload.get("version") or 0) == 1 else None


def _apply_success_outcome(state: JobState, outcome: dict) -> None:
    state.status = str(outcome.get("status") or "invalid_result")
    state.output_path = str(outcome.get("output_path") or "")
    state.review_path = str(outcome.get("review_path") or "")
    state.exception_path = str(outcome.get("exception_path") or "")
    state.pending_product_ids = [
        str(value) for value in outcome.get("pending_product_ids") or []
    ]
    state.isolated_product_ids = [
        str(value) for value in outcome.get("isolated_product_ids") or []
    ]


def _classify_outcome_failure(
    outcome: dict,
    *,
    attempt: int,
    max_retries: int,
) -> tuple[str, str, str]:
    failure = outcome.get("failure")
    details = failure if isinstance(failure, dict) else outcome
    kind = str(
        details.get("kind") or details.get("failure_kind") or "internal"
    )
    message = str(details.get("message") or kind)
    if kind in {"auth", "quota"}:
        return "blocked", message, kind
    if kind == "input":
        return "invalid_input", message, kind
    if kind == "transient":
        return "retry_wait", message, kind
    retry_limit = max_retries if max_retries > 0 else 3
    if attempt >= retry_limit:
        return "failed", message, "internal"
    return "retry_wait", message, "internal"


def _pending_review_is_retryable(review_path: Path) -> bool:
    """Retry a pending batch when the blocker is operational uncertainty."""
    pending_path = review_path.parent / "待定商品.json"
    if not pending_path.is_file():
        return False
    try:
        items = json.loads(pending_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return False
    if not isinstance(items, list) or not items:
        return False
    statuses: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        reason_codes = {
            str(reason.get("code") or "")
            for reason in item.get("reasons") or []
            if isinstance(reason, dict)
        }
        if "formal_row_validation_failed" in reason_codes:
            return False
        for image in item.get("images") or []:
            if not isinstance(image, dict):
                continue
            for key in ("assessment", "text_assessment"):
                assessment = image.get(key)
                if isinstance(assessment, dict):
                    statuses.append(
                        str(assessment.get("status") or "").lower()
                    )
    return bool(statuses) and "unknown" in statuses


def _progress_from_log(path: Path) -> tuple[str, int, int]:
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


def _disk_free_gb(path: Path = PROJECT_ROOT) -> float:
    return shutil.disk_usage(path).free / (1024 ** 3)


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


def _remove_old_entries(
    root: Path,
    *,
    cutoff: datetime,
    protected: set[Path] | None = None,
) -> int:
    removed = 0
    protected_values = {path.resolve() for path in (protected or set())}
    if not root.exists():
        return 0
    entries = sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True)
    for entry in entries:
        try:
            if entry.resolve() in protected_values:
                continue
            if entry.is_file():
                modified = datetime.fromtimestamp(
                    entry.stat().st_mtime,
                    tz=timezone.utc,
                )
                if modified < cutoff:
                    entry.unlink()
                    removed += 1
            elif entry.is_dir() and not any(entry.iterdir()):
                entry.rmdir()
        except OSError:
            continue
    return removed


def _prune_cache_until(
    cache_root: Path,
    *,
    target_free_gb: float,
    disk_free: Callable[[], float],
) -> int:
    if not cache_root.exists() or processor_is_running():
        return 0
    files: list[tuple[float, Path]] = []
    for path in cache_root.rglob("*"):
        try:
            if path.is_file():
                files.append((path.stat().st_mtime, path))
        except OSError:
            continue
    removed = 0
    for _modified, path in sorted(files):
        if disk_free() >= target_free_gb:
            break
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def run_retention(
    *,
    now: datetime | None = None,
    disk_free: Callable[[], float] | None = None,
    accepted_root: Path | None = None,
    deliveries_root: Path | None = None,
    cache_root: Path | None = None,
) -> dict:
    """Apply bounded retention only while no processor task is active."""
    now_value = now or datetime.now(timezone.utc)
    free_reader = disk_free or (lambda: _disk_free_gb(PROJECT_ROOT))
    accepted = Path(accepted_root or ACCEPTED_ROOT)
    deliveries = Path(deliveries_root or DELIVERIES_ROOT)
    cache = Path(cache_root or (PROJECT_ROOT / ".runtime" / "cache"))
    active_sources = {
        Path(state.source_path)
        for state in _iter_states()
        if state.status in ACTIVE_STATUSES and state.source_path
    }
    active_state_paths = {
        _state_path(state.sha256)
        for state in _iter_states()
        if state.status in ACTIVE_STATUSES
    }
    active_outcomes = {
        Path(state.outcome_path)
        for state in _iter_states()
        if state.status in ACTIVE_STATUSES and state.outcome_path
    }
    report = {
        "accepted_removed": _remove_old_entries(
            accepted,
            cutoff=now_value - timedelta(days=90),
            protected=active_sources,
        ),
        "deliveries_removed": _remove_old_entries(
            deliveries,
            cutoff=now_value - timedelta(days=90),
        ),
        "job_states_removed": _remove_old_entries(
            JOBS_ROOT,
            cutoff=now_value - timedelta(days=90),
            protected=active_state_paths,
        ),
        "outcomes_removed": _remove_old_entries(
            OUTCOMES_ROOT,
            cutoff=now_value - timedelta(days=90),
            protected=active_outcomes,
        ),
        "logs_removed": _remove_old_entries(
            LOGS_ROOT,
            cutoff=now_value - timedelta(days=30),
        ),
        "cache_removed": 0,
    }
    if free_reader() < 30.0:
        report["cache_removed"] = _prune_cache_until(
            cache,
            target_free_gb=50.0,
            disk_free=free_reader,
        )
    report["free_disk_gb"] = round(free_reader(), 2)
    report["finished_at"] = _utc_now()
    _atomic_json(RETENTION_STATE_PATH, report)
    return report


def run_child(
    source: Path,
    log_path: Path,
    *,
    outcome_path: Path | None = None,
    timeout_hours: float = 24.0,
    stall_minutes: float = 45.0,
    heartbeat: Callable[[], None] | None = None,
) -> tuple[int, str]:
    """Run one isolated CLI child while streaming output to its durable log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "amazon_processor",
        "run",
        str(source),
        "--unattended",
    ]
    if outcome_path is not None:
        outcome_path.parent.mkdir(parents=True, exist_ok=True)
        outcome_path.unlink(missing_ok=True)
        command.extend(["--outcome", str(outcome_path)])
    started = time.monotonic()
    last_progress = started
    last_size = -1
    environment = dict(os.environ)
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    timed_out = False
    with log_path.open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
        while process.poll() is None:
            try:
                current_size = log_path.stat().st_size
            except OSError:
                current_size = last_size
            if current_size != last_size:
                last_size = current_size
                last_progress = time.monotonic()
            if heartbeat:
                heartbeat()
            if time.monotonic() - started > max(0.1, timeout_hours * 3600):
                timed_out = True
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                stream.write("\n[WORKER] 单个任务超过最大运行时间，已终止\n")
                stream.flush()
                break
            if (
                time.monotonic() - last_progress
                > max(1.0, stall_minutes * 60)
            ):
                timed_out = True
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                stream.write(
                    "\n[WORKER] 子进程长时间无日志进展，已终止并准备续跑\n"
                )
                stream.flush()
                break
            time.sleep(2)
        exit_code = 124 if timed_out else int(process.returncode or 0)
    return exit_code, log_path.read_text(encoding="utf-8", errors="replace")


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
    if previous and previous.status == "running" and processor_is_running():
        # A restarted watcher must not launch a second child while the old
        # child still owns the processor lock.
        return previous

    attempt = (previous.attempt if previous else 0) + 1
    log_path = LOGS_ROOT / f"{file_hash}_{attempt:02d}.log"
    outcome_root = OUTCOMES_ROOT
    if LOGS_ROOT.parent != RUNTIME_ROOT:
        outcome_root = LOGS_ROOT.parent / "outcomes"
    outcome_path = (
        outcome_root
        / file_hash
        / f"attempt_{attempt:02d}"
        / "任务结果.json"
    )
    state = JobState(
        source_path=str(source.resolve()),
        source_name=(previous.source_name if previous else "") or source.name,
        sha256=file_hash,
        status="running",
        submitted_at=previous.submitted_at if previous else "",
        accepted_at=previous.accepted_at if previous else "",
        row_count=previous.row_count if previous else 0,
        attempt=attempt,
        started_at=_utc_now(),
        log_path=str(log_path),
        outcome_path=str(outcome_path),
        stage="processing",
    )
    _save_state(state)
    print(f"[WORKER] 开始处理: {source.name} ({file_hash[:12]})", flush=True)
    saved_progress: tuple[str, int, int] | None = None

    def heartbeat_running() -> None:
        nonlocal saved_progress
        stage, current, total = _progress_from_log(log_path)
        state.stage = stage
        state.progress_current = current
        state.progress_total = total
        current_progress = (stage, current, total)
        if current_progress != saved_progress:
            _save_state(state)
            saved_progress = current_progress
        _write_health(
            "running",
            current_job=file_hash,
            source_path=str(source),
            attempt=attempt,
            log_path=str(log_path),
            stage=stage,
            progress_current=current,
            progress_total=total,
            queue_depth=len(_active_queue()),
            free_disk_gb=round(_disk_free_gb(), 2),
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
    if outcome and str(outcome.get("status")) != "failed":
        _apply_success_outcome(state, outcome)
    elif exit_code == 0:
        state.status, state.output_path, state.review_path = _extract_result(output)
    else:
        if outcome:
            state.status, state.error, state.failure_kind = (
                _classify_outcome_failure(
                    outcome,
                    attempt=attempt,
                    max_retries=max_retries,
                )
            )
        else:
            state.status, state.error, state.failure_kind = _classify_failure(
                output,
                attempt,
                max_retries,
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
    elif state.status == "pending_review":
        review = Path(state.review_path)
        if review.is_file() and _pending_review_is_retryable(review):
            state.status = "retry_wait"
            state.failure_kind = "transient"
            state.error = "待定商品仍含 operational unknown，将断点续审"
            state.stage = "waiting_provider"
        else:
            state.delivery_path = str(snapshot_delivery(
                state,
                category="待处理",
                artifact_dir=review.parent if review.is_file() else None,
            ))
            state.failure_kind = "row_quality"
            state.error = "全部商品均无法自动放行，正式表未覆盖"
            state.blocker_reason = state.error
            state.stage = "needs_review"
    elif exit_code == 0 and state.status == "invalid_result":
        message = "PublishedArtifactValidationError: 子进程成功但没有结果标记"
        state.status, state.error, state.failure_kind = _classify_failure(
            message,
            attempt,
            max_retries,
        )
    if state.status == "retry_wait":
        delay = retry_delay_seconds(
            attempt,
            retry_base_seconds=retry_base_seconds,
        )
        state.next_retry_at = (
            datetime.now(timezone.utc) + timedelta(seconds=delay)
        ).isoformat(timespec="seconds")
        state.stage = "waiting_provider" if state.failure_kind == "transient" else "recovering"
        state.blocker_reason = state.error
        state.error = f"{state.error}\n{_error_tail(output)}".strip()
    elif state.status == "blocked":
        state.next_retry_at = (
            datetime.now(timezone.utc)
            + timedelta(hours=max(0.1, blocked_retry_hours))
        ).isoformat(timespec="seconds")
        state.stage = "needs_attention"
        state.blocker_reason = state.error
        state.error = f"{state.error}\n{_error_tail(output)}".strip()
        state.delivery_path = str(snapshot_delivery(
            state,
            category="阻塞",
        ))
    elif state.status in {"failed", "invalid_input"}:
        state.stage = "stopped"
        state.blocker_reason = state.error
        state.error = f"{state.error}\n{_error_tail(output)}".strip()
        state.delivery_path = str(snapshot_delivery(
            state,
            category="阻塞",
        ))
    _save_state(state)
    _write_delivery_state(state)
    _write_health(
        "idle" if state.status != "retry_wait" else "degraded",
        last_job=file_hash,
        last_job_status=state.status,
        delivery_path=state.delivery_path,
        next_retry_at=state.next_retry_at,
        isolated_count=len(state.isolated_product_ids or []),
        free_disk_gb=round(_disk_free_gb(), 2),
    )
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
        tracker = StabilityTracker()
        last_retention = 0.0
        last_preflight = time.monotonic()
        configuration_blocked = bool(readiness.get("missing_operations"))
        once_observed = False
        _write_health("idle", queue_depth=len(_active_queue()), **readiness)
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

            inbox_files = list(iter_input_files(input_path))
            if not maintenance and free_gb >= 10.0:
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
                    except Exception as exc:
                        print(
                            f"[WORKER] 受理失败 {source.name}: "
                            f"{type(exc).__name__}: {exc}",
                            flush=True,
                        )

            queue = _active_queue()
            current = queue[0] if queue else None
            now = datetime.now(timezone.utc)
            if (
                current
                and not maintenance
                and not configuration_blocked
                and free_gb >= 10.0
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
            status = (
                "maintenance"
                if maintenance
                else "needs_attention"
                if configuration_blocked
                else "blocked_disk"
                if free_gb < 10.0
                else "needs_attention"
                if current and current.status == "blocked"
                else "paused_provider"
                if current and current.status == "retry_wait"
                else "idle"
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
                status,
                input_dir=str(input_path),
                accepted_dir=str(accepted_root),
                queue_depth=len(queue),
                inbox_depth=len(inbox_files),
                current_job=current.sha256 if current else "",
                stage=current.stage if current else status,
                progress_current=current.progress_current if current else 0,
                progress_total=current.progress_total if current else 0,
                next_retry_at=current.next_retry_at if current else "",
                blocker_reason=(
                    str(readiness.get("blocker_reason") or "")
                    if configuration_blocked
                    else "磁盘剩余低于 10 GB，已停止接收新任务"
                    if free_gb < 10.0
                    else current.blocker_reason if current else ""
                ),
                free_disk_gb=round(free_gb, 2),
                last_success_at=last_success,
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
    "iter_input_files",
    "is_file_stable",
    "process_one",
    "preflight",
    "reconcile_jobs",
    "retry_delay_seconds",
    "run_child",
    "run_worker",
    "run_retention",
    "sha256_file",
    "snapshot_delivery",
    "validate_published_output",
    "worker_health",
]
