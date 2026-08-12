"""Amazon column-oriented JSON input/output contract."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from datetime import datetime
from typing import Any

from .markets import normalize_market_code


AMAZON_JSON_LEGACY_INPUT_FIELDS = (
    '商品id',
    '产品标题',
    '产品描述',
    '产品图片链接',
    '变种图片链接',
)

AMAZON_JSON_INPUT_FIELDS = (
    '商品id',
    '产品站点',
    '产品标题',
    '产品描述',
    '产品图片链接',
    '变种图片链接',
)

AMAZON_JSON_OUTPUT_FIELDS = (
    '商品id',
    '产品站点',
    '产品标题',
    '副标题',
    '产品描述',
    '产品图片链接',
    '变种图片链接',
    'Bullet Point1',
    'Bullet Point2',
    'Bullet Point3',
    'Bullet Point4',
    'Bullet Point5',
    '关键词信息',
    '有问题的产品id',
)

_IMAGE_FIELDS = ('产品图片链接', '变种图片链接')


def _escape_raw_string_newlines(raw: str) -> str:
    """Escape literal line breaks inside JSON strings from collector exports."""
    output: list[str] = []
    in_string = False
    escaped = False
    for char in raw:
        if in_string:
            if escaped:
                output.append(char)
                escaped = False
                continue
            if char == '\\':
                output.append(char)
                escaped = True
                continue
            if char == '"':
                output.append(char)
                in_string = False
                continue
            if char == '\r':
                continue
            if char == '\n':
                output.append('\\n')
                continue
            output.append(char)
            continue
        output.append(char)
        if char == '"':
            in_string = True
    return ''.join(output)


def _insert_unambiguous_array_commas(raw: str) -> str:
    """Repair omitted commas between adjacent JSON array values only."""
    repaired = re.sub(
        r'(?<=[\]"])\s*(?=[\["])',
        ',\n',
        raw,
    )
    # Some collectors omit the outer close bracket after nested product-image
    # arrays. The following field boundary makes that missing bracket
    # unambiguous without touching arbitrary JSON structures.
    repaired = re.sub(
        r'(\n\s*\],\n\s*"变种图片链接"\s*:)',
        '\n  ],\n  ],\n  "变种图片链接":',
        repaired,
        count=1,
    )
    repaired = repaired.replace(
        '\n  ],\n  ],\n  "变种图片链接":',
        '\n  ]\n],\n  "变种图片链接":',
        1,
    )
    if repaired.rstrip().endswith(']'):
        repaired = repaired.rstrip() + '\n}\n'
    return repaired


def prepare_input_copy(
    path: str | os.PathLike[str],
    *,
    runtime_root: str | os.PathLike[str] | None = None,
) -> tuple[Path, list[str]]:
    """Validate input, or create a timestamped copy for safe structural repair.

    The original collector file is never rewritten. Only literal string
    newlines and unambiguous adjacent array separators are repaired. A
    missing trailing variant row is treated as an empty optional list; any
    other array-length mismatch remains a hard error.
    """
    source = Path(path).expanduser().resolve()
    try:
        raw = source.read_text(encoding='utf-8-sig')
    except (OSError, UnicodeError) as exc:
        raise ValueError(f'无法读取 Amazon 输入文件: {exc}') from exc
    try:
        payload = json.loads(raw)
        return source, []
    except json.JSONDecodeError:
        repaired = _insert_unambiguous_array_commas(
            _escape_raw_string_newlines(raw)
        )
    try:
        payload = json.loads(repaired)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f'输入 JSON 损坏，无法安全修复（未调用 API）: {exc}'
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError('输入 JSON 顶层必须是对象')
    warnings: list[str] = []
    row_count = len(payload.get('商品id') or [])
    for field in ('商品id', '产品站点', '产品标题', '产品描述', '产品图片链接'):
        values = payload.get(field)
        if not isinstance(values, list) or len(values) != row_count:
            raise ValueError(
                f'字段“{field}”无法安全修复：需要 {row_count} 行，实际 '
                f'{len(values) if isinstance(values, list) else "非数组"}'
            )
    variants = payload.get('变种图片链接')
    if not isinstance(variants, list):
        raise ValueError('字段“变种图片链接”无法安全修复：不是数组')
    if len(variants) < row_count:
        missing = row_count - len(variants)
        variants.extend([[] for _ in range(missing)])
        warnings.append(
            f'变种图片链接缺少末尾 {missing} 行，按无变种图补为空数组'
        )
    elif len(variants) != row_count:
        raise ValueError(
            f'字段“变种图片链接”无法安全修复：需要 {row_count} 行，实际 '
            f'{len(variants)}'
        )
    payload['变种图片链接'] = variants
    root = Path(runtime_root) if runtime_root else source.parent / '.runtime'
    destination_dir = root / 'normalized_inputs'
    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    destination = destination_dir / f'{source.stem}_修复副本_{stamp}.json'
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    return destination, warnings


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

    product_ids = payload['商品id']
    blank_rows = [
        index
        for index, product_id in enumerate(product_ids, start=1)
        if not product_id.strip()
    ]
    if blank_rows:
        raise ValueError(
            'Amazon JSON 商品id不能为空，问题行: '
            + ', '.join(str(index) for index in blank_rows[:20])
        )

    id_rows: dict[str, list[int]] = {}
    for index, product_id in enumerate(product_ids, start=1):
        id_rows.setdefault(product_id, []).append(index)
    duplicates = [
        f'{product_id}（第 {", ".join(map(str, rows))} 行）'
        for product_id, rows in id_rows.items()
        if len(rows) > 1
    ]
    if duplicates:
        raise ValueError(
            'Amazon JSON 商品id不能重复: '
            + '；'.join(duplicates[:20])
        )

    if '有问题的产品id' in required_fields:
        overlap = sorted(
            set(product_ids) & set(payload['有问题的产品id'])
        )
        if overlap:
            raise ValueError(
                '有问题的产品id对应商品必须从全部逐行字段删除: '
                + ', '.join(overlap[:20])
            )

    return row_count


def load_columnar_json(path: str, *, max_rows: int | None = None) -> dict:
    """Load and validate an Amazon collection JSON file."""
    try:
        with open(path, encoding='utf-8-sig') as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'文件不是有效的 Amazon JSON: {exc}') from exc
    legacy = '产品站点' not in payload
    validate_columnar_payload(
        payload,
        required_fields=(
            AMAZON_JSON_LEGACY_INPUT_FIELDS
            if legacy else AMAZON_JSON_INPUT_FIELDS
        ),
        max_rows=max_rows,
    )
    if legacy:
        payload = dict(payload)
        payload['产品站点'] = ['US'] * len(payload['商品id'])
        payload['_legacy_site_defaulted'] = True
    else:
        payload['产品站点'] = [
            normalize_market_code(value)
            for value in payload['产品站点']
        ]
    return payload


def _unique_urls(values) -> list[str]:
    return list(dict.fromkeys(
        value.strip()
        for value in values
        if isinstance(value, str) and value.strip()
    ))


def rows_from_payload(
    payload: dict,
    *,
    max_rows: int = 0,
) -> list[dict[str, Any]]:
    """Convert the column-oriented source contract into processing rows."""
    row_count = validate_columnar_payload(
        payload,
        required_fields=AMAZON_JSON_INPUT_FIELDS,
    )
    selected = min(row_count, max_rows) if max_rows else row_count
    rows = []
    for index in range(selected):
        product_images = _unique_urls(payload["产品图片链接"][index])
        variant_images = [
            value.strip()
            for value in payload["变种图片链接"][index]
            if isinstance(value, str) and value.strip()
        ]
        rows.append(
            {
                "id": str(payload["商品id"][index] or ""),
                "site": normalize_market_code(payload["产品站点"][index]),
                "_legacy_site_defaulted": bool(
                    payload.get("_legacy_site_defaulted")
                ),
                "title": str(payload["产品标题"][index] or "").strip(),
                "desc": str(payload["产品描述"][index] or ""),
                "_source_title": str(
                    payload["产品标题"][index] or ""
                ).strip(),
                "_source_desc": str(payload["产品描述"][index] or ""),
                "main_img": product_images[0] if product_images else "",
                "extra_imgs": product_images[1:],
                "var_imgs": variant_images,
                "var_img": variant_images[0] if variant_images else "",
            }
        )
    return rows


def load_rows(
    path: str | os.PathLike[str],
    *,
    max_rows: int = 0,
    safety_limit: int = 10_000,
) -> list[dict[str, Any]]:
    payload = load_columnar_json(str(path), max_rows=safety_limit)
    return rows_from_payload(payload, max_rows=max_rows)


def validate_input_rows(rows: list[dict[str, Any]]) -> None:
    """Reject unusable rows while marking known missing descriptions."""
    if not rows:
        raise ValueError("Amazon 输入表没有商品数据")
    issues = []
    for index, row in enumerate(rows, 1):
        if not str(row.get("title") or "").strip():
            issues.append(f"第 {index} 行缺少产品标题")
        if not re.match(
            r"^https?://",
            str(row.get("main_img") or ""),
            re.IGNORECASE,
        ):
            issues.append(f"第 {index} 行缺少有效的主图 URL")
        if not str(row.get("desc") or "").strip():
            row.setdefault("_quality_issues", []).append(
                {
                    "code": "missing_source_description",
                    "message": "源产品描述为空，已保留空值并标记人工复核",
                }
            )
    if issues:
        suffix = "（仅显示前 20 项）" if len(issues) > 20 else ""
        raise ValueError(
            "Amazon 输入质量检查失败"
            + suffix
            + "："
            + "；".join(issues[:20])
        )


def build_output_payload(
    rows: list[dict],
    *,
    problem_product_ids: list[str] | tuple[str, ...] = (),
) -> dict:
    """Build the exact column order and nested image shape of the output template."""
    normalized_problem_ids = list(
        dict.fromkeys(
            str(value)
            for value in problem_product_ids
            if str(value)
        )
    )
    problem_id_set = set(normalized_problem_ids)
    payload = {
        '商品id': [],
        '产品站点': [],
        '产品标题': [],
        '副标题': [],
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
    for row in rows:
        product_id = str(row.get('id') or '')
        if product_id in problem_id_set:
            continue
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

        payload['商品id'].append(product_id)
        payload['产品站点'].append(
            normalize_market_code(row.get('site') or 'US')
        )
        payload['产品标题'].append(str(row.get('title') or ''))
        payload['副标题'].append(str(row.get('subtitle') or ''))
        payload['产品描述'].append(str(row.get('desc') or ''))
        payload['产品图片链接'].append(product_images)
        payload['变种图片链接'].append(variant_images)
        for index in range(5):
            payload[f'Bullet Point{index + 1}'].append(bullets[index])
        payload['关键词信息'].append(str(row.get('keywords') or ''))
    payload["有问题的产品id"] = normalized_problem_ids
    validate_columnar_payload(
        payload,
        required_fields=AMAZON_JSON_OUTPUT_FIELDS,
    )
    if tuple(payload) != AMAZON_JSON_OUTPUT_FIELDS:
        raise ValueError('Amazon 回填 JSON 字段顺序不符合模板')
    return payload


def write_output_json(
    rows: list[dict],
    output_path: str | os.PathLike[str],
    *,
    problem_product_ids: list[str] | tuple[str, ...] = (),
) -> str:
    """Atomically write the exact Amazon refill contract."""
    payload = build_output_payload(
        rows,
        problem_product_ids=problem_product_ids,
    )
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output.with_suffix(output.suffix + f".{os.getpid()}.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, output)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return str(output)
