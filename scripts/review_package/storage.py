"""Shared cache seeding and atomic JSON storage."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

def prepare_shared_review_cache(review_root: Path) -> dict[str, int | bool]:
    """Seed new shared caches from the legacy one-folder review package."""
    shared_images = review_root / '.共享图片缓存'
    shared_translation = (
        review_root / '.共享缓存' / '中文翻译缓存.json'
    )
    shared_images.mkdir(parents=True, exist_ok=True)
    shared_translation.parent.mkdir(parents=True, exist_ok=True)
    linked_images = 0
    legacy_images = review_root / '图片'
    if legacy_images.is_dir():
        for source in legacy_images.iterdir():
            if not source.is_file():
                continue
            target = shared_images / source.name
            if target.exists():
                continue
            try:
                os.link(source, target)
            except OSError:
                shutil.copy2(source, target)
            linked_images += 1
    translation_seeded = False
    legacy_translation = review_root / '翻译缓存.json'
    if legacy_translation.is_file() and not shared_translation.exists():
        try:
            os.link(legacy_translation, shared_translation)
        except OSError:
            shutil.copy2(legacy_translation, shared_translation)
        translation_seeded = True
    return {
        'linked_images': linked_images,
        'translation_seeded': translation_seeded,
    }


def _load_json(path: str | Path) -> dict:
    with open(path, encoding='utf-8-sig') as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f'JSON 顶层必须是对象: {path}')
    return value


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f'.{os.getpid()}.tmp')
    try:
        temp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass



__all__ = ["_atomic_json", "_load_json", "prepare_shared_review_cache"]
