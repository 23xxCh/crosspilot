#!/usr/bin/env python3
"""Benchmark pre-generation image routing without generating images."""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
for _path in (str(_ROOT), str(_SCRIPTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from concurrency import adaptive_map
from model_provider import ProviderQuotaError, get_provider, reload_provider
from pipelines.amazon_io import _stage_read_json
from pipelines.amazon_review_gen import _current_image_cache_versions


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _load_cache(path: Path, review_version: str) -> dict[str, bool]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if payload.get("review_prompt_version") != review_version:
        return {}
    return {
        str(url): result
        for url, result in (payload.get("review_results") or {}).items()
        if isinstance(result, bool)
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--concurrency", type=int, default=30)
    args = parser.parse_args()

    started = time.time()
    input_path = Path(args.input).resolve()
    cache_path = Path(args.cache).resolve()
    report_path = Path(args.report).resolve()
    rows = _stage_read_json(None, str(input_path))
    urls = sorted({
        url
        for row in rows
        for url in [row.get("main_img"), *row.get("var_imgs", [])]
        if url
    })
    review_version, _generation_version = (
        _current_image_cache_versions()
    )
    results = _load_cache(cache_path, review_version)
    pending = [url for url in urls if url not in results]
    lock = threading.Lock()
    completed = 0

    def persist() -> None:
        with lock:
            payload = {
                "review_prompt_version": review_version,
                "review_results": dict(results),
            }
        _atomic_write(cache_path, payload)

    reload_provider()
    provider = get_provider()

    def review(url: str):
        return provider.call_vision(url)

    def on_result(url: str, result) -> None:
        nonlocal completed
        if isinstance(result, Exception):
            result = None
        with lock:
            completed += 1
            if isinstance(result, bool):
                results[url] = result
            current = completed
        if isinstance(result, bool) and (
            current % 10 == 0 or current == len(pending)
        ):
            persist()
        if current % 25 == 0 or current == len(pending):
            print(
                f"routing review {current}/{len(pending)}",
                flush=True,
            )

    try:
        _mapped, concurrency = adaptive_map(
            pending,
            review,
            operation="amazon_routing_benchmark",
            initial_workers=max(1, args.concurrency),
            min_workers=2,
            is_success=lambda value: isinstance(value, bool),
            on_result=on_result,
            terminal_exceptions=(ProviderQuotaError,),
            backoff_s=2,
            max_backoff_s=15,
        )
    finally:
        persist()

    reviewed = sum(1 for url in urls if url in results)
    flagged = sum(1 for url in urls if results.get(url) is True)
    clean = sum(1 for url in urls if results.get(url) is False)
    unknown = len(urls) - reviewed
    provider_metrics = (
        provider.metrics_snapshot()
        if hasattr(provider, "metrics_snapshot")
        else {}
    )
    report = {
        "input": str(input_path),
        "rows": len(rows),
        "unique_main_variant_images": len(urls),
        "cache_hits": len(urls) - len(pending),
        "reviewed": reviewed,
        "flagged_for_generation": flagged,
        "clean_retained": clean,
        "unknown": unknown,
        "flag_rate": round(flagged / reviewed, 4) if reviewed else None,
        "elapsed_s": round(time.time() - started, 3),
        "concurrency": concurrency,
        "provider_metrics": provider_metrics,
    }
    _atomic_write(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if unknown == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
