#!/usr/bin/env python3
"""Normalize Amazon brand titles with recoverable delivery backups."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.amazon_json import (
    AMAZON_JSON_OUTPUT_FIELDS,
    validate_columnar_payload,
)
from services.amazon_titles import normalize_amazon_title_details


_REVIEW_FILES = (
    '中文文案检查表.json',
    '中文文案检查表.html',
    '翻译缓存.json',
    '图片映射.json',
    '导出说明.txt',
)


def _load_json(path: Path) -> dict:
    with path.open(encoding='utf-8-sig') as handle:
        return json.load(handle)


def _atomic_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(path.suffix + f'.{os.getpid()}.tmp')
    try:
        temp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _payload_hash(payload: dict, *, exclude=()) -> str:
    filtered = {
        key: value
        for key, value in payload.items()
        if key not in set(exclude)
    }
    encoded = json.dumps(
        filtered,
        ensure_ascii=False,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def normalize_payload_titles(payload: dict) -> list[dict]:
    """Mutate only 产品标题 and return an auditable change list."""
    validate_columnar_payload(
        payload,
        required_fields=AMAZON_JSON_OUTPUT_FIELDS,
    )
    if tuple(payload) != AMAZON_JSON_OUTPUT_FIELDS:
        raise ValueError('Amazon 回填 JSON 字段顺序不符合模板')

    changes = []
    for index, (product_id, title) in enumerate(zip(
        payload['商品id'],
        payload['产品标题'],
    )):
        result = normalize_amazon_title_details(title)
        if not result.changed:
            continue
        payload['产品标题'][index] = result.title
        changes.append({
            'row': index + 1,
            'product_id': product_id,
            'before': title,
            'after': result.title,
            'compatibility': result.compatibility,
        })
    return changes


def _backup_delivery_files(
    input_path: Path,
    archive_dir: Path,
    review_dir: Path | None,
) -> list[str]:
    archive_dir.mkdir(parents=True, exist_ok=False)
    backup = archive_dir / f'{input_path.stem}_标题规范前{input_path.suffix}'
    shutil.copy2(input_path, backup)
    copied = [str(backup)]
    if review_dir:
        review_backup = archive_dir / '检查图片文字_标题规范前'
        review_backup.mkdir()
        for name in _REVIEW_FILES:
            source = review_dir / name
            if source.exists():
                destination = review_backup / name
                shutil.copy2(source, destination)
                copied.append(str(destination))
    return copied


def apply_title_normalization(
    input_path: str | Path,
    *,
    archive_root: str | Path,
    review_dir: str | Path | None = None,
    timestamp: str | None = None,
    dry_run: bool = False,
) -> dict:
    input_path = Path(input_path).resolve()
    archive_root = Path(archive_root).resolve()
    review_path = Path(review_dir).resolve() if review_dir else None
    payload = _load_json(input_path)
    original_non_title_hash = _payload_hash(
        payload,
        exclude=('产品标题',),
    )
    changes = normalize_payload_titles(payload)
    normalized_non_title_hash = _payload_hash(
        payload,
        exclude=('产品标题',),
    )
    if original_non_title_hash != normalized_non_title_hash:
        raise RuntimeError('标题规范化意外修改了非标题字段')

    timestamp = timestamp or time.strftime('%Y%m%d_%H%M%S')
    archive_dir = archive_root / f'标题规范_{timestamp}'
    report = {
        'source': str(input_path),
        'rows': len(payload['商品id']),
        'changed_titles': len(changes),
        'non_title_sha256_before': original_non_title_hash,
        'non_title_sha256_after': normalized_non_title_hash,
        'archive_dir': str(archive_dir),
        'dry_run': dry_run,
        'changes': changes,
    }
    if dry_run:
        return report

    backups = _backup_delivery_files(
        input_path,
        archive_dir,
        review_path,
    )
    report['backups'] = backups
    _atomic_json(input_path, payload)
    _atomic_json(archive_dir / '标题变更清单.json', report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('input')
    parser.add_argument('--archive-root', required=True)
    parser.add_argument('--review-dir')
    parser.add_argument('--timestamp')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    report = apply_title_normalization(
        args.input,
        archive_root=args.archive_root,
        review_dir=args.review_dir,
        timestamp=args.timestamp,
        dry_run=args.dry_run,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
