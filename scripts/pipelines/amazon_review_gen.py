#!/usr/bin/env python3
"""Amazon image review and generation stage."""
from __future__ import annotations

from io import BytesIO
import json
import os
import threading
import time

import requests

from crosspilot.prompt_registry import build_runtime_signature
from concurrency import adaptive_map
from model_provider import ProviderQuotaError, get_provider as _default_get_provider
from pipeline_log import log as _log
from services.constants import IMAGE_POLICY_VERSION
from .amazon_constants import (
    AMAZON_IMAGE_GEN_CONCURRENCY,
    AMAZON_REVIEW_CONCURRENCY,
    _add_audit,
    _add_quality_issue,
    _audit_text,
)

_CACHE_IO_LOCK = threading.Lock()


def _env_enabled(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def _validate_generated_image_url(
    url: str,
    *,
    timeout_s: float = 30,
    max_bytes: int = 25 * 1024 * 1024,
) -> tuple[bool, str]:
    """Download and decode a generated image before accepting its URL."""
    url = str(url or '').strip()
    if not url.startswith(('http://', 'https://')):
        return False, 'invalid_url'
    try:
        response = requests.get(
            url,
            timeout=max(1.0, float(timeout_s)),
            stream=True,
            headers={'User-Agent': 'CrossPilot/1.0'},
        )
        response.raise_for_status()
        content_type = str(
            response.headers.get('content-type') or ''
        ).lower()
        if content_type and 'image/' not in content_type:
            return False, 'non_image_content_type'
        chunks = []
        size = 0
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > max_bytes:
                return False, 'image_too_large'
            chunks.append(chunk)
        if not chunks:
            return False, 'empty_image'
        try:
            from PIL import Image

            with Image.open(BytesIO(b''.join(chunks))) as image:
                image.verify()
                if image.width < 1 or image.height < 1:
                    return False, 'invalid_dimensions'
        except ImportError:
            return False, 'image_decoder_unavailable'
        except Exception:
            return False, 'image_decode_failed'
        return True, ''
    except requests.Timeout:
        return False, 'download_timeout'
    except requests.RequestException:
        return False, 'download_failed'


def _current_image_cache_versions() -> tuple[str, str]:
    review_version = build_runtime_signature(
        IMAGE_POLICY_VERSION,
        "images.review",
    )
    generation_version = build_runtime_signature(
        IMAGE_POLICY_VERSION,
        "images.main_product",
        "images.variant",
        "images.quality_gate",
    )
    return review_version, generation_version


def _atomic_save_cache(cache_path: str, cache: dict) -> None:
    """原子写缓存，带重试（Windows 文件锁兼容）。"""
    if not cache_path:
        return
    with _CACHE_IO_LOCK:
        snapshot = {
            'review_results': dict(cache.get('review_results') or {}),
            'gen_results': dict(cache.get('gen_results') or {}),
            'review_prompt_version': cache.get('review_prompt_version', ''),
            'gen_prompt_version': cache.get('gen_prompt_version', ''),
            'gen_meta': dict(cache.get('gen_meta') or {}),
            'gen_failures': dict(cache.get('gen_failures') or {}),
            'image_policy_version': cache.get('image_policy_version', IMAGE_POLICY_VERSION),
        }
        for attempt in range(3):
            temp_path = (
                cache_path
                + f'.{os.getpid()}.{threading.get_ident()}.{attempt}.tmp'
            )
            try:
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(snapshot, f, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, cache_path)
                return
            except (OSError, IOError) as e:
                if attempt < 2:
                    time.sleep(0.1 * (attempt + 1))
                else:
                    _log.warn('缓存保存失败', error=str(e)[:100])
            finally:
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except OSError:
                    pass

def _stage_review_and_gen(
    data,
    cache_path=None,
    quality_issues=None,
    progress=None,
    runtime_metrics=None,
    provider_getter=None,
):
    """按需修复主图/变种，并删除有风险的附图。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    review_version, generation_version = _current_image_cache_versions()
    quality_issues = quality_issues if quality_issues is not None else []
    runtime_metrics = runtime_metrics if isinstance(runtime_metrics, dict) else {}
    concurrency_stats = runtime_metrics.setdefault('concurrency', {})
    quality_gate_enabled = _env_enabled(
        'CROSSPILOT_IMAGE_QUALITY_GATE',
    )
    remediate_only = _env_enabled(
        'CROSSPILOT_IMAGE_REMEDIATE_ONLY',
    )
    validate_generated_images = _env_enabled(
        'CROSSPILOT_VALIDATE_GENERATED_IMAGE',
    )
    try:
        validation_route_limit = max(
            1,
            min(
                3,
                int(
                    os.environ.get(
                        'CROSSPILOT_IMAGE_VALIDATION_ROUTE_LIMIT',
                        '3',
                    )
                ),
            ),
        )
    except ValueError:
        validation_route_limit = 3
    try:
        quality_regen_limit = max(
            0,
            min(
                3,
                int(
                    os.environ.get(
                        'CROSSPILOT_IMAGE_QUALITY_REGEN_LIMIT',
                        '1',
                    )
                ),
            ),
        )
    except ValueError:
        quality_regen_limit = 1
    gate_metrics = runtime_metrics.setdefault(
        'image_quality_gate',
        {
            'checked': 0,
            'accepted': 0,
            'rejected': 0,
            'unavailable': 0,
            'regenerated': 0,
            'retained_original': 0,
            'reasons': {},
        },
    )
    delivery_metrics = runtime_metrics.setdefault(
        'image_delivery_validation',
        {
            'enabled': validate_generated_images,
            'checked': 0,
            'accepted': 0,
            'rejected': 0,
            'reasons': {},
        },
    )

    main_urls, var_urls, extra_urls = set(), set(), set()
    image_context = {}
    generation_reference = {}
    for row in data:
        title = str(row.get('title') or '').strip()
        variant_images = [
            url for url in row.get('var_imgs', []) if url
        ]
        if row['main_img']:
            main_urls.add(row['main_img'])
            image_context.setdefault(row['main_img'], title)
            title_lower = title.lower()
            is_flat_graphic_product = any(
                token in title_lower
                for token in (
                    'sticker',
                    'decal',
                    'film',
                    'tape',
                    'label',
                    'wrap',
                )
            )
            if is_flat_graphic_product and variant_images:
                generation_reference.setdefault(
                    row['main_img'],
                    variant_images[0],
                )
        var_urls.update(u for u in row.get('var_imgs', []) if u)
        for url in row.get('var_imgs', []):
            if url:
                image_context.setdefault(url, title)

    # Load cache
    cache = {}
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding='utf-8') as f:
                cache = json.load(f) or {}
        except Exception as e:
            _log.warn('缓存读取失败', error=str(e)[:100])

    if cache.get('review_prompt_version') == review_version:
        review_results = cache.get('review_results', {}) or {}
    else:
        review_results = {}
        print("图片策略已更新，旧附图图审缓存已失效", flush=True)
    if cache.get('gen_prompt_version') == generation_version:
        gen_results = cache.get('gen_results', {}) or {}
        gen_meta = cache.get('gen_meta', {}) or {}
        gen_failures = cache.get('gen_failures', {}) or {}
    else:
        gen_results = {}
        gen_meta = {}
        gen_failures = {}
    cache['review_prompt_version'] = review_version
    cache['gen_prompt_version'] = generation_version
    cache['image_policy_version'] = IMAGE_POLICY_VERSION
    cache.setdefault('gen_meta', gen_meta)
    cache.setdefault('gen_failures', gen_failures)
    mem_lock = threading.Lock()

    def _persist():
        with mem_lock:
            cache['review_results'] = {u: r for u, r in review_results.items() if r is not None}
            cache['gen_results'] = dict(gen_results)
            cache['gen_meta'] = dict(gen_meta)
            cache['gen_failures'] = dict(gen_failures)
        _atomic_save_cache(cache_path, cache)

    # Phase 1: 附图总是审；按需修复模式下主图/变种也先审。
    for row in data:
        for u in row.get('extra_imgs', []):
            if u:
                extra_urls.add(u)
    review_urls = set(extra_urls)
    if remediate_only:
        review_urls.update(main_urls)
        review_urls.update(var_urls)
    urls_to_review = [
        u for u in review_urls if u not in review_results
    ]

    def _cached_generation_is_usable(kind, url):
        cache_key = f'{kind}:{url}'
        if cache_key not in gen_results:
            return False
        if not quality_gate_enabled:
            return True
        gate = (gen_meta.get(cache_key) or {}).get('quality_gate') or {}
        return gate.get('accepted') is True

    def _cached_generation_is_rejected(kind, url):
        if not quality_gate_enabled:
            return False
        cache_key = f'{kind}:{url}'
        failure = gen_failures.get(cache_key) or {}
        return (
            failure.get('terminal') is True
            and failure.get('prompt_version') == generation_version
        )

    progress_total = (
        len(urls_to_review) + len(main_urls) + len(var_urls)
    )
    progress_done = 0
    review_cached = len(review_urls) - len(urls_to_review)
    if review_cached:
        print(
            f'图片审图缓存命中: {review_cached}/{len(review_urls)}',
            flush=True,
        )
    if urls_to_review:
        print(
            f'图片审图 {len(urls_to_review)} 张（Agnes {AMAZON_REVIEW_CONCURRENCY} 并发，自适应退避）...',
            flush=True,
        )
        done = none_streak = 0
        quota_hit = False

        def _review_image(url):
            """使用 model_provider 进行图审。"""
            try:
                provider = (provider_getter or _default_get_provider)()
                return provider.call_vision(url)
            except ProviderQuotaError:
                raise
            except Exception as e:
                _log.warn('图审异常', error=str(e)[:100])
                return None

        def _review_done(url, result):
            nonlocal done, none_streak, progress_done
            if isinstance(result, Exception):
                result = None
            with mem_lock:
                if result is not None:
                    review_results[url] = result
                    none_streak = 0
                else:
                    none_streak += 1
                done += 1
            progress_done += 1
            if progress:
                progress(progress_done, max(1, progress_total))
            if result is not None:
                _persist()

        try:
            _review_batch, review_stats = adaptive_map(
                urls_to_review,
                _review_image,
                operation='amazon_review',
                initial_workers=AMAZON_REVIEW_CONCURRENCY,
                min_workers=2,
                is_success=lambda result: result is not None and not isinstance(result, Exception),
                on_result=_review_done,
                terminal_exceptions=(ProviderQuotaError,),
                backoff_s=2,
                max_backoff_s=15,
            )
            concurrency_stats['amazon_review'] = review_stats
            if review_stats.get('reductions'):
                print(
                    f"附图审图并发自适应降级: {review_stats.get('initial_workers')} → "
                    f"{review_stats.get('final_workers')} ({review_stats.get('reductions')} 次)",
                    flush=True,
                )
        except ProviderQuotaError as e:
            quota_hit = True
            print(f'\n[X] 图审已停止: {e}', flush=True)
        if not quota_hit:
            _persist()
        reviewed_ok = sum(
            1 for u in review_urls if u in review_results
        )
        flagged = sum(
            1 for u in review_urls
            if review_results.get(u) is True
        )
        print(
            f'图片审图完成: 已缓存 {reviewed_ok}/{len(review_urls)}, '
            f'需处理 {flagged}',
            flush=True,
        )
    else:
        print(
            f'图片审图全部缓存命中: {len(review_urls)} 张',
            flush=True,
        )
    unreviewed_extra = [u for u in extra_urls if u not in review_results]
    if unreviewed_extra:
        print(f'\n[WARN] 附图仍有 {len(unreviewed_extra)} 张未完成图审，已跳过（继续处理）', flush=True)
        quality_issues.append(
            f'有 {len(unreviewed_extra)} 张附图未完成图审，必须人工复核'
        )

    target_urls = main_urls | var_urls
    if remediate_only:
        remediation_metrics = {
            'reviewed': sum(
                1 for u in target_urls if u in review_results
            ),
            'flagged': sum(
                1 for u in target_urls
                if review_results.get(u) is True
            ),
            'clean_retained': sum(
                1 for u in target_urls
                if review_results.get(u) is False
            ),
            'unknown_retained': sum(
                1 for u in target_urls if u not in review_results
            ),
        }
        runtime_metrics['image_remediation'] = remediation_metrics
        print(
            f"主图 {len(main_urls)} + 变种 {len(var_urls)} 按需修复："
            f"需生图 {remediation_metrics['flagged']}，"
            f"原图通过 {remediation_metrics['clean_retained']}，"
            f"未判定 {remediation_metrics['unknown_retained']}",
            flush=True,
        )
        if remediation_metrics['unknown_retained']:
            quality_issues.append(
                '有 '
                f"{remediation_metrics['unknown_retained']} 张主图/变种图"
                '未完成修复判断，已保留原图并需人工复核'
            )
        expected_main = {
            u for u in main_urls if review_results.get(u) is True
        }
        expected_var = {
            u for u in var_urls if review_results.get(u) is True
        }
    else:
        print(
            f'主图 {len(main_urls)} + 变种 {len(var_urls)} 全部必生',
            flush=True,
        )
        expected_main = set(main_urls)
        expected_var = set(var_urls)

    to_gen_main = [
        u for u in expected_main
        if (
            u
            and not _cached_generation_is_usable('main', u)
            and not _cached_generation_is_rejected('main', u)
        )
    ]
    to_gen_var = [
        u for u in expected_var
        if (
            u
            and not _cached_generation_is_usable('variant', u)
            and not _cached_generation_is_rejected('variant', u)
        )
    ]
    cached_rejections = (
        sum(
            1 for u in expected_main
            if _cached_generation_is_rejected('main', u)
        )
        + sum(
            1 for u in expected_var
            if _cached_generation_is_rejected('variant', u)
        )
    )
    if cached_rejections:
        print(
            f'质量门禁历史拒绝缓存命中: {cached_rejections} 张，'
            '本轮不重复消耗生图请求',
            flush=True,
        )

    # Phase 2: 只生成策略要求修复且尚无可用缓存的主图/变种。
    print(
        f'生图缓存命中: 主图 {len(expected_main)-len(to_gen_main)}/'
        f'{len(expected_main)}, 变种 {len(expected_var)-len(to_gen_var)}/'
        f'{len(expected_var)}',
        flush=True,
    )
    total_to_gen = len(to_gen_main) + len(to_gen_var)
    if total_to_gen:
        print(f'待生成: 主图 {len(to_gen_main)} + 变种 {len(to_gen_var)} = {total_to_gen} (每张实时落盘, 限额不够自动停)', flush=True)
    done = ok = fail_streak = quota_hit = 0

    def _record_gate_result(result):
        with mem_lock:
            gate_metrics['checked'] += 1
            if result and result.get('accepted') is True:
                gate_metrics['accepted'] += 1
                return
            gate_metrics['rejected'] += 1
            reasons = (
                list(result.get('reasons') or [])
                if isinstance(result, dict)
                else ['quality_gate_unavailable']
            )
            if not result:
                gate_metrics['unavailable'] += 1
            if not reasons:
                reasons = ['unspecified_product_mismatch']
            for reason in reasons:
                key = str(reason).strip()[:80] or 'unspecified_product_mismatch'
                gate_metrics['reasons'][key] = (
                    gate_metrics['reasons'].get(key, 0) + 1
                )

    def _record_delivery_result(accepted, reason=''):
        with mem_lock:
            delivery_metrics['checked'] += 1
            if accepted:
                delivery_metrics['accepted'] += 1
                return
            delivery_metrics['rejected'] += 1
            key = str(reason or 'unknown_validation_error')[:80]
            delivery_metrics['reasons'][key] = (
                delivery_metrics['reasons'].get(key, 0) + 1
            )

    def _gen_one(url, kind):
        """使用 model_provider 进行图生图。"""
        try:
            provider = (provider_getter or _default_get_provider)()
            is_variant = kind != 'main'
            reference_url = generation_reference.get(url, url)
            last_gate = None
            last_failure = 'provider_returned_no_url'
            semantic_attempts = (
                quality_regen_limit + 1
                if quality_gate_enabled else 1
            )
            delivery_attempts = (
                validation_route_limit
                if validate_generated_images else 1
            )
            generation_attempts = max(
                semantic_attempts,
                delivery_attempts,
            )
            for generation_attempt in range(generation_attempts):
                generated = (
                    provider.call_image_gen(
                        reference_url,
                        is_variant=is_variant,
                        context=image_context.get(url, ''),
                        route_offset=generation_attempt,
                    )
                    or ''
                )
                if not generated:
                    last_failure = 'provider_returned_no_url'
                    continue
                delivery_result = None
                if validate_generated_images:
                    accepted, reason = _validate_generated_image_url(
                        generated,
                    )
                    delivery_result = {
                        'accepted': accepted,
                        'reason': reason,
                    }
                    _record_delivery_result(accepted, reason)
                    if not accepted:
                        last_failure = reason
                        continue
                if not quality_gate_enabled:
                    return {
                        'url': generated,
                        'quality_gate': None,
                        'delivery_validation': delivery_result,
                    }
                try:
                    last_gate = provider.call_image_quality(
                        reference_url,
                        generated,
                        context=image_context.get(url, ''),
                        is_variant=is_variant,
                    )
                except ProviderQuotaError:
                    raise
                except Exception as e:
                    _log.warn(
                        '图片质量门禁异常',
                        error=str(e)[:100],
                    )
                    last_gate = None
                _record_gate_result(last_gate)
                if (
                    isinstance(last_gate, dict)
                    and last_gate.get('accepted') is True
                ):
                    return {
                        'url': generated,
                        'quality_gate': last_gate,
                        'delivery_validation': delivery_result,
                    }
                if generation_attempt < generation_attempts - 1:
                    with mem_lock:
                        gate_metrics['regenerated'] += 1
            with mem_lock:
                if quality_gate_enabled:
                    gate_metrics['retained_original'] += 1
            return {
                'url': '',
                'quality_gate': last_gate,
                'failure_reason': last_failure,
            }
        except ProviderQuotaError:
            raise
        except Exception as e:
            _log.warn('生图异常', error=str(e)[:100])
            return {
                'url': '',
                'quality_gate': None,
                'failure_reason': type(e).__name__,
            }

    if total_to_gen:
        gen_items = [(u, 'main') for u in to_gen_main]
        gen_items.extend((u, 'variant') for u in to_gen_var)

        def _gen_item(item):
            u, kind = item
            return _gen_one(u, kind)

        def _gen_done(item, r):
            nonlocal done, ok, fail_streak, progress_done
            u, kind = item
            if isinstance(r, Exception):
                r = {'url': '', 'quality_gate': None}
            if not isinstance(r, dict):
                r = {'url': str(r or ''), 'quality_gate': None}
            generated_url = str(r.get('url') or '')
            done += 1
            progress_done += 1
            if generated_url:
                with mem_lock:
                    cache_key = f'{kind}:{u}'
                    gen_results[cache_key] = generated_url
                    gen_meta[cache_key] = {
                        'kind': kind,
                        'prompt_version': generation_version,
                        'ts': int(time.time()),
                    }
                    if isinstance(r.get('quality_gate'), dict):
                        gen_meta[cache_key]['quality_gate'] = dict(
                            r['quality_gate']
                        )
                    if isinstance(r.get('delivery_validation'), dict):
                        gen_meta[cache_key]['delivery_validation'] = dict(
                            r['delivery_validation']
                        )
                    gen_meta[cache_key]['reference_url'] = (
                        generation_reference.get(u, u)
                    )
                    gen_failures.pop(cache_key, None)
                    ok += 1
                    fail_streak = 0
                _persist()
            else:
                with mem_lock:
                    fail_streak += 1
                    gate_result = r.get('quality_gate')
                    if (
                        quality_gate_enabled
                        and isinstance(gate_result, dict)
                        and gate_result.get('accepted') is False
                    ):
                        cache_key = f'{kind}:{u}'
                        gen_failures[cache_key] = {
                            'terminal': True,
                            'kind': kind,
                            'prompt_version': generation_version,
                            'quality_gate': dict(gate_result),
                            'ts': int(time.time()),
                        }
                    elif not quality_gate_enabled:
                        cache_key = f'{kind}:{u}'
                        gen_failures[cache_key] = {
                            'terminal': False,
                            'kind': kind,
                            'prompt_version': generation_version,
                            'reason': str(
                                r.get('failure_reason')
                                or 'generation_failed'
                            )[:120],
                            'ts': int(time.time()),
                        }
            if done % 10 == 0 or (r and done % 5 == 0):
                print(f'  生图: {done}/{total_to_gen} (成功{ok}){" [已缓存]" if generated_url else ""}', flush=True)
            if progress:
                progress(progress_done, max(1, progress_total))

        try:
            _gen_batch, gen_stats = adaptive_map(
                gen_items,
                _gen_item,
                operation='amazon_image_gen',
                initial_workers=AMAZON_IMAGE_GEN_CONCURRENCY,
                min_workers=2,
                is_success=lambda result: (
                    isinstance(result, dict)
                    and bool(result.get('url'))
                ),
                on_result=_gen_done,
                terminal_exceptions=(ProviderQuotaError,),
                backoff_s=2,
                max_backoff_s=15,
            )
            concurrency_stats['amazon_image_gen'] = gen_stats
            if gen_stats.get('reductions'):
                print(
                    f"生图并发自适应降级: {gen_stats.get('initial_workers')} → "
                    f"{gen_stats.get('final_workers')} ({gen_stats.get('reductions')} 次)",
                    flush=True,
                )
        except Exception as qe:
            quota_hit = True
            print(f'\n[X] 生图失败: {qe}。已存 {ok}/{total_to_gen}，续跑可跳过已生成的。', flush=True)
        _persist()
        if not quota_hit:
            print(f'生图完成: {ok}/{total_to_gen}', flush=True)
        if quality_gate_enabled:
            print(
                '图片质量门禁: '
                f"检查 {gate_metrics['checked']}，"
                f"通过 {gate_metrics['accepted']}，"
                f"拒绝 {gate_metrics['rejected']}，"
                f"重生 {gate_metrics['regenerated']}，"
                f"保留原图 {gate_metrics['retained_original']}",
                flush=True,
            )
            if gate_metrics['retained_original']:
                quality_issues.append(
                    '图片质量门禁拒绝 '
                    f"{gate_metrics['retained_original']} 张，"
                    '已保留原图并需人工复核'
                )
    else:
        print('生图全部缓存命中', flush=True)

    # Phase 3: 含水印、品牌覆盖或人物的附图直接删
    deleted = 0
    for row in data:
        before_extra = list(row.get('extra_imgs', []))
        keep = []
        for u in row.get('extra_imgs', []):
            if review_results.get(u) is True:
                deleted += 1
            else:
                keep.append(u)
        row['extra_imgs'] = keep
        if _audit_text(before_extra) != _audit_text(keep):
            _add_audit(
                row,
                '审图+生图',
                'attachment_image',
                before_extra,
                keep,
                method='vision',
                reason='remove_flagged_attachments',
                severity='warning',
                action='确认被删除附图确有水印、人物或品牌风险',
            )
    if deleted:
        print(f'含水印/品牌/人物的附图已删除: {deleted} 张', flush=True)

    _persist()
    missing_main = [
        u for u in expected_main
        if not gen_results.get(f'main:{u}')
    ]
    missing_var = [
        u for u in expected_var
        if not gen_results.get(f'variant:{u}')
    ]
    if missing_main or missing_var:
        issue_prefix = (
            '风险图片修复未通过'
            if remediate_only
            else '图片生成不完整'
        )
        print(
            f'\n[WARN] {issue_prefix}：主图 {len(missing_main)} 张，'
            f'变种图 {len(missing_var)} 张，保留原图继续',
            flush=True,
        )
        quality_issues.append(
            f'{issue_prefix}：主图 {len(missing_main)} 张，'
            f'变种图 {len(missing_var)} 张'
        )
        missing_main_set = set(missing_main)
        missing_var_set = set(missing_var)
        for row in data:
            if row.get('main_img') in missing_main_set:
                _add_quality_issue(
                    row,
                    'main_image_generation_failed',
                    '风险主图生成失败，已保留原图并标记人工复核',
                )
            if any(
                url in missing_var_set
                for url in row.get('var_imgs', [])
            ):
                _add_quality_issue(
                    row,
                    'variant_image_generation_failed',
                    '风险变种图生成失败，已保留原图并标记人工复核',
                )

    remediation_metrics = runtime_metrics.setdefault(
        'image_remediation',
        {},
    )
    remediation_metrics.update({
        'attachment_reviewed': sum(
            1 for url in extra_urls if url in review_results
        ),
        'attachment_flagged': sum(
            1 for url in extra_urls
            if review_results.get(url) is True
        ),
        'attachment_deleted': deleted,
        'generated_main': len(expected_main) - len(missing_main),
        'generated_variant': len(expected_var) - len(missing_var),
        'failed_main': len(missing_main),
        'failed_variant': len(missing_var),
        'generation_url_checked': int(
            delivery_metrics.get('checked') or 0
        ),
        'generation_url_valid': int(
            delivery_metrics.get('accepted') or 0
        ),
        'generation_url_invalid': int(
            delivery_metrics.get('rejected') or 0
        ),
    })

    for row in data:
        if row['main_img']:
            before_main = row['main_img']
            if not remediate_only or before_main in expected_main:
                row['main_img'] = gen_results.get(
                    f'main:{before_main}',
                    before_main,
                )
            if row['main_img'] != before_main:
                _add_audit(
                    row,
                    '审图+生图',
                    'main_image',
                    before_main,
                    row['main_img'],
                    method='image_gen',
                    reason='remediate_main_image',
                    action='抽样确认重生主图无水印、人物、品牌且主体未变形',
                )
        before_var_imgs = list(row.get('var_imgs', []))
        row['var_imgs'] = [
            (
                gen_results.get(f'variant:{u}', u)
                if not remediate_only or u in expected_var
                else u
            )
            for u in row.get('var_imgs', [])
            if u
        ]
        if _audit_text(before_var_imgs) != _audit_text(row['var_imgs']):
            _add_audit(
                row,
                '审图+生图',
                'variant_image',
                before_var_imgs,
                row['var_imgs'],
                method='image_gen',
                reason='remediate_variant_images',
                action='抽样确认变种图重生后仍对应正确款式',
            )
        row['var_img'] = row['var_imgs'][0] if row.get('var_imgs') else ''
        row['extra_imgs'] = [u for u in row.get('extra_imgs', []) if u]
    if progress:
        progress(1, 1)
    return data
