"""The single Amazon JSON collection-table processing workflow."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .config.env import load_config
from .config.locking import processing_lock
from .delivery import deliver
from .images.gate import run_structured_image_safety_gate
from .log import log as _log, new_request_id
from .providers import get_provider, reload_provider
from .policy import enforce_prohibited_listing_terms
from .runtime import RunContext, RunResult
from .schema import load_rows, validate_input_rows
from .text.descriptions import (
    clean_descriptions,
    enforce_description_safety,
    partition_product_description_rows,
)
from .text.listing import generate_bullets_keywords
from .text.titles import optimize_titles
from .text.localization import LocalizationCache, ensure_localized_rows


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_ROOT = PROJECT_ROOT / ".runtime"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _partition_rows_without_attachments(
    rows: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Remove products that have no usable attachment after image review."""
    retained = []
    rejected = []
    for row in rows:
        if row.get("extra_imgs"):
            retained.append(row)
            continue
        rejected.append({
            "product_id": str(row.get("id") or ""),
            "title": str(row.get("title") or ""),
            "code": "missing_safe_attachments",
            "message": (
                "风险附图删除后没有可用附图，商品已从正式回填表删除"
            ),
        })
    return retained, rejected


def process_json(input_path: str | Path) -> RunResult:
    """Process one Amazon JSON collection table and publish formal artifacts."""
    with processing_lock():
        return _process_json_unlocked(input_path)


def _process_json_unlocked(input_path: str | Path) -> RunResult:
    """Run with an exclusive configuration snapshot for the whole task."""
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
    legacy_rows = sum(
        bool(row.get("_legacy_site_defaulted")) for row in rows
    )
    context.runtime_metrics["marketplaces"] = {
        "legacy_defaulted_to_us": legacy_rows,
        "input_by_site": {
            site: sum(row.get("site") == site for row in rows)
            for site in sorted({str(row.get("site") or "US") for row in rows})
        },
    }
    if legacy_rows:
        print(
            f"旧采集表缺少产品站点：{legacy_rows} 行已按 US/en-US 处理",
            flush=True,
        )
    context.data, description_rejected = (
        partition_product_description_rows(rows)
    )
    context.runtime_metrics["description_rejected_products"] = (
        description_rejected
    )
    problem_product_ids = [
        str(item.get("product_id") or "")
        for item in description_rejected
        if str(item.get("product_id") or "")
    ]

    cache_path = (
        RUNTIME_ROOT
        / "cache"
        / "pipeline"
        / f"{source_hash}.json"
    )
    image_references = [
        url
        for row in context.data
        for url in (
            [row.get("main_img")]
            + list(row.get("extra_imgs") or [])
            + list(row.get("var_imgs") or [])
        )
        if str(url or "")
    ]
    context.runtime_metrics["image_deduplication"] = {
        "references": len(image_references),
        "unique_urls": len(set(image_references)),
        "deduplicated_references": (
            len(image_references) - len(set(image_references))
        ),
    }
    context.transform(
        "审图与生图",
        run_structured_image_safety_gate,
        str(cache_path),
        context.quality_issues,
        runtime_metrics=context.runtime_metrics,
        provider_getter=get_provider,
    )
    generated_urls = {
        str(image.get("url") or "")
        for row in context.data
        for image in row.get("_image_assessments") or []
        if image.get("source") == "generated" and image.get("url")
    }
    context.runtime_metrics["image_deduplication"][
        "unique_generated_images"
    ] = len(generated_urls)
    context.data, attachment_rejected = (
        _partition_rows_without_attachments(context.data)
    )
    context.runtime_metrics["attachment_rejected_products"] = (
        attachment_rejected
    )
    problem_product_ids = list(dict.fromkeys([
        *problem_product_ids,
        *(
            str(item.get("product_id") or "")
            for item in attachment_rejected
            if str(item.get("product_id") or "")
        ),
    ]))
    text_cache = LocalizationCache(
        RUNTIME_ROOT
        / "cache"
        / "localization"
        / f"{source_hash}.json"
    )
    all_rows = context.data
    for row in all_rows:
        text_cache.restore(row)
    pending_rows = [
        row for row in all_rows
        if not row.get("_localization_cache_hit")
    ]
    localization_metrics = context.runtime_metrics.setdefault(
        "localization",
        {},
    )
    localization_metrics.setdefault("initial_pending", len(pending_rows))
    title_pending = [
        row for row in pending_rows
        if "title" not in row.get("_localization_partial_fields", [])
    ]
    if title_pending:
        context.data = title_pending
        context.transform(
            "标题优化",
            optimize_titles,
            provider_getter=get_provider,
        )
        for row in context.data:
            text_cache.store_partial(row, ("title",))

    description_pending = [
        row for row in pending_rows
        if "desc" not in row.get("_localization_partial_fields", [])
    ]
    if description_pending:
        context.data = description_pending
        context.transform(
            "描述清洗",
            clean_descriptions,
            provider_getter=get_provider,
        )
        context.data = enforce_description_safety(context.data)
        for row in context.data:
            text_cache.store_partial(row, ("desc",))

    listing_fields = {"subtitle", "bullets", "keywords"}
    listing_pending = [
        row for row in pending_rows
        if not listing_fields.issubset(
            row.get("_localization_partial_fields", [])
        )
    ]
    if listing_pending:
        context.data = listing_pending
        context.transform(
            "Bullet与关键词",
            generate_bullets_keywords,
            provider_getter=get_provider,
        )
        context.data = enforce_prohibited_listing_terms(context.data)
        for row in context.data:
            text_cache.store_partial(
                row,
                ("subtitle", "bullets", "keywords"),
            )

    if pending_rows:
        context.data = pending_rows
        context.data = enforce_description_safety(context.data)
        context.data = enforce_prohibited_listing_terms(context.data)
        context.transform(
            "多站点文案校验",
            ensure_localized_rows,
            cache=text_cache,
            provider_getter=get_provider,
            runtime_metrics=context.runtime_metrics,
        )
    context.data = all_rows
    marketplace_metrics = context.runtime_metrics["marketplaces"]
    marketplace_metrics["completed_by_site"] = {
        site: sum(row.get("site") == site for row in context.data)
        for site in sorted({str(row.get("site") or "US") for row in context.data})
    }
    marketplace_metrics["localization_cache_hits"] = text_cache.hits
    localization_metrics["cache_hits"] = text_cache.hits
    localization_metrics["cache_writes"] = text_cache.writes
    localization_metrics["completed"] = len(context.data)
    result = deliver(
        context,
        problem_product_ids=problem_product_ids,
    )
    print(f"回填表: {result.output_path}", flush=True)
    print(f"终审包: {result.review_path}", flush=True)
    return result


__all__ = ["process_json"]
