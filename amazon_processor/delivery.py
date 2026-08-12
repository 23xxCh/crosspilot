"""Atomic delivery of the refill JSON and offline final-review package."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import time
from uuid import uuid4
import zipfile

from .quality import (
    attach_audit_to_validation,
    summarize_row_quality_issues,
    validate_amazon_rows,
)
from .review.exporter import export_review
from .runtime import RunContext, RunResult
from .schema import AMAZON_JSON_OUTPUT_FIELDS, write_output_json
from .config.env import get


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = PROJECT_ROOT / "02_处理结果"
RUNTIME_ROOT = PROJECT_ROOT / ".runtime"
LATEST_DIR = OUTPUT_ROOT / "最新"
ARCHIVE_DIR = OUTPUT_ROOT / "归档"
REFILL_NAME = "跨境电商自动化回填表.json"
REVIEW_NAME = "终审包.html"
REVIEW_DATA_NAME = "审核数据.json"
STATUS_NAME = "运行状态.json"
EXCEPTIONS_NAME = "异常商品.json"


def _provider_metrics_from_run(run_metrics: dict | None) -> dict:
    """Normalize pipeline metrics into the public provider metric shape."""
    values = run_metrics if isinstance(run_metrics, dict) else {}
    calls = int(values.get("api_calls") or values.get("calls") or 0)
    errors = int(values.get("api_errors") or values.get("errors") or 0)
    return {
        "api_calls": calls,
        "api_errors": errors,
        "api_success_rate": (
            round(1 - errors / calls, 3) if calls else None
        ),
        "http_attempts": int(values.get("http_attempts") or 0),
        "http_errors": int(values.get("http_errors") or 0),
        "http_retries": int(values.get("http_retries") or 0),
        "circuit_open": int(values.get("circuit_open") or 0),
        "fallback_attempts": int(values.get("fallback_attempts") or 0),
        "fallback_successes": int(values.get("fallback_successes") or 0),
        "fallback_failures": int(values.get("fallback_failures") or 0),
        "http_status": dict(values.get("http_status") or {}),
        "by_operation": dict(values.get("api_by_operation") or {}),
    }


def _combine_provider_metrics(
    pipeline_metrics: dict | None,
    review_metrics: dict | None,
) -> dict:
    """Combine pipeline and review-translation metrics without hiding stages."""
    stages = {
        "pipeline": _provider_metrics_from_run(pipeline_metrics),
        "review_translation": _provider_metrics_from_run(review_metrics),
    }
    totals = {
        key: sum(item[key] for item in stages.values())
        for key in (
            "api_calls",
            "api_errors",
            "http_attempts",
            "http_errors",
            "http_retries",
            "circuit_open",
            "fallback_attempts",
            "fallback_successes",
            "fallback_failures",
        )
    }
    status: dict[str, int] = {}
    for item in stages.values():
        for code, count in item["http_status"].items():
            status[str(code)] = status.get(str(code), 0) + int(count or 0)
    calls = totals["api_calls"]
    return {
        **totals,
        "api_success_rate": (
            round(1 - totals["api_errors"] / calls, 3) if calls else None
        ),
        "http_status": status,
        "by_stage": stages,
    }


def _write_status(
    output_dir: Path,
    *,
    context: RunContext,
    published: bool,
    released_products: int,
    pending_product_ids: list[str] | tuple[str, ...],
    problem_product_ids: list[str],
    run_metrics: dict,
) -> None:
    """Write one machine-readable status manifest next to every review package."""
    source = dict(context.runtime_metrics.get("source") or {})
    input_by_site = (
        context.runtime_metrics.get("marketplaces", {}).get("input_by_site")
        or {}
    )
    payload = {
        "version": 1,
        "run_id": context.request_id,
        "status": (
            "published_with_warnings"
            if published and pending_product_ids
            else "published"
            if published
            else "pending_review"
        ),
        "published": bool(published),
        "source": source,
        "counts": {
            "input_rows": sum(int(value or 0) for value in input_by_site.values()),
            "processed_rows": len(context.data),
            "released_rows": int(released_products),
            "pending_rows": len(tuple(pending_product_ids)),
            "problem_product_ids": len(tuple(problem_product_ids)),
        },
        "pending_product_ids": list(pending_product_ids),
        "isolated_product_ids": list(pending_product_ids),
        "problem_product_ids": list(dict.fromkeys(
            str(value) for value in problem_product_ids if str(value)
        )),
        "output_path": (
            str(LATEST_DIR / REFILL_NAME) if published else None
        ),
        "review_path": str(output_dir / REVIEW_NAME),
        "review_data_path": str(output_dir / REVIEW_DATA_NAME),
        "image_safety": dict(run_metrics.get("image_safety_gate") or {}),
        "provider_metrics": _combine_provider_metrics(
            run_metrics,
            run_metrics.get("review_translation_provider_metrics"),
        ),
        "quality": dict(run_metrics.get("quality") or {}),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    temporary = output_dir / f".{STATUS_NAME}.{os.getpid()}.tmp"
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, output_dir / STATUS_NAME)
    finally:
        temporary.unlink(missing_ok=True)


def _assert_formal_images_are_safe(
    data: list[dict],
    runtime_metrics: dict,
) -> None:
    """Refuse delivery when any retained image lacks a current safe record."""
    if not runtime_metrics.get("image_safety_gate"):
        return
    processing_mode = str(
        (runtime_metrics.get("image_safety_gate") or {}).get(
            "processing_mode"
        )
        or get("IMAGE_PROCESSING_MODE", "select_existing")
    ).strip().lower()
    select_existing = processing_mode == "select_existing"
    human_review_generated = (
        get("GENERATED_IMAGE_REVIEW_MODE", "strict").strip().lower()
        == "human_review"
    )
    violations = []
    for row in data:
        by_role_url = {
            (str(item.get("role") or ""), str(item.get("url") or "")): item
            for item in row.get("_image_assessments") or []
        }
        final_images = [
            ("main", str(row.get("main_img") or "")),
            *[
                ("attachment", str(url or ""))
                for url in row.get("extra_imgs") or []
            ],
            *[
                ("variant", str(url or ""))
                for url in row.get("var_imgs") or []
            ],
        ]
        for role, url in final_images:
            if not url:
                continue
            record = by_role_url.get((role, url)) or {}
            if (
                human_review_generated
                and record.get("source") == "generated"
                and record.get("accepted_without_machine_review") is True
            ):
                continue
            if (
                not select_existing
                and record.get("image_action") == "keep_review"
            ):
                continue
            assessment = record.get("assessment") or {}
            if assessment.get("status") != "safe":
                violations.append(
                    {
                        "product_id": str(row.get("id") or ""),
                        "role": role,
                        "status": assessment.get("status") or "missing",
                    }
                )
                continue
            if select_existing and role == "main":
                text_assessment = record.get("text_assessment") or {}
                if text_assessment.get("status") != "safe":
                    violations.append({
                        "product_id": str(row.get("id") or ""),
                        "role": role,
                        "status": "missing_text_free_safe",
                    })
                if record.get("source") == "generated":
                    violations.append({
                        "product_id": str(row.get("id") or ""),
                        "role": role,
                        "status": "generated_not_allowed",
                    })
    if violations:
        sample = ", ".join(
            f"{item['product_id']}:{item['role']}={item['status']}"
            for item in violations[:5]
        )
        raise ValueError(
            f"图片安全门验收失败：正式表仍有 {len(violations)} 张"
            f"未获 safe / text_free 记录的图片（{sample}）"
        )


def _run_metrics(
    context: RunContext,
    validation: dict,
    problem_product_ids: list[str],
) -> dict:
    if hasattr(context.provider, "metrics_snapshot"):
        context.metrics.set_provider_metrics(
            context.provider.metrics_snapshot()
        )
    context.metrics.set_concurrency_metrics(
        context.runtime_metrics.get("concurrency")
    )
    context.metrics.set_image_remediation_metrics(
        context.runtime_metrics.get("image_remediation")
    )
    context.metrics.set_quality_metrics(validation)
    run_metrics = context.metrics.to_dict()
    run_metrics["image_safety_gate"] = dict(
        context.runtime_metrics.get("image_safety_gate") or {}
    )
    run_metrics["description_rejected_products"] = list(
        context.runtime_metrics.get("description_rejected_products") or []
    )
    run_metrics["problem_product_ids"] = list(dict.fromkeys(
        str(product_id)
        for product_id in problem_product_ids
        if str(product_id)
    ))
    for key in (
        "source",
        "marketplaces",
        "image_deduplication",
        "localization",
        "attachment_rejected_products",
        "pending_main_products",
    ):
        value = context.runtime_metrics.get(key)
        if value is not None:
            run_metrics[key] = value
    return run_metrics


def _deliver_pending(
    context: RunContext,
    *,
    problem_product_ids: list[str],
) -> RunResult:
    pending_ids = tuple(dict.fromkeys(
        str(row.get("id") or "")
        for row in context.data
        if row.get("_main_selection_pending") and str(row.get("id") or "")
    ))
    pending_set = set(pending_ids)
    safe_rows = [
        row for row in context.data
        if str(row.get("id") or "") not in pending_set
    ]
    pending_by_id = {
        str(item.get("product_id") or ""): dict(item)
        for item in context.runtime_metrics.get("pending_main_products") or []
    }
    pending_products = []
    for row in context.data:
        product_id = str(row.get("id") or "")
        if product_id not in pending_set:
            continue
        item = pending_by_id.get(product_id, {})
        reasons = list(item.get("reasons") or [])
        if not reasons:
            reasons = [{
                "code": "missing_clean_main",
                "message": "没有同时通过普通安全审查和主图无文字审查的原图",
            }]
        item.update({
            "product_id": product_id,
            "site": str(row.get("site") or "US"),
            "title": str(row.get("title") or ""),
            "reasons": reasons,
            "images": list(row.get("_image_assessments") or []),
            "source_row": {
                "site": str(row.get("site") or "US"),
                "title": str(row.get("title") or ""),
                "subtitle": str(row.get("subtitle") or ""),
                "description": str(row.get("desc") or ""),
                "bullets": list(row.get("bullets") or []),
                "keywords": str(row.get("keywords") or ""),
            },
        })
        pending_products.append(item)

    context.quality_issues.extend(
        summarize_row_quality_issues(context.data)
    )
    validation = validate_amazon_rows(
        context.data,
        extra_issues=context.quality_issues,
    )
    validation = attach_audit_to_validation(validation, context.data)
    run_metrics = _run_metrics(
        context,
        validation,
        problem_product_ids,
    )

    stamp = time.strftime("%Y%m%d_%H%M%S")
    pending_root = OUTPUT_ROOT / "待人工审核"
    output_dir = pending_root / f"待定_{stamp}"
    suffix = 1
    while output_dir.exists():
        output_dir = pending_root / f"待定_{stamp}_{suffix:02d}"
        suffix += 1
    output_dir.mkdir(parents=True)
    staging = RUNTIME_ROOT / "staging" / f"{context.request_id}_pending"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    temporary_output = staging / REFILL_NAME
    try:
        if safe_rows:
            write_output_json(
                safe_rows,
                temporary_output,
                problem_product_ids=problem_product_ids,
            )
        else:
            empty_payload = {
                field: [] for field in AMAZON_JSON_OUTPUT_FIELDS
            }
            empty_payload["有问题的产品id"] = list(dict.fromkeys(
                str(product_id)
                for product_id in problem_product_ids
                if str(product_id)
            ))
            temporary_output.write_text(
                json.dumps(empty_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        audit_by_product = {
            str(row.get("id") or ""): list(
                row.get("_image_assessments") or []
            )
            for row in safe_rows
        }
        export_review(
            temporary_output,
            output_dir,
            audit_by_product=audit_by_product,
            quarantine_products=pending_products,
            shared_cache_dir=RUNTIME_ROOT / "cache" / "images",
            translation_cache_path=(
                RUNTIME_ROOT / "cache" / "review_translation.json"
            ),
            run_id=context.request_id,
            run_metrics=run_metrics,
            allow_empty_released=True,
        )
        (output_dir / "待定商品.json").write_text(
            json.dumps(pending_products, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _write_status(
            output_dir,
            context=context,
            published=False,
            released_products=len(safe_rows),
            pending_product_ids=pending_ids,
            problem_product_ids=problem_product_ids,
            run_metrics=run_metrics,
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return RunResult(
        output_path=None,
        review_path=output_dir / REVIEW_NAME,
        review_data_path=output_dir / REVIEW_DATA_NAME,
        archived_path=None,
        retained_products=len(safe_rows),
        quarantined_products=0,
        elapsed_s=time.time() - context.started_at,
        published=False,
        pending_product_ids=pending_ids,
        isolated_product_ids=pending_ids,
        exception_path=output_dir / "待定商品.json",
    )


def _partition_unattended_rows(
    rows: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Keep releasable rows and explain deterministic per-row rejections."""
    released: list[dict] = []
    rejected: list[dict] = []
    for row in rows:
        reasons: list[dict[str, str]] = []
        localization_failures = list(
            row.get("_localization_failure_reasons") or []
        )
        if localization_failures:
            reasons.append({
                "code": "localization_validation_failed",
                "message": "多站点文案未通过自动修复："
                + ", ".join(str(value) for value in localization_failures[:5]),
            })
        if row.get("_main_selection_pending"):
            reasons.append({
                "code": "missing_clean_main",
                "message": "没有可自动放行的安全主图",
            })
        validation = validate_amazon_rows([row])
        if not validation.get("passed"):
            reasons.append({
                "code": "formal_row_validation_failed",
                "message": "；".join(validation.get("issues") or [])[:500],
            })
        if not reasons:
            released.append(row)
            continue
        rejected.append({
            "product_id": str(row.get("id") or ""),
            "site": str(row.get("site") or "US"),
            "title": str(row.get("title") or ""),
            "reasons": reasons,
            "images": list(row.get("_image_assessments") or []),
            "source_row": {
                "site": str(row.get("site") or "US"),
                "title": str(row.get("title") or ""),
                "subtitle": str(row.get("subtitle") or ""),
                "description": str(row.get("desc") or ""),
                "bullets": list(row.get("bullets") or []),
                "keywords": str(row.get("keywords") or ""),
            },
        })
    return released, rejected


