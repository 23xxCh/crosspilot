"""Command-line entry points for one Agent-managed Amazon JSON task."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

from .pipeline import process_json
from .providers import ProviderError
from .review.decisions import apply_latest_decisions
from .config.locking import processing_lock


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LATEST_DIR = PROJECT_ROOT / "02_处理结果" / "最新"


def _atomic_json(path: str | None, payload: dict) -> None:
    if not path:
        return
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    finally:
        try:
            Path(temp_name).unlink(missing_ok=True)
        except OSError:
            pass


def _failure(exc: Exception) -> dict:
    if isinstance(exc, ProviderError):
        return exc.to_dict()
    return {
        "type": type(exc).__name__,
        "detail": str(exc)[:300],
        "retryable": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Amazon JSON 核心处理器")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="处理一份 Amazon JSON 采集表")
    run.add_argument("input")
    run.add_argument("--unattended", action="store_true")
    run.add_argument("--outcome")
    run.add_argument("--open", action="store_true")
    apply = subparsers.add_parser("apply", help="应用终审决定")
    apply.add_argument("decisions")
    apply.add_argument("--open", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            result = process_json(args.input, unattended=args.unattended)
            payload = {
                "version": 1,
                "finished_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "status": "published" if result.published else "pending",
                "published": result.published,
                "output_path": str(result.output_path or ""),
                "review_path": str(result.review_path),
                "review_data_path": str(result.review_data_path),
                "exception_path": str(result.exception_path or ""),
                "retained_products": result.retained_products,
                "quarantined_products": result.quarantined_products,
                "pending_product_ids": list(result.pending_product_ids),
                "isolated_product_ids": list(result.isolated_product_ids),
                "elapsed_s": result.elapsed_s,
            }
            _atomic_json(args.outcome, payload)
            if result.published:
                print(f"正式表已更新: {result.output_path}")
                open_path = LATEST_DIR
            else:
                print(f"正式表未覆盖，待审核包: {result.review_path}")
                open_path = result.review_path.parent
            exit_code = 0 if result.published else 2
        else:
            with processing_lock():
                report = apply_latest_decisions(Path(args.decisions))
            print(f"已应用 {len(report.get('decisions') or [])} 条审核决定")
            open_path = LATEST_DIR
            exit_code = 0
        if args.open and os.name == "nt":
            os.startfile(open_path)  # type: ignore[attr-defined]
        return exit_code
    except Exception as exc:
        if args.command == "run":
            _atomic_json(
                args.outcome,
                {
                    "version": 1,
                    "finished_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "status": "failed",
                    "published": False,
                    "failure": _failure(exc),
                },
            )
        print(f"处理失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
