"""Resumable Chinese-copy translation for final-review packages."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import html
import json
from pathlib import Path
import re
import threading

from ..config.prompts import get_prompt_registry
from ..providers import get_provider
from .exporter import _atomic_json, _load_json

_CJK_RE = re.compile(r'[\u3400-\u9fff]')
_TAG_RE = re.compile(r'<[^>]+>')

def _plain_text(value: str) -> str:
    text = html.unescape(str(value or ''))
    text = _TAG_RE.sub(' ', text)
    return re.sub(r'\s+', ' ', text).strip()

def _plain_description(value: str) -> str:
    """Remove markup while preserving intentional description paragraphs."""
    text = html.unescape(str(value or ''))
    text = re.sub(r'<\s*(?:br|/p|/div)\s*/?\s*>', '\n', text, flags=re.IGNORECASE)
    text = _TAG_RE.sub(' ', text)
    lines = [
        re.sub(r'[^\S\r\n]+', ' ', line).strip()
        for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    ]
    normalized = '\n'.join(lines)
    return re.sub(r'\n{3,}', '\n\n', normalized).strip()


def _source_row(payload: dict, index: int) -> dict:
    return {
        'title': _plain_text(payload['产品标题'][index]),
        'subtitle': _plain_text(
            (payload.get('副标题') or [''])[index]
            if index < len(payload.get('副标题') or [])
            else ''
        ),
        'description': _plain_description(payload['产品描述'][index]),
        'bullets': [
            _plain_text(payload[f'Bullet Point{number}'][index])
            for number in range(1, 6)
        ],
        'keywords': _plain_text(payload['关键词信息'][index]),
    }


def _translation_signature(row: dict) -> str:
    payload = {
        "row": row,
        "prompt": get_prompt_registry().signature("review.translation"),
    }
    encoded = json.dumps(
        payload,
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
    subtitle = value.get('subtitle', '')
    description = value.get('description')
    bullets = value.get('bullets')
    keywords = value.get('keywords')
    if not isinstance(title, str) or not title.strip():
        return False
    if not isinstance(subtitle, str):
        return False
    if source.get('subtitle') and not subtitle.strip():
        return False
    if not isinstance(description, str):
        return False
    if source['description'] and not description.strip():
        return False
    if '\n\n' in source['description'] and '\n\n' not in description:
        return False
    source_detail_lines = [
        line for line in source['description'].splitlines()
        if ':' in line
    ]
    translated_detail_lines = [
        line for line in description.splitlines()
        if ':' in line or '：' in line
    ]
    if source_detail_lines and len(translated_detail_lines) < len(source_detail_lines):
        return False
    if not isinstance(bullets, list) or len(bullets) != 5:
        return False
    if any(not isinstance(item, str) for item in bullets):
        return False
    if any(
        source_item and not translated_item.strip()
        for source_item, translated_item in zip(
            source['bullets'],
            bullets,
            strict=True,
        )
    ):
        return False
    if not isinstance(keywords, str):
        return False
    if source['keywords'] and not keywords.strip():
        return False
    combined = ' '.join([
        title,
        subtitle,
        description,
        *bullets,
        keywords,
    ])
    return bool(_CJK_RE.search(combined))


def _translate_row(provider, source: dict) -> dict | None:
    prompt = get_prompt_registry().render(
        "review.translation",
        source_json=json.dumps(source, ensure_ascii=False),
    )
    raw = provider.call_text(prompt, max_tokens=6000)
    value = _extract_json_object(raw)
    if not _valid_translation(source, value):
        return None
    return {
        'title': value['title'].strip(),
        'subtitle': str(value.get('subtitle') or '').strip(),
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



__all__ = [
    "_source_row",
    "_translation_signature",
    "_valid_translation",
    "translate_payload",
]
