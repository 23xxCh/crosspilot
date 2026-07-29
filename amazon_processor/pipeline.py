"""The single Amazon JSON collection-table processing workflow."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .config.env import load_config
from .delivery import deliver
from .images.gate import run_structured_image_safety_gate
from .log import log as _log, new_request_id
from .providers import get_provider, reload_provider
from .runtime import RunContext, RunResult
from .schema import load_rows, validate_input_rows
from .text.descriptions import clean_descriptions, remove_dirty_descriptions
from .text.listing import generate_bullets_keywords
from .text.titles import optimize_titles


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_ROOT = PROJECT_ROOT / ".runtime"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def process_json(input_path: str | Path) -> RunResult:
    """Process one Amazon JSON collection table and publish formal artifacts."""
    source = Path(input_path).expanduser().resolve()
    if source.suffix.lower() != ".json":
        raise ValueError("仅支持 Amazon JSON 采集表")
    if not source.is_file():
        raise FileNotFoundError(f"采集表不存在: {source}")

    reload_provider()
    provider = get_provider()
    config = load_config()
    source_hash = _sha256(source)
    request_id = new_request_id()
    context = RunContext(
        source_path=source,
        request_id=request_id,
        provider=provider,
    )
    context.runtime_metrics["source"] = {
        "path": str(source),
        "sha256": source_hash,
    }
    _log.info(
        "Amazon JSON处理启动",
        request_id=request_id,
        file=source.name,
        sha256=source_hash,
    )
    print("=== Amazon JSON 采集表 → 回填表 ===", flush=True)
    print(f"输入: {source}", flush=True)

    rows = context.execute(
        "读取采集表",
        load_rows,
        source,
        max_rows=max(0, int(config.get("MAX_ROWS", "0") or 0)),
        safety_limit=max(
            1,
            int(config.get("MAX_INPUT_ROWS", "10000") or 10000),
        ),
    )
    validate_input_rows(rows)
    context.data = rows

    cache_path = (
        RUNTIME_ROOT
        / "cache"
        / "pipeline"
        / f"{source_hash}.json"
    )
    context.transform(
        "审图与生图",
        run_structured_image_safety_gate,
        str(cache_path),
        context.quality_issues,
        runtime_metrics=context.runtime_metrics,
        provider_getter=get_provider,
    )
    context.transform(
        "标题优化",
        optimize_titles,
        provider_getter=get_provider,
    )
    context.transform(
        "描述清洗",
        clean_descriptions,
        provider_getter=get_provider,
    )
    context.transform(
        "Bullet与关键词",
        generate_bullets_keywords,
        provider_getter=get_provider,
    )

    context.data, dirty_ids = remove_dirty_descriptions(context.data)
    quarantined_ids = [
        str(item.get("product_id") or "")
        for item in context.runtime_metrics.get("quarantined_products") or []
        if str(item.get("product_id") or "")
    ]
    problem_ids = list(dict.fromkeys([*quarantined_ids, *dirty_ids]))
    result = deliver(
        context,
        additional_problem_ids=problem_ids,
    )
    print(f"回填表: {result.output_path}", flush=True)
    print(f"终审包: {result.review_path}", flush=True)
    return result


__all__ = ["process_json"]
