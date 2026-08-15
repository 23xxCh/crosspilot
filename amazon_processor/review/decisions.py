#!/usr/bin/env python3
"""Validate and apply decisions exported by an Amazon final-review package."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil

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
    "recheck_main_candidate",
    "reorder_images",
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
    for field in AMAZON_JSON_OUTPUT_FIELDS:
        if field == "有问题的产品id":
            continue
        del payload[field][index]


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
                "主图/变种图不能直接删除；只能删除整个商品"
            )
        if action == "recheck_main_candidate" and role not in {
            "main",
            "attachment",
        }:
            raise ValueError(
                "recheck_main_candidate 只能用于主图或附图候选"
            )
        if action in {
            "delete_image",
            "recheck_main_candidate",
        } and not image_url:
            raise ValueError(
                f"第 {index} 条图片决定缺少 image_url"
            )
        image_urls = item.get("image_urls") or []
        if action == "reorder_images":
            if role != "product_images":
                raise ValueError(
                    "reorder_images 的 role 必须是 product_images"
                )
            if not isinstance(image_urls, list) or not image_urls:
                raise ValueError(
                    f"第 {index} 条排序决定缺少 image_urls"
                )
            image_urls = [str(url or "").strip() for url in image_urls]
            if any(not url for url in image_urls):
                raise ValueError(
                    f"第 {index} 条排序决定包含空图片 URL"
                )
        else:
            image_urls = []
        normalized.append({
            "product_id": product_id,
            "action": action,
            "role": role,
            "image_url": image_url,
            "image_urls": image_urls,
            "note": str(item.get("note") or ""),
            "recorded_at": str(item.get("recorded_at") or ""),
        })
    return normalized


def _select_existing_eligibility(
    package_dir: Path | None,
) -> dict[str, set[str]] | None:
    if package_dir is None:
        return None
    data_path = package_dir / "审核数据.json"
    if not data_path.is_file():
        return None
    review_data = _load_json(data_path)
    mode = str(
        ((((review_data.get("summary") or {}).get("run_metrics") or {}).get(
            "image_safety_gate"
        ) or {}).get("processing_mode"))
        or ""
    )
    if mode != "select_existing":
        return None
    return {
        str(product.get("product_id") or ""): {
            str(image.get("url") or "")
            for image in product.get("images") or []
            if image.get("main_eligible") is True
        }
        for product in review_data.get("products") or []
    }


def _update_review_package(
    package_dir: Path,
    decisions: list[dict],
    *,
    application_report: dict,
    formal_payload: dict,
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
        if decision["action"] != "reorder_images":
            continue
        product = product_map.get(decision["product_id"])
        if not product:
            continue
        product_images = [
            image
            for image in product.get("images") or []
            if image.get("role_key") in {"main", "attachment"}
        ]
        other_images = [
            image
            for image in product.get("images") or []
            if image.get("role_key") not in {"main", "attachment"}
        ]
        buckets: dict[str, list[dict]] = {}
        for image in product_images:
            buckets.setdefault(str(image.get("url") or ""), []).append(image)
        reordered = []
        for position, url in enumerate(decision["image_urls"]):
            matches = buckets.get(url) or []
            if not matches:
                continue
            image = matches.pop(0)
            image["role_key"] = "main" if position == 0 else "attachment"
            image["role"] = "主图" if position == 0 else f"附图 {position}"
            image["position"] = position
            reordered.append(image)
        product["images"] = [*reordered, *other_images]
    for decision in decisions:
        product = product_map.get(decision["product_id"])
        if not product:
            continue
        if decision["role"] == "product":
            product["decision"] = decision
            continue
        if decision["action"] == "reorder_images":
            product["image_order_decision"] = decision
            continue
        if decision["action"] == "delete_image":
            product["images"] = [
                image
                for image in product.get("images") or []
                if not (
                    image.get("role_key") == decision["role"]
                    and image.get("url") == decision["image_url"]
                )
            ]
            product_images = [
                image
                for image in product.get("images") or []
                if image.get("role_key") in {"main", "attachment"}
            ]
            other_images = [
                image
                for image in product.get("images") or []
                if image.get("role_key") not in {"main", "attachment"}
            ]
            for position, image in enumerate(product_images):
                image["role_key"] = "main" if position == 0 else "attachment"
                image["role"] = "主图" if position == 0 else f"附图 {position}"
                image["position"] = position
            product["images"] = [*product_images, *other_images]
            continue
        for image in product.get("images") or []:
            if (
                image.get("role_key") == decision["role"]
                and image.get("url") == decision["image_url"]
            ):
                image["decision"] = decision
                break
    review_data["applied_decisions"] = decisions
    review_data["products"] = [
        item
        for item in products
        if str(item.get("product_id") or "")
        not in deleted_products
    ]
    for row_number, product in enumerate(
        review_data["products"],
        start=1,
    ):
        product["row"] = row_number

    referenced_urls = {
        str(url)
        for product in review_data["products"]
        for image in (product.get("images") or [])
        for url in (image.get("url"), image.get("source_url"))
        if str(url or "")
    }
    image_mapping = review_data.get("images")
    if isinstance(image_mapping, dict):
        review_data["images"] = {
            url: item
            for url, item in image_mapping.items()
            if url in referenced_urls
        }

    summary = review_data.get("summary") or {}
    released_products = sum(
        not bool(item.get("quarantined"))
        for item in review_data["products"]
    )
    quarantined_products = (
        len(review_data["products"]) - released_products
    )
    image_occurrences = sum(
        len(item.get("images") or [])
        for item in review_data["products"]
    )
    downloaded_urls = {
        str(image.get("url") or "")
        for product in review_data["products"]
        for image in (product.get("images") or [])
        if image.get("download_ok") is True
        and str(image.get("url") or "")
    }
    summary["products"] = len(review_data["products"])
    summary["released_products"] = released_products
    summary["quarantined_products"] = quarantined_products
    summary["image_occurrences"] = image_occurrences
    summary["unique_images"] = len(referenced_urls)
    summary["downloaded_unique_images"] = len(downloaded_urls)
    review_data["summary"] = summary
    review_data["application_report"] = application_report
    _atomic_json(data_path, review_data)
    (package_dir / "终审包.html").write_text(
        render_html(
            review_data["products"],
            summary,
            formal_payload=formal_payload,
        ),
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
    # Validate every target before any write or backup.
    formal_ids = {
        str(value) for value in payload["商品id"]
    }
    reorder_by_product: dict[str, list[str]] = {}
    main_eligibility = _select_existing_eligibility(package_dir)
    for item in decisions:
        if item["action"] != "reorder_images":
            continue
        product_id = item["product_id"]
        if product_id in reorder_by_product:
            raise ValueError(f"商品 {product_id} 存在重复图片排序决定")
        reorder_by_product[product_id] = item["image_urls"]
    delete_product_ids = {
        item["product_id"]
        for item in decisions
        if item["action"] == "delete_product"
    }
    present_delete_ids = delete_product_ids & formal_ids
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
                "不能直接执行图片删除；请标记误判后重新跑任务"
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
        if action == "reorder_images":
            images = payload["产品图片链接"][index]
            if Counter(item["image_urls"]) != Counter(images):
                raise ValueError(
                    f"商品 {item['product_id']} 的图片排序必须完整包含"
                    "现有主图和全部附图，不能新增、遗漏或重复图片"
                )
            if (
                main_eligibility is not None
                and item["image_urls"][0]
                not in main_eligibility.get(item["product_id"], set())
            ):
                raise ValueError(
                    f"商品 {item['product_id']} 的第 1 张图片没有 "
                    "safe + text_free 主图资格"
                )
        elif action == "delete_image":
            images = reorder_by_product.get(
                item["product_id"],
                payload["产品图片链接"][index],
            )
            if item["image_url"] not in images[1:]:
                raise ValueError(
                    f"商品 {item['product_id']} 未找到指定附图，"
                    "或该 URL 是主图，拒绝删除"
                )
        elif action == "recheck_main_candidate":
            raise ValueError(
                "重新审查主图资格必须使用最新任务入口应用，"
                "不能直接修改正式回填表"
            )
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

    for product_id in present_delete_ids:
        index = _row_index(payload, product_id)
        _remove_row(payload, index)
    payload["有问题的产品id"] = list(dict.fromkeys([
        *payload["有问题的产品id"],
        *[
            item["product_id"]
            for item in decisions
            if item["action"] == "delete_product"
        ],
    ]))
    for product_id, image_urls in reorder_by_product.items():
        if product_id in delete_product_ids or product_id not in formal_ids:
            continue
        index = _row_index(payload, product_id)
        payload["产品图片链接"][index] = list(image_urls)
    for item in decisions:
        if item["product_id"] in delete_product_ids:
            continue
        if item["product_id"] not in formal_ids:
            continue
        index = _row_index(payload, item["product_id"])
        if item["action"] == "reorder_images":
            continue
        if item["action"] == "delete_image":
            images = payload["产品图片链接"][index]
            payload["产品图片链接"][index] = [
                images[0],
                *[
                    url for url in images[1:]
                    if url != item["image_url"]
                ],
            ]

    missing_attachment_ids = [
        str(payload["商品id"][index])
        for index, images in enumerate(payload["产品图片链接"])
        if len(images) <= 1
    ]
    for product_id in missing_attachment_ids:
        _remove_row(payload, _row_index(payload, product_id))
    payload["有问题的产品id"] = list(dict.fromkeys([
        *payload["有问题的产品id"],
        *missing_attachment_ids,
    ]))

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
        "central_false_positive_overrides": str(
            central_overrides_path
        ),
    }
    if package_dir:
        _update_review_package(
            package_dir,
            decisions,
            application_report=report,
            formal_payload=payload,
        )
    return report


def apply_latest_decisions(decisions_json: str | Path) -> dict:
    """Apply an exported decision file to the single expanded latest run."""
    from ..delivery import LATEST_DIR, REFILL_NAME

    decision_path = Path(decisions_json).resolve()
    envelope = _load_json(decision_path)
    normalized = _validate_decisions(envelope)
    rechecks = [
        item for item in normalized
        if item["action"] == "recheck_main_candidate"
    ]
    if rechecks:
        if len(rechecks) != len(normalized):
            raise ValueError(
                "重新审查主图资格不能与删除或排序决定混合应用"
            )
        source = Path(str(envelope.get("source") or "")).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(
                f"审核决定对应的原采集表不存在: {source}"
            )
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        from ..pipeline import RUNTIME_ROOT, _process_json_unlocked

        cache_path = (
            RUNTIME_ROOT / "cache" / "pipeline" / f"{digest.hexdigest()}.json"
        )
        if cache_path.is_file():
            cache = _load_json(cache_path)
            for item in rechecks:
                url = item["image_url"]
                (cache.get("risk_assessments") or {}).pop(url, None)
                (cache.get("main_text_assessments") or {}).pop(url, None)
            _atomic_json(cache_path, cache)
        result = _process_json_unlocked(source)
        return {
            "version": 1,
            "status": "rechecked",
            "decisions": rechecks,
            "source": str(source),
            "published": result.published,
            "output_path": str(result.output_path or ""),
            "review_path": str(result.review_path),
            "pending_product_ids": list(result.pending_product_ids),
        }

    return apply_decisions(
        LATEST_DIR / REFILL_NAME,
        decisions_json,
        review_package=LATEST_DIR,
    )


__all__ = ["apply_decisions", "apply_latest_decisions"]
