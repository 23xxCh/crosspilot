"""Immutable task snapshots and operator-facing delivery repair."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import threading
import time

from . import operator_workspace, server_jobs
from .schema import AMAZON_JSON_OUTPUT_FIELDS, validate_columnar_payload


JobState = server_jobs.JobState
StateSaver = Callable[[JobState], None]
SuccessPublisher = Callable[[JobState, Path], Path]
AttentionPublisher = Callable[[JobState], Path]
ErrorReporter = Callable[[JobState, Exception], None]

REQUIRED_PUBLISHED_TEXT_FIELDS = (
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


def _load_json_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层不是对象: {path}")
    return value


def validate_published_output(path: Path, *, status_name: str) -> dict:
    """Reject a false-positive publish before a job is marked complete."""
    output = Path(path)
    if not output.is_file():
        raise FileNotFoundError(f"正式回填表不存在: {output}")
    payload = _load_json_object(output)
    if tuple(payload) != AMAZON_JSON_OUTPUT_FIELDS:
        raise ValueError("正式回填表字段名称或顺序不符合 14 字段契约")
    row_count = validate_columnar_payload(
        payload,
        required_fields=AMAZON_JSON_OUTPUT_FIELDS,
    )
    for field in REQUIRED_PUBLISHED_TEXT_FIELDS:
        if any(not str(value or "").strip() for value in payload[field]):
            raise ValueError(f"正式回填表字段“{field}”仍有空值")
    if any(not images for images in payload["产品图片链接"]):
        raise ValueError("正式回填表存在没有产品主图的商品")
    status_path = output.parent / status_name
    if not status_path.is_file():
        raise FileNotFoundError(f"正式结果缺少 {status_name}")
    status = _load_json_object(status_path)
    if status.get("published") is not True:
        raise ValueError("运行状态没有确认正式发布")
    status["validated_rows"] = row_count
    return status


def pending_review_is_retryable(review_path: Path) -> bool:
    """Retry a pending batch only when its blocker is operational uncertainty."""
    pending_path = Path(review_path).parent / "待定商品.json"
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


def hardlink_or_copy(source: str, target: str) -> str:
    """Prefer a space-saving hard link and fall back to a normal copy."""
    try:
        os.link(source, target)
        return target
    except OSError:
        return shutil.copy2(source, target)


def _replace_latest(refill: Path, latest: Path) -> None:
    temporary = latest.with_suffix(
        latest.suffix
        + f".{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )
    try:
        hardlink_or_copy(str(refill), str(temporary))
        for attempt in range(5):
            try:
                os.replace(temporary, latest)
                break
            except PermissionError:
                if attempt >= 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def snapshot_delivery(
    state: JobState,
    *,
    category: str,
    deliveries_root: Path,
    refill_name: str,
    artifact_dir: Path | None = None,
) -> Path:
    """Create one immutable per-job package and update the success alias."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    stem = server_jobs.safe_name(Path(state.source_path).stem)
    target = (
        Path(deliveries_root)
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
    try:
        if artifact_dir and artifact_dir.is_dir():
            shutil.copytree(
                artifact_dir,
                staging,
                copy_function=hardlink_or_copy,
            )
        else:
            staging.mkdir(parents=True)
        source = Path(state.source_path)
        if source.is_file():
            hardlink_or_copy(str(source), str(staging / source.name))
        log = Path(state.log_path)
        if log.is_file():
            hardlink_or_copy(str(log), str(staging / log.name))
        os.replace(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    if category == "成功":
        refill = target / refill_name
        if refill.is_file():
            _replace_latest(
                refill,
                Path(deliveries_root) / "跨境电商自动化回填表_最新.json",
            )
    return target


def write_delivery_state(
    state: JobState,
    *,
    atomic_json: Callable[[Path, dict], None] = server_jobs.atomic_json,
) -> None:
    """Persist a state snapshot inside an existing delivery package."""
    if not state.delivery_path:
        return
    atomic_json(Path(state.delivery_path) / "任务状态.json", asdict(state))


def _default_success_publisher(
    operator_root: Path,
) -> SuccessPublisher:
    return lambda state, artifact: operator_workspace.publish_success(
        state,
        artifact,
        root=operator_root,
    )


def _default_attention_publisher(
    operator_root: Path,
) -> AttentionPublisher:
    return lambda state: operator_workspace.publish_attention(
        state,
        root=operator_root,
    )


def _default_error_reporter(state: JobState, exc: Exception) -> None:
    print(
        "[WORKER] 历史任务交付目录暂未补齐: "
        f"{state.source_name}: {type(exc).__name__}: {exc}",
        flush=True,
    )


def repair_operator_deliveries(
    states: Iterable[JobState],
    *,
    formal_latest_root: Path,
    refill_name: str,
    operator_root: Path,
    save_state: StateSaver,
    publish_success: SuccessPublisher | None = None,
    publish_attention: AttentionPublisher | None = None,
    report_error: ErrorReporter = _default_error_reporter,
) -> int:
    """Expose existing task results after an upgrade without rerunning AI."""
    operator_workspace.ensure_workspace(operator_root)
    operator_workspace.bootstrap_latest_result(
        formal_latest_root,
        root=operator_root,
    )
    state_list = list(states)
    successful = [
        state
        for state in state_list
        if state.status in {"published", "published_with_warnings"}
    ]
    latest_success = max(
        successful,
        key=lambda state: state.finished_at or state.updated_at or "",
        default=None,
    )
    success_publisher = publish_success or _default_success_publisher(operator_root)
    attention_publisher = (
        publish_attention or _default_attention_publisher(operator_root)
    )
    repaired = 0
    for state in state_list:
        existing = (
            Path(state.operator_delivery_path)
            if state.operator_delivery_path
            else None
        )
        if existing and existing.exists():
            continue
        try:
            if state.status in {"published", "published_with_warnings"}:
                candidates: list[Path] = []
                if state.output_path:
                    candidates.append(Path(state.output_path).parent)
                if state.delivery_path:
                    candidates.append(Path(state.delivery_path))
                artifact_dir = next(
                    (
                        candidate
                        for candidate in candidates
                        if (candidate / refill_name).is_file()
                    ),
                    None,
                )
                if (
                    artifact_dir is None
                    and state is latest_success
                    and (formal_latest_root / refill_name).is_file()
                ):
                    artifact_dir = formal_latest_root
                if artifact_dir is None:
                    continue
                state.operator_delivery_path = str(
                    success_publisher(state, artifact_dir)
                )
            elif state.status in operator_workspace.ATTENTION_STATUSES:
                state.operator_delivery_path = str(attention_publisher(state))
            else:
                continue
            save_state(state)
            repaired += 1
        except Exception as exc:
            report_error(state, exc)
    return repaired


__all__ = [
    "hardlink_or_copy",
    "pending_review_is_retryable",
    "repair_operator_deliveries",
    "snapshot_delivery",
    "validate_published_output",
    "write_delivery_state",
]