def _archive_latest() -> Path | None:
    """Compress the previous formal artifacts, excluding shared image copies."""
    if not LATEST_DIR.is_dir():
        return None
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    archive = ARCHIVE_DIR / f"Amazon处理结果_{stamp}.zip"
    suffix = 1
    while archive.exists():
        archive = ARCHIVE_DIR / f"Amazon处理结果_{stamp}_{suffix:02d}.zip"
        suffix += 1
    with zipfile.ZipFile(
        archive,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as bundle:
        for path in LATEST_DIR.rglob("*"):
            if not path.is_file() or "图片" in path.relative_to(LATEST_DIR).parts:
                continue
            bundle.write(path, path.relative_to(LATEST_DIR))
    if not zipfile.is_zipfile(archive):
        raise RuntimeError(f"历史输出归档校验失败: {archive}")
    return archive


def _publish(staging: Path) -> Path | None:
    """Publish a fully validated staging directory with rollback."""
    archived = _archive_latest()
    previous = RUNTIME_ROOT / "previous_latest"
    if previous.exists():
        shutil.rmtree(previous)
    if LATEST_DIR.exists():
        try:
            os.replace(LATEST_DIR, previous)
        except PermissionError:
            _publish_open_latest_files(staging)
            return archived
    try:
        LATEST_DIR.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, LATEST_DIR)
    except Exception:
        if not LATEST_DIR.exists() and previous.exists():
            os.replace(previous, LATEST_DIR)
        raise
    finally:
        if previous.exists():
            shutil.rmtree(previous)
    return archived


