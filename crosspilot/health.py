#!/usr/bin/env python3
"""CrossPilot API 预检 — 启动时检测所有 API 可用性。"""
from __future__ import annotations

import time
import requests
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from crosspilot.model_registry import get_model_registry


_MODEL_DEFAULTS = get_model_registry().as_config()


@dataclass
class HealthResult:
    name: str
    ok: bool
    latency_ms: float
    detail: str = ''


def _ping_deepseek(
    api_key: str,
    base_url: str = _MODEL_DEFAULTS["DEEPSEEK_BASE_URL"],
    model: str = _MODEL_DEFAULTS["DEEPSEEK_TEXT_MODEL"],
) -> HealthResult:
    """检测 DeepSeek 文本 API。"""
    t0 = time.perf_counter()
    try:
        r = requests.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={
                'model': model,
                'messages': [{'role': 'user', 'content': 'ping'}],
                'max_tokens': 5,
                'thinking': {'type': 'disabled'},
            },
            timeout=15,
        )
        lat = (time.perf_counter() - t0) * 1000
        if r.ok:
            return HealthResult('DeepSeek Text', True, lat, f'{lat:.0f}ms')
        return HealthResult('DeepSeek Text', False, lat, f'HTTP {r.status_code}: {r.text[:80]}')
    except Exception as e:
        return HealthResult('DeepSeek Text', False, (time.perf_counter() - t0) * 1000, str(e)[:80])


def _ping_agnes_text(
    api_key: str,
    base_url: str = _MODEL_DEFAULTS["AGNES_TEXT_BASE_URL"],
    model: str = _MODEL_DEFAULTS["AGNES_TEXT_MODEL"],
) -> HealthResult:
    """检测 Agnes 文本 API。"""
    t0 = time.perf_counter()
    try:
        r = requests.post(
            f"{base_url.rstrip('/')}/v1/chat/completions",
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={
                'model': model,
                'messages': [{'role': 'user', 'content': 'ping'}],
                'max_tokens': 5,
            },
            timeout=15,
        )
        lat = (time.perf_counter() - t0) * 1000
        if r.ok:
            return HealthResult('Agnes Text', True, lat, f'{lat:.0f}ms')
        return HealthResult('Agnes Text', False, lat, f'HTTP {r.status_code}: {r.text[:80]}')
    except Exception as e:
        return HealthResult('Agnes Text', False, (time.perf_counter() - t0) * 1000, str(e)[:80])


def _ping_agnes_image(
    api_key: str,
    model: str = _MODEL_DEFAULTS["AGNES_IMAGE_MODEL"],
    base_url: str = _MODEL_DEFAULTS["AGNES_IMAGE_BASE_URL"],
) -> HealthResult:
    """检测 Agnes 生图 API。"""
    t0 = time.perf_counter()
    try:
        r = requests.post(
            f"{base_url.rstrip('/')}/v1/images/generations",
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={
                'model': model,
                'prompt': (
                    'A simple gray cube centered on a pure white background, '
                    'no text, product photography'
                ),
                'size': '1K',
                'ratio': '1:1',
                'extra_body': {'response_format': 'url'},
            },
            timeout=120,
        )
        lat = (time.perf_counter() - t0) * 1000
        if r.ok:
            return HealthResult('Agnes Image Gen', True, lat, f'{lat:.0f}ms')
        return HealthResult('Agnes Image Gen', False, lat, f'HTTP {r.status_code}: {r.text[:80]}')
    except Exception as e:
        return HealthResult('Agnes Image Gen', False, (time.perf_counter() - t0) * 1000, str(e)[:80])


def _ping_gpt_image(
    api_key: str,
    base_url: str = _MODEL_DEFAULTS["GPT_IMAGE_BASE_URL"],
    model: str = _MODEL_DEFAULTS["GPT_IMAGE_MODEL"],
) -> HealthResult:
    """检测 GPT-image-2 备选生图 API。"""
    t0 = time.perf_counter()
    try:
        r = requests.post(
            f"{base_url.rstrip('/')}/v1/images/generations",
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={'model': model, 'prompt': 'ping', 'size': '1024x1024'},
            timeout=120,
        )
        lat = (time.perf_counter() - t0) * 1000
        if r.ok:
            return HealthResult('GPT Image Gen', True, lat, f'{lat:.0f}ms')
        return HealthResult('GPT Image Gen', False, lat, f'HTTP {r.status_code}: {r.text[:80]}')
    except Exception as e:
        return HealthResult('GPT Image Gen', False, (time.perf_counter() - t0) * 1000, str(e)[:80])


