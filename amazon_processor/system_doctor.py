"""Offline server self-check used by the administrator entry point."""
from __future__ import annotations

import os
import subprocess
from typing import Any

from . import operator_workspace, server_worker


TASK_NAMES = (
    "AmazonProcessor-Unattended",
    "AmazonProcessor-API",
    "AmazonProcessor-Watchdog",
)
RESTART_MARKER_NAME = "restart_required.json"


def _run_schtasks(*arguments: str) -> subprocess.CompletedProcess[str]:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.run(
        ["schtasks", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=flags,
    )


def _task_installed(name: str) -> bool:
    if os.name != "nt":
        return False
    return _run_schtasks("/Query", "/TN", name).returncode == 0


def _start_task(name: str) -> bool:
    if os.name != "nt":
        return False
    return _run_schtasks("/Run", "/TN", name).returncode == 0


def _restart_task(name: str) -> bool:
    if os.name != "nt":
        return False
    _run_schtasks("/End", "/TN", name)
    return _start_task(name)


def run_system_doctor(*, repair: bool = True) -> dict[str, Any]:
    """Check local prerequisites and apply only safe, offline repairs."""
    paths = operator_workspace.ensure_workspace(server_worker.OPERATOR_ROOT)
    operator_workspace.bootstrap_latest_result(
        server_worker.FORMAL_LATEST_ROOT,
        root=server_worker.OPERATOR_ROOT,
    )
    readiness: dict[str, Any]
    try:
        readiness = server_worker.preflight(paths.inbox)
        preflight_ok = not readiness.get("missing_operations")
    except Exception as exc:
        readiness = {
            "free_disk_gb": 0,
            "missing_operations": ["preflight"],
            "blocker_reason": f"{type(exc).__name__}: {exc}",
        }
        preflight_ok = False

    try:
        worker = server_worker.worker_health(120.0)
    except Exception:
        worker = {"healthy": False, "status": "unavailable"}
    try:
        from .api_server import api_health_check

        api = api_health_check().get("api") or {"healthy": False}
    except Exception:
        api = {"healthy": False, "status": "unavailable"}

    tasks = {name: _task_installed(name) for name in TASK_NAMES}
    started: list[str] = []
    restarted: list[str] = []
    restart_marker = server_worker.RUNTIME_ROOT / RESTART_MARKER_NAME
    restart_required = restart_marker.is_file()
    worker_idle = bool(
        worker.get("healthy")
        and worker.get("status") == "idle"
        and not worker.get("current_job")
        and int(worker.get("queue_depth") or 0) == 0
    )
    if repair:
        if tasks[TASK_NAMES[0]] and not worker.get("healthy"):
            if _start_task(TASK_NAMES[0]):
                started.append(TASK_NAMES[0])
        if tasks[TASK_NAMES[1]] and not api.get("healthy"):
            if _start_task(TASK_NAMES[1]):
                started.append(TASK_NAMES[1])
        if restart_required and worker_idle:
            for name in TASK_NAMES[:2]:
                if tasks[name] and _restart_task(name):
                    restarted.append(name)
            if len(restarted) == 2:
                restart_marker.unlink(missing_ok=True)
                restart_required = False
        server_worker.refresh_operator_status(
            healthy=bool(preflight_ok),
        )

    tasks_ok = all(tasks.values()) if os.name == "nt" else True
    healthy = bool(
        preflight_ok
        and tasks_ok
        and worker.get("healthy")
        and api.get("healthy")
    )
    return {
        "healthy": healthy,
        "repair_requested": bool(repair),
        "operator_root": str(paths.root),
        "free_disk_gb": float(readiness.get("free_disk_gb") or 0),
        "directories_writable": "preflight" not in (
            readiness.get("missing_operations") or []
        ),
        "model_credentials_ready": not bool(
            readiness.get("missing_operations")
        ),
        "missing_operations": list(
            readiness.get("missing_operations") or []
        ),
        "worker_healthy": bool(worker.get("healthy")),
        "api_healthy": bool(api.get("healthy")),
        "scheduled_tasks": tasks,
        "started_tasks": started,
        "restarted_tasks": restarted,
        "restart_required": restart_required,
        "restart_deferred": bool(restart_required and not worker_idle),
        "next_action": (
            "系统可以正常使用"
            if healthy and not restart_required
            else "系统正在处理任务，更新重启会在空闲后再执行"
            if restart_required and not worker_idle
            else "请按下方未通过项目处理，必要时重新运行首次安装"
        ),
    }


def format_report(report: dict[str, Any]) -> str:
    mark = lambda value: "通过" if value else "需要处理"
    lines = [
        "Amazon 服务器系统自检与修复",
        "=" * 36,
        f"总体：{mark(report.get('healthy'))}",
        f"目录读写：{mark(report.get('directories_writable'))}",
        f"模型与密钥：{mark(report.get('model_credentials_ready'))}",
        f"自动处理：{mark(report.get('worker_healthy'))}",
        f"任务接口：{mark(report.get('api_healthy'))}",
        f"磁盘剩余：{float(report.get('free_disk_gb') or 0):.1f} GB",
        "",
        "后台任务：",
    ]
    for name, installed in (report.get("scheduled_tasks") or {}).items():
        lines.append(f"  {name}：{'已安装' if installed else '未安装'}")
    if report.get("started_tasks"):
        lines.extend([
            "",
            "本次已尝试启动：" + "、".join(report["started_tasks"]),
        ])
    if report.get("restarted_tasks"):
        lines.extend([
            "",
            "本次已安全重启：" + "、".join(report["restarted_tasks"]),
        ])
    lines.extend(["", f"下一步：{report.get('next_action')}"])
    return "\n".join(lines)


__all__ = ["TASK_NAMES", "format_report", "run_system_doctor"]