def _publish_open_latest_files(staging: Path) -> None:
    """Replace published files individually when Windows keeps latest open."""
    for optional_name in (EXCEPTIONS_NAME,):
        if not (staging / optional_name).exists():
            (LATEST_DIR / optional_name).unlink(missing_ok=True)
    for source in staging.rglob("*"):
        if not source.is_file():
            continue
        target = LATEST_DIR / source.relative_to(staging)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
    shutil.rmtree(staging)


def deliver(
    context: RunContext,
    *,
    problem_product_ids: list[str],
) -> RunResult:
    """Build all formal artifacts in staging, validate, then publish once."""
    unattended_pending_ids: tuple[str, ...] = ()
    if context.runtime_metrics.get("unattended"):
        original_rows = context.data
        released_rows, rejected_products = _partition_unattended_rows(
            original_rows
        )
        if rejected_products:
            unattended_pending_ids = tuple(dict.fromkeys(
                str(item.get("product_id") or "")
                for item in rejected_products
                if str(item.get("product_id") or "")
            ))
            context.runtime_metrics["unattended_delivery"] = {
                "input_rows": len(original_rows),
                "released_rows": len(released_rows),
                "isolated_rows": len(rejected_products),
                "isolated_product_ids": list(unattended_pending_ids),
            }
            existing_quarantine = list(
                context.runtime_metrics.get("quarantined_products") or []
            )
            context.runtime_metrics["quarantined_products"] = [
                *existing_quarantine,
                *rejected_products,
            ]
            if not released_rows:
                for row in original_rows:
                    row["_main_selection_pending"] = True
                context.runtime_metrics["pending_main_products"] = (
                    rejected_products
                )
                return _deliver_pending(
                    context,
                    problem_product_ids=problem_product_ids,
                )
            context.data = released_rows
    if any(row.get("_main_selection_pending") for row in context.data):
        return _deliver_pending(
            context,
            problem_product_ids=problem_product_ids,
        )
    _assert_formal_images_are_safe(context.data, context.runtime_metrics)
    context.quality_issues.extend(
        summarize_row_quality_issues(context.data)
    )
    validation = validate_amazon_rows(
        context.data,
        extra_issues=context.quality_issues,
    )
    validation = attach_audit_to_validation(validation, context.data)

    staging = RUNTIME_ROOT / "staging" / context.request_id
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    output = staging / REFILL_NAME
    write_output_json(
        context.data,
        output,
        problem_product_ids=problem_product_ids,
    )

    run_metrics = _run_metrics(context, validation, problem_product_ids)

    audit_by_product = {
        str(row.get("id") or ""): list(
            row.get("_image_assessments") or []
        )
        for row in context.data
    }
    quarantine_products = list(
        context.runtime_metrics.get("quarantined_products") or []
    )
    if quarantine_products:
        (staging / EXCEPTIONS_NAME).write_text(
            json.dumps(
                {
                    "version": 1,
                    "run_id": context.request_id,
                    "items": quarantine_products,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    export_review(
        output,
        staging,
        audit_by_product=audit_by_product,
        quarantine_products=quarantine_products,
        shared_cache_dir=RUNTIME_ROOT / "cache" / "images",
        translation_cache_path=(
            RUNTIME_ROOT / "cache" / "review_translation.json"
        ),
        run_id=context.request_id,
        run_metrics=run_metrics,
    )
    _write_status(
        staging,
        context=context,
        published=True,
        released_products=len(context.data),
        pending_product_ids=unattended_pending_ids,
        problem_product_ids=problem_product_ids,
        run_metrics=run_metrics,
    )
    archived = _publish(staging)
    return RunResult(
        output_path=LATEST_DIR / REFILL_NAME,
        review_path=LATEST_DIR / REVIEW_NAME,
        review_data_path=LATEST_DIR / REVIEW_DATA_NAME,
        archived_path=archived,
        retained_products=len(context.data),
        quarantined_products=len(quarantine_products),
        elapsed_s=time.time() - context.started_at,
        pending_product_ids=unattended_pending_ids,
        isolated_product_ids=unattended_pending_ids,
        exception_path=(
            LATEST_DIR / EXCEPTIONS_NAME
            if unattended_pending_ids
            else None
        ),
    )


def load_review_data(path: str | os.PathLike[str]) -> dict:
    """Load the single machine-readable review artifact."""
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


__all__ = [
    "ARCHIVE_DIR",
    "LATEST_DIR",
    "OUTPUT_ROOT",
    "REFILL_NAME",
    "REVIEW_DATA_NAME",
    "REVIEW_NAME",
    "STATUS_NAME",
    "deliver",
    "load_review_data",
]
