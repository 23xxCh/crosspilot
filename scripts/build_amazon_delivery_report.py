#!/usr/bin/env python3
"""Build an actionable manual-review report for an Amazon JSON delivery."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

if __package__ in {None, ""}:
    from _bootstrap import ensure_package_imports
    ensure_package_imports()

from crosspilot.image_risk import assessment_status
from scripts.services.amazon_json import (
    AMAZON_JSON_OUTPUT_FIELDS,
    load_columnar_json,
    validate_columnar_payload,
)


def _load_json(path: str | Path) -> dict:
    with open(path, encoding='utf-8-sig') as handle:
        value = json.load(handle)
    return value if isinstance(value, dict) else {}


def build_delivery_report(
    source_path: str | Path,
    output_path: str | Path,
    cache_path: str | Path,
    destination_dir: str | Path,
    *,
    metrics_path: str | Path | None = None,
) -> dict:
    """Compare source/output/cache and write JSON, CSV, and Markdown reports."""
    source = load_columnar_json(str(source_path))
    output = _load_json(output_path)
    row_count = validate_columnar_payload(
        output,
        required_fields=AMAZON_JSON_OUTPUT_FIELDS,
    )
    if row_count != len(source['商品id']):
        raise ValueError(
            f'源数据 {len(source["商品id"])} 行，输出 {row_count} 行'
        )

    cache = _load_json(cache_path)
    risk_assessments = cache.get('risk_assessments') or {}
    gen_meta = cache.get('gen_meta') or {}
    gen_failures = cache.get('gen_failures') or {}
    metrics = (
        _load_json(metrics_path)
        if metrics_path and Path(metrics_path).exists()
        else {}
    )
    problem_ids = set(output.get('有问题的产品id') or [])

    rows = []
    summary = {
        'products': row_count,
        'generated_main': 0,
        'generated_variant': 0,
        'deleted_attachments': 0,
        'retained_risky_main': 0,
        'retained_risky_variant': 0,
        'problem_products': 0,
        'manual_review_products': 0,
        'source_variant_images': sum(
            len(images) for images in source['变种图片链接']
        ),
        'output_variant_images': sum(
            len(images) for images in output['变种图片链接']
        ),
    }

    for index in range(row_count):
        product_id = str(source['商品id'][index] or '')
        source_product = list(source['产品图片链接'][index] or [])
        output_product = list(output['产品图片链接'][index] or [])
        source_main = source_product[0] if source_product else ''
        output_main = output_product[0] if output_product else ''
        source_extra = source_product[1:]
        output_extra = output_product[1:]
        source_variants = list(source['变种图片链接'][index] or [])
        output_variants = list(output['变种图片链接'][index] or [])

        main_generated = bool(
            source_main and output_main and source_main != output_main
        )
        generated_variants = [
            {
                'position': position + 1,
                'source': source_url,
                'generated': output_url,
            }
            for position, (source_url, output_url) in enumerate(
                zip(source_variants, output_variants)
            )
            if source_url != output_url
        ]
        deleted_attachments = [
            url for url in source_extra if url not in output_extra
        ]
        retained_risky_main = bool(
            source_main
            and assessment_status(risk_assessments.get(source_main)) == 'risk'
            and output_main == source_main
        )
        retained_risky_variants = [
            {
                'position': position + 1,
                'url': source_url,
                'failure': gen_failures.get(
                    f'variant:{source_url}',
                    {},
                ),
            }
            for position, (source_url, output_url) in enumerate(
                zip(source_variants, output_variants)
            )
            if (
                assessment_status(
                    risk_assessments.get(source_url)
                ) == 'risk'
                and output_url == source_url
            )
        ]

        text_issues = []
        if not str(output['产品标题'][index] or '').strip():
            text_issues.append('title_empty')
        if (
            str(source['产品描述'][index] or '').strip()
            and not str(output['产品描述'][index] or '').strip()
        ):
            text_issues.append('description_lost')
        empty_bullets = [
            number
            for number in range(1, 6)
            if not str(
                output[f'Bullet Point{number}'][index] or ''
            ).strip()
        ]
        if empty_bullets:
            text_issues.append(
                'empty_bullets:' + ','.join(map(str, empty_bullets))
            )
        keyword_count = len([
            term.strip()
            for term in str(
                output['关键词信息'][index] or ''
            ).replace('，', ',').split(',')
            if term.strip()
        ])
        if keyword_count != 10:
            text_issues.append(f'keyword_count:{keyword_count}')

        reasons = []
        if main_generated:
            reasons.append('generated_main')
        if generated_variants:
            reasons.append('generated_variant')
        if retained_risky_main:
            reasons.append('risky_main_retained')
        if retained_risky_variants:
            reasons.append('risky_variant_retained')
        if product_id in problem_ids:
            reasons.append('pipeline_problem_id')
        reasons.extend(text_issues)

        if main_generated:
            summary['generated_main'] += 1
        summary['generated_variant'] += len(generated_variants)
        summary['deleted_attachments'] += len(deleted_attachments)
        summary['retained_risky_main'] += int(retained_risky_main)
        summary['retained_risky_variant'] += len(
            retained_risky_variants
        )
        summary['problem_products'] += int(product_id in problem_ids)
        summary['manual_review_products'] += int(bool(reasons))

        if reasons:
            rows.append({
                'row': index + 1,
                'product_id': product_id,
                'title': output['产品标题'][index],
                'reasons': reasons,
                'source_main': source_main,
                'output_main': output_main,
                'main_generation_meta': gen_meta.get(
                    f'main:{source_main}',
                    {},
                ),
                'main_generation_failure': gen_failures.get(
                    f'main:{source_main}',
                    {},
                ),
                'generated_variants': generated_variants,
                'retained_risky_variants': retained_risky_variants,
                'deleted_attachments': deleted_attachments,
                'text_issues': text_issues,
            })

    report = {
        'source': str(Path(source_path).resolve()),
        'output': str(Path(output_path).resolve()),
        'cache': str(Path(cache_path).resolve()),
        'summary': summary,
        'provider_metrics': {
            key: metrics.get(key)
            for key in (
                'total_elapsed_s',
                'api_calls',
                'api_errors',
                'http_status',
                'http_retries',
                'circuit_open',
                'fallback_attempts',
                'fallback_successes',
                'fallback_failures',
                'fallback_routes',
                'concurrency',
                'image_remediation',
            )
            if key in metrics
        },
        'rows': rows,
    }

    destination = Path(destination_dir)
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / '人工复核报告.json'
    csv_path = destination / '人工复核清单.csv'
    markdown_path = destination / '人工复核报告.md'
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )

    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                'row',
                'product_id',
                'title',
                'reasons',
                'source_main',
                'output_main',
                'generated_variant_count',
                'retained_risky_variant_count',
                'deleted_attachment_count',
            ),
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({
                'row': row['row'],
                'product_id': row['product_id'],
                'title': row['title'],
                'reasons': ';'.join(row['reasons']),
                'source_main': row['source_main'],
                'output_main': row['output_main'],
                'generated_variant_count': len(
                    row['generated_variants']
                ),
                'retained_risky_variant_count': len(
                    row['retained_risky_variants']
                ),
                'deleted_attachment_count': len(
                    row['deleted_attachments']
                ),
            })

    lines = [
        '# Amazon 回填表人工复核报告',
        '',
        f'- 商品数：{summary["products"]}',
        f'- 新生成主图：{summary["generated_main"]}',
        f'- 新生成变种图：{summary["generated_variant"]}',
        f'- 删除风险附图：{summary["deleted_attachments"]}',
        (
            '- 生图失败后保留风险原图：'
            f'主图 {summary["retained_risky_main"]}，'
            f'变种图 {summary["retained_risky_variant"]}'
        ),
        f'- 需人工复核商品：{summary["manual_review_products"]}',
        '',
        '生成图未执行语义/相似度质量门禁，请重点检查产品主体、'
        '规格、数量、颜色、Logo、水印和白底效果。',
        '',
        '详细逐商品信息见 `人工复核清单.csv` 和 `人工复核报告.json`。',
    ]
    markdown_path.write_text('\n'.join(lines), encoding='utf-8')
    return {
        'report': report,
        'json_path': str(json_path),
        'csv_path': str(csv_path),
        'markdown_path': str(markdown_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('source')
    parser.add_argument('output')
    parser.add_argument('cache')
    parser.add_argument('destination')
    parser.add_argument('--metrics')
    args = parser.parse_args()
    result = build_delivery_report(
        args.source,
        args.output,
        args.cache,
        args.destination,
        metrics_path=args.metrics,
    )
    print(result['markdown_path'])


if __name__ == '__main__':
    main()
