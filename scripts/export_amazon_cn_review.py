#!/usr/bin/env python3
"""Export a Chinese-copy and all-image review package for Amazon JSON."""
from __future__ import annotations

import argparse
import hashlib
import html
from io import BytesIO
import json
import os
from pathlib import Path
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

from model_provider import get_provider, reload_provider
from services.amazon_json import (
    AMAZON_JSON_OUTPUT_FIELDS,
    validate_columnar_payload,
)


_CJK_RE = re.compile(r'[\u3400-\u9fff]')
_TAG_RE = re.compile(r'<[^>]+>')
_THREAD_LOCAL = threading.local()
_IMAGE_EXTENSIONS = {
    'JPEG': '.jpg',
    'PNG': '.png',
    'WEBP': '.webp',
    'GIF': '.gif',
    'BMP': '.bmp',
    'TIFF': '.tif',
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


def _plain_text(value: str) -> str:
    text = html.unescape(str(value or ''))
    text = _TAG_RE.sub(' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _source_row(payload: dict, index: int) -> dict:
    return {
        'title': _plain_text(payload['产品标题'][index]),
        'description': _plain_text(payload['产品描述'][index]),
        'bullets': [
            _plain_text(payload[f'Bullet Point{number}'][index])
            for number in range(1, 6)
        ],
        'keywords': _plain_text(payload['关键词信息'][index]),
    }


def _translation_signature(row: dict) -> str:
    encoded = json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()[:20]


def _extract_json_object(raw: str) -> dict | None:
    raw = str(raw or '').strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
    candidates = [raw]
    start = raw.find('{')
    end = raw.rfind('}')
    if 0 <= start < end:
        candidates.append(raw[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _valid_translation(source: dict, value: dict | None) -> bool:
    if not isinstance(value, dict):
        return False
    title = value.get('title')
    description = value.get('description')
    bullets = value.get('bullets')
    keywords = value.get('keywords')
    if not isinstance(title, str) or not title.strip():
        return False
    if not isinstance(description, str):
        return False
    if source['description'] and not description.strip():
        return False
    if not isinstance(bullets, list) or len(bullets) != 5:
        return False
    if any(not isinstance(item, str) or not item.strip() for item in bullets):
        return False
    if not isinstance(keywords, str) or not keywords.strip():
        return False
    combined = ' '.join([
        title,
        description,
        *bullets,
        keywords,
    ])
    return bool(_CJK_RE.search(combined))


def _translate_row(provider, source: dict) -> dict | None:
    prompt = (
        '你是跨境电商商品文案翻译员。把下面 JSON 中所有英文商品文案'
        '翻译成简体中文，仅翻译文字，不增加、删除或猜测产品事实。'
        '数字、尺寸、数量、型号、单位和兼容关系必须原样保留。'
        '关键词翻译成中文搜索词，并保持逗号分隔。'
        '必须只返回一个 JSON 对象，字段严格为 '
        'title、description、bullets、keywords；'
        'bullets 必须恰好 5 条，不要 Markdown，不要解释。\n\n'
        + json.dumps(source, ensure_ascii=False)
    )
    raw = provider.call_text(prompt, max_tokens=6000)
    value = _extract_json_object(raw)
    if not _valid_translation(source, value):
        return None
    return {
        'title': value['title'].strip(),
        'description': value['description'].strip(),
        'bullets': [str(item).strip() for item in value['bullets']],
        'keywords': value['keywords'].strip(),
    }


def translate_payload(
    payload: dict,
    cache_path: Path,
    *,
    workers: int = 30,
) -> tuple[list[dict], list[int], dict]:
    """Translate all rows with a resumable per-row cache."""
    cache = (
        _load_json(cache_path)
        if cache_path.exists()
        else {'version': 1, 'rows': {}}
    )
    cache_rows = cache.setdefault('rows', {})
    sources = [_source_row(payload, index) for index in range(
        len(payload['商品id'])
    )]
    results: list[dict | None] = [None] * len(sources)
    pending = []
    for index, source in enumerate(sources):
        signature = _translation_signature(source)
        cached = cache_rows.get(str(index)) or {}
        translation = cached.get('translation')
        if (
            cached.get('signature') == signature
            and _valid_translation(source, translation)
        ):
            results[index] = translation
        else:
            pending.append((index, source, signature))

    print(
        f'中文翻译缓存命中 {len(sources) - len(pending)}/'
        f'{len(sources)}，待翻译 {len(pending)} 条',
        flush=True,
    )
    lock = threading.Lock()
    completed = 0

    def worker(item):
        index, source, signature = item
        provider = get_provider()
        last_error = ''
        for _attempt in range(3):
            try:
                translated = _translate_row(provider, source)
                if translated:
                    return index, signature, translated, ''
                last_error = '模型返回格式或中文内容不合格'
            except Exception as exc:
                last_error = f'{type(exc).__name__}: {exc}'[:200]
        return index, signature, None, last_error

    if pending:
        with ThreadPoolExecutor(
            max_workers=max(1, min(workers, len(pending)))
        ) as pool:
            futures = {
                pool.submit(worker, item): item
                for item in pending
            }
            for future in as_completed(futures):
                index, signature, translated, error = future.result()
                with lock:
                    completed += 1
                    if translated:
                        results[index] = translated
                        cache_rows[str(index)] = {
                            'signature': signature,
                            'translation': translated,
                        }
                    else:
                        cache_rows.pop(str(index), None)
                    if completed % 20 == 0 or completed == len(pending):
                        print(
                            f'  中文翻译 {completed}/{len(pending)}',
                            flush=True,
                        )
                    _atomic_json(cache_path, cache)
                if error:
                    print(
                        f'  [WARN] 第 {index + 1} 行翻译失败: {error}',
                        flush=True,
                    )

    failures = [
        index + 1
        for index, result in enumerate(results)
        if result is None
    ]
    metrics = {}
    provider = get_provider()
    if hasattr(provider, 'metrics_snapshot'):
        metrics = provider.metrics_snapshot()
    return [
        result or sources[index]
        for index, result in enumerate(results)
    ], failures, metrics


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
    timeout_s: float = 30,
    max_bytes: int = 25 * 1024 * 1024,
) -> dict:
    digest = hashlib.sha256(url.encode('utf-8')).hexdigest()[:24]
    existing = list(image_dir.glob(digest + '.*'))
    if existing:
        try:
            from PIL import Image

            with Image.open(existing[0]) as image:
                image.verify()
            return {
                'url': url,
                'ok': True,
                'path': f'图片/{existing[0].name}',
                'bytes': existing[0].stat().st_size,
                'cached': True,
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
            target = image_dir / f'{digest}{extension}'
            temp = target.with_suffix(target.suffix + '.tmp')
            temp.write_bytes(content)
            os.replace(temp, target)
            return {
                'url': url,
                'ok': True,
                'path': f'图片/{target.name}',
                'bytes': len(content),
                'format': image_format,
                'cached': False,
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
) -> tuple[dict[str, dict], list[str]]:
    """Download every unique product/variant URL and validate each image."""
    image_dir = output_dir / '图片'
    image_dir.mkdir(parents=True, exist_ok=True)
    urls = list(dict.fromkeys(
        url
        for field in ('产品图片链接', '变种图片链接')
        for images in payload[field]
        for url in images
    ))
    mapping: dict[str, dict] = {}
    completed = 0
    print(f'下载全部图片：{len(urls)} 个唯一 URL', flush=True)
    with ThreadPoolExecutor(
        max_workers=max(1, min(workers, len(urls)))
    ) as pool:
        futures = {
            pool.submit(_download_image, url, image_dir): url
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


def _row_images(
    payload: dict,
    mapping: dict[str, dict],
    index: int,
) -> list[dict]:
    images = []
    for position, url in enumerate(payload['产品图片链接'][index]):
        role = '主图' if position == 0 else f'附图 {position}'
        images.append({
            'role': role,
            'url': url,
            'local_path': (mapping.get(url) or {}).get('path', ''),
            'download_ok': bool((mapping.get(url) or {}).get('ok')),
        })
    for position, url in enumerate(
        payload['变种图片链接'][index],
        start=1,
    ):
        images.append({
            'role': f'变种图 {position}',
            'url': url,
            'local_path': (mapping.get(url) or {}).get('path', ''),
            'download_ok': bool((mapping.get(url) or {}).get('ok')),
        })
    return images


def build_review_rows(
    payload: dict,
    translations: list[dict],
    mapping: dict[str, dict],
) -> list[dict]:
    return [
        {
            'row': index + 1,
            'product_id': payload['商品id'][index],
            **translations[index],
            'images': _row_images(payload, mapping, index),
        }
        for index in range(len(payload['商品id']))
    ]


def render_html(rows: list[dict], summary: dict) -> str:
    cards = []
    for row in rows:
        image_cards = []
        for image in row['images']:
            role = html.escape(image['role'])
            if image['download_ok']:
                src = html.escape(image['local_path'].replace('\\', '/'))
                image_cards.append(
                    '<figure class="image">'
                    f'<img loading="lazy" src="{src}" alt="{role}">'
                    f'<figcaption>{role}</figcaption>'
                    '</figure>'
                )
            else:
                remote = html.escape(image['url'])
                image_cards.append(
                    '<figure class="image missing">'
                    f'<a href="{remote}">图片下载失败，打开原链接</a>'
                    f'<figcaption>{role}</figcaption>'
                    '</figure>'
                )
        bullets = ''.join(
            f'<li>{html.escape(item)}</li>'
            for item in row['bullets']
        )
        cards.append(
            f'<article class="product" id="row-{row["row"]}" '
            f'data-search="{html.escape(row["title"].lower())}">'
            '<header>'
            f'<span class="row">第 {row["row"]} 行</span>'
            f'<span class="id">商品 ID：{html.escape(row["product_id"])}</span>'
            '</header>'
            f'<h2>{html.escape(row["title"])}</h2>'
            '<section><h3>产品描述</h3>'
            f'<p class="description">{html.escape(row["description"])}</p>'
            '</section>'
            f'<section><h3>五点描述</h3><ol>{bullets}</ol></section>'
            '<section><h3>关键词</h3>'
            f'<p>{html.escape(row["keywords"])}</p></section>'
            '<section><h3>全部图片</h3>'
            f'<div class="images">{"".join(image_cards)}</div></section>'
            '</article>'
        )
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Amazon 中文文案与图片检查表</title>
<style>
body{{margin:0;background:#f4f6f8;color:#17202a;font:15px/1.65 Arial,"Microsoft YaHei",sans-serif}}
.toolbar{{position:sticky;top:0;z-index:5;padding:14px 22px;background:#17202a;color:white;box-shadow:0 2px 10px #0003}}
.toolbar h1{{display:inline;margin:0 20px 0 0;font-size:20px}}
.toolbar input{{width:min(420px,55vw);padding:9px 12px;border:0;border-radius:6px}}
.summary{{max-width:1500px;margin:18px auto;padding:0 20px;color:#445}}
.product{{max-width:1460px;margin:18px auto;padding:22px;background:white;border-radius:12px;box-shadow:0 2px 12px #2232}}
.product header{{display:flex;gap:18px;color:#667;font-size:13px}}
h2{{margin:10px 0 16px;font-size:21px}} h3{{margin:14px 0 6px;font-size:15px;color:#425}}
.description{{white-space:pre-wrap}} ol{{margin-top:5px}}
.images{{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:14px}}
.image{{margin:0;border:1px solid #dde3e8;border-radius:9px;overflow:hidden;background:#fafafa}}
.image img{{display:block;width:100%;height:190px;object-fit:contain;background:white}}
.image figcaption{{padding:7px 10px;font-weight:700;text-align:center}}
.missing{{min-height:120px;display:grid;place-items:center;padding:12px}}
</style>
</head>
<body>
<div class="toolbar"><h1>Amazon 中文文案与图片检查表</h1>
<input id="search" placeholder="输入中文标题搜索"></div>
<div class="summary">商品 {summary["products"]} 个；图片引用 {summary["image_occurrences"]} 个；
本地图片 {summary["downloaded_unique_images"]}/{summary["unique_images"]} 个。</div>
<main>{''.join(cards)}</main>
<script>
document.getElementById('search').addEventListener('input',function(){{
 const q=this.value.trim().toLowerCase();
 document.querySelectorAll('.product').forEach(x=>x.hidden=q&&!x.dataset.search.includes(q));
}});
</script>
</body>
</html>'''


def export_review(
    input_path: str | Path,
    output_dir: str | Path,
    *,
    translate_workers: int = 30,
    download_workers: int = 32,
) -> dict:
    input_path = Path(input_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = _load_json(input_path)
    row_count = validate_columnar_payload(
        payload,
        required_fields=AMAZON_JSON_OUTPUT_FIELDS,
    )
    reload_provider()
    translations, translation_failures, provider_metrics = (
        translate_payload(
            payload,
            output_dir / '翻译缓存.json',
            workers=translate_workers,
        )
    )
    mapping, image_failures = download_all_images(
        payload,
        output_dir,
        workers=download_workers,
    )
    rows = build_review_rows(payload, translations, mapping)
    image_occurrences = sum(
        len(row['images']) for row in rows
    )
    summary = {
        'products': row_count,
        'translation_failures': translation_failures,
        'image_occurrences': image_occurrences,
        'unique_images': len(mapping),
        'downloaded_unique_images': sum(
            item.get('ok') is True for item in mapping.values()
        ),
        'image_failures': image_failures,
        'provider_metrics': provider_metrics,
        'source': str(input_path.resolve()),
    }
    _atomic_json(output_dir / '中文文案检查表.json', {
        'summary': summary,
        'products': rows,
    })
    _atomic_json(output_dir / '图片映射.json', {
        'summary': {
            'image_occurrences': image_occurrences,
            'unique_images': len(mapping),
            'downloaded_unique_images': summary[
                'downloaded_unique_images'
            ],
            'image_failures': image_failures,
        },
        'images': mapping,
    })
    (output_dir / '中文文案检查表.html').write_text(
        render_html(rows, summary),
        encoding='utf-8',
    )
    (output_dir / '导出说明.txt').write_text(
        '\n'.join([
            'Amazon 中文文案与全图片检查包',
            f'商品数：{row_count}',
            f'图片引用数：{image_occurrences}',
            f'唯一图片数：{len(mapping)}',
            (
                '成功下载唯一图片：'
                f'{summary["downloaded_unique_images"]}'
            ),
            f'翻译失败行：{translation_failures or "无"}',
            f'图片失败 URL 数：{len(image_failures)}',
            '请用浏览器打开“中文文案检查表.html”检查。',
            '本目录为检查副本，未修改正式回填表。',
        ]),
        encoding='utf-8',
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('input')
    parser.add_argument('output_dir')
    parser.add_argument('--translate-workers', type=int, default=30)
    parser.add_argument('--download-workers', type=int, default=32)
    args = parser.parse_args()
    summary = export_review(
        args.input,
        args.output_dir,
        translate_workers=args.translate_workers,
        download_workers=args.download_workers,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if (
        summary['translation_failures']
        or summary['image_failures']
    ):
        raise SystemExit(2)


if __name__ == '__main__':
    main()
