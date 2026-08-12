"""Minimal command Interface used by the two Windows launchers."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

from .config.locking import processing_lock
from .delivery import LATEST_DIR
from .pipeline import process_json
from .review.decisions import apply_latest_decisions


_SECRET_RE = re.compile(
    r"(?i)(bearer\s+|(?:sk|cpk)-)[A-Za-z0-9._:/+\-]+"
)


def _atomic_outcome(path: str | Path | None, payload: dict) -> None:
    if not path:
        return
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _failure_outcome(exc: Exception) -> dict:
    from .providers.support import (
        ProviderAuthError,
        ProviderError,
        ProviderQuotaError,
    )

    if isinstance(exc, ProviderAuthError):
        kind, retryable = "auth", False
    elif isinstance(exc, ProviderQuotaError):
        kind, retryable = "quota", False
    elif isinstance(exc, ProviderError):
        kind, retryable = (
            ("transient", True)
            if exc.retryable
            else ("internal", False)
        )
    elif isinstance(exc, (FileNotFoundError, ValueError)):
        kind, retryable = "input", False
    else:
        kind, retryable = "internal", True
    detail = _SECRET_RE.sub(
        lambda match: f"{match.group(1)}***",
        str(exc or type(exc).__name__),
    )[:500]
    failure = {
        "kind": kind,
        "retryable": retryable,
        "type": type(exc).__name__,
        "message": detail,
    }
    if isinstance(exc, ProviderError):
        failure.update(exc.to_dict())
        failure["kind"] = kind
        failure["retryable"] = retryable
    return failure


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amazon_processor",
        description="Amazon JSON 采集表核心处理器",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    run = subcommands.add_parser("run", help="处理 Amazon JSON 采集表")
    run.add_argument("input", help="采集表 JSON 路径")
    run.add_argument("--open", action="store_true", help="处理后打开输出目录")
    run.add_argument(
        "--unattended",
        action="store_true",
        help="隔离无法自动处理的商品，并发布其余合格商品",
    )
    run.add_argument(
        "--outcome",
        help="Worker 使用的结构化任务结果路径",
    )
    apply = subcommands.add_parser("apply", help="应用终审决定")
    apply.add_argument("decisions", help="审核决定 JSON 路径")
    apply.add_argument("--open", action="store_true", help="处理后打开输出目录")
    config = subcommands.add_parser("config", help="打开本地配置管理中心")
    config.add_argument(
        "--no-open",
        action="store_true",
        help="只启动服务，不自动打开浏览器",
    )
    image_lab = subcommands.add_parser(
        "image-lab",
        help="打开本机 Agnes 生图测试台",
    )
    image_lab.add_argument(
        "--no-open",
        action="store_true",
        help="只启动服务，不自动打开浏览器",
    )
    worker = subcommands.add_parser(
        "worker",
        help="Windows 全天监控采集表并逐个处理",
    )
    worker.add_argument(
        "--input-dir",
        default=str(Path("01_输入采集表") / "待处理"),
        help="只读采集表目录",
    )
    worker.add_argument("--poll-seconds", type=float, default=15.0)
    worker.add_argument("--stable-seconds", type=float, default=5.0)
    worker.add_argument("--max-retries", type=int, default=3)
    worker.add_argument("--retry-base-seconds", type=float, default=30.0)
    worker.add_argument("--timeout-hours", type=float, default=24.0)
    worker.add_argument(
        "--stall-minutes",
        type=float,
        default=45.0,
        help="子进程无日志进展多久后自动终止续跑",
    )
    worker.add_argument(
        "--blocked-retry-hours",
        type=float,
        default=6.0,
        help="鉴权或余额阻塞后的低频自动复查间隔",
    )
    worker.add_argument(
        "--retry-terminal",
        action="store_true",
        help="人工修复鉴权/额度后，允许重试已终止任务",
    )
    worker.add_argument(
        "--once",
        action="store_true",
        help="扫描并处理一轮后退出，适合任务计划程序",
    )
    worker_status = subcommands.add_parser(
        "worker-status",
        help="检查全天 Worker 心跳",
    )
    worker_status.add_argument("--max-age-seconds", type=float, default=120.0)
    api = subcommands.add_parser(
        "api",
        help="启动供其他系统调用的异步任务 API",
    )
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8765)
    api.add_argument(
        "--input-dir",
        default=str(Path("01_输入采集表") / "待处理"),
        help="API 验证通过后原子写入的 Worker 输入目录",
    )
    api.add_argument("--max-body-mb", type=int, default=20)
    api.add_argument("--worker-max-age-seconds", type=float, default=120.0)
    api_status = subcommands.add_parser(
        "api-status",
        help="检查异步任务 API 是否存活",
    )
    api_status.add_argument(
        "--url",
        default="http://127.0.0.1:8765/api/v1/health",
    )
    api_status.add_argument("--timeout-seconds", type=float, default=5.0)
    system_status = subcommands.add_parser(
        "system-status",
        help="用简明中文查看服务器整体状态",
    )
    system_status.add_argument(
        "--json",
        action="store_true",
        help="输出机器可读 JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "config":
            from .config.manager import serve_config_manager

            serve_config_manager(open_browser=not args.no_open)
            return 0
        if args.command == "image-lab":
            from .image_lab import serve_image_lab

            serve_image_lab(open_browser=not args.no_open)
            return 0
        if args.command == "worker":
            from .server_worker import run_worker

            return run_worker(
                input_dir=args.input_dir,
                poll_seconds=args.poll_seconds,
                stable_seconds=args.stable_seconds,
                max_retries=max(0, args.max_retries),
                retry_base_seconds=max(1.0, args.retry_base_seconds),
                timeout_hours=max(0.1, args.timeout_hours),
                stall_minutes=max(1.0, args.stall_minutes),
                blocked_retry_hours=max(0.1, args.blocked_retry_hours),
                retry_terminal=args.retry_terminal,
                once=args.once,
            )
        if args.command == "worker-status":
            import json

            from .server_worker import worker_health

            health = worker_health(max(1.0, args.max_age_seconds))
            print(json.dumps(health, ensure_ascii=False, indent=2))
            return 0 if health.get("healthy") else 2
        if args.command == "api":
            from .api_server import serve_job_api

            serve_job_api(
                host=args.host,
                port=args.port,
                input_dir=Path(args.input_dir),
                max_body_bytes=max(1, args.max_body_mb) * 1024 * 1024,
                worker_max_age_seconds=max(
                    1.0,
                    args.worker_max_age_seconds,
                ),
            )
            return 0
        if args.command == "api-status":
            import json

            from .api_server import api_health_check

            health = api_health_check(
                url=args.url,
                timeout_seconds=max(0.5, args.timeout_seconds),
            )
            print(json.dumps(health, ensure_ascii=False, indent=2))
            return 0
        if args.command == "system-status":
            import json

            from .api_server import format_system_overview, system_overview

            overview = system_overview()
            if args.json:
                print(json.dumps(overview, ensure_ascii=False, indent=2))
            else:
                print(format_system_overview(overview))
            return 0
        if args.command == "run":
            result = process_json(
                Path(args.input),
                unattended=args.unattended,
            )
            outcome = {
                "version": 1,
                "finished_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "status": (
                    "published_with_warnings"
                    if result.published and result.isolated_product_ids
                    else "published"
                    if result.published
                    else "pending_review"
                ),
                "published": bool(result.published),
                "output_path": str(result.output_path or ""),
                "review_path": str(result.review_path),
                "exception_path": str(result.exception_path or ""),
                "retained_products": result.retained_products,
                "isolated_product_ids": list(result.isolated_product_ids),
                "pending_product_ids": list(result.pending_product_ids),
            }
            _atomic_outcome(args.outcome, outcome)
            if result.published:
                print(f"正式表已更新: {result.output_path}")
                if result.pending_product_ids:
                    print(
                        "自动隔离商品: "
                        + ", ".join(result.pending_product_ids)
                    )
                open_path = LATEST_DIR
            else:
                print(
                    "存在待定商品，正式表未覆盖: "
                    + ", ".join(result.pending_product_ids)
                )
                print(f"待人工审核包: {result.review_path}")
                open_path = result.review_path.parent
        else:
            with processing_lock():
                report = apply_latest_decisions(Path(args.decisions))
            if report.get("status") == "rechecked":
                print(
                    f"已重新审查 {len(report.get('decisions') or [])} 张主图候选"
                )
                if report.get("published"):
                    print(f"正式表已更新: {report.get('output_path')}")
                    open_path = LATEST_DIR
                else:
                    print("仍存在待定商品，正式表未覆盖")
                    print(f"待人工审核包: {report.get('review_path')}")
                    open_path = Path(str(report.get("review_path"))).parent
            else:
                print(f"已应用 {len(report.get('decisions') or [])} 条审核决定")
                print(LATEST_DIR)
                open_path = LATEST_DIR
        if args.open and os.name == "nt":
            os.startfile(open_path)  # type: ignore[attr-defined]
        return 0
    except Exception as exc:
        if getattr(args, "command", "") == "run":
            _atomic_outcome(
                getattr(args, "outcome", None),
                {
                    "version": 1,
                    "finished_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "status": "failed",
                    "published": False,
                    "failure": _failure_outcome(exc),
                },
            )
        print(f"处理失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
