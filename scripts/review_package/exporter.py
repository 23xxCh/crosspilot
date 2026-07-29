"""Orchestrate creation of an Amazon final-review package."""
from __future__ import annotations

from pathlib import Path

from scripts.model_provider import reload_provider
from scripts.services.amazon_json import (
    AMAZON_JSON_OUTPUT_FIELDS,
    validate_columnar_payload,
)
from .assets import download_all_images
from .html_renderer import render_html
from .rows import build_quarantine_rows, build_review_rows
from .storage import _atomic_json, _load_json
from .translation import translate_payload


def export_review(
    input_path: str | Path,
    output_dir: str | Path,
    *,
    translate_workers: int = 30,
    download_workers: int = 32,
    audit_by_product: dict[str, list[dict]] | None = None,
    quarantine_products: list[dict] | None = None,
    shared_cache_dir: str | Path | None = None,
    translation_cache_path: str | Path | None = None,
    run_id: str | None = None,
) -> dict:
    input_path = Path(input_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    audit_by_product = audit_by_product or {}
    quarantine_products = quarantine_products or []
    shared_cache = (
        Path(shared_cache_dir)
        if shared_cache_dir is not None
        else None
    )
    translation_cache = (
        Path(translation_cache_path)
        if translation_cache_path is not None
        else output_dir / '翻译缓存.json'
    )
    payload = _load_json(input_path)
    row_count = validate_columnar_payload(
        payload,
        required_fields=AMAZON_JSON_OUTPUT_FIELDS,
    )
    reload_provider()
    translations, translation_failures, provider_metrics = (
        translate_payload(
            payload,
            translation_cache,
            workers=translate_workers,
        )
    )
    extra_urls = []
    for images in audit_by_product.values():
        for image in images:
            extra_urls.extend([
                image.get('url'),
                image.get('source_url'),
            ])
    for product in quarantine_products:
        for image in product.get('images') or []:
            extra_urls.extend([
                image.get('url'),
                image.get('source_url'),
            ])
    mapping, image_failures = download_all_images(
        payload,
        output_dir,
        workers=download_workers,
        extra_urls=extra_urls,
        shared_cache_dir=shared_cache,
    )
    rows = build_review_rows(
        payload,
        translations,
        mapping,
        audit_by_product=audit_by_product,
    )
    rows.extend(build_quarantine_rows(
        quarantine_products,
        mapping,
        row_offset=len(rows),
    ))
    image_occurrences = sum(
        len(row['images']) for row in rows
    )
    summary = {
        'version': 2,
        'run_id': run_id or output_dir.name,
        'products': len(rows),
        'released_products': row_count,
        'quarantined_products': len(quarantine_products),
        'translation_failures': translation_failures,
        'image_occurrences': image_occurrences,
        'unique_images': len(mapping),
        'downloaded_unique_images': sum(
            item.get('ok') is True for item in mapping.values()
        ),
        'image_failures': image_failures,
        'provider_metrics': provider_metrics,
        'source': str(input_path.resolve()),
    }
    _atomic_json(output_dir / '中文文案检查表.json', {
        'version': 2,
        'run_id': summary['run_id'],
        'summary': summary,
        'products': rows,
    })
    _atomic_json(output_dir / '图片映射.json', {
        'summary': {
            'image_occurrences': image_occurrences,
            'unique_images': len(mapping),
            'downloaded_unique_images': summary[
                'downloaded_unique_images'
            ],
            'image_failures': image_failures,
        },
        'images': mapping,
    })
    (output_dir / '中文文案检查表.html').write_text(
        render_html(rows, summary),
        encoding='utf-8',
    )
    (output_dir / '导出说明.txt').write_text(
        '\n'.join([
            'Amazon 中文文案与全图片检查包',
            f'正式表商品数：{row_count}',
            f'隔离商品数：{len(quarantine_products)}',
            f'图片引用数：{image_occurrences}',
            f'唯一图片数：{len(mapping)}',
            (
                '成功下载唯一图片：'
                f'{summary["downloaded_unique_images"]}'
            ),
            f'翻译失败行：{translation_failures or "无"}',
            f'图片失败 URL 数：{len(image_failures)}',
            '请用浏览器打开“中文文案检查表.html”检查。',
            '本目录为检查副本，未修改正式回填表。',
        ]),
        encoding='utf-8',
    )
    return summary


__all__ = ["export_review"]
