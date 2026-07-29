#!/usr/bin/env python3
"""Read-only structured image safety audit for an Amazon JSON delivery."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from typing import Any

if __package__ in {None, ""}:
    from _bootstrap import ensure_package_imports
    ensure_package_imports()

from scripts.concurrency import adaptive_map
from scripts.review_package import (
    export_review,
    prepare_shared_review_cache,
)
from scripts.model_provider import (
    ProviderQuotaError,
    get_provider,
    reload_provider,
)
from scripts.pipelines.amazon_quality import AMAZON_REVIEW_CONCURRENCY
from scripts.pipelines.amazon_image_safety import (
    is_current_assessment,
    load_cache,
    safe_assess,
    save_cache,
    current_cache_versions,
)
from scripts.process_amazon import (
    _review_root_for_output,
    _write_latest_review_entry,
)
from scripts.services.amazon_json import (
    AMAZON_JSON_INPUT_FIELDS,
    load_columnar_json,
)
from crosspilot.image_risk import (
    assessment_is_intrinsic_brand,
    assessment_status,
    load_confirmed_image_quarantine,
    unknown_image_assessment,
)


def _atomic_json(path: Path, value: dict) -> None:
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


def _selected_indices(
    payload: dict,
    product_ids: set[str] | None,
) -> list[int]:
    if not product_ids:
        return list(range(len(payload["商品id"])))
    index_by_id = {
        str(product_id): index
        for index, product_id in enumerate(payload["商品id"])
    }
    missing = sorted(product_ids - set(index_by_id))
    if missing:
        raise ValueError(
            "输入 JSON 中找不到商品 ID: " + ", ".join(missing)
        )
    return [index_by_id[product_id] for product_id in product_ids]


def _image_usages(payload: dict, indices: list[int]) -> dict[str, list[dict]]:
    usage: dict[str, list[dict]] = defaultdict(list)
    for index in indices:
        product_id = str(payload["商品id"][index])
        product_images = payload["产品图片链接"][index]
        for position, url in enumerate(product_images):
            usage[url].append({
                "product_id": product_id,
                "row": index + 1,
                "role": "main" if position == 0 else "attachment",
                "position": position,
            })
        for position, url in enumerate(
            payload["变种图片链接"][index]
        ):
            usage[url].append({
                "product_id": product_id,
                "row": index + 1,
                "role": "variant",
                "position": position,
            })
    return usage


def audit_payload(
    payload: dict,
    *,
    cache_path: Path,
    product_ids: set[str] | None = None,
) -> dict[str, Any]:
    indices = _selected_indices(payload, product_ids)
    usage = _image_usages(payload, indices)
    review_version, generation_version = current_cache_versions()
    cache = load_cache(
        str(cache_path),
        review_version,
        generation_version,
    )
    assessments = cache["risk_assessments"]
    confirmations = cache["risk_confirmations"]
    missing = [
        url for url in sorted(usage)
        if not is_current_assessment(assessments.get(url))
    ]

    def assess(url: str) -> dict:
        return safe_assess(get_provider(), url)

    def assessed(url: str, result: object) -> None:
        if isinstance(result, Exception) or not isinstance(result, dict):
            result = unknown_image_assessment("audit worker failed")
        assessments[url] = result
        save_cache(str(cache_path), cache)

    if missing:
        print(
            f"只读结构化审图：{len(missing)} 张待审，"
            f"{len(usage) - len(missing)} 张缓存命中",
            flush=True,
        )
        adaptive_map(
            missing,
            assess,
            operation="amazon_readonly_image_audit",
            initial_workers=AMAZON_REVIEW_CONCURRENCY,
            min_workers=2,
            is_success=lambda value: (
                isinstance(value, dict)
                and assessment_status(value) != "unknown"
            ),
            on_result=assessed,
            terminal_exceptions=(ProviderQuotaError,),
            backoff_s=2,
            max_backoff_s=15,
        )
    else:
        print(
            f"只读结构化审图全部缓存命中：{len(usage)} 张",
            flush=True,
        )

    intrinsic = [
        url for url in usage
        if assessment_is_intrinsic_brand(assessments.get(url))
    ]
    confirm_missing = [
        url for url in intrinsic
        if not is_current_assessment(confirmations.get(url))
    ]

    def confirm(url: str) -> dict:
        return safe_assess(
            get_provider(),
            url,
            confirmation=True,
        )

    def confirmed(url: str, result: object) -> None:
        if isinstance(result, Exception) or not isinstance(result, dict):
            result = unknown_image_assessment(
                "confirmation audit worker failed"
            )
        confirmations[url] = result
        save_cache(str(cache_path), cache)

    if confirm_missing:
        adaptive_map(
            confirm_missing,
            confirm,
            operation="amazon_readonly_high_risk_confirmation",
            initial_workers=min(
                AMAZON_REVIEW_CONCURRENCY,
                max(1, len(confirm_missing)),
            ),
            min_workers=1,
            is_success=lambda value: (
                isinstance(value, dict)
                and assessment_status(value) != "unknown"
            ),
            on_result=confirmed,
            terminal_exceptions=(ProviderQuotaError,),
            backoff_s=2,
            max_backoff_s=15,
        )

    audit_by_product: dict[str, list[dict]] = defaultdict(list)
    findings_by_product: dict[str, list[dict]] = defaultdict(list)
    confirmed_quarantine = load_confirmed_image_quarantine()
    for url, uses in usage.items():
        first = assessments.get(url) or unknown_image_assessment()
        status = assessment_status(first)
        second = confirmations.get(url)
        for use in uses:
            product_id = use["product_id"]
            audit_by_product[product_id].append({
                "url": url,
                "role": use["role"],
                "source": "source",
                "source_url": "",
                "assessment": first,
                "decision": "",
                "evidence": first.get("evidence", ""),
            })
            action = ""
            code = ""
            if use["role"] == "attachment" and status != "safe":
                action = "delete_attachment"
                code = f"{status}_attachment"
            elif use["role"] in {"main", "variant"}:
                if status == "unknown":
                    action = "quarantine_product"
                    code = f"unknown_{use['role']}"
                elif status == "risk":
                    if assessment_is_intrinsic_brand(first):
                        action = "quarantine_product"
                        code = (
                            "intrinsic_brand_confirmed"
                            if assessment_is_intrinsic_brand(second)
                            else "high_risk_confirmation_conflict"
                        )
                    else:
                        action = f"regenerate_{use['role']}"
                        code = f"risk_{use['role']}"
            if code:
                findings_by_product[product_id].append({
                    "code": code,
                    "suggested_action": action,
                    "role": use["role"],
                    "url": url,
                    "assessment": first,
                    "confirmation": second,
                })
    selected_ids = {
        str(payload["商品id"][index]) for index in indices
    }
    for product_id, block in confirmed_quarantine.items():
        if product_id not in selected_ids:
            continue
        findings_by_product[product_id].append({
            "code": "human_confirmed_image_risk",
            "suggested_action": "quarantine_product",
            "role": "product",
            "url": "",
            "assessment": {
                "status": "risk",
                "risk_categories": ["brand_logo"],
                "placement": "unknown",
                "confidence": 1.0,
                "evidence": (
                    block.get("reason")
                    or "历史人工终审确认"
                ),
                "manual_confirmation": True,
            },
            "confirmation": None,
        })

    status_counts = {
        status: sum(
            assessment_status(assessments.get(url)) == status
            for url in usage
        )
        for status in ("safe", "risk", "unknown")
    }
    provider = get_provider()
    provider_metrics = (
        provider.metrics_snapshot()
        if hasattr(provider, "metrics_snapshot") else {}
    )
    result = {
        "version": 1,
        "policy_version": cache["image_risk_policy_version"],
        "audited_at": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "products": len(indices),
            "unique_images": len(usage),
            **status_counts,
            "products_with_findings": len(findings_by_product),
            "high_risk_confirmation_count": len(intrinsic),
            "human_confirmed_quarantine": sum(
                product_id in selected_ids
                for product_id in confirmed_quarantine
            ),
        },
        "products": [
            {
                "product_id": product_id,
                "findings": findings,
            }
            for product_id, findings in findings_by_product.items()
        ],
        "audit_by_product": dict(audit_by_product),
        "provider_metrics": provider_metrics,
    }
    save_cache(str(cache_path), cache)
    return result


def audit_file(
    input_path: str | Path,
    *,
    output_root: str | Path | None = None,
    cache_path: str | Path | None = None,
    product_ids: set[str] | None = None,
    create_package: bool = True,
) -> dict:
    input_path = Path(input_path).resolve()
    payload = load_columnar_json(input_path)
    review_root = (
        Path(output_root).resolve()
        if output_root else _review_root_for_output(str(input_path))
    )
    review_root.mkdir(parents=True, exist_ok=True)
    prepare_shared_review_cache(review_root)
    cache = (
        Path(cache_path).resolve()
        if cache_path
        else review_root / ".审图缓存" / "结构化图片风险.json"
    )
    reload_provider()
    report = audit_payload(
        payload,
        cache_path=cache,
        product_ids=product_ids,
    )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = review_root / f"运行_{stamp}_只读审计"
    suffix = 1
    while run_dir.exists():
        run_dir = review_root / (
            f"运行_{stamp}_只读审计_{suffix:02d}"
        )
        suffix += 1
    run_dir.mkdir(parents=True)
    report["source"] = str(input_path)
    report["run_dir"] = str(run_dir)
    report_path = run_dir / "只读图片安全审计报告.json"
    _atomic_json(report_path, report)
    package_summary = None
    if create_package:
        if not all(
            field in payload
            for field in (
                "Bullet Point1",
                "Bullet Point2",
                "Bullet Point3",
                "Bullet Point4",
                "Bullet Point5",
                "关键词信息",
                "有问题的产品id",
            )
        ):
            raise ValueError(
                "创建终审包需要完整 12 字段回填表；"
                "采集表请使用 --no-package"
            )
        package_summary = export_review(
            input_path,
            run_dir,
            audit_by_product=report["audit_by_product"],
            quarantine_products=[],
            shared_cache_dir=review_root / ".共享图片缓存",
            translation_cache_path=(
                review_root / ".共享缓存" / "中文翻译缓存.json"
            ),
            run_id=f"readonly-{stamp}",
        )
        _write_latest_review_entry(review_root, run_dir)
    report["report_path"] = str(report_path)
    report["package_summary"] = package_summary
    return report


def main(argv: list[str] | None = None) -> int:
    """Compatibility adapter for the unified CrossPilot CLI."""
    from crosspilot.cli import main as cli_main

    arguments = list(sys.argv[1:] if argv is None else argv)
    return cli_main(["audit", *arguments])


if __name__ == "__main__":
    raise SystemExit(main())
