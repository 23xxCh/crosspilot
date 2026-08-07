"""Minimal command Interface used by the two Windows launchers."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from .config.locking import processing_lock
from .delivery import LATEST_DIR
from .pipeline import process_json
from .review.decisions import apply_latest_decisions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amazon_processor",
        description="Amazon JSON 采集表核心处理器",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    run = subcommands.add_parser("run", help="处理 Amazon JSON 采集表")
    run.add_argument("input", help="采集表 JSON 路径")
    run.add_argument("--open", action="store_true", help="处理后打开输出目录")
    apply = subcommands.add_parser("apply", help="应用终审决定")
    apply.add_argument("decisions", help="审核决定 JSON 路径")
    apply.add_argument("--open", action="store_true", help="处理后打开输出目录")
    config = subcommands.add_parser("config", help="打开本地配置管理中心")
    config.add_argument(
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
        default=str(Path("01_输入采集表")),
        help="只读采集表目录",
    )
    worker.add_argument("--poll-seconds", type=float, default=15.0)
    worker.add_argument("--stable-seconds", type=float, default=5.0)
    worker.add_argument("--max-retries", type=int, default=3)
    worker.add_argument("--retry-base-seconds", type=float, default=30.0)
    worker.add_argument("--timeout-hours", type=float, default=24.0)
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "config":
            from .config.manager import serve_config_manager

            serve_config_manager(open_browser=not args.no_open)
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
                retry_terminal=args.retry_terminal,
                once=args.once,
            )
        if args.command == "run":
            result = process_json(Path(args.input))
            if result.published:
                print(f"正式表已更新: {result.output_path}")
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
        print(f"处理失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
