"""Deterministic Agent runner for one Amazon collection JSON file."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any

from amazon_processor import process_json
from amazon_processor.providers import ProviderError
from amazon_processor.schema import (
    AMAZON_JSON_OUTPUT_FIELDS,
    validate_columnar_payload,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AGENT_RUNS = PROJECT_ROOT / ".runtime" / "agent_runs"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            Path(temp_name).unlink(missing_ok=True)
        except OSError:
            pass


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层不是对象: {path}")
    return value


def verify_output(path: Path) -> dict[str, int]:
    payload = load_json(path)
    actual_fields = tuple(payload)
    if actual_fields != AMAZON_JSON_OUTPUT_FIELDS:
        raise ValueError(
            "回填表字段或顺序错误: " + ", ".join(actual_fields)
        )
    rows = validate_columnar_payload(
        payload,
        required_fields=AMAZON_JSON_OUTPUT_FIELDS,
    )
    product_ids = set(payload["商品id"])
    problem_ids = set(payload["有问题的产品id"])
    overlap = sorted(product_ids & problem_ids)
    if overlap:
        raise ValueError(
            "问题商品仍存在于正式逐行数据: " + ", ".join(overlap[:10])
        )
    if any(len(images) < 2 for images in payload["产品图片链接"]):
        raise ValueError("正式商品必须包含一张主图和至少一张产品附图")
    return {"released_rows": rows, "problem_product_ids": len(problem_ids)}


def read_run_metrics(review_path: Path) -> dict[str, Any]:
    status_path = review_path.parent / "运行状态.json"
    if status_path.is_file():
        status = load_json(status_path)
        return {
            "provider": dict(status.get("provider_metrics") or {}),
            "image_safety": dict(status.get("image_safety") or {}),
        }
    review_data_path = review_path.parent / "审核数据.json"
    if review_data_path.is_file():
        summary = dict(load_json(review_data_path).get("summary") or {})
        return {
            "provider": dict(summary.get("provider_metrics") or {}),
            "image_safety": dict(
                (summary.get("run_metrics") or {}).get("image_safety_gate")
                or {}
            ),
        }
    return {"provider": {}, "image_safety": {}}


def run(
    input_path: Path,
    *,
    attempts: int = 2,
    retry_delay_s: float = 30,
) -> tuple[int, Path]:
    source = input_path.expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".json":
        raise ValueError(f"采集表不存在或不是 JSON: {source}")
    before_hash = sha256_file(source)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_path = AGENT_RUNS / f"{stamp}_{before_hash[:8]}" / "result.json"
    started_at = time.time()
    last_error: Exception | None = None

    for attempt in range(1, max(1, attempts) + 1):
        try:
            result = process_json(source, unattended=True)
            if sha256_file(source) != before_hash:
                raise RuntimeError("输入采集表在处理期间被修改")
            if not result.published or result.output_path is None:
                payload = {
                    "version": 1,
                    "status": "pending_review",
                    "published": False,
                    "input_path": str(source),
                    "input_sha256": before_hash,
                    "review_path": str(result.review_path),
                    "review_data_path": str(result.review_data_path),
                    "exception_path": str(result.exception_path or ""),
                    "pending_product_ids": list(result.pending_product_ids),
                    "elapsed_s": round(time.time() - started_at, 3),
                    "attempts": attempt,
                }
                atomic_json(result_path, payload)
                return 2, result_path
            validation = verify_output(result.output_path)
            metrics = read_run_metrics(result.review_path)
            payload = {
                "version": 1,
                "status": "published",
                "published": True,
                "input_path": str(source),
                "input_sha256": before_hash,
                "output_path": str(result.output_path),
                "review_path": str(result.review_path),
                "review_data_path": str(result.review_data_path),
                "exception_path": str(result.exception_path or ""),
                "retained_products": result.retained_products,
                "quarantined_products": result.quarantined_products,
                "isolated_product_ids": list(result.isolated_product_ids),
                "elapsed_s": round(time.time() - started_at, 3),
                "attempts": attempt,
                "validation": validation,
                "request_stats": metrics["provider"],
                "image_stats": metrics["image_safety"],
            }
            atomic_json(result_path, payload)
            return 0, result_path
        except ProviderError as exc:
            last_error = exc
            if not exc.retryable or attempt >= max(1, attempts):
                break
            time.sleep(max(0.0, retry_delay_s))
        except Exception as exc:
            last_error = exc
            break

    assert last_error is not None
    failure = (
        last_error.to_dict()
        if isinstance(last_error, ProviderError)
        else {
            "type": type(last_error).__name__,
            "detail": str(last_error)[:500],
            "retryable": False,
        }
    )
    atomic_json(result_path, {
        "version": 1,
        "status": "failed",
        "published": False,
        "input_path": str(source),
        "input_sha256": before_hash,
        "elapsed_s": round(time.time() - started_at, 3),
        "attempts": max(1, attempts),
        "failure": failure,
    })
    return 1, result_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Agent 调用的 Amazon JSON 确定性处理入口"
    )
    parser.add_argument("input")
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--retry-delay", type=float, default=30)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        exit_code, result_path = run(
            Path(args.input),
            attempts=args.attempts,
            retry_delay_s=args.retry_delay,
        )
    except Exception as exc:
        print(f"处理入口失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"AGENT_RESULT={result_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
