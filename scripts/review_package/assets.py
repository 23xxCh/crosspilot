"""Validated image download and shared-cache delivery."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
from io import BytesIO
import os
from pathlib import Path
import re
import shutil
import threading
from urllib.parse import urlparse

import requests

_THREAD_LOCAL = threading.local()
_IMAGE_EXTENSIONS = {
    'JPEG': '.jpg',
    'PNG': '.png',
    'WEBP': '.webp',
    'GIF': '.gif',
    'BMP': '.bmp',
    'TIFF': '.tif',
}

def _session() -> requests.Session:
    session = getattr(_THREAD_LOCAL, 'session', None)
    if session is None:
        session = requests.Session()
        session.headers.update({'User-Agent': 'CrossPilot/1.0'})
        _THREAD_LOCAL.session = session
    return session


def _download_image(
    url: str,
    image_dir: Path,
    *,
    shared_cache_dir: Path | None = None,
    timeout_s: float = 30,
    max_bytes: int = 25 * 1024 * 1024,
) -> dict:
    digest = hashlib.sha256(url.encode('utf-8')).hexdigest()[:24]
    cache_dir = shared_cache_dir or image_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    existing = list(cache_dir.glob(digest + '.*'))
    if existing:
        try:
            from PIL import Image

            with Image.open(existing[0]) as image:
                image.verify()
            target = image_dir / existing[0].name
            if target != existing[0] and not target.exists():
                try:
                    os.link(existing[0], target)
                except OSError:
                    shutil.copy2(existing[0], target)
            return {
                'url': url,
                'ok': True,
                'path': f'图片/{target.name}',
                'bytes': existing[0].stat().st_size,
                'cached': True,
                'shared_cache': shared_cache_dir is not None,
            }
        except Exception:
            existing[0].unlink(missing_ok=True)

    last_error = ''
    for _attempt in range(3):
        try:
            response = _session().get(
                url,
                timeout=timeout_s,
                stream=True,
            )
            response.raise_for_status()
            chunks = []
            size = 0
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError('image_too_large')
                chunks.append(chunk)
            content = b''.join(chunks)
            if not content:
                raise ValueError('empty_image')
            from PIL import Image

            with Image.open(BytesIO(content)) as image:
                image_format = str(image.format or '').upper()
                image.verify()
            extension = _IMAGE_EXTENSIONS.get(
                image_format,
                Path(urlparse(url).path).suffix.lower() or '.img',
            )
            if not re.fullmatch(r'\.[a-z0-9]{2,5}', extension):
                extension = '.img'
            cache_target = cache_dir / f'{digest}{extension}'
            temp = cache_target.with_suffix(cache_target.suffix + '.tmp')
            temp.write_bytes(content)
            os.replace(temp, cache_target)
            target = image_dir / cache_target.name
            if target != cache_target:
                try:
                    os.link(cache_target, target)
                except OSError:
                    shutil.copy2(cache_target, target)
            return {
                'url': url,
                'ok': True,
                'path': f'图片/{target.name}',
                'bytes': len(content),
                'format': image_format,
                'cached': False,
                'shared_cache': shared_cache_dir is not None,
            }
        except Exception as exc:
            last_error = f'{type(exc).__name__}: {exc}'[:200]
    return {
        'url': url,
        'ok': False,
        'path': '',
        'bytes': 0,
        'error': last_error,
        'cached': False,
    }


def download_all_images(
    payload: dict,
    output_dir: Path,
    *,
    workers: int = 32,
    extra_urls: list[str] | None = None,
    shared_cache_dir: Path | None = None,
) -> tuple[dict[str, dict], list[str]]:
    """Download every unique product/variant URL and validate each image."""
    image_dir = output_dir / '图片'
    image_dir.mkdir(parents=True, exist_ok=True)
    urls = list(dict.fromkeys(
        [
            url
            for field in ('产品图片链接', '变种图片链接')
            for images in payload[field]
            for url in images
        ]
        + [
            str(url).strip()
            for url in (extra_urls or [])
            if str(url or '').strip()
        ]
    ))
    mapping: dict[str, dict] = {}
    completed = 0
    print(f'下载全部图片：{len(urls)} 个唯一 URL', flush=True)
    with ThreadPoolExecutor(
        max_workers=max(1, min(workers, len(urls)))
    ) as pool:
        futures = {
            pool.submit(
                _download_image,
                url,
                image_dir,
                shared_cache_dir=shared_cache_dir,
            ): url
            for url in urls
        }
        for future in as_completed(futures):
            result = future.result()
            mapping[result['url']] = result
            completed += 1
            if completed % 100 == 0 or completed == len(urls):
                ok_count = sum(item['ok'] for item in mapping.values())
                print(
                    f'  图片下载 {completed}/{len(urls)}，成功 {ok_count}',
                    flush=True,
                )
    failures = [
        url for url, result in mapping.items()
        if not result.get('ok')
    ]
    return mapping, failures



__all__ = ["download_all_images"]
