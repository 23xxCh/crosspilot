import json
from pathlib import Path

from scripts.normalize_amazon_brand_titles import (
    apply_title_normalization,
)
from scripts.services.amazon_json import AMAZON_JSON_OUTPUT_FIELDS


def _payload():
    rows = 3
    return {
        '商品id': ['a', 'b', 'c'],
        '产品标题': [
            'For Generic Washer Nozzle for Toyota Camry',
            'For 2x DIY Chrome Badge',
            'Seat Cover Organizer for Car',
        ],
        '产品描述': ['description'] * rows,
        '产品图片链接': [['https://example.com/main.jpg']] * rows,
        '变种图片链接': [[] for _ in range(rows)],
        'Bullet Point1': ['bullet one'] * rows,
        'Bullet Point2': ['bullet two'] * rows,
        'Bullet Point3': ['bullet three'] * rows,
        'Bullet Point4': ['bullet four'] * rows,
        'Bullet Point5': ['bullet five'] * rows,
        '关键词信息': ['one,two,three'] * rows,
        '有问题的产品id': [],
    }


def test_apply_title_normalization_only_changes_targeted_titles(tmp_path):
    input_path = tmp_path / '回填表.json'
    review_dir = tmp_path / '检查图片文字'
    review_dir.mkdir()
    (review_dir / '中文文案检查表.html').write_text(
        'before',
        encoding='utf-8',
    )
    input_path.write_text(
        json.dumps(_payload(), ensure_ascii=False),
        encoding='utf-8',
    )

    report = apply_title_normalization(
        input_path,
        archive_root=tmp_path / 'archive',
        review_dir=review_dir,
        timestamp='20260729_120000',
    )

    result = json.loads(input_path.read_text(encoding='utf-8'))
    assert tuple(result) == AMAZON_JSON_OUTPUT_FIELDS
    assert result['产品标题'] == [
        'Generic Washer Nozzle for Toyota Camry',
        'Generic 2x Chrome Badge',
        'Seat Cover Organizer for Car',
    ]
    assert report['changed_titles'] == 2
    assert (
        report['non_title_sha256_before']
        == report['non_title_sha256_after']
    )
    archive_dir = Path(report['archive_dir'])
    assert (archive_dir / '标题变更清单.json').exists()
    assert (
        archive_dir
        / '检查图片文字_标题规范前'
        / '中文文案检查表.html'
    ).read_text(encoding='utf-8') == 'before'


def test_dry_run_does_not_write_or_create_archive(tmp_path):
    input_path = tmp_path / '回填表.json'
    original = json.dumps(_payload(), ensure_ascii=False)
    input_path.write_text(original, encoding='utf-8')

    report = apply_title_normalization(
        input_path,
        archive_root=tmp_path / 'archive',
        timestamp='20260729_120001',
        dry_run=True,
    )

    assert report['changed_titles'] == 2
    assert input_path.read_text(encoding='utf-8') == original
    assert not Path(report['archive_dir']).exists()
