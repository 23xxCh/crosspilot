"""Amazon column-oriented JSON contract tests."""
import json
import os

import pytest

from scripts.services.amazon_json import (
    AMAZON_JSON_INPUT_FIELDS,
    AMAZON_JSON_OUTPUT_FIELDS,
    build_output_payload,
    load_columnar_json,
)


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT_TEMPLATE = os.path.join(
    ROOT,
    '亚马逊表',
    '跨境电商自动化采集表(数据格式模板).json',
)
OUTPUT_TEMPLATE = os.path.join(
    ROOT,
    '亚马逊表',
    '跨境电商自动化回填表(数据格式模板) .json',
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


def test_stage_write_output_uses_json_for_json_input(tmp_path):
    import scripts.process_amazon as process_amazon

    input_path = tmp_path / '商品采集表.json'
    input_path.write_text(
        json.dumps(_valid_input(), ensure_ascii=False),
        encoding='utf-8',
    )

    output = process_amazon._stage_write_output(
        _processed_rows(),
        str(input_path),
    )

    assert output.endswith('商品回填表.json')
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
