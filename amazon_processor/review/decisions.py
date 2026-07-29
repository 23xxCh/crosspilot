#!/usr/bin/env python3
"""Validate and apply decisions exported by an Amazon final-review package."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import shutil
from typing import Any

from ..images.risk import validate_image_url
from ..providers import get_provider, reload_provider
from ..schema import (
    AMAZON_JSON_OUTPUT_FIELDS,
    validate_columnar_payload,
)
from .html import render_html
from .exporter import _load_json


ALLOWED_ACTIONS = {
    "approve_product",
    "delete_product",
    "delete_image",
    "regenerate_image",
    "false_positive",
}


def _atomic_json(path: Path, value: dict) -> None:
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


def _backup(
    formal_path: Path,
    review_package: Path | None,
) -> dict[str, str]:
    del review_package
    from ..delivery import LATEST_DIR, _archive_latest

    if formal_path.parent.resolve() == LATEST_DIR.resolve():
        archive = _archive_latest()
        return {"archive": str(archive or "")}
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = formal_path.with_name(
        f"{formal_path.stem}.backup_{stamp}{formal_path.suffix}"
    )
    shutil.copy2(formal_path, backup)
    return {"formal_json": str(backup)}


def _central_override_path(formal_path: Path) -> Path:
    from ..delivery import LATEST_DIR, RUNTIME_ROOT

    if formal_path.parent.resolve() == LATEST_DIR.resolve():
        root = RUNTIME_ROOT
    else:
        root = formal_path.parent / ".runtime"
    return root / "cache" / "review_overrides.json"


def _merge_overrides(path: Path, overrides: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = (
        _load_json(path)
        if path.exists() else {"version": 1, "overrides": []}
    )
    merged = {
        (
            str(item.get("product_id") or ""),
            str(item.get("role") or ""),
            str(item.get("image_url") or ""),
        ): item
        for item in existing.get("overrides") or []
    }
    for item in overrides:
        merged[(
            item["product_id"],
            item["role"],
            item["image_url"],
        )] = item
    existing["overrides"] = list(merged.values())
    _atomic_json(path, existing)


def _row_index(payload: dict, product_id: str) -> int:
    try:
        return [
            str(value) for value in payload["商品id"]
        ].index(str(product_id))
    except ValueError as exc:
        raise ValueError(
            f"审核决定引用了不存在的商品 ID: {product_id}"
        ) from exc


def _remove_row(payload: dict, index: int) -> None:
    removed_id = str(payload["商品id"][index])
    for field in AMAZON_JSON_OUTPUT_FIELDS:
        if field == "有问题的产品id":
            continue
        del payload[field][index]
    problem_ids = [
        str(value) for value in payload["有问题的产品id"]
    ]
    if removed_id not in problem_ids:
        problem_ids.append(removed_id)
    payload["有问题的产品id"] = problem_ids


def _regenerate_safe_image(
    source_url: str,
    *,
    role: str,
    routes: int = 3,
) -> tuple[str, dict]:
    provider = get_provider()
    last_reason = "generation_failed"
    for route_offset in range(max(1, min(3, routes))):
        generated = str(
            provider.call_image_gen(
                source_url,
                is_variant=role == "variant",
                context="",
                route_offset=route_offset,
            )
            or ""
        )
        valid, reason = validate_image_url(generated)
        if not valid:
            last_reason = reason
            continue
        assessment = provider.assess_image(generated)
        if (
            isinstance(assessment, dict)
            and assessment.get("status") == "safe"
        ):
            return generated, assessment
        last_reason = (
            "generated_image_"
            + str(
                (assessment or {}).get("status")
                if isinstance(assessment, dict)
                else "unknown"
            )
        )
    raise ValueError(
        f"图片重新生成后未通过安全复审: {last_reason}"
    )


def _validate_decisions(value: dict) -> list[dict]:
    if not isinstance(value, dict):
        raise ValueError("审核决定 JSON 顶层必须是对象")
    decisions = value.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("审核决定 JSON 缺少 decisions 数组")
    normalized = []
    for index, item in enumerate(decisions, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 条审核决定必须是对象")
        action = str(item.get("action") or "")
        product_id = str(item.get("product_id") or "")
        role = str(item.get("role") or "product")
        image_url = str(item.get("image_url") or "")
        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"第 {index} 条决定动作无效: {action}")
        if not product_id:
            raise ValueError(f"第 {index} 条决定缺少 product_id")
        if action == "delete_image" and role != "attachment":
            raise ValueError(
                "主图/变种图不能直接删除；请使用 regenerate_image "
                "或删除整个商品"
            )
        if action == "regenerate_image" and role not in {
            "main",
            "variant",
        }:
            raise ValueError(
                "regenerate_image 只能用于主图或变种图"
            )
        if action in {"delete_image", "regenerate_image"} and not image_url:
            raise ValueError(
                f"第 {index} 条图片决定缺少 image_url"
            )
        normalized.append({
            "product_id": product_id,
            "action": action,
            "role": role,
            "image_url": image_url,
            "note": str(item.get("note") or ""),
            "recorded_at": str(item.get("recorded_at") or ""),
        })
    return normalized


def _update_review_package(
    package_dir: Path,
    decisions: list[dict],
    *,
    application_report: dict,
) -> None:
    data_path = package_dir / "审核数据.json"
    if not data_path.exists():
        return
    review_data = _load_json(data_path)
    products = review_data.get("products") or []
    product_map = {
        str(item.get("product_id") or ""): item
        for item in products
    }
    deleted_products = {
        item["product_id"]
        for item in decisions
        if item["action"] == "delete_product"
    }
    for decision in decisions:
        product = product_map.get(decision["product_id"])
        if not product:
            continue
        if decision["role"] == "product":
            product["decision"] = decision
            continue
        for image in product.get("images") or []:
            if (
                image.get("role_key") == decision["role"]
                and image.get("url") == decision["image_url"]
            ):
                image["decision"] = decision
                if decision["action"] == "delete_image":
                    image["applied"] = "deleted"
                break
    review_data["applied_decisions"] = decisions
    review_data["products"] = [
        item
        for item in products
        if str(item.get("product_id") or "")
        not in deleted_products
    ]
    summary = review_data.get("summary") or {}
    summary["products"] = len(review_data["products"])
    review_data["summary"] = summary
    review_data["application_report"] = application_report
    _atomic_json(data_path, review_data)
    (package_dir / "终审包.html").write_text(
        render_html(review_data["products"], summary),
        encoding="utf-8",
    )


def apply_decisions(
    formal_json: str | Path,
    decisions_json: str | Path,
    *,
    review_package: str | Path | None = None,
    dry_run: bool = False,
) -> dict:
    formal_path = Path(formal_json).resolve()
    decisions_path = Path(decisions_json).resolve()
    package_dir = (
        Path(review_package).resolve()
        if review_package else None
    )
    payload = _load_json(formal_path)
    validate_columnar_payload(
        payload,
        required_fields=AMAZON_JSON_OUTPUT_FIELDS,
    )
    decision_envelope = _load_json(decisions_path)
    decisions = _validate_decisions(decision_envelope)
    planned = []
    false_positive_overrides = []
    regeneration_results: list[dict[str, Any]] = []

    # Validate targets and do costly generation before any write or backup.
    formal_ids = {
        str(value) for value in payload["商品id"]
    }
    delete_product_ids = {
        item["product_id"]
        for item in decisions
        if item["action"] == "delete_product"
    }
    present_delete_ids = delete_product_ids & formal_ids
    if (
        not dry_run
        and any(
            item["action"] == "regenerate_image"
            for item in decisions
        )
    ):
        reload_provider()
    for item in decisions:
        if item["product_id"] not in formal_ids:
            if item["action"] in {
                "false_positive",
                "delete_product",
                "approve_product",
            }:
                if item["action"] == "false_positive":
                    false_positive_overrides.append(item)
                planned.append({
                    **item,
                    "status": "quarantined_product_decision_recorded",
                })
                continue
            raise ValueError(
                f"隔离商品 {item['product_id']} 不在正式表中，"
                "不能直接执行图片删除或重新生成；请标记误判后重新跑任务"
            )
        if item["product_id"] in delete_product_ids:
            if item["action"] != "delete_product":
                planned.append({
                    **item,
                    "status": "superseded_by_delete_product",
                })
            continue
        index = _row_index(payload, item["product_id"])
        action = item["action"]
        if action == "delete_image":
            images = payload["产品图片链接"][index]
            if item["image_url"] not in images[1:]:
                raise ValueError(
                    f"商品 {item['product_id']} 未找到指定附图，"
                    "或该 URL 是主图，拒绝删除"
                )
        elif action == "regenerate_image":
            field = (
                "产品图片链接"
                if item["role"] == "main"
                else "变种图片链接"
            )
            images = payload[field][index]
            if item["image_url"] not in images:
                raise ValueError(
                    f"商品 {item['product_id']} 的 {item['role']} "
                    "中找不到待生成 URL"
                )
            if not dry_run:
                generated, assessment = _regenerate_safe_image(
                    item["image_url"],
                    role=item["role"],
                )
                regeneration_results.append({
                    **item,
                    "generated_url": generated,
                    "assessment": assessment,
                })
        elif action == "false_positive":
            false_positive_overrides.append(item)
        planned.append({**item, "status": "validated"})

    if dry_run:
        return {
            "status": "dry_run",
            "formal_json": str(formal_path),
            "decisions": planned,
            "would_backup_review_package": str(package_dir or ""),
        }

    replacements = {
        (
            item["product_id"],
            item["role"],
            item["image_url"],
        ): item
        for item in regeneration_results
    }
    for product_id in present_delete_ids:
        index = _row_index(payload, product_id)
        _remove_row(payload, index)
    for item in decisions:
        if item["product_id"] in delete_product_ids:
            continue
        if item["product_id"] not in formal_ids:
            continue
        index = _row_index(payload, item["product_id"])
        if item["action"] == "delete_image":
            images = payload["产品图片链接"][index]
            payload["产品图片链接"][index] = [
                images[0],
                *[
                    url for url in images[1:]
                    if url != item["image_url"]
                ],
            ]
        elif item["action"] == "regenerate_image":
            result = replacements[(
                item["product_id"],
                item["role"],
                item["image_url"],
            )]
            field = (
                "产品图片链接"
                if item["role"] == "main"
                else "变种图片链接"
            )
            payload[field][index] = [
                (
                    result["generated_url"]
                    if url == item["image_url"] else url
                )
                for url in payload[field][index]
            ]

    validate_columnar_payload(
        payload,
        required_fields=AMAZON_JSON_OUTPUT_FIELDS,
    )
    backups = _backup(formal_path, package_dir)
    _atomic_json(formal_path, payload)
    central_overrides_path = _central_override_path(formal_path)
    _merge_overrides(
        central_overrides_path,
        false_positive_overrides,
    )
    report = {
        "version": 1,
        "status": "applied",
        "applied_at": datetime.now().isoformat(timespec="seconds"),
        "formal_json": str(formal_path),
        "decisions_source": str(decisions_path),
        "backups": backups,
        "decisions": decisions,
        "regenerations": regeneration_results,
        "central_false_positive_overrides": str(
            central_overrides_path
        ),
    }
    if package_dir:
        _update_review_package(
            package_dir,
            decisions,
            application_report=report,
        )
    return report


def apply_latest_decisions(decisions_json: str | Path) -> dict:
    """Apply an exported decision file to the single expanded latest run."""
    from ..delivery import LATEST_DIR, REFILL_NAME

    return apply_decisions(
        LATEST_DIR / REFILL_NAME,
        decisions_json,
        review_package=LATEST_DIR,
    )


__all__ = ["apply_decisions", "apply_latest_decisions"]
