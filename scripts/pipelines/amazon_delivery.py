"""Amazon formal-output validation, metrics, and review-package delivery."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

from scripts.pipeline_log import log as _log
from scripts.pipelines.amazon_quality import (
    attach_audit_to_validation as _attach_audit_to_validation,
    summarize_row_quality_issues as _summarize_row_quality_issues,
    validate_amazon_rows as _validate_amazon_rows,
)
from scripts.pipelines.amazon_io import (
    _stage_write_output,
    _validate_amazon_output,
)
from scripts.pipelines.amazon_runtime import AmazonRunContext


def _atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        temp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _review_root_for_output(output: str) -> Path:
    configured = str(
        os.environ.get("CROSSPILOT_AMAZON_REVIEW_ROOT") or ""
    ).strip()
    if configured:
        return Path(configured).resolve()
    output_path = Path(output).resolve()
    for parent in output_path.parents:
        if parent.name == "亚马逊表":
            return parent / "检查图片文字"
    return output_path.parent / "检查图片文字"


def _write_latest_review_entry(
    review_root: Path,
    run_dir: Path,
) -> Path:
    relative = (
        run_dir.relative_to(review_root)
        / "中文文案检查表.html"
    ).as_posix()
    latest = review_root / "最新终审包.html"
    latest.write_text(
        '<!doctype html><meta charset="utf-8">'
        f'<meta http-equiv="refresh" content="0;url={relative}">'
        "<title>最新 Amazon 终审包</title>"
        f'<p><a href="{relative}">打开最新 Amazon 终审包</a></p>',
        encoding="utf-8",
    )
    return latest


def _create_review_package(
    output: str,
    data: list[dict],
    runtime_metrics: dict,
    *,
    run_id: str,
) -> dict:
    if not output.lower().endswith(".json"):
        return {
            "status": "skipped",
            "reason": "automatic review package currently requires JSON",
        }
    from scripts.review_package import (
        export_review,
        prepare_shared_review_cache,
    )

    review_root = _review_root_for_output(output)
    review_root.mkdir(parents=True, exist_ok=True)
    prepare_shared_review_cache(review_root)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = review_root / f"运行_{stamp}"
    suffix = 1
    while run_dir.exists():
        run_dir = review_root / f"运行_{stamp}_{suffix:02d}"
        suffix += 1
    run_dir.mkdir(parents=True)
    audit_by_product = {
        str(row.get("id") or ""): list(
            row.get("_image_assessments") or []
        )
        for row in data
    }
    quarantine_products = list(
        runtime_metrics.get("quarantined_products") or []
    )
    summary = export_review(
        output,
        run_dir,
        audit_by_product=audit_by_product,
        quarantine_products=quarantine_products,
        shared_cache_dir=review_root / ".共享图片缓存",
        translation_cache_path=(
            review_root / ".共享缓存" / "中文翻译缓存.json"
        ),
        run_id=run_id,
    )
    latest = _write_latest_review_entry(review_root, run_dir)
    _atomic_write_json(review_root / "最新终审包.json", {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "html": str(run_dir / "中文文案检查表.html"),
        "summary": summary,
    })
    return {
        "status": "created",
        "run_dir": str(run_dir),
        "html": str(run_dir / "中文文案检查表.html"),
        "latest_html": str(latest),
        "summary": summary,
    }


def _assert_formal_images_are_safe(
    data: list[dict],
    runtime_metrics: dict,
) -> None:
    if not runtime_metrics.get("image_safety_gate"):
        return
    violations = []
    for row in data:
        records = row.get("_image_assessments") or []
        by_role_url = {
            (str(item.get("role") or ""), str(item.get("url") or "")): (
                item.get("assessment") or {}
            )
            for item in records
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
            assessment = by_role_url.get((role, url)) or {}
            if assessment.get("status") != "safe":
                violations.append({
                    "product_id": str(row.get("id") or ""),
                    "role": role,
                    "url": url,
                    "status": (
                        assessment.get("status") or "missing"
                    ),
                })
    if violations:
        sample = ", ".join(
            f"{item['product_id']}:{item['role']}={item['status']}"
            for item in violations[:5]
        )
        raise ValueError(
            f"图片安全门验收失败：正式表仍有 {len(violations)} 张"
            f"未获 safe 记录的图片（{sample}）"
        )


def deliver_amazon_output(context: AmazonRunContext) -> str:
    """Write and validate all formal artifacts for one completed run."""
    data = context.data
    runtime_metrics = context.runtime_metrics
    _assert_formal_images_are_safe(data, runtime_metrics)
    context.quality_issues.extend(
        _summarize_row_quality_issues(data)
    )
    output = context.execute(
        "写回填表",
        _stage_write_output,
        data,
        context.source_path,
    )
    if output.lower().endswith(".xlsx"):
        validation = _validate_amazon_output(
            output,
            len(data),
            extra_issues=context.quality_issues,
        )
    else:
        validation = _validate_amazon_rows(
            data,
            extra_issues=context.quality_issues,
        )
    validation = _attach_audit_to_validation(validation, data)

    quarantine_path = Path(
        os.path.splitext(output)[0] + "_隔离清单.json"
    )
    _atomic_write_json(quarantine_path, {
        "version": 1,
        "run_id": context.request_id,
        "policy_version": (
            runtime_metrics.get("image_safety_gate") or {}
        ).get("policy_version", ""),
        "source": str(Path(context.source_path).resolve()),
        "formal_output": str(Path(output).resolve()),
        "products": list(
            runtime_metrics.get("quarantined_products") or []
        ),
    })

    if hasattr(context.provider, "metrics_snapshot"):
        context.metrics.set_provider_metrics(
            context.provider.metrics_snapshot()
        )
    context.metrics.set_concurrency_metrics(
        runtime_metrics.get("concurrency")
    )
    context.metrics.set_image_remediation_metrics(
        runtime_metrics.get("image_remediation")
    )
    context.metrics.set_quality_metrics(validation)
    metrics_data = context.metrics.to_dict()
    metrics_data["image_safety_gate"] = dict(
        runtime_metrics.get("image_safety_gate") or {}
    )
    metrics_data["quarantine_manifest"] = str(quarantine_path)

    try:
        review_package = _create_review_package(
            output,
            data,
            runtime_metrics,
            run_id=context.request_id,
        )
    except Exception as exc:
        _log.error(
            "自动终审包生成失败",
            error=str(exc),
            exc_info=True,
        )
        review_package = {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
        validation.setdefault("issues", []).append(
            f"自动终审包生成失败: {type(exc).__name__}: {exc}"
        )
        validation["passed"] = False
        context.metrics.set_quality_metrics(validation)
        metrics_data["quality"] = (
            context.metrics.to_dict().get("quality", {})
        )
    metrics_data["review_package"] = review_package
    context.status.finish(output, validation, metrics_data)

    try:
        metrics_path = Path(
            os.path.splitext(output)[0] + "_metrics.json"
        )
        _atomic_write_json(metrics_path, metrics_data)
    except Exception:
        pass
    print(f"输出: {output}")
    return output


__all__ = [
    "_assert_formal_images_are_safe",
    "_atomic_write_json",
    "_create_review_package",
    "_review_root_for_output",
    "_write_latest_review_entry",
    "deliver_amazon_output",
]
