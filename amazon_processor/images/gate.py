"""Review-only image safety gate for the formal Amazon pipeline.

The formal pipeline audits source images, deletes unsafe or unresolved images,
and selects an eligible source image as the main image. It never calls an
image-generation provider.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

from ..concurrency import adaptive_map
from ..providers.support import (
    ProviderAuthError,
    ProviderQuotaError,
    ProviderUnavailableError,
)
from ..quality import AMAZON_REVIEW_CONCURRENCY
from .cache import (
    current_cache_versions,
    load_cache,
    load_manual_overrides,
    manual_safe_assessment,
    save_cache,
)
from .risk import (
    ROLE_PRIORITY,
    assessment_record,
    assessment_status,
    row_image_roles,
    safe_assess_batch,
    unknown_image_assessment,
    unknown_main_text_assessment,
)


def _index_images(
    data: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    usage: dict[str, list[dict[str, Any]]] = defaultdict(list)
    row_by_id: dict[str, dict[str, Any]] = {}
    for row_index, row in enumerate(data):
        product_id = str(row.get("id") or row_index + 1)
        row_by_id[product_id] = row
        for url, role, position in row_image_roles(row):
            usage[url].append(
                {
                    "product_id": product_id,
                    "role": role,
                    "position": position,
                }
            )
    return usage, row_by_id


def _review_batches(urls: list[str]) -> tuple[list[list[str]], int]:
    from ..config.env import get_int

    batch_size = max(1, min(5, get_int("REVIEW_BATCH_SIZE", 3)))
    return (
        [
            urls[index:index + batch_size]
            for index in range(0, len(urls), batch_size)
        ],
        batch_size,
    )


def _review_source_images(
    urls: list[str],
    *,
    assessments: dict[str, dict[str, Any]],
    cache: dict[str, Any],
    cache_path: str | None,
    provider_getter: Callable[[], object],
    concurrency_stats: dict[str, Any],
    progress: Callable[[int, int], None] | None,
) -> None:
    missing = [url for url in urls if url not in assessments]
    batches, batch_size = _review_batches(missing)
    total_work = max(1, len(missing))
    completed = 0

    def assess_one(batch: list[str]) -> list[dict[str, Any]]:
        return safe_assess_batch(provider_getter(), batch)

    def assess_done(batch: list[str], result: object) -> None:
        nonlocal completed
        if isinstance(result, Exception) or not isinstance(result, list):
            result = []
        for index, url in enumerate(batch):
            value = result[index] if index < len(result) else None
            if not isinstance(value, dict):
                value = unknown_image_assessment("assessment worker failed")
                value["operational_failure"] = True
            assessments[url] = value
            completed += 1
        save_cache(cache_path, cache)
        if progress:
            progress(completed, total_work)

    if not missing:
        print(f"结构化图片初审全部缓存命中: {len(urls)} 张", flush=True)
        return
    print(
        f"结构化图片初审 {len(missing)} 张（每批 {batch_size} 张，"
        f"{AMAZON_REVIEW_CONCURRENCY} 批并发，自适应退避）...",
        flush=True,
    )
    _, review_stats = adaptive_map(
        batches,
        assess_one,
        operation="amazon_structured_review",
        initial_workers=AMAZON_REVIEW_CONCURRENCY,
        min_workers=min(5, AMAZON_REVIEW_CONCURRENCY),
        is_success=lambda value: (
            isinstance(value, list)
            and bool(value)
            and all(assessment_status(item) != "unknown" for item in value)
        ),
        on_result=assess_done,
        terminal_exceptions=(ProviderAuthError, ProviderQuotaError),
        backoff_s=2,
        max_backoff_s=15,
    )
    review_stats["images"] = len(missing)
    review_stats["batch_size"] = batch_size
    concurrency_stats["amazon_structured_review"] = review_stats


def _review_main_candidates(
    urls: list[str],
    *,
    assessments: dict[str, dict[str, Any]],
    cache: dict[str, Any],
    cache_path: str | None,
    provider_getter: Callable[[], object],
    concurrency_stats: dict[str, Any],
) -> None:
    missing = [url for url in urls if url not in assessments]
    batches, batch_size = _review_batches(missing)

    def assess_one(batch: list[str]) -> list[dict[str, Any]]:
        return safe_assess_batch(
            provider_getter(),
            batch,
            policy="main_text_free",
        )

    def assess_done(batch: list[str], result: object) -> None:
        if isinstance(result, Exception) or not isinstance(result, list):
            result = []
        for index, url in enumerate(batch):
            value = result[index] if index < len(result) else None
            if not isinstance(value, dict):
                value = unknown_main_text_assessment(
                    "main-image assessment worker failed"
                )
                value["operational_failure"] = True
            assessments[url] = value
        save_cache(cache_path, cache)

    if not missing:
        print(f"主图资格检查全部缓存命中: {len(urls)} 张", flush=True)
        return
    print(
        f"主图资格检查 {len(missing)} 张（每批 {batch_size} 张，"
        f"{AMAZON_REVIEW_CONCURRENCY} 批并发）...",
        flush=True,
    )
    _, review_stats = adaptive_map(
        batches,
        assess_one,
        operation="amazon_main_text_review",
        initial_workers=AMAZON_REVIEW_CONCURRENCY,
        min_workers=min(5, AMAZON_REVIEW_CONCURRENCY),
        is_success=lambda value: (
            isinstance(value, list)
            and bool(value)
            and all(assessment_status(item) != "unknown" for item in value)
        ),
        on_result=assess_done,
        terminal_exceptions=(ProviderAuthError, ProviderQuotaError),
        backoff_s=2,
        max_backoff_s=15,
    )
    review_stats["images"] = len(missing)
    review_stats["batch_size"] = batch_size
    concurrency_stats["amazon_main_text_review"] = review_stats


def _main_quality_rank(value: object) -> int:
    if not isinstance(value, dict):
        return 0
    explicit = {
        "preferred": 3,
        "acceptable": 2,
        "fallback": 1,
    }.get(str(value.get("main_image_quality") or "").strip(), 0)
    if explicit:
        return explicit
    evidence = str(value.get("evidence") or "").lower()
    if any(
        token in evidence
        for token in ("white background", "isolated product", "single product")
    ):
        return 3
    if any(
        token in evidence
        for token in ("collage", "lifestyle", "installation scene", "dimension diagram")
    ):
        return 1
    return 2


def run_structured_image_safety_gate(
    data: list[dict[str, Any]],
    cache_path: str | None = None,
    quality_issues: list[str] | None = None,
    progress: Callable[[int, int], None] | None = None,
    runtime_metrics: dict[str, Any] | None = None,
    provider_getter: Callable[[], object] | None = None,
) -> list[dict[str, Any]]:
    """Audit every source image, delete failures, and select source mains."""
    if provider_getter is None:
        raise ValueError("structured image gate requires provider_getter")
    quality_issues = quality_issues if quality_issues is not None else []
    runtime_metrics = runtime_metrics if isinstance(runtime_metrics, dict) else {}
    concurrency_stats = runtime_metrics.setdefault("concurrency", {})
    review_version, main_version = current_cache_versions()
    cache = load_cache(
        cache_path,
        review_version,
        main_version,
    )
    assessments = cache["risk_assessments"]
    main_assessments = cache["main_text_assessments"]
    manual_overrides = load_manual_overrides(cache_path)

    def effective(product_id: str, role: str, url: str) -> dict[str, Any]:
        override = manual_overrides.get((product_id, role, url))
        if override:
            return manual_safe_assessment(override)
        return assessments.get(url) or unknown_image_assessment()

    usage, row_by_id = _index_images(data)
    urls = sorted(usage)
    _review_source_images(
        urls,
        assessments=assessments,
        cache=cache,
        cache_path=cache_path,
        provider_getter=provider_getter,
        concurrency_stats=concurrency_stats,
        progress=progress,
    )
    source_failures = [
        url
        for url, value in assessments.items()
        if assessment_status(value) == "unknown"
        and value.get("operational_failure")
    ]
    if source_failures:
        raise ProviderUnavailableError(
            f"DeepSeek 仍有 {len(source_failures)} 张源图未完成审查",
            provider="deepseek",
            operation="vision",
        )

    candidate_urls: list[str] = []
    for product_id, row in row_by_id.items():
        product_images = [str(row.get("main_img") or "").strip()]
        product_images.extend(
            str(value).strip() for value in row.get("extra_imgs") or []
        )
        for position, url in enumerate(dict.fromkeys(product_images)):
            if not url:
                continue
            role = "main" if position == 0 else "attachment"
            if assessment_status(effective(product_id, role, url)) == "safe":
                candidate_urls.append(url)
    candidate_urls = list(dict.fromkeys(candidate_urls))
    _review_main_candidates(
        candidate_urls,
        assessments=main_assessments,
        cache=cache,
        cache_path=cache_path,
        provider_getter=provider_getter,
        concurrency_stats=concurrency_stats,
    )
    main_failures = [
        url
        for url in candidate_urls
        if assessment_status(main_assessments.get(url)) == "unknown"
        and bool((main_assessments.get(url) or {}).get("operational_failure"))
    ]
    if main_failures:
        raise ProviderUnavailableError(
            f"DeepSeek 仍有 {len(main_failures)} 张主图候选未完成审查",
            provider="deepseek",
            operation="vision",
        )

    rejected: list[dict[str, Any]] = []
    attachment_rejected: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    deleted_images = 0
    deleted_variants = 0
    main_reselected = 0
    original_main_retained = 0

    for product_id, row in row_by_id.items():
        original_main = str(row.get("main_img") or "").strip()
        original_images = [original_main]
        original_images.extend(
            str(value).strip() for value in row.get("extra_imgs") or []
        )
        product_images = list(dict.fromkeys(x for x in original_images if x))
        original_roles = {
            url: ("main" if index == 0 else "attachment")
            for index, url in enumerate(product_images)
        }

        safe_product_images = [
            url
            for url in product_images
            if assessment_status(
                effective(product_id, original_roles[url], url)
            ) == "safe"
        ]
        eligible = [
            url
            for url in safe_product_images
            if assessment_status(main_assessments.get(url)) == "safe"
        ]
        selected_url = min(
            eligible,
            key=lambda url: (
                -_main_quality_rank(main_assessments.get(url)),
                product_images.index(url),
            ),
            default="",
        )
        kept_extras = [url for url in safe_product_images if url != selected_url]
        deletion_reason = ""
        if not selected_url:
            deletion_reason = "missing_eligible_main"
        elif not kept_extras:
            deletion_reason = "missing_product_attachment"

        image_records: list[dict[str, Any]] = []
        for position, url in enumerate(product_images):
            source_role = original_roles[url]
            general = effective(product_id, source_role, url)
            final_role = "main" if url == selected_url else "attachment"
            record = assessment_record(
                url=url,
                role=final_role,
                assessment=general,
                text_assessment=main_assessments.get(url),
                source="source",
            )
            record["original_role"] = source_role
            record["original_position"] = position
            record["main_eligible"] = url in eligible
            if assessment_status(general) != "safe":
                record["image_action"] = "delete_attachment"
                deleted_images += 1
            elif url == selected_url:
                record["image_action"] = "keep"
                record["selection_action"] = (
                    "retain_main" if url == original_main else "promote_to_main"
                )
            else:
                record["image_action"] = "keep"
                record["selection_action"] = (
                    "demote_from_main" if url == original_main else "keep_attachment"
                )
            image_records.append(record)

        kept_variants: list[str] = []
        for position, value in enumerate(row.get("var_imgs") or []):
            url = str(value or "").strip()
            if not url:
                continue
            general = effective(product_id, "variant", url)
            record = assessment_record(
                url=url,
                role="variant",
                assessment=general,
                source="source",
            )
            record["original_position"] = position
            record["main_eligible"] = False
            if assessment_status(general) == "safe":
                kept_variants.append(url)
                record["image_action"] = "keep"
            else:
                deleted_variants += 1
                record["image_action"] = "delete_variant"
            image_records.append(record)

        row["_image_assessments"] = sorted(
            image_records,
            key=lambda item: (
                ROLE_PRIORITY.get(item["role"], 9),
                int(item.get("original_position") or 0),
            ),
        )
        if deletion_reason:
            item = {
                "product_id": product_id,
                "reason": deletion_reason,
                "source_row": dict(row),
                "images": list(row["_image_assessments"]),
            }
            rejected.append(item)
            if deletion_reason == "missing_product_attachment":
                attachment_rejected.append(item)
            continue

        row["main_img"] = selected_url
        row["extra_imgs"] = kept_extras
        row["var_imgs"] = kept_variants
        row["var_img"] = kept_variants[0] if kept_variants else ""
        retained.append(row)
        if selected_url == original_main:
            original_main_retained += 1
        else:
            main_reselected += 1

    if rejected:
        quality_issues.append(
            f"{len(rejected)} 个商品因没有合格主图或产品附图不足被移除"
        )
    status_counts = {
        status: sum(
            1 for value in assessments.values()
            if assessment_status(value) == status
        )
        for status in ("safe", "risk", "unknown")
    }
    main_counts = {
        status: sum(
            1 for value in main_assessments.values()
            if assessment_status(value) == status
        )
        for status in ("safe", "risk", "unknown")
    }
    runtime_metrics["image_rejected_products"] = rejected
    runtime_metrics["attachment_rejected_products"] = attachment_rejected
    runtime_metrics["quarantined_products"] = rejected
    runtime_metrics["image_assessments"] = {
        url: dict(value) for url, value in assessments.items()
    }
    runtime_metrics["main_text_assessments"] = {
        url: dict(value) for url, value in main_assessments.items()
    }
    runtime_metrics["image_safety_gate"] = {
        "processing_mode": "select_existing",
        "source_references": sum(len(items) for items in usage.values()),
        "unique_source_images": len(usage),
        "source_status_counts": status_counts,
        "main_text_status_counts": main_counts,
        "main_candidates_reviewed": len(candidate_urls),
        "main_original_retained": original_main_retained,
        "main_reselected": main_reselected,
        "attachment_deleted": deleted_images,
        "variant_deleted": deleted_variants,
        "rejected_products": len(rejected),
        "retained_products": len(retained),
        "generation_requests": 0,
        "generated_main": 0,
        "generated_variant": 0,
        "generated_attachment": 0,
    }
    runtime_metrics["image_remediation"] = {
        "requested": 0,
        "succeeded": 0,
        "failed": 0,
        "generation_requests": 0,
    }
    save_cache(cache_path, cache)
    if progress:
        progress(1, 1)
    return retained


__all__ = ["run_structured_image_safety_gate"]
