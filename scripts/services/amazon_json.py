"""Amazon column-oriented JSON input/output contract."""
from __future__ import annotations

import json
import os
import time
from typing import Any


AMAZON_JSON_INPUT_FIELDS = (
    '商品id',
    '产品标题',
    '产品描述',
    '产品图片链接',
    '变种图片链接',
)

AMAZON_JSON_OUTPUT_FIELDS = (
    *AMAZON_JSON_INPUT_FIELDS,
    'Bullet Point1',
    'Bullet Point2',
    'Bullet Point3',
    'Bullet Point4',
    'Bullet Point5',
    '关键词信息',
    '有问题的产品id',
)

_IMAGE_FIELDS = ('产品图片链接', '变种图片链接')


def validate_columnar_payload(
        payload: Any,
        *,
        required_fields=AMAZON_JSON_INPUT_FIELDS,
        max_rows: int | None = None,
) -> int:
    """Validate the template's column-oriented arrays and return row count."""
    if not isinstance(payload, dict):
        raise ValueError('Amazon JSON 顶层必须是对象')

    missing = [field for field in required_fields if field not in payload]
    if missing:
        raise ValueError('Amazon JSON 缺少字段: ' + ', '.join(missing))

    for field in required_fields:
        if not isinstance(payload[field], list):
            raise ValueError(f'Amazon JSON 字段“{field}”必须是数组')

    row_count = len(payload['产品标题'])
    if row_count == 0:
        raise ValueError('Amazon JSON 没有商品数据')
    if max_rows is not None and row_count > max_rows:
        raise ValueError(
            f'Amazon JSON 数据行数 {row_count} 超过安全上限 {max_rows}'
        )

    mismatched = [
        f'{field}={len(payload[field])}'
        for field in required_fields
        if field != '有问题的产品id' and len(payload[field]) != row_count
    ]
    if mismatched:
        raise ValueError(
            f'Amazon JSON 各字段数组长度必须一致，产品标题={row_count}，'
            + ', '.join(mismatched)
        )

    for field in _IMAGE_FIELDS:
        for index, images in enumerate(payload[field], start=1):
            if not isinstance(images, list):
                raise ValueError(
                    f'Amazon JSON 字段“{field}”第 {index} 项必须是图片 URL 数组'
                )
            if any(not isinstance(url, str) for url in images):
                raise ValueError(
                    f'Amazon JSON 字段“{field}”第 {index} 项只能包含字符串'
                )
            invalid_urls = [
                url for url in images
                if not url.startswith(('http://', 'https://'))
            ]
            if invalid_urls:
                raise ValueError(
                    f'Amazon JSON 字段“{field}”第 {index} 项包含无效图片 URL'
                )

    scalar_fields = [
        field for field in required_fields
        if field not in _IMAGE_FIELDS
    ]
    for field in scalar_fields:
        if any(not isinstance(value, str) for value in payload[field]):
            raise ValueError(f'Amazon JSON 字段“{field}”只能包含字符串')

    return row_count


def load_columnar_json(path: str, *, max_rows: int | None = None) -> dict:
    """Load and validate an Amazon collection JSON file."""
    try:
        with open(path, encoding='utf-8-sig') as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'文件不是有效的 Amazon JSON: {exc}') from exc
    validate_columnar_payload(payload, max_rows=max_rows)
    return payload


def _unique_urls(values) -> list[str]:
    return list(dict.fromkeys(
        value.strip()
        for value in values
        if isinstance(value, str) and value.strip()
    ))


_removed_dirty_ids: list[str] = []

def build_output_payload(rows: list[dict]) -> dict:
    """Build the exact column order and nested image shape of the output template."""
    global _removed_dirty_ids
    payload = {
        '商品id': [],
        '产品标题': [],
        '产品描述': [],
        '产品图片链接': [],
        '变种图片链接': [],
        'Bullet Point1': [],
        'Bullet Point2': [],
        'Bullet Point3': [],
        'Bullet Point4': [],
        'Bullet Point5': [],
        '关键词信息': [],
        '有问题的产品id': [],
    }
    problem_ids = []

    for row in rows:
        bullets = row.get('bullets')
        if not isinstance(bullets, list):
            bullets = []
        bullets = [
            str(bullets[index] or '') if index < len(bullets) else ''
            for index in range(5)
        ]
        product_images = _unique_urls([
            row.get('main_img', ''),
            *(row.get('extra_imgs') or []),
        ])
        # Variant positions are meaningful. Preserve duplicate source
        # occurrences and order; only normalize empty/whitespace values.
        variant_images = [
            value.strip()
            for value in (row.get('var_imgs') or [])
            if isinstance(value, str) and value.strip()
        ]

        payload['商品id'].append(str(row.get('id') or ''))
        payload['产品标题'].append(str(row.get('title') or ''))
        payload['产品描述'].append(str(row.get('desc') or ''))
        payload['产品图片链接'].append(product_images)
        payload['变种图片链接'].append(variant_images)
        for index in range(5):
            payload[f'Bullet Point{index + 1}'].append(bullets[index])
        payload['关键词信息'].append(str(row.get('keywords') or ''))
        # Collect problem IDs
        issues = row.get('_quality_issues', [])
        if issues:
            problem_ids.append(str(row.get('id') or ''))

    payload['有问题的产品id'] = problem_ids + list(_removed_dirty_ids)
    validate_columnar_payload(
        payload,
        required_fields=AMAZON_JSON_OUTPUT_FIELDS,
    )
    if tuple(payload) != AMAZON_JSON_OUTPUT_FIELDS:
        raise ValueError('Amazon 回填 JSON 字段顺序不符合模板')
    return payload


def output_path_for_input(input_path: str) -> str:
    """Return a non-overwriting JSON output path derived from the collection file."""
    directory = os.path.dirname(input_path)
    stem = os.path.splitext(os.path.basename(input_path))[0].rstrip()
    output_stem = stem.replace('采集表', '回填表', 1) \
        if '采集表' in stem else stem + '_回填'
    output = os.path.join(directory, output_stem + '.json')
    if not os.path.exists(output):
        return output
    suffix = time.strftime('_%H%M%S_') + str(
        int(time.time() * 1000) % 1000
    ).zfill(3)
    return os.path.join(directory, output_stem + suffix + '.json')


def write_output_json(rows: list[dict], input_path: str) -> str:
    """Atomically write the Amazon refill JSON beside its collection file."""
    payload = build_output_payload(rows)
    output = output_path_for_input(input_path)
    temp_path = output + f'.{os.getpid()}.tmp'
    try:
        with open(temp_path, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, output)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
    return output
