"""Child-process supervision and result classification for the Worker.

This module knows how to run one isolated ``amazon_processor run`` command,
keep supervising it when a status heartbeat is temporarily unavailable, stop
stalled work, sanitize log tails, and classify structured or legacy failures.
It does not schedule retries or mutate durable Worker job state.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Mapping


AUTH_MARKERS = (
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
TRANSIENT_MARKERS = (
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
INPUT_MARKERS = (
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
SECRET_RE = re.compile(
    r"(?i)(bearer\s+|(?:sk|cpk)-)[A-Za-z0-9._:/+\-]+"
)


def classify_failure(
    text: str,
    attempt: int,
    max_retries: int,
) -> tuple[str, str, str]:
    lowered = text.lower()
    if any(marker.lower() in lowered for marker in AUTH_MARKERS):
        return "blocked", "鉴权或余额错误，进入低频自动复查", "auth"
    if any(marker in lowered for marker in INPUT_MARKERS):
        return "invalid_input", "输入格式或源数据错误，不进行无效重试", "input"
    transient = any(marker in lowered for marker in TRANSIENT_MARKERS)
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


def extract_result(text: str) -> tuple[str, str, str]:
    published = re.search(r"正式表已更新:\s*(.+)", text)
    if published:
        return "published", published.group(1).strip(), ""
    pending = re.search(r"待人工审核包:\s*(.+)", text)
    if pending:
        return "pending_review", "", pending.group(1).strip()
    return "invalid_result", "", ""


def error_tail(text: str, limit: int = 1200) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    tail = "\n".join(lines[-12:])[-limit:]
    return SECRET_RE.sub(lambda match: f"{match.group(1)}***", tail)


def read_outcome(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload if int(payload.get("version") or 0) == 1 else None


def classify_outcome_failure(
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


def _stop_process(
    process: Any,
    *,
    timeout_error: type[BaseException],
) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except timeout_error:
        process.kill()
        process.wait(timeout=10)


def run_child(
    source: Path,
    log_path: Path,
    *,
    outcome_path: Path | None = None,
    timeout_hours: float = 24.0,
    stall_minutes: float = 45.0,
    heartbeat: Callable[[], None] | None = None,
    project_root: Path | None = None,
    python_executable: str | None = None,
    environment: Mapping[str, str] | None = None,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    timeout_error: type[BaseException] = subprocess.TimeoutExpired,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, str]:
    """Run one isolated CLI child while streaming output to its durable log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        python_executable or sys.executable,
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
    started = monotonic()
    last_progress = started
    last_size = -1
    child_environment = dict(os.environ if environment is None else environment)
    child_environment["PYTHONUNBUFFERED"] = "1"
    child_environment["PYTHONUTF8"] = "1"
    child_environment["PYTHONIOENCODING"] = "utf-8"
    timed_out = False
    heartbeat_warning_logged = False
    with log_path.open("w", encoding="utf-8") as stream:
        process = popen_factory(
            command,
            cwd=str(project_root or Path.cwd()),
            stdout=stream,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_environment,
        )
        while process.poll() is None:
            try:
                current_size = log_path.stat().st_size
            except OSError:
                current_size = last_size
            if current_size != last_size:
                last_size = current_size
                last_progress = monotonic()
            if heartbeat:
                try:
                    heartbeat()
                    heartbeat_warning_logged = False
                except Exception as exc:
                    if not heartbeat_warning_logged:
                        stream.write(
                            "\n[WORKER] 心跳写入暂时失败，任务继续运行: "
                            f"{type(exc).__name__}: {exc}\n"
                        )
                        stream.flush()
                        heartbeat_warning_logged = True
            if monotonic() - started > max(0.1, timeout_hours * 3600):
                timed_out = True
                _stop_process(process, timeout_error=timeout_error)
                stream.write("\n[WORKER] 单个任务超过最大运行时间，已终止\n")
                stream.flush()
                break
            if monotonic() - last_progress > max(1.0, stall_minutes * 60):
                timed_out = True
                _stop_process(process, timeout_error=timeout_error)
                stream.write(
                    "\n[WORKER] 子进程长时间无日志进展，已终止并准备续跑\n"
                )
                stream.flush()
                break
            sleep(2)
        exit_code = 124 if timed_out else int(process.returncode or 0)
    return exit_code, log_path.read_text(encoding="utf-8", errors="replace")


__all__ = [
    "classify_failure",
    "classify_outcome_failure",
    "error_tail",
    "extract_result",
    "read_outcome",
    "run_child",
]
