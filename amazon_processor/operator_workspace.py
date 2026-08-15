"""Non-technical operator workspace for the unattended Windows server."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import html
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OPERATOR_ROOT = PROJECT_ROOT / "Amazon日常操作"
INBOX_NAME = "1_把采集表放这里"
RESULTS_NAME = "2_到这里取结果"
STATUS_NAME = "3_查看处理状态.html"
COMPLETED_NAME = "已完成"
ATTENTION_NAME = "需要管理员处理"
LATEST_NAME = "最新回填表.json"

SOURCE_REFILL_NAME = "跨境电商自动化回填表.json"
SOURCE_REVIEW_NAME = "终审包.html"
SOURCE_EXCEPTIONS_NAME = "异常商品.json"

STATUS_LABELS = {
    "queued": "等待处理",
    "running": "正在处理",
    "retry_wait": "系统正在自动重试",
    "delivery_retry": "正在整理结果",
    "blocked": "需要管理员处理",
    "failed": "需要管理员处理",
    "invalid_input": "需要管理员处理",
    "pending_review": "需要管理员处理",
    "published": "已完成",
    "published_with_warnings": "已完成（部分商品未交付）",
}
ACTIVE_STATUSES = {"queued", "running", "retry_wait", "delivery_retry"}
SUCCESS_STATUSES = {"published", "published_with_warnings"}
ATTENTION_STATUSES = {"blocked", "failed", "invalid_input", "pending_review"}


@dataclass(frozen=True)
class OperatorPaths:
    root: Path
    inbox: Path
    results: Path
    completed: Path
    attention: Path
    status: Path


def paths_for(root: str | Path = OPERATOR_ROOT) -> OperatorPaths:
    base = Path(root)
    results = base / RESULTS_NAME
    return OperatorPaths(
        root=base,
        inbox=base / INBOX_NAME,
        results=results,
        completed=results / COMPLETED_NAME,
        attention=results / ATTENTION_NAME,
        status=base / STATUS_NAME,
    )


def ensure_workspace(root: str | Path = OPERATOR_ROOT) -> OperatorPaths:
    paths = paths_for(root)
    paths.inbox.mkdir(parents=True, exist_ok=True)
    paths.completed.mkdir(parents=True, exist_ok=True)
    paths.attention.mkdir(parents=True, exist_ok=True)
    if not paths.status.is_file():
        write_status_page(
            summarize_jobs([]),
            healthy=False,
            path=paths.status,
        )
    return paths


def _field(value: object, name: str, default: Any = "") -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _state_time(value: object) -> str:
    return str(
        _field(value, "updated_at")
        or _field(value, "finished_at")
        or _field(value, "started_at")
        or _field(value, "submitted_at")
        or ""
    )


def _public_state(value: object) -> dict[str, Any]:
    status = str(_field(value, "status") or "")
    return {
        "status": status,
        "label": STATUS_LABELS.get(status, "状态更新中"),
        "source_name": str(
            _field(value, "source_name")
            or Path(str(_field(value, "source_path") or "")).name
            or "未命名采集表"
        ),
        "row_count": int(_field(value, "row_count", 0) or 0),
        "progress_current": int(_field(value, "progress_current", 0) or 0),
        "progress_total": int(_field(value, "progress_total", 0) or 0),
        "queue_position": int(_field(value, "queue_position", 0) or 0),
        "isolated_count": len(_field(value, "isolated_product_ids", []) or []),
        "updated_at": _state_time(value),
        "operator_delivery_path": str(
            _field(value, "operator_delivery_path") or ""
        ),
    }


def summarize_jobs(states: Iterable[object], *, recent_limit: int = 20) -> dict:
    public = [_public_state(state) for state in states]
    public.sort(key=lambda state: state["updated_at"], reverse=True)
    counts = {status: 0 for status in STATUS_LABELS}
    for state in public:
        status = state["status"]
        counts[status] = counts.get(status, 0) + 1
    return {
        "counts": counts,
        "latest": public[0] if public else None,
        "recent": public[:max(1, int(recent_limit))],
    }


def _relative_result_link(state: Mapping[str, Any]) -> str:
    delivery = str(state.get("operator_delivery_path") or "")
    if not delivery or state.get("status") not in SUCCESS_STATUSES:
        return ""
    folder = Path(delivery).name
    if not folder:
        return ""
    return f"{RESULTS_NAME}/{COMPLETED_NAME}/{folder}/1_回填表.json"


def _status_class(status: str) -> str:
    if status in SUCCESS_STATUSES:
        return "success"
    if status in {"running", "queued", "retry_wait"}:
        return "working"
    if status in ATTENTION_STATUSES:
        return "attention"
    return "neutral"


def render_status_page(overview: Mapping[str, Any], *, healthy: bool) -> str:
    counts = overview.get("counts") or {}
    recent = overview.get("recent") or []
    active_count = sum(int(counts.get(status) or 0) for status in ACTIVE_STATUSES)
    completed_count = sum(
        int(counts.get(status) or 0) for status in SUCCESS_STATUSES
    )
    attention_count = sum(
        int(counts.get(status) or 0) for status in ATTENTION_STATUSES
    )
    headline = "系统运行正常" if healthy else "系统正在准备或需要管理员检查"
    headline_class = "success" if healthy else "attention"
    if not healthy:
        next_action = "系统正在准备或维护，请先联系管理员，暂时不要投放新的采集表。"
    elif active_count:
        next_action = "任务已经受理，请保持服务器开机，处理完成后到“2_到这里取结果”领取。"
    elif completed_count:
        next_action = "最近任务已经完成，可以直接打开下方“打开结果”。"
    elif attention_count:
        next_action = "当前有任务需要管理员处理，普通用户无需修改任何系统文件。"
    else:
        next_action = "把采集表 JSON 复制到“1_把采集表放这里”，系统会自动受理。"

    rows: list[str] = []
    for state in recent:
        status = str(state.get("status") or "")
        label = html.escape(str(state.get("label") or "状态更新中"))
        source_name = html.escape(str(state.get("source_name") or "未命名采集表"))
        progress_total = int(state.get("progress_total") or 0)
        progress = ""
        if progress_total:
            progress = (
                f'<span class="detail">进度 {int(state.get("progress_current") or 0)}/'
                f"{progress_total}</span>"
            )
        elif int(state.get("queue_position") or 0):
            progress = (
                f'<span class="detail">排队第 {int(state.get("queue_position") or 0)} 个</span>'
            )
        isolated = ""
        if int(state.get("isolated_count") or 0):
            isolated = (
                f'<span class="detail">未交付 {int(state.get("isolated_count") or 0)} 个</span>'
            )
        link = _relative_result_link(state)
        action = (
            f'<a class="open" href="{html.escape(link, quote=True)}">打开结果</a>'
            if link
            else '<span class="waiting">请等待</span>'
            if status in {"queued", "running", "retry_wait", "delivery_retry"}
            else '<span class="waiting">请联系管理员</span>'
        )
        rows.append(
            '<article class="job">'
            f'<div><span class="badge {_status_class(status)}">{label}</span>'
            f'<h3>{source_name}</h3>{progress}{isolated}</div>{action}</article>'
        )
    if not rows:
        rows.append(
            '<div class="empty">暂无任务。把采集表放入第一个文件夹即可。</div>'
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="10">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Amazon 处理状态</title>
  <style>
    *{{box-sizing:border-box}}body{{margin:0;background:#f4f7fb;color:#182230;font-family:"Microsoft YaHei",Arial,sans-serif}}
    main{{max-width:980px;margin:32px auto;padding:0 20px}}header{{background:#132238;color:white;border-radius:18px;padding:28px}}
    h1{{margin:0 0 8px;font-size:30px}}p{{line-height:1.7}}.system{{display:inline-block;margin-top:8px;padding:8px 13px;border-radius:999px;font-weight:700}}
    .system.success,.badge.success{{background:#d7f7e5;color:#12653a}}.system.attention,.badge.attention{{background:#fff0d2;color:#8b4b00}}
    .steps{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:18px 0}}.step,.panel{{background:white;border-radius:14px;padding:18px;box-shadow:0 4px 18px #18324b12}}
    .step strong{{display:block;font-size:20px;margin-bottom:6px}}.panel h2{{margin-top:0}}.job{{display:flex;justify-content:space-between;gap:16px;align-items:center;border-top:1px solid #e6ebf1;padding:16px 0}}
    .job:first-of-type{{border-top:0}}.job h3{{display:inline;margin:0 10px;font-size:16px}}.badge{{display:inline-block;padding:5px 9px;border-radius:999px;font-size:13px;font-weight:700}}
    .badge.working{{background:#dcecff;color:#185a9d}}.badge.neutral{{background:#edf0f4;color:#465466}}.detail{{margin-right:12px;color:#66758a;font-size:14px}}
    .open{{background:#157347;color:white;text-decoration:none;padding:10px 16px;border-radius:9px;font-weight:700;white-space:nowrap}}.waiting{{color:#6b7788;white-space:nowrap}}
    .next{{border-left:5px solid #2b76d2;background:#edf5ff;padding:13px 16px;border-radius:8px}}.empty{{color:#66758a;padding:18px 0}}
    footer{{color:#7a8798;text-align:center;margin:22px}}@media(max-width:700px){{.steps{{grid-template-columns:1fr}}.job{{align-items:flex-start;flex-direction:column}}}}
  </style>
</head>
<body><main>
  <header><h1>Amazon 自动处理</h1><p>不需要打开程序，也不需要输入命令。</p><span class="system {headline_class}">{html.escape(headline)}</span></header>
  <section class="steps">
    <div class="step"><strong>1 放采集表</strong>复制 JSON 到第一个文件夹</div>
    <div class="step"><strong>2 等待处理</strong>本页面每 10 秒自动刷新</div>
    <div class="step"><strong>3 领取结果</strong>在第二个文件夹取回填表</div>
  </section>
  <section class="panel"><h2>下一步</h2><p class="next">{html.escape(next_action)}</p></section>
  <section class="panel"><h2>最近任务</h2>{''.join(rows)}</section>
  <footer>页面会自动刷新，关闭后重新双击即可。</footer>
</main></body></html>"""


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(
        path.suffix
        + f".{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )
    try:
        temporary.write_text(value, encoding="utf-8")
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


def write_status_page(
    overview: Mapping[str, Any],
    *,
    healthy: bool,
    path: str | Path | None = None,
) -> Path:
    target = Path(path or paths_for().status)
    _atomic_text(target, render_status_page(overview, healthy=healthy))
    return target


def _safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", value).strip(" ._")
    return cleaned[:80] or "Amazon任务"


def _stamp(value: object) -> str:
    raw = str(
        _field(value, "submitted_at")
        or _field(value, "accepted_at")
        or _field(value, "finished_at")
        or ""
    )
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.strftime("%Y%m%d_%H%M%S")
    except ValueError:
        return time.strftime("%Y%m%d_%H%M%S")


def _package_name(value: object, label: str) -> str:
    source_name = str(_field(value, "source_name") or "Amazon任务.json")
    stem = _safe_name(Path(source_name).stem)
    digest = str(_field(value, "sha256") or "")[:8]
    suffix = f"_{digest}" if digest else ""
    return f"{stem}_{label}_{_stamp(value)}{suffix}"


def _hardlink_or_copy(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _replace_file(source: Path, target: Path) -> None:
    temporary = target.with_suffix(
        target.suffix
        + f".{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )
    try:
        _hardlink_or_copy(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def publish_success(
    state: object,
    artifact_dir: str | Path,
    *,
    root: str | Path = OPERATOR_ROOT,
) -> Path:
    paths = ensure_workspace(root)
    artifact = Path(artifact_dir)
    refill = artifact / SOURCE_REFILL_NAME
    if not refill.is_file():
        raise FileNotFoundError(f"正式交付缺少回填表: {refill}")
    target = paths.completed / _package_name(state, "完成")
    if not target.is_dir():
        staging = target.parent / (
            f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        try:
            _hardlink_or_copy(refill, staging / "1_回填表.json")
            review = artifact / SOURCE_REVIEW_NAME
            if review.is_file():
                _hardlink_or_copy(review, staging / "2_人工检查.html")
            payload = json.loads(refill.read_text(encoding="utf-8"))
            released = len(payload.get("商品id") or [])
            problem_count = len(payload.get("有问题的产品id") or [])
            isolated_count = len(_field(state, "isolated_product_ids", []) or [])
            summary = (
                f"采集表：{_field(state, 'source_name') or '未命名采集表'}\n"
                f"输入商品：{int(_field(state, 'row_count', 0) or 0)} 个\n"
                f"成功交付：{released} 个\n"
                f"问题商品：{problem_count} 个\n"
                f"自动隔离：{isolated_count} 个\n\n"
                "回填表可以直接交给上游使用。需要检查图片或文案时，打开“2_人工检查.html”。\n"
            )
            (staging / "3_处理摘要.txt").write_text(summary, encoding="utf-8")
            exceptions = artifact / SOURCE_EXCEPTIONS_NAME
            if exceptions.is_file():
                _hardlink_or_copy(exceptions, staging / "4_异常商品.json")
            os.replace(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    _replace_file(target / "1_回填表.json", paths.results / LATEST_NAME)
    return target


def bootstrap_latest_result(
    source_dir: str | Path,
    *,
    root: str | Path = OPERATOR_ROOT,
) -> Path | None:
    """Expose the existing latest formal result without duplicating its bytes."""
    source = Path(source_dir) / SOURCE_REFILL_NAME
    if not source.is_file():
        return None
    paths = ensure_workspace(root)
    target = paths.results / LATEST_NAME
    _replace_file(source, target)
    return target


def _attention_message(status: str) -> str:
    if status == "invalid_input":
        return "采集表格式不符合要求，请让管理员检查采集表。"
    if status == "blocked":
        return "系统配置、密钥或服务额度需要管理员处理。"
    if status == "pending_review":
        return "本批没有可自动交付的商品，上一版回填表没有被覆盖。"
    return "系统自动恢复后仍未完成，请让管理员检查任务状态。"


def publish_attention(
    state: object,
    *,
    root: str | Path = OPERATOR_ROOT,
) -> Path:
    paths = ensure_workspace(root)
    target = paths.attention / _package_name(state, "需处理")
    if target.is_dir():
        return target
    staging = target.parent / (
        f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        status = str(_field(state, "status") or "")
        explanation = (
            "处理状态：需要管理员处理\n"
            f"采集表：{_field(state, 'source_name') or '未命名采集表'}\n"
            f"输入商品：{int(_field(state, 'row_count', 0) or 0)} 个\n\n"
            f"原因：{_attention_message(status)}\n\n"
            "普通用户无需修改配置、密钥或系统文件。\n"
        )
        (staging / "处理说明.txt").write_text(explanation, encoding="utf-8")
        os.replace(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return target


__all__ = [
    "ATTENTION_NAME",
    "COMPLETED_NAME",
    "INBOX_NAME",
    "LATEST_NAME",
    "OPERATOR_ROOT",
    "OperatorPaths",
    "RESULTS_NAME",
    "STATUS_LABELS",
    "STATUS_NAME",
    "ensure_workspace",
    "bootstrap_latest_result",
    "paths_for",
    "publish_attention",
    "publish_success",
    "render_status_page",
    "summarize_jobs",
    "write_status_page",
]
