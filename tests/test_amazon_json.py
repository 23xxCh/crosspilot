"""Amazon column-oriented JSON contract tests."""
import json
import os

import pytest

from amazon_processor.schema import (
    AMAZON_JSON_INPUT_FIELDS,
    AMAZON_JSON_OUTPUT_FIELDS,
    build_output_payload,
    load_columnar_json,
    load_rows,
    write_output_json,
)


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT_TEMPLATE = os.path.join(
    ROOT,
    'tests',
    'fixtures',
    'amazon_json',
    'input_template.json',
)
OUTPUT_TEMPLATE = os.path.join(
    ROOT,
    'tests',
    'fixtures',
    'amazon_json',
    'output_template.json',
)


def _valid_input():
    return {
        '商品id': ['item-1'],
        '产品标题': ['Original title'],
        '产品描述': ['Original description'],
        '产品图片链接': [[
            'https://img/main.jpg',
            'https://img/attachment.jpg',
        ]],
        '变种图片链接': [['https://img/variant.jpg']],
    }


def _processed_rows():
    return [{
        'id': 'item-1',
        'title': 'Optimized title',
        'desc': 'Clean description',
        'main_img': 'https://generated/main.jpg',
        'extra_imgs': ['https://img/attachment-clean.jpg'],
        'var_imgs': ['https://generated/variant.jpg'],
        'bullets': [f'Bullet {index}' for index in range(1, 6)],
        'keywords': 'keyword one, keyword two',
    }]


def test_supplied_templates_match_declared_contract():
    input_payload = load_columnar_json(INPUT_TEMPLATE)
    with open(OUTPUT_TEMPLATE, encoding='utf-8-sig') as handle:
        output_payload = json.load(handle)

    assert tuple(input_payload) == AMAZON_JSON_INPUT_FIELDS
    assert tuple(output_payload) == AMAZON_JSON_OUTPUT_FIELDS
    assert len(input_payload['产品标题']) == 1
    assert isinstance(input_payload['产品图片链接'][0], list)
    assert isinstance(output_payload['变种图片链接'][0], list)


def test_build_output_payload_matches_refill_template_shape():
    payload = build_output_payload(_processed_rows())

    assert tuple(payload) == AMAZON_JSON_OUTPUT_FIELDS
    assert payload['商品id'] == ['item-1']
    assert payload['产品标题'] == ['Optimized title']
    assert payload['产品描述'] == ['Clean description']
    assert payload['产品图片链接'] == [[
        'https://generated/main.jpg',
        'https://img/attachment-clean.jpg',
    ]]
    assert payload['变种图片链接'] == [['https://generated/variant.jpg']]
    assert payload['Bullet Point5'] == ['Bullet 5']
    assert payload['关键词信息'] == ['keyword one, keyword two']


def test_build_output_payload_preserves_duplicate_variant_positions():
    row = _processed_rows()[0]
    row['var_imgs'] = [
        'https://generated/variant.jpg',
        'https://generated/variant.jpg',
    ]

    payload = build_output_payload([row])

    assert payload['变种图片链接'] == [[
        'https://generated/variant.jpg',
        'https://generated/variant.jpg',
    ]]


def test_build_output_payload_includes_missing_description_problem_id():
    row = _processed_rows()[0]
    row['desc'] = ''
    row['_quality_issues'] = [{
        'code': 'missing_source_description',
        'message': '源产品描述为空，已保留空值并标记人工复核',
    }]

    payload = build_output_payload([row])

    assert payload['产品描述'] == ['']
    assert payload['有问题的产品id'] == ['item-1']


def test_write_output_uses_exact_json_contract(tmp_path):
    input_path = tmp_path / '商品采集表.json'
    input_path.write_text(
        json.dumps(_valid_input(), ensure_ascii=False),
        encoding='utf-8',
    )

    output = write_output_json(
        _processed_rows(),
        tmp_path / "跨境电商自动化回填表.json",
    )

    assert output.endswith('跨境电商自动化回填表.json')
    with open(output, encoding='utf-8') as handle:
        payload = json.load(handle)
    assert tuple(payload) == AMAZON_JSON_OUTPUT_FIELDS
    assert payload['产品图片链接'][0][0] == 'https://generated/main.jpg'


def test_load_rejects_mismatched_column_lengths(tmp_path):
    payload = _valid_input()
    payload['产品描述'] = []
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')

    with pytest.raises(ValueError, match='数组长度必须一致'):
        load_columnar_json(str(path))


def test_load_rejects_flat_image_column(tmp_path):
    payload = _valid_input()
    payload['产品图片链接'] = ['https://img/main.jpg']
    path = tmp_path / 'bad-images.json'
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')

    with pytest.raises(ValueError, match='图片 URL 数组'):
        load_columnar_json(str(path))


def test_load_rejects_missing_template_field(tmp_path):
    payload = _valid_input()
    del payload['商品id']
    path = tmp_path / 'missing.json'
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')

    with pytest.raises(ValueError, match='缺少字段: 商品id'):
        load_columnar_json(str(path))


def test_load_rejects_non_string_scalar_value(tmp_path):
    payload = _valid_input()
    payload['商品id'] = [177983309003880863]
    path = tmp_path / 'numeric-id.json'
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')

    with pytest.raises(ValueError, match='商品id.*字符串'):
        load_columnar_json(str(path))


def test_load_rejects_invalid_image_url(tmp_path):
    payload = _valid_input()
    payload['产品图片链接'] = [['not-a-url']]
    path = tmp_path / 'invalid-url.json'
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')

    with pytest.raises(ValueError, match='无效图片 URL'):
        load_columnar_json(str(path))


def test_load_rows_applies_requested_row_limit(tmp_path):
    """CLI --max-rows must slice valid input instead of rejecting the full file."""
    payload = {
        '商品id': [f'item-{index}' for index in range(3)],
        '产品标题': [f'Title {index}' for index in range(3)],
        '产品描述': [f'Description {index}' for index in range(3)],
        '产品图片链接': [
            [f'https://img.example/{index}.jpg']
            for index in range(3)
        ],
        '变种图片链接': [[] for _ in range(3)],
    }
    path = tmp_path / '商品采集表.json'
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    rows = load_rows(path, max_rows=2)

    assert [row['id'] for row in rows] == ['item-0', 'item-1']
