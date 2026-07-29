#!/usr/bin/env python3
"""Safely remove exact product IDs from an Amazon refill and review package."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

if __package__ in {None, ""}:
    from _bootstrap import ensure_package_imports
    ensure_package_imports()

from scripts.review_package import (
    _atomic_json,
    _source_row,
    _translation_signature,
    render_html,
)
from scripts.services.amazon_json import (
    AMAZON_JSON_OUTPUT_FIELDS,
    validate_columnar_payload,
)


def _load(path: Path) -> dict:
    with open(path, encoding='utf-8-sig') as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f'JSON 顶层必须为对象: {path}')
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _safe_archive_dir(root: Path) -> Path:
    timestamp = time.strftime('%Y%m%d_%H%M%S')
    target = root / f'删除商品前_{timestamp}'
    target.mkdir(parents=True, exist_ok=False)
    return target


def _remove_from_payload(payload: dict, product_ids: list[str]):
    validate_columnar_payload(
        payload,
        required_fields=AMAZON_JSON_OUTPUT_FIELDS,
    )
    requested = list(dict.fromkeys(str(value) for value in product_ids))
    positions = {}
    for product_id in requested:
        matches = [
            index
            for index, value in enumerate(payload['商品id'])
            if value == product_id
        ]
        if len(matches) != 1:
            raise ValueError(
                f'商品 ID {product_id} 应命中 1 行，实际 {len(matches)} 行'
            )
        positions[product_id] = matches[0]

    removed = [
        {
            'product_id': product_id,
            'row': positions[product_id] + 1,
            'title': payload['产品标题'][positions[product_id]],
            'product_images': payload['产品图片链接'][
                positions[product_id]
            ],
            'variant_images': payload['变种图片链接'][
                positions[product_id]
            ],
        }
        for product_id in requested
    ]
    remove_indexes = set(positions.values())
    for field in AMAZON_JSON_OUTPUT_FIELDS:
        if field == '有问题的产品id':
            payload[field] = [
                value for value in payload[field]
                if value not in positions
            ]
            continue
        payload[field] = [
            value
            for index, value in enumerate(payload[field])
            if index not in remove_indexes
        ]
    validate_columnar_payload(
        payload,
        required_fields=AMAZON_JSON_OUTPUT_FIELDS,
    )
    return removed


def _archive_review_files(review_dir: Path, archive_dir: Path) -> None:
    for name in (
        '中文文案检查表.html',
        '中文文案检查表.json',
        '图片映射.json',
        '导出说明.txt',
        '翻译缓存.json',
    ):
        source = review_dir / name
        if source.exists():
            shutil.copy2(source, archive_dir / name)


def _update_review_package(
    payload: dict,
    review_dir: Path,
    product_ids: set[str],
    archive_dir: Path,
) -> dict:
    review_path = review_dir / '中文文案检查表.json'
    mapping_path = review_dir / '图片映射.json'
    cache_path = review_dir / '翻译缓存.json'
    review = _load(review_path)
    mapping_payload = _load(mapping_path)

    products = [
        product for product in review.get('products', [])
        if product.get('product_id') not in product_ids
    ]
    if len(products) != len(payload['商品id']):
        raise ValueError(
            '中文检查包商品数与删除后的正式回填表不一致'
        )
    for index, product in enumerate(products, start=1):
        product['row'] = index

    referenced_urls = set(
        url
        for field in ('产品图片链接', '变种图片链接')
        for images in payload[field]
        for url in images
    )
    old_mapping = mapping_payload.get('images') or {}
    missing_mapping = referenced_urls - set(old_mapping)
    if missing_mapping:
        raise ValueError(
            f'图片映射缺少 {len(missing_mapping)} 个仍在使用的 URL'
        )
    new_mapping = {
        url: old_mapping[url]
        for url in old_mapping
        if url in referenced_urls
    }

    image_archive = archive_dir / '被移走图片'
    image_archive.mkdir(parents=True, exist_ok=True)
    moved_images = []
    image_root = (review_dir / '图片').resolve()
    for url, info in old_mapping.items():
        if url in referenced_urls:
            continue
        relative = str((info or {}).get('path') or '').strip()
        if not relative:
            continue
        source = (review_dir / relative).resolve()
        if (
            source.exists()
            and source.is_file()
            and image_root in source.parents
        ):
            target = image_archive / source.name
            shutil.move(str(source), str(target))
            moved_images.append({
                'url': url,
                'from': str(source),
                'to': str(target),
            })

    image_occurrences = sum(
        len(product.get('images') or [])
        for product in products
    )
    downloaded = sum(
        item.get('ok') is True
        for item in new_mapping.values()
    )
    review_summary = dict(review.get('summary') or {})
    review_summary.update({
        'products': len(products),
        'translation_failures': [],
        'image_occurrences': image_occurrences,
        'unique_images': len(new_mapping),
        'downloaded_unique_images': downloaded,
        'image_failures': [
            url for url, item in new_mapping.items()
            if not item.get('ok')
        ],
    })
    _atomic_json(review_path, {
        'summary': review_summary,
        'products': products,
    })
    _atomic_json(mapping_path, {
        'summary': {
            'image_occurrences': image_occurrences,
            'unique_images': len(new_mapping),
            'downloaded_unique_images': downloaded,
            'image_failures': review_summary['image_failures'],
        },
        'images': new_mapping,
    })
    (review_dir / '中文文案检查表.html').write_text(
        render_html(products, review_summary),
        encoding='utf-8',
    )

    rebuilt_cache = {'version': 1, 'rows': {}}
    for index, product in enumerate(products):
        translation = {
            key: product[key]
            for key in (
                'title',
                'description',
                'bullets',
                'keywords',
            )
        }
        source = _source_row(payload, index)
        rebuilt_cache['rows'][str(index)] = {
            'signature': _translation_signature(source),
            'translation': translation,
        }
    _atomic_json(cache_path, rebuilt_cache)

    (review_dir / '导出说明.txt').write_text(
        '\n'.join([
            'Amazon 中文文案与全图片检查包',
            f'商品数：{len(products)}',
            f'图片引用数：{image_occurrences}',
            f'唯一图片数：{len(new_mapping)}',
            f'成功下载唯一图片：{downloaded}',
            '翻译失败行：无',
            f'图片失败 URL 数：{len(review_summary["image_failures"])}',
            '请用浏览器打开“中文文案检查表.html”检查。',
            '本目录为检查副本，未修改原始采集表。',
        ]),
        encoding='utf-8',
    )
    return {
        'products': len(products),
        'image_occurrences': image_occurrences,
        'unique_images': len(new_mapping),
        'downloaded_unique_images': downloaded,
        'moved_images': moved_images,
    }


def remove_products(
    output_path: str | Path,
    review_dir: str | Path,
    archive_root: str | Path,
    product_ids: list[str],
) -> dict:
    output_path = Path(output_path).resolve()
    review_dir = Path(review_dir).resolve()
    archive_root = Path(archive_root).resolve()
    archive_root.mkdir(parents=True, exist_ok=True)
    archive_dir = _safe_archive_dir(archive_root)

    shutil.copy2(
        output_path,
        archive_dir / '跨境电商自动化回填表_删除前.json',
    )
    _archive_review_files(review_dir, archive_dir)
    payload = _load(output_path)
    before_hash = _sha256(output_path)
    removed = _remove_from_payload(payload, product_ids)
    _atomic_json(output_path, payload)
    review_result = _update_review_package(
        payload,
        review_dir,
        set(product_ids),
        archive_dir,
    )
    result = {
        'output': str(output_path),
        'archive': str(archive_dir),
        'before_hash': before_hash,
        'after_hash': _sha256(output_path),
        'remaining_products': len(payload['商品id']),
        'removed': removed,
        'review': review_result,
    }
    _atomic_json(archive_dir / '删除记录.json', result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('output')
    parser.add_argument('review_dir')
    parser.add_argument('archive_root')
    parser.add_argument('product_ids', nargs='+')
    args = parser.parse_args()
    result = remove_products(
        args.output,
        args.review_dir,
        args.archive_root,
        args.product_ids,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
