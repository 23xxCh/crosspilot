"""Amazon delivery review-report tests."""
from __future__ import annotations

import json

from scripts.build_amazon_delivery_report import build_delivery_report


def _write(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False),
        encoding='utf-8',
    )


def test_delivery_report_tracks_generated_deleted_and_failed_images(
        tmp_path):
    source = {
        '商品id': ['item-1'],
        '产品标题': ['Product'],
        '产品描述': ['Source description'],
        '产品图片链接': [[
            'https://img/main.jpg',
            'https://img/clean-extra.jpg',
            'https://img/risky-extra.jpg',
        ]],
        '变种图片链接': [[
            'https://img/clean-var.jpg',
            'https://img/risky-var.jpg',
        ]],
    }
    output = {
        **source,
        '产品标题': ['Processed product'],
        '产品图片链接': [[
            'https://generated/main.png',
            'https://img/clean-extra.jpg',
        ]],
        '变种图片链接': [[
            'https://img/clean-var.jpg',
            'https://img/risky-var.jpg',
        ]],
        'Bullet Point1': ['Detail one'],
        'Bullet Point2': ['Detail two'],
        'Bullet Point3': ['Detail three'],
        'Bullet Point4': ['Detail four'],
        'Bullet Point5': ['Detail five'],
        '关键词信息': [
            'one, two, three, four, five, six, seven, eight, nine, ten'
        ],
        '有问题的产品id': ['item-1'],
    }
    def image_risk(status):
        return {
            'status': status,
            'reasons': ['seller_watermark'] if status == 'risk' else [],
            'placement': 'overlay' if status == 'risk' else 'none',
            'detected_text': [],
            'confidence': 0.95,
            'evidence': 'test assessment',
        }

    cache = {
        'risk_assessments': {
            'https://img/main.jpg': image_risk('risk'),
            'https://img/clean-extra.jpg': image_risk('safe'),
            'https://img/risky-extra.jpg': image_risk('risk'),
            'https://img/clean-var.jpg': image_risk('safe'),
            'https://img/risky-var.jpg': image_risk('risk'),
        },
        'gen_meta': {
            'main:https://img/main.jpg': {
                'delivery_validation': {'accepted': True},
            },
        },
        'gen_failures': {
            'variant:https://img/risky-var.jpg': {
                'reason': 'generation_failed',
            },
        },
    }
    source_path = tmp_path / 'source.json'
    output_path = tmp_path / 'output.json'
    cache_path = tmp_path / 'cache.json'
    _write(source_path, source)
    _write(output_path, output)
    _write(cache_path, cache)

    result = build_delivery_report(
        source_path,
        output_path,
        cache_path,
        tmp_path / 'review',
    )

    summary = result['report']['summary']
    assert summary['generated_main'] == 1
    assert summary['generated_variant'] == 0
    assert summary['deleted_attachments'] == 1
    assert summary['retained_risky_variant'] == 1
    assert summary['source_variant_images'] == 2
    assert summary['output_variant_images'] == 2
    assert (tmp_path / 'review' / '人工复核报告.md').exists()
    assert (tmp_path / 'review' / '人工复核清单.csv').exists()


def test_delivery_report_preserves_empty_variant_arrays(tmp_path):
    source = {
        '商品id': ['item-1'],
        '产品标题': ['Product'],
        '产品描述': ['Source description'],
        '产品图片链接': [['https://img/main.jpg']],
        '变种图片链接': [[]],
    }
    output = {
        **source,
        'Bullet Point1': ['Detail one'],
        'Bullet Point2': ['Detail two'],
        'Bullet Point3': ['Detail three'],
        'Bullet Point4': ['Detail four'],
        'Bullet Point5': ['Detail five'],
        '关键词信息': [
            'one, two, three, four, five, six, seven, eight, nine, ten'
        ],
        '有问题的产品id': [],
    }
    source_path = tmp_path / 'source.json'
    output_path = tmp_path / 'output.json'
    cache_path = tmp_path / 'cache.json'
    _write(source_path, source)
    _write(output_path, output)
    _write(cache_path, {'risk_assessments': {}})

    result = build_delivery_report(
        source_path,
        output_path,
        cache_path,
        tmp_path / 'review',
    )

    assert result['report']['summary']['source_variant_images'] == 0
    assert result['report']['summary']['output_variant_images'] == 0
