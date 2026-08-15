"""Offline review data, image assets, storage, and export orchestration."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil

def prepare_shared_review_cache(review_root: Path) -> dict[str, int | bool]:
    """Seed new shared caches from the legacy one-folder review package."""
    shared_images = review_root / '.共享图片缓存'
    shared_translation = review_root / '.共享缓存' / '中文翻译缓存.json'
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
    if legacy_translation.is_file() and (not shared_translation.exists()):
        try:
            os.link(legacy_translation, shared_translation)
        except OSError:
            shutil.copy2(legacy_translation, shared_translation)
        translation_seeded = True
    return {'linked_images': linked_images, 'translation_seeded': translation_seeded}

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
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _pipeline_provider_metrics(run_metrics: dict | None) -> dict:
    """Expose the pipeline snapshot separately from review translation calls."""
    values = run_metrics if isinstance(run_metrics, dict) else {}
    calls = int(values.get("api_calls") or 0)
    errors = int(values.get("api_errors") or 0)
    return {
        "api_calls": calls,
        "api_errors": errors,
        "api_success_rate": (
            round(1 - errors / calls, 3) if calls else None
        ),
        "http_attempts": int(values.get("http_attempts") or 0),
        "http_errors": int(values.get("http_errors") or 0),
        "http_retries": int(values.get("http_retries") or 0),
        "circuit_open": int(values.get("circuit_open") or 0),
        "fallback_attempts": int(values.get("fallback_attempts") or 0),
        "fallback_successes": int(values.get("fallback_successes") or 0),
        "fallback_failures": int(values.get("fallback_failures") or 0),
        "http_status": dict(values.get("http_status") or {}),
        "by_operation": dict(values.get("api_by_operation") or {}),
    }
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
_IMAGE_EXTENSIONS = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp', 'GIF': '.gif', 'BMP': '.bmp', 'TIFF': '.tif'}

def _session() -> requests.Session:
    session = getattr(_THREAD_LOCAL, 'session', None)
    if session is None:
        session = requests.Session()
        session.headers.update({'User-Agent': 'AmazonProcessor/1.0'})
        _THREAD_LOCAL.session = session
    return session

def _download_image(url: str, image_dir: Path, *, shared_cache_dir: Path | None=None, timeout_s: float=30, max_bytes: int=25 * 1024 * 1024) -> dict:
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
            if target != existing[0] and (not target.exists()):
                try:
                    os.link(existing[0], target)
                except OSError:
                    shutil.copy2(existing[0], target)
            return {'url': url, 'ok': True, 'path': f'图片/{target.name}', 'bytes': existing[0].stat().st_size, 'cached': True, 'shared_cache': shared_cache_dir is not None}
        except Exception:
            existing[0].unlink(missing_ok=True)
    last_error = ''
    for _attempt in range(3):
        try:
            response = _session().get(url, timeout=timeout_s, stream=True)
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
            extension = _IMAGE_EXTENSIONS.get(image_format, Path(urlparse(url).path).suffix.lower() or '.img')
            if not re.fullmatch('\\.[a-z0-9]{2,5}', extension):
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
            return {'url': url, 'ok': True, 'path': f'图片/{target.name}', 'bytes': len(content), 'format': image_format, 'cached': False, 'shared_cache': shared_cache_dir is not None}
        except Exception as exc:
            last_error = f'{type(exc).__name__}: {exc}'[:200]
    return {'url': url, 'ok': False, 'path': '', 'bytes': 0, 'error': last_error, 'cached': False}

def download_all_images(payload: dict, output_dir: Path, *, workers: int=32, extra_urls: list[str] | None=None, shared_cache_dir: Path | None=None) -> tuple[dict[str, dict], list[str]]:
    """Download every unique product/variant URL and validate each image."""
    image_dir = output_dir / '图片'
    image_dir.mkdir(parents=True, exist_ok=True)
    urls = list(dict.fromkeys([url for field in ('产品图片链接', '变种图片链接') for images in payload[field] for url in images] + [str(url).strip() for url in extra_urls or [] if str(url or '').strip()]))
    mapping: dict[str, dict] = {}
    completed = 0
    print(f'下载全部图片：{len(urls)} 个唯一 URL', flush=True)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(urls)))) as pool:
        futures = {pool.submit(_download_image, url, image_dir, shared_cache_dir=shared_cache_dir): url for url in urls}
        for future in as_completed(futures):
            result = future.result()
            mapping[result['url']] = result
            completed += 1
            if completed % 100 == 0 or completed == len(urls):
                ok_count = sum((item['ok'] for item in mapping.values()))
                print(f'  图片下载 {completed}/{len(urls)}，成功 {ok_count}', flush=True)
    failures = [url for url, result in mapping.items() if not result.get('ok')]
    return (mapping, failures)

def _row_images(payload: dict, mapping: dict[str, dict], index: int, audit_images: list[dict] | None=None) -> list[dict]:
    audit_images = audit_images or []

    def audit_for(url: str, role_key: str) -> dict:
        candidates = [item for item in audit_images if item.get('url') == url and item.get('role') == role_key]
        if not candidates:
            return {}
        return candidates[-1]

    def image_record(*, role: str, role_key: str, url: str, position: int) -> dict:
        audit = audit_for(url, role_key)
        assessment = audit.get('assessment') or {}
        text_assessment = audit.get('text_assessment') or {}
        return {'role': role, 'role_key': role_key, 'position': position, 'url': url, 'local_path': (mapping.get(url) or {}).get('path', ''), 'download_ok': bool((mapping.get(url) or {}).get('ok')), 'source': audit.get('source') or 'source', 'source_url': audit.get('source_url') or '', 'source_local_path': (mapping.get(audit.get('source_url')) or {}).get('path', '') if audit.get('source_url') else '', 'assessment': assessment, 'text_assessment': text_assessment, 'source_text_assessment': audit.get('source_text_assessment') or {}, 'image_action': audit.get('image_action') or '', 'source_image_action': audit.get('source_image_action') or '', 'source_detected_text': list(audit.get('source_detected_text') or []), 'detected_text': list(audit.get('detected_text') or text_assessment.get('detected_text') or []), 'text_evidence': audit.get('text_evidence') or text_assessment.get('evidence') or '', 'generation_route_offset': audit.get('generation_route_offset'), 'candidates_reviewed': audit.get('candidates_reviewed'), 'accepted_without_machine_review': bool(audit.get('accepted_without_machine_review')), 'main_eligible': bool(audit.get('main_eligible')), 'original_role': audit.get('original_role') or role_key, 'selection_action': audit.get('selection_action') or '', 'decision': audit.get('decision') or '', 'evidence': audit.get('evidence') or assessment.get('evidence') or ''}
    images = []
    for position, url in enumerate(payload['产品图片链接'][index]):
        role = '主图' if position == 0 else f'附图 {position}'
        images.append(image_record(role=role, role_key='main' if position == 0 else 'attachment', url=url, position=position))
    for position, url in enumerate(payload['变种图片链接'][index], start=1):
        images.append(image_record(role=f'变种图 {position}', role_key='variant', url=url, position=position))
    return images

def build_review_rows(payload: dict, translations: list[dict], mapping: dict[str, dict], audit_by_product: dict[str, list[dict]] | None=None) -> list[dict]:
    audit_by_product = audit_by_product or {}
    rows = []
    for index, product_id in enumerate(payload['商品id']):
        rows.append({
            'row': index + 1,
            'product_id': product_id,
            'site': str(payload['产品站点'][index]),
            **translations[index],
            'localized': {
                'title': str(payload['产品标题'][index]),
                'subtitle': str(payload['副标题'][index]),
                'description': str(payload['产品描述'][index]),
                'bullets': [
                    str(payload[f'Bullet Point{number}'][index])
                    for number in range(1, 6)
                ],
                'keywords': str(payload['关键词信息'][index]),
            },
            'images': _row_images(
                payload,
                mapping,
                index,
                audit_by_product.get(str(product_id), []),
            ),
            'quarantined': False,
            'quarantine_reasons': [],
        })
    return rows

def build_quarantine_rows(quarantine_products: list[dict], mapping: dict[str, dict], *, row_offset: int) -> list[dict]:
    rows = []
    for index, item in enumerate(quarantine_products):
        source_row = item.get('source_row') or {}
        images = []
        for position, image in enumerate(item.get('images') or []):
            url = str(image.get('url') or '')
            if not url:
                continue
            assessment = image.get('assessment') or {}
            text_assessment = image.get('text_assessment') or {}
            images.append({'role': {'main': '主图', 'variant': '变种图', 'attachment': '附图'}.get(image.get('role'), str(image.get('role') or '图片')), 'role_key': image.get('role') or 'attachment', 'position': position, 'url': url, 'local_path': (mapping.get(url) or {}).get('path', ''), 'download_ok': bool((mapping.get(url) or {}).get('ok')), 'source': image.get('source') or 'source', 'source_url': image.get('source_url') or '', 'source_local_path': (mapping.get(image.get('source_url')) or {}).get('path', '') if image.get('source_url') else '', 'assessment': assessment, 'text_assessment': text_assessment, 'source_text_assessment': image.get('source_text_assessment') or {}, 'image_action': image.get('image_action') or '', 'source_image_action': image.get('source_image_action') or '', 'source_detected_text': list(image.get('source_detected_text') or []), 'detected_text': list(image.get('detected_text') or text_assessment.get('detected_text') or []), 'text_evidence': image.get('text_evidence') or text_assessment.get('evidence') or '', 'generation_route_offset': image.get('generation_route_offset'), 'candidates_reviewed': image.get('candidates_reviewed'), 'accepted_without_machine_review': bool(image.get('accepted_without_machine_review')), 'main_eligible': bool(image.get('main_eligible')), 'original_role': image.get('original_role') or image.get('role') or 'attachment', 'selection_action': image.get('selection_action') or '', 'decision': image.get('decision') or '', 'evidence': image.get('evidence') or assessment.get('evidence') or ''})
        bullets = source_row.get('bullets') or []
        localized = {
            'title': str(source_row.get('title') or item.get('title') or ''),
            'subtitle': str(source_row.get('subtitle') or ''),
            'description': str(source_row.get('description') or ''),
            'bullets': [
                str(bullets[i] or '') if i < len(bullets) else ''
                for i in range(5)
            ],
            'keywords': str(source_row.get('keywords') or ''),
        }
        rows.append({
            'row': row_offset + index + 1,
            'product_id': str(item.get('product_id') or ''),
            'site': str(source_row.get('site') or item.get('site') or 'US'),
            **localized,
            'localized': localized,
            'images': images,
            'quarantined': True,
            'quarantine_reasons': item.get('reasons') or [],
        })
    return rows
from pathlib import Path
from ..providers import reload_provider
from ..schema import AMAZON_JSON_OUTPUT_FIELDS, validate_columnar_payload
from .html import render_html
from .translation import translate_payload

def export_review(input_path: str | Path, output_dir: str | Path, *, translate_workers: int=30, download_workers: int=32, audit_by_product: dict[str, list[dict]] | None=None, quarantine_products: list[dict] | None=None, shared_cache_dir: str | Path | None=None, translation_cache_path: str | Path | None=None, run_id: str | None=None, run_metrics: dict | None=None, allow_empty_released: bool=False) -> dict:
    input_path = Path(input_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    audit_by_product = audit_by_product or {}
    quarantine_products = quarantine_products or []
    shared_cache = Path(shared_cache_dir) if shared_cache_dir is not None else None
    translation_cache = Path(translation_cache_path) if translation_cache_path is not None else output_dir / '翻译缓存.json'
    payload = _load_json(input_path)
    empty_released = (
        allow_empty_released
        and all(
            isinstance(payload.get(field), list)
            for field in AMAZON_JSON_OUTPUT_FIELDS
        )
        and not payload.get('商品id')
    )
    row_count = 0 if empty_released else validate_columnar_payload(payload, required_fields=AMAZON_JSON_OUTPUT_FIELDS)
    reload_provider()
    translations, translation_failure_rows, provider_metrics = translate_payload(payload, translation_cache, workers=translate_workers)
    if isinstance(run_metrics, dict):
        # Keep review translation calls separate from the pipeline snapshot;
        # delivery will combine both into one canonical public metric object.
        run_metrics["review_translation_provider_metrics"] = provider_metrics
    extra_urls = []
    for images in audit_by_product.values():
        for image in images:
            extra_urls.extend([image.get('url'), image.get('source_url')])
    for product in quarantine_products:
        for image in product.get('images') or []:
            extra_urls.extend([image.get('url'), image.get('source_url')])
    mapping, image_failures = download_all_images(payload, output_dir, workers=download_workers, extra_urls=extra_urls, shared_cache_dir=shared_cache)
    rows = build_review_rows(payload, translations, mapping, audit_by_product=audit_by_product)
    rows.extend(build_quarantine_rows(quarantine_products, mapping, row_offset=len(rows)))
    image_occurrences = sum((len(row['images']) for row in rows))
    actual_source = str(
        ((run_metrics or {}).get('source') or {}).get('path')
        or input_path.resolve()
    )
    translation_failures = []
    for row_number in translation_failure_rows:
        index = int(row_number) - 1
        if not 0 <= index < len(payload.get("商品id") or []):
            continue
        translation_failures.append({
            "row": int(row_number),
            "product_id": str(payload["商品id"][index]),
            "site": str((payload.get("产品站点") or ["US"] * len(payload["商品id"]))[index]),
            "fields": ["title", "subtitle", "description", "bullets", "keywords"],
            "status": "fallback_source_copy",
            "message": "中文终审翻译未通过校验，终审包暂使用站点原文",
        })
    summary = {
        'version': 2,
        'run_id': run_id or output_dir.name,
        'products': len(rows),
        'released_products': row_count,
        'quarantined_products': len(quarantine_products),
        'problem_product_ids': list(payload.get('有问题的产品id') or []),
        'translation_failures': translation_failures,
        'image_occurrences': image_occurrences,
        'unique_images': len(mapping),
        'downloaded_unique_images': sum(
            (item.get('ok') is True for item in mapping.values())
        ),
        'image_failures': image_failures,
        # Keep the old field for compatibility, while exposing a clear
        # stage-by-stage view for operators and the status manifest.
        'provider_metrics': provider_metrics,
        'provider_metrics_by_stage': {
            'pipeline': _pipeline_provider_metrics(run_metrics),
            'review_translation': provider_metrics,
        },
        'run_metrics': run_metrics or {},
        'source': actual_source,
    }
    _atomic_json(output_dir / '审核数据.json', {'version': 2, 'run_id': summary['run_id'], 'summary': summary, 'products': rows, 'images': mapping})
    (output_dir / '终审包.html').write_text(
        render_html(rows, summary, formal_payload=payload),
        encoding='utf-8',
    )
    return summary
