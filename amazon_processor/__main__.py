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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "config":
            from .config.manager import serve_config_manager

            serve_config_manager(open_browser=not args.no_open)
            return 0
        if args.command == "run":
            result = process_json(Path(args.input))
            print(result.output_path)
        else:
            with processing_lock():
                report = apply_latest_decisions(Path(args.decisions))
            print(f"已应用 {len(report.get('decisions') or [])} 条审核决定")
            print(LATEST_DIR)
        if args.open and os.name == "nt":
            os.startfile(LATEST_DIR)  # type: ignore[attr-defined]
        return 0
    except Exception as exc:
        print(f"处理失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