def run_health_check(
    deepseek_key: str = '',
    agnes_key: str = '',
    *,
    text_provider: str = 'deepseek',
    vision_provider: str = 'agnes',
    image_provider: str = 'agnes',
    gpt_image_key: str = '',
    deepseek_base_url: str = _MODEL_DEFAULTS["DEEPSEEK_BASE_URL"],
    deepseek_text_model: str = _MODEL_DEFAULTS["DEEPSEEK_TEXT_MODEL"],
    agnes_text_base_url: str = _MODEL_DEFAULTS["AGNES_TEXT_BASE_URL"],
    agnes_text_model: str = _MODEL_DEFAULTS["AGNES_TEXT_MODEL"],
    agnes_vision_base_url: str = _MODEL_DEFAULTS["AGNES_VISION_BASE_URL"],
    agnes_vision_model: str = _MODEL_DEFAULTS["AGNES_VISION_MODEL"],
    agnes_image_model: str = _MODEL_DEFAULTS["AGNES_IMAGE_MODEL"],
    agnes_base_url: str = _MODEL_DEFAULTS["AGNES_IMAGE_BASE_URL"],
    gpt_image_base_url: str = _MODEL_DEFAULTS["GPT_IMAGE_BASE_URL"],
    gpt_image_model: str = _MODEL_DEFAULTS["GPT_IMAGE_MODEL"],
) -> list[HealthResult]:
    """并行检测当前启用的 API，返回结果列表。"""
    if not any((deepseek_key, agnes_key, gpt_image_key)):
        return [HealthResult('No API', False, 0, 'No API keys configured')]

    checks = []
    static_results = []

    if text_provider == 'deepseek' and deepseek_key:
        checks.append((
            _ping_deepseek,
            (deepseek_key, deepseek_base_url, deepseek_text_model),
        ))
    elif text_provider == 'agnes' and not agnes_key:
        static_results.append(
            HealthResult('Agnes Text', False, 0, 'API key not configured')
        )

    if agnes_key and (
        text_provider == 'agnes'
        or vision_provider == 'agnes'
    ):
        if text_provider == 'agnes':
            text_base_url = agnes_text_base_url
            text_model = agnes_text_model
        else:
            text_base_url = agnes_vision_base_url
            text_model = agnes_vision_model
        checks.append((
            _ping_agnes_text,
            (agnes_key, text_base_url, text_model),
        ))

    if image_provider == 'agnes':
        if agnes_key:
            checks.append((
                _ping_agnes_image,
                (agnes_key, agnes_image_model, agnes_base_url),
            ))
        else:
            static_results.append(
                HealthResult(
                    'Agnes Image Gen',
                    False,
                    0,
                    'API key not configured',
                )
            )
    elif image_provider == 'gpt':
        if gpt_image_key:
            checks.append((
                _ping_gpt_image,
                (gpt_image_key, gpt_image_base_url, gpt_image_model),
            ))
        else:
            static_results.append(
                HealthResult(
                    'GPT Image Gen',
                    False,
                    0,
                    'API key not configured',
                )
            )

    results = list(static_results)
    if checks:
        with ThreadPoolExecutor(max_workers=len(checks)) as pool:
            futures = {
                pool.submit(fn, *args): fn
                for fn, args in checks
            }
            for f in as_completed(futures):
                results.append(f.result())

    return results


def run_configured_health_check(
    cfg: dict[str, str] | None = None,
) -> list[HealthResult]:
    """Run health checks with the same effective model config as providers."""
    if cfg is None:
        from crosspilot.config import load_config

        cfg = load_config()
    return run_health_check(
        deepseek_key=cfg.get('DEEPSEEK_KEY', ''),
        agnes_key=cfg.get('AGNES_KEY', ''),
        text_provider=cfg.get('TEXT_PROVIDER', 'deepseek'),
        vision_provider=cfg.get('VISION_PROVIDER', 'agnes'),
        image_provider=cfg.get('IMAGE_PROVIDER', 'agnes'),
        gpt_image_key=cfg.get('GPT_IMAGE_KEY', ''),
        deepseek_base_url=cfg.get('DEEPSEEK_BASE_URL', ''),
        deepseek_text_model=cfg.get('DEEPSEEK_TEXT_MODEL', ''),
        agnes_text_base_url=cfg.get('AGNES_TEXT_BASE_URL', ''),
        agnes_text_model=cfg.get('AGNES_TEXT_MODEL', ''),
        agnes_vision_base_url=cfg.get('AGNES_VISION_BASE_URL', ''),
        agnes_vision_model=cfg.get('AGNES_VISION_MODEL', ''),
        agnes_image_model=cfg.get('AGNES_IMAGE_MODEL', ''),
        agnes_base_url=cfg.get('AGNES_IMAGE_BASE_URL', ''),
        gpt_image_base_url=cfg.get('GPT_IMAGE_BASE_URL', ''),
        gpt_image_model=cfg.get('GPT_IMAGE_MODEL', ''),
    )


def print_health_report(results: list[HealthResult]) -> bool:
    """打印预检报告，返回是否全部通过。"""
    print('\n' + '=' * 55)
    print('  API Health Check')
    print('=' * 55)
    all_ok = True
    for r in results:
        status = 'PASS' if r.ok else 'FAIL'
        symbol = '[OK]' if r.ok else '[!!]'
        print(f'  {symbol} {r.name:<20s} {r.latency_ms:>6.0f}ms  {r.detail}')
        if not r.ok:
            all_ok = False
    print('=' * 55)
    if not all_ok:
        print('  WARNING: Some APIs are unavailable, pipeline will degrade.')
        print('  Failed APIs will trigger fallback: keep originals / rule-based cleanup.')
    print()
    return all_ok
