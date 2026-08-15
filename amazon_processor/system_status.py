"""Operator-facing status aggregation and plain-Chinese rendering.

This module reads durable Worker state only.  It does not serve HTTP, accept
jobs, expose credentials, or run the processing pipeline.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from . import server_worker


JOB_STATUS_LABELS = {
    "queued": "排队",
    "running": "处理中",
    "retry_wait": "等待自动重试",
    "delivery_retry": "正在整理结果",
    "blocked": "需要处理",
    "failed": "处理失败",
    "invalid_input": "输入不合格",
    "pending_review": "等待人工审核",
    "published": "已完成",
    "published_with_warnings": "已完成（有隔离商品）",
}


def _default_api_health_check() -> dict:
    # Imported lazily so the HTTP adapter can re-export this module without a
    # circular import during module initialization.
    from .api_server import api_health_check

    return api_health_check()


def system_overview(
    *,
    jobs_root: Path | None = None,
    worker_health_func: Callable[[float], dict] | None = None,
    api_health_func: Callable[[], dict] | None = None,
) -> dict[str, Any]:
    """Return a small operator-facing summary without logs or secrets."""
    worker_check = worker_health_func or server_worker.worker_health
    api_check = api_health_func or _default_api_health_check
    try:
        worker = worker_check(120.0)
    except Exception:
        worker = {"healthy": False, "status": "unavailable"}
    try:
        api = api_check().get("api") or {"healthy": False}
    except Exception:
        api = {"healthy": False, "status": "stopped"}

    root = Path(jobs_root or server_worker.JOBS_ROOT)
    counts = {status: 0 for status in JOB_STATUS_LABELS}
    latest: dict[str, Any] | None = None
    latest_key = ""
    if root.is_dir():
        for path in root.glob("*.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                continue
            status = str(state.get("status") or "")
            counts[status] = counts.get(status, 0) + 1
            key = str(
                state.get("finished_at")
                or state.get("started_at")
                or state.get("submitted_at")
                or ""
            )
            if key >= latest_key:
                latest_key = key
                latest = {
                    "status": status,
                    "source_name": str(state.get("source_name") or "")
                    or Path(str(state.get("source_path") or "")).name,
                    "row_count": int(state.get("row_count") or 0),
                    "attempt": int(state.get("attempt") or 0),
                    "stage": str(state.get("stage") or ""),
                    "progress_current": int(
                        state.get("progress_current") or 0
                    ),
                    "progress_total": int(state.get("progress_total") or 0),
                    "queue_position": int(state.get("queue_position") or 0),
                    "isolated_count": len(
                        state.get("isolated_product_ids") or []
                    ),
                    "blocker_reason": str(
                        state.get("blocker_reason") or ""
                    ),
                    "updated_at": key,
                }
    healthy = bool(
        worker.get("healthy")
        and worker.get("ready", True)
        and api.get("healthy")
    )
    return {
        "healthy": healthy,
        "worker": worker,
        "api": api,
        "counts": counts,
        "latest": latest,
        "input_dir": str(server_worker.DEFAULT_INBOX),
        "delivery_dir": str(
            server_worker.operator_workspace.paths_for(
                server_worker.OPERATOR_ROOT
            ).results
        ),
        "operator_status_path": str(
            server_worker.operator_workspace.paths_for(
                server_worker.OPERATOR_ROOT
            ).status
        ),
    }


def format_system_overview(overview: dict[str, Any]) -> str:
    """Render the status in plain Chinese for a double-click console."""
    worker_ok = bool((overview.get("worker") or {}).get("healthy"))
    worker_ready = bool((overview.get("worker") or {}).get("ready", True))
    api_ok = bool((overview.get("api") or {}).get("healthy"))
    lines = [
        "Amazon 自动处理系统",
        "=" * 36,
        f"总体状态：{'运行正常' if overview.get('healthy') else '需要检查'}",
        "自动处理："
        + (
            "正常"
            if worker_ok and worker_ready
            else "需要处理"
            if worker_ok
            else "未启动或异常"
        ),
        f"调用接口：{'正常' if api_ok else '未启动或异常'}",
        "",
        "任务统计：",
    ]
    counts = overview.get("counts") or {}
    visible = [
        "queued",
        "running",
        "retry_wait",
        "delivery_retry",
        "pending_review",
        "blocked",
        "invalid_input",
        "failed",
        "published",
        "published_with_warnings",
    ]
    for status in visible:
        count = int(counts.get(status) or 0)
        if count or status in {"queued", "running", "retry_wait"}:
            lines.append(f"  {JOB_STATUS_LABELS[status]}：{count}")
    latest = overview.get("latest")
    if isinstance(latest, dict):
        label = JOB_STATUS_LABELS.get(
            str(latest.get("status") or ""),
            "状态未知",
        )
        lines.extend([
            "",
            "最近任务：",
            f"  文件：{latest.get('source_name') or '未知'}",
            f"  商品：{int(latest.get('row_count') or 0)} 个",
            f"  状态：{label}",
        ])
        if latest.get("progress_total"):
            lines.append(
                "  进度："
                f"{int(latest.get('progress_current') or 0)}/"
                f"{int(latest.get('progress_total') or 0)}"
            )
        if latest.get("isolated_count"):
            lines.append(
                f"  隔离商品：{int(latest.get('isolated_count') or 0)} 个"
            )
        if latest.get("blocker_reason"):
            lines.append(f"  阻塞原因：{latest.get('blocker_reason')}")
    else:
        lines.extend(["", "最近任务：暂无"])
    if not overview.get("healthy"):
        lines.extend([
            "",
            "建议操作：打开 00_常用入口，双击 04_一键安装服务器.bat；",
            "如果已经安装，等待 1 分钟后再查看。",
        ])
    lines.extend([
        "",
        f"采集表入口：{overview.get('input_dir')}",
        f"结果目录：{overview.get('delivery_dir')}",
    ])
    return "\n".join(lines)


__all__ = ["JOB_STATUS_LABELS", "format_system_overview", "system_overview"]
