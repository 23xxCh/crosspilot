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
from .schema import write_output_json
from .config.env import get


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = PROJECT_ROOT / "02_处理结果"
RUNTIME_ROOT = PROJECT_ROOT / ".runtime"
LATEST_DIR = OUTPUT_ROOT / "最新"
ARCHIVE_DIR = OUTPUT_ROOT / "归档"
REFILL_NAME = "跨境电商自动化回填表.json"
REVIEW_NAME = "终审包.html"
REVIEW_DATA_NAME = "审核数据.json"


def _assert_formal_images_are_safe(
    data: list[dict],
    runtime_metrics: dict,
) -> None:
    """Refuse delivery when any retained image lacks a current safe record."""
    if not runtime_metrics.get("image_safety_gate"):
        return
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
            if record.get("image_action") == "keep_review":
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
    if violations:
        sample = ", ".join(
            f"{item['product_id']}:{item['role']}={item['status']}"
            for item in violations[:5]
        )
        raise ValueError(
            f"图片安全门验收失败：正式表仍有 {len(violations)} 张"
            f"未获 safe 记录的图片（{sample}）"
        )


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
    run_metrics["problem_product_ids"] = list(
        dict.fromkeys(
            str(product_id)
            for product_id in problem_product_ids
            if str(product_id)
        )
    )

    audit_by_product = {
        str(row.get("id") or ""): list(
            row.get("_image_assessments") or []
        )
        for row in context.data
    }
    quarantine_products = list(
        context.runtime_metrics.get("quarantined_products") or []
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
    archived = _publish(staging)
    return RunResult(
        output_path=LATEST_DIR / REFILL_NAME,
        review_path=LATEST_DIR / REVIEW_NAME,
        review_data_path=LATEST_DIR / REVIEW_DATA_NAME,
        archived_path=archived,
        retained_products=len(context.data),
        quarantined_products=len(quarantine_products),
        elapsed_s=time.time() - context.started_at,
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
    "deliver",
    "load_review_data",
]
