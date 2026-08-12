"""Image remediation and the fail-closed Amazon image-safety gate."""
from __future__ import annotations
import json
import time
from threading import Lock
from typing import Any, Callable
from .risk import (
    assessment_status,
    image_action,
    image_requires_edit,
    unknown_image_assessment,
    unknown_main_text_assessment,
)
from ..concurrency import adaptive_map
from ..providers.support import (
    ProviderCircuitOpenError,
    ProviderQuotaError,
)
from ..quality import AMAZON_IMAGE_GEN_CONCURRENCY
from ..config.prompts import get_prompt_registry
from .risk import safe_assess, validate_image_url
from .cache import (
    is_current_assessment,
    save_cache,
)


def _generated_image_review_mode() -> str:
    from ..config.env import get

    return str(get("GENERATED_IMAGE_REVIEW_MODE", "strict")).strip().lower()


def _image_processing_mode() -> str:
    import os
    from ..config.env import get

    mode = str(
        os.environ.get("IMAGE_PROCESSING_MODE")
        or get("IMAGE_PROCESSING_MODE", "select_existing")
    ).strip().lower()
    if mode not in {
        "select_existing",
        "generate_replacements",
        "regenerate_all_localized",
    }:
        return "select_existing"
    return mode


def _generation_role(kind: str) -> str:
    """Extract the image role from a cache kind with an optional locale."""
    return str(kind or '').split('|', 1)[0].strip() or 'attachment'


def _generation_locale(kind: str) -> str:
    parts = str(kind or '').split('|', 1)
    return parts[1].strip() if len(parts) == 2 else ''

def cached_generation(cache: dict[str, Any], generation_version: str, kind: str, source_url: str, main_text_version: str | None=None) -> str:
    """Return only a current generated URL that passed structured review."""
    key = f'{kind}:{source_url}'
    generated = str(cache['gen_results'].get(key) or '')
    meta = cache['gen_meta'].get(key) or {}
    if (
        _generated_image_review_mode() == "human_review"
        and generated
        and meta.get('prompt_version') == generation_version
        and meta.get('accepted_without_machine_review') is True
    ):
        return generated
    generated_assessment = meta.get('risk_assessment')
    if not (
        generated
        and meta.get('prompt_version') == generation_version
        and is_current_assessment(generated_assessment)
        and assessment_status(generated_assessment) == 'safe'
    ):
        return ''
    return generated

def _generation_attempt_plan(
    image_route: str | None = None,
) -> list[tuple[int, str]]:
    from ..config.env import get_int
    limit = max(1, min(10, get_int('IMAGE_SAFETY_REGEN_LIMIT', 3)))
    if image_route == 'gpt':
        return [(0, 'precise') for _ in range(limit)]
    if image_route == 'agnes':
        return [
            (route_offset, 'precise')
            for route_offset in range(min(limit, 2))
        ]
    return [(route_offset, 'precise') for route_offset in range(limit)]


def _generation_provider_route(
    cache: dict[str, Any],
    source_url: str,
    kind: str = '',
) -> str:
    """Use GPT for translation and Agnes for local removal/no-op edits."""
    assessment = cache.get('risk_assessments', {}).get(source_url) or {}
    if image_action(assessment) == 'edit_translate':
        return 'gpt'
    locale = _generation_locale(kind).lower()
    detected_text = ' '.join(
        str(value or '').strip()
        for value in assessment.get('detected_text') or []
    )
    has_letters = any(character.isalpha() for character in detected_text)
    is_english = locale == 'en' or locale.startswith('en-')
    if locale and not is_english and has_letters:
        return 'gpt'
    if is_english and any(
        character.isalpha() and not character.isascii()
        for character in detected_text
    ):
        return 'gpt'
    return 'agnes'


def _assessment_feedback(assessment: object) -> str:
    if not isinstance(assessment, dict):
        return ''
    reasons = ', '.join(
        str(value).strip()
        for value in (
            assessment.get('risk_categories')
            or assessment.get('reasons')
            or []
        )
        if str(value).strip()
    )
    detected = ', '.join(
        repr(str(value).strip())
        for value in assessment.get('detected_text') or []
        if str(value).strip()
    )
    evidence = str(assessment.get('evidence') or '').strip()
    return '; '.join(
        value
        for value in (
            f'remaining risks: {reasons}' if reasons else '',
            f'remaining text: {detected}' if detected else '',
            f'latest review: {evidence}' if evidence else '',
        )
        if value
    )


def _standalone_product_title(title: str) -> str:
    import re
    text = str(title or '').strip()
    text = re.sub(r'^generic\s+', '', text, flags=re.IGNORECASE)
    subject = re.split(r'\s+for\s+', text, maxsplit=1, flags=re.IGNORECASE)[0]
    subject = re.sub(
        r'\b(?:car|auto|automotive|vehicle)\b',
        ' ',
        subject,
        flags=re.IGNORECASE,
    )
    subject = re.sub(r'\s+', ' ', subject).strip(' ,;-')
    quantities = re.findall(
        r'\b(?:\d+\s*(?:pcs?|pack)|\d+x|x\d+)\b',
        text,
        flags=re.IGNORECASE,
    )
    if quantities and not any(
        value.lower() in subject.lower()
        for value in quantities
    ):
        subject += ', ' + quantities[0]
    return subject[:120]


def _removal_context(
    cache: dict[str, Any],
    source_url: str,
    *,
    kind: str = 'main',
    listing_title: str = '',
    strategy: str = 'precise',
    previous_rejection: str = '',
) -> str:
    role = _generation_role(kind)
    locale = _generation_locale(kind)
    general = cache.get('risk_assessments', {}).get(source_url) or {}
    text_review = (
        cache.get('main_text_assessments', {}).get(source_url) or {}
    )
    detected_text = [
        str(value).strip()
        for value in text_review.get('detected_text') or []
        if str(value).strip()
    ]
    for value in general.get('detected_text') or []:
        text = str(value).strip()
        if text and text not in detected_text:
            detected_text.append(text)
    categories = [
        str(value).strip()
        for value in (
            general.get('risk_categories')
            or general.get('reasons')
            or []
        )
        if str(value).strip()
    ]
    evidence = str(
        general.get('evidence')
        or text_review.get('evidence')
        or ''
    ).strip()
    localized_mode = (
        _image_processing_mode() == 'regenerate_all_localized'
    )
    if role == 'main' and not localized_mode:
        edit_policy = 'main_zero_text'
    elif role == 'main':
        edit_policy = 'main_localized'
    else:
        edit_policy = 'attachment_localized'
    title_text = str(listing_title or '').strip()[:120]
    standalone = (
        _standalone_product_title(title_text)
        if strategy in {'rebuild', 'minimal', 'fallback'}
        else ''
    )
    return get_prompt_registry().render(
        'images.edit_request',
        listing_title=title_text,
        standalone_item=standalone,
        image_role=role,
        edit_policy=edit_policy,
        edit_strategy=strategy,
        target_locale=locale or 'not specified',
        detected_text_json=json.dumps(
            detected_text[:12], ensure_ascii=False
        ),
        risk_categories_json=json.dumps(categories, ensure_ascii=False),
        review_evidence=evidence,
        previous_rejection=str(previous_rejection).strip(),
    )


def _is_transient_generation_failure(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    if int(record.get('candidates_reviewed') or 0) > 0:
        return False
    ignored = {
        'ProviderQuotaError:gpt',
        'route_disabled_after_quota',
    }
    transient = {
        'ProviderUnavailableError',
        'ProviderTimeoutError',
        'ProviderCircuitOpenError',
    }
    errors = [
        str(attempt.get('error') or '')
        for attempt in record.get('attempts') or []
        if isinstance(attempt, dict)
        and str(attempt.get('error') or '')
        and str(attempt.get('error') or '') not in ignored
    ]
    return bool(errors) and all(error in transient for error in errors)


def generate_safe_replacements(targets: list[tuple[str, str]], *, cache: dict[str, Any], cache_path: str | None, generation_version: str, main_text_version: str, provider_getter: Callable[[], object], concurrency_stats: dict[str, Any], listing_contexts: dict[tuple[str, str], str] | None=None) -> list[tuple[str, str]]:
    """Generate missing replacements and accept only decoded, reviewed images."""
    targets_to_generate = [item for item in targets if not cached_generation(cache, generation_version, item[1], item[0], main_text_version)]
    if not targets_to_generate:
        return targets_to_generate
    listing_contexts = listing_contexts or {}
    disabled_routes: set[tuple[str, int]] = set()
    disabled_routes_lock = Lock()

    def generate_one(item: tuple[str, str]) -> dict[str, Any]:
        source_url, kind = item
        role = _generation_role(kind)
        provider_route = _generation_provider_route(cache, source_url, kind)
        generation_plan = _generation_attempt_plan(provider_route)
        provider = provider_getter()
        last_reason = 'generation_failed'
        last_assessment = unknown_image_assessment('no generated candidate')
        last_text_assessment = unknown_main_text_assessment(
            'no generated candidate'
        )
        candidates_reviewed = 0
        attempts = []
        previous_rejection = ''
        for attempt_index, (route_offset, strategy) in enumerate(
            generation_plan
        ):
            reference_free = role == 'main' and strategy in {
                'rebuild',
                'minimal',
                'fallback',
            }
            with disabled_routes_lock:
                route_disabled = (
                    provider_route,
                    route_offset,
                ) in disabled_routes
            if route_disabled:
                attempts.append({
                    'attempt_index': attempt_index,
                    'route_offset': route_offset,
                    'strategy': strategy,
                    'reference_free': reference_free,
                    'provider_route': provider_route,
                    'error': 'route_disabled_after_quota',
                })
                continue
            context = _removal_context(
                cache,
                source_url,
                kind=kind,
                listing_title=listing_contexts.get((source_url, kind), ''),
                strategy=strategy,
                previous_rejection=previous_rejection,
            )
            try:
                call_kwargs: dict[str, Any] = {}
                if provider_route == 'gpt':
                    call_kwargs['prompt_override'] = context
                generated = str(provider.call_image_gen(
                    source_url,
                    is_variant=role in {'variant', 'attachment'},
                    context=context,
                    route_offset=route_offset,
                    reference_free=reference_free,
                    image_route=provider_route,
                    **call_kwargs,
                ) or '')
            except ProviderCircuitOpenError:
                raise
            except ProviderQuotaError as exc:
                if str(getattr(exc, 'provider', '')).lower() == 'gpt':
                    with disabled_routes_lock:
                        disabled_routes.add((provider_route, route_offset))
                    attempts.append({
                        'attempt_index': attempt_index,
                        'route_offset': route_offset,
                        'strategy': strategy,
                        'reference_free': reference_free,
                        'provider_route': provider_route,
                        'error': 'ProviderQuotaError:gpt',
                    })
                    if not candidates_reviewed:
                        last_reason = 'ProviderQuotaError:gpt'
                    continue
                raise
            except Exception as exc:
                last_reason = type(exc).__name__
                attempts.append({
                    'attempt_index': attempt_index,
                    'route_offset': route_offset,
                    'strategy': strategy,
                    'reference_free': reference_free,
                    'provider_route': provider_route,
                    'error': last_reason,
                })
                continue
            valid, reason = validate_image_url(generated)
            if not valid:
                last_reason = reason
                attempts.append({
                    'attempt_index': attempt_index,
                    'route_offset': route_offset,
                    'strategy': strategy,
                    'reference_free': reference_free,
                    'provider_route': provider_route,
                    'candidate_url': generated,
                    'error': reason,
                })
                continue
            if _generated_image_review_mode() == "human_review":
                last_assessment = unknown_image_assessment(
                    "generated image accepted for human review without "
                    "machine recheck"
                )
                if role == 'main':
                    last_text_assessment = unknown_main_text_assessment(
                        "generated main image accepted for human review "
                        "without machine text recheck"
                    )
                attempts.append({
                    'attempt_index': attempt_index,
                    'route_offset': route_offset,
                    'strategy': strategy,
                    'reference_free': reference_free,
                    'provider_route': provider_route,
                    'candidate_url': generated,
                    'accepted_without_machine_review': True,
                })
                return {
                    'url': generated,
                    'assessment': last_assessment,
                    'text_assessment': (
                        last_text_assessment if role == 'main' else None
                    ),
                    'strategy': strategy,
                    'reference_free': reference_free,
                    'route_offset': route_offset,
                    'provider_route': provider_route,
                    'candidates_reviewed': 0,
                    'attempts': attempts,
                    'accepted_without_machine_review': True,
                }
            last_assessment = safe_assess(provider, generated)
            if assessment_status(last_assessment) == 'unknown':
                time.sleep(1)
                last_assessment = safe_assess(provider, generated)
            candidates_reviewed += 1
            if assessment_status(last_assessment) != 'safe':
                last_reason = (
                    'generated_image_'
                    + assessment_status(last_assessment)
                )
                previous_rejection = _assessment_feedback(last_assessment)
                attempts.append({
                    'attempt_index': attempt_index,
                    'route_offset': route_offset,
                    'strategy': strategy,
                    'reference_free': reference_free,
                    'provider_route': provider_route,
                    'candidate_url': generated,
                    'reason': last_reason,
                    'risk_assessment': last_assessment,
                })
                continue
            return {
                'url': generated,
                'assessment': last_assessment,
                    'text_assessment': (
                    last_text_assessment if role == 'main' else None
                ),
                'strategy': strategy,
                'reference_free': reference_free,
                'route_offset': route_offset,
                'provider_route': provider_route,
                'candidates_reviewed': candidates_reviewed,
                'attempts': attempts,
            }
        return {
            'url': '',
            'assessment': last_assessment,
            'text_assessment': (
                last_text_assessment if kind == 'main' else None
            ),
            'failure_reason': last_reason,
            'candidates_reviewed': candidates_reviewed,
            'provider_route': provider_route,
            'attempts': attempts,
        }

    def generate_done(item: tuple[str, str], result: object) -> None:
        source_url, kind = item
        role = _generation_role(kind)
        locale = _generation_locale(kind)
        key = f'{kind}:{source_url}'
        if isinstance(result, Exception) or not isinstance(result, dict):
            result = {'url': '', 'assessment': unknown_image_assessment('generation worker failed'), 'failure_reason': 'generation_worker_failed'}
        generated = str(result.get('url') or '')
        if generated:
            cache['gen_results'][key] = generated
            cache['gen_meta'][key] = {'kind': kind, 'role': role, 'locale': locale, 'source_url': source_url, 'prompt_version': generation_version, 'main_text_prompt_version': main_text_version if role == 'main' else '', 'risk_assessment': result['assessment'], 'text_assessment': result.get('text_assessment'), 'strategy': str(result.get('strategy') or 'precise'), 'reference_free': bool(result.get('reference_free')), 'route_offset': int(result.get('route_offset') or 0), 'provider_route': str(result.get('provider_route') or ''), 'candidates_reviewed': int(result.get('candidates_reviewed') or 0), 'attempts': list(result.get('attempts') or []), 'accepted_without_machine_review': bool(result.get('accepted_without_machine_review')), 'ts': int(time.time())}
            cache['gen_failures'].pop(key, None)
        else:
            cache['gen_failures'][key] = {'kind': kind, 'role': role, 'locale': locale, 'source_url': source_url, 'prompt_version': generation_version, 'main_text_prompt_version': main_text_version if role == 'main' else '', 'reason': str(result.get('failure_reason') or 'generation_failed')[:120], 'risk_assessment': result.get('assessment'), 'text_assessment': result.get('text_assessment'), 'provider_route': str(result.get('provider_route') or ''), 'candidates_reviewed': int(result.get('candidates_reviewed') or 0), 'attempts': list(result.get('attempts') or []), 'ts': int(time.time())}
        save_cache(cache_path, cache)
    print(f'风险图片局部编辑: {len(targets_to_generate)} 张...', flush=True)
    from ..config.env import get_int
    circuit_resumes = 0
    circuit_wait_total_s = 0
    stage_retry_rounds = 0
    stage_retry_wait_total_s = 0
    stage_retry_limit = max(
        1,
        get_int('AGNES_503_RETRY_LIMIT', 1) + 2,
    )
    workers = AMAZON_IMAGE_GEN_CONCURRENCY
    pending = [
        item for item in targets_to_generate
        if not cached_generation(
            cache,
            generation_version,
            item[1],
            item[0],
            main_text_version,
        )
    ]
    generation_stats = {
        'operation': 'amazon_safe_image_gen',
        'items': len(targets_to_generate),
        'initial_workers': AMAZON_IMAGE_GEN_CONCURRENCY,
        'final_workers': workers,
        'min_workers': 2,
        'reductions': 0,
        'recoveries': 0,
        'failures': 0,
        'backoff_total_s': 0.0,
        'events': [],
    }
    while pending:
        try:
            _, generation_stats = adaptive_map(
                pending,
                generate_one,
                operation='amazon_safe_image_gen',
                initial_workers=workers,
                min_workers=2,
                is_success=lambda value: (
                    isinstance(value, dict) and bool(value.get('url'))
                ),
                on_result=generate_done,
                terminal_exceptions=(
                    ProviderQuotaError,
                    ProviderCircuitOpenError,
                ),
                backoff_s=2,
                max_backoff_s=15,
            )
            transient_pending = [
                item
                for item in pending
                if _is_transient_generation_failure(
                    cache['gen_failures'].get(
                        f'{item[1]}:{item[0]}'
                    )
                )
            ]
            if (
                not transient_pending
                or stage_retry_rounds >= stage_retry_limit
            ):
                break
            stage_retry_rounds += 1
            workers = max(2, workers // 2)
            wait_s = max(
                get_int('AGNES_503_CIRCUIT_COOLDOWN_S', 120),
                get_int('CIRCUIT_COOLDOWN_S', 60),
            ) + 2
            stage_retry_wait_total_s += wait_s
            print(
                '  纯网络生图失败 '
                f'{len(transient_pending)} 张，冷却 {wait_s} 秒后'
                f'以 {workers} 并发续跑'
                f'（第 {stage_retry_rounds}/{stage_retry_limit} 轮）...',
                flush=True,
            )
            time.sleep(wait_s)
            pending = transient_pending
        except ProviderCircuitOpenError:
            if circuit_resumes >= 3:
                raise
            circuit_resumes += 1
            workers = max(2, workers // 2)
            wait_s = max(1, get_int('CIRCUIT_COOLDOWN_S', 60)) + 2
            circuit_wait_total_s += wait_s
            print(
                '  生图线路临时熔断，'
                f'{wait_s} 秒后以 {workers} 并发续跑'
                f'（第 {circuit_resumes}/3 次）...',
                flush=True,
            )
            time.sleep(wait_s)
            pending = [
                item for item in pending
                if not cached_generation(
                    cache,
                    generation_version,
                    item[1],
                    item[0],
                    main_text_version,
                )
            ]
    generation_stats['circuit_resumes'] = circuit_resumes
    generation_stats['circuit_wait_total_s'] = circuit_wait_total_s
    generation_stats['stage_retry_rounds'] = stage_retry_rounds
    generation_stats['stage_retry_wait_total_s'] = (
        stage_retry_wait_total_s
    )
    concurrency_stats['amazon_safe_image_gen'] = generation_stats
    return targets_to_generate
from collections import defaultdict
from typing import Any, Callable
from .risk import IMAGE_RISK_POLICY_VERSION, assessment_is_intrinsic_brand, assessment_status, attachment_should_delete, image_action, image_requires_edit, unknown_image_assessment
from ..concurrency import adaptive_map
from ..providers.support import ProviderQuotaError
from ..quality import AMAZON_REVIEW_CONCURRENCY
from .risk import (
    ROLE_PRIORITY,
    assessment_record,
    row_image_roles,
    safe_assess,
    safe_assess_batch,
)
from .cache import current_cache_versions, load_cache, load_manual_overrides, manual_safe_assessment, save_cache

def _index_images(data: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    usage: dict[str, list[dict[str, Any]]] = defaultdict(list)
    row_by_id: dict[str, dict[str, Any]] = {}
    for row_index, row in enumerate(data):
        product_id = str(row.get('id') or row_index + 1)
        row_by_id[product_id] = row
        for url, role, position in row_image_roles(row):
            usage[url].append({'product_id': product_id, 'role': role, 'position': position})
    return (usage, row_by_id)


def _review_batches(urls: list[str]) -> tuple[list[list[str]], int]:
    from ..config.env import get_int

    batch_size = max(1, min(5, get_int("REVIEW_BATCH_SIZE", 3)))
    return (
        [urls[index:index + batch_size] for index in range(0, len(urls), batch_size)],
        batch_size,
    )

def _review_source_images(urls: list[str], *, assessments: dict[str, dict[str, Any]], cache: dict[str, Any], cache_path: str | None, provider_getter: Callable[[], object], concurrency_stats: dict[str, Any], progress: Callable[[int, int], None] | None) -> None:
    missing = [url for url in urls if url not in assessments]
    batches, batch_size = _review_batches(missing)
    total_work = max(1, len(missing))
    completed = 0

    def assess_one(batch: list[str]) -> list[dict[str, Any]]:
        return safe_assess_batch(provider_getter(), batch)

    def assess_done(batch: list[str], result: object) -> None:
        nonlocal completed
        if isinstance(result, Exception) or not isinstance(result, list):
            result = []
        for index, url in enumerate(batch):
            value = result[index] if index < len(result) else None
            if not isinstance(value, dict):
                value = unknown_image_assessment('assessment worker failed')
                value['operational_failure'] = True
            assessments[url] = value
            completed += 1
        save_cache(cache_path, cache)
        if progress:
            progress(completed, total_work)
    if not missing:
        print(f'结构化图片初审全部缓存命中: {len(urls)} 张', flush=True)
        return
    print(
        f'结构化图片初审 {len(missing)} 张（每批 {batch_size} 张，'
        f'{AMAZON_REVIEW_CONCURRENCY} 批并发，自适应退避）...',
        flush=True,
    )
    _, review_stats = adaptive_map(batches, assess_one, operation='amazon_structured_review', initial_workers=AMAZON_REVIEW_CONCURRENCY, min_workers=min(5, AMAZON_REVIEW_CONCURRENCY), is_success=lambda value: isinstance(value, list) and bool(value) and all(assessment_status(item) != 'unknown' for item in value), on_result=assess_done, terminal_exceptions=(ProviderQuotaError,), backoff_s=2, max_backoff_s=15)
    review_stats['images'] = len(missing)
    review_stats['batch_size'] = batch_size
    concurrency_stats['amazon_structured_review'] = review_stats

def _review_main_text_images(main_urls: list[str], *, assessments: dict[str, dict[str, Any]], cache: dict[str, Any], cache_path: str | None, provider_getter: Callable[[], object], concurrency_stats: dict[str, Any]) -> None:
    """Apply the independent zero-text policy to source main images only."""
    missing = [url for url in main_urls if url not in assessments]
    batches, batch_size = _review_batches(missing)

    def assess_one(batch: list[str]) -> list[dict[str, Any]]:
        return safe_assess_batch(
            provider_getter(),
            batch,
            policy='main_text_free',
        )

    def assess_done(batch: list[str], result: object) -> None:
        if isinstance(result, Exception) or not isinstance(result, list):
            result = []
        for index, url in enumerate(batch):
            value = result[index] if index < len(result) else None
            if not isinstance(value, dict):
                value = unknown_main_text_assessment(
                    'main-image text assessment worker failed'
                )
                value['operational_failure'] = True
            assessments[url] = value
        save_cache(cache_path, cache)

    if not missing:
        print(
            f'主图零文字检查全部缓存命中: {len(main_urls)} 张',
            flush=True,
        )
        return
    print(
        f'主图零文字检查 {len(missing)} 张'
        f'（每批 {batch_size} 张，{AMAZON_REVIEW_CONCURRENCY} 批并发）...',
        flush=True,
    )
    _, review_stats = adaptive_map(
        batches,
        assess_one,
        operation='amazon_main_text_review',
        initial_workers=AMAZON_REVIEW_CONCURRENCY,
        min_workers=min(5, AMAZON_REVIEW_CONCURRENCY),
        is_success=lambda value: (
            isinstance(value, list)
            and bool(value)
            and all(
                assessment_status(item) != 'unknown'
                for item in value
            )
        ),
        on_result=assess_done,
        terminal_exceptions=(ProviderQuotaError,),
        backoff_s=2,
        max_backoff_s=15,
    )
    review_stats['images'] = len(missing)
    review_stats['batch_size'] = batch_size
    concurrency_stats['amazon_main_text_review'] = review_stats


def _select_existing_images(
    data: list[dict[str, Any]],
    *,
    usage: dict[str, list[dict[str, Any]]],
    row_by_id: dict[str, dict[str, Any]],
    assessments: dict[str, dict[str, Any]],
    main_text_assessments: dict[str, dict[str, Any]],
    cache: dict[str, Any],
    cache_path: str | None,
    provider_getter: Callable[[], object],
    concurrency_stats: dict[str, Any],
    runtime_metrics: dict[str, Any],
    quality_issues: list[str],
    effective_assessment: Callable[[str, str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Prefer a clean white-background source image and never generate."""
    candidates: dict[str, list[tuple[str, str, int]]] = {}
    candidate_index: dict[str, int] = {}
    selected: dict[str, tuple[str, str, int]] = {}
    selected_rank: dict[str, int] = {}
    quality_rank = {
        'preferred': 3,
        'acceptable': 2,
        'fallback': 1,
        'unknown': 0,
    }

    for product_id, row in row_by_id.items():
        product_images = [str(row.get('main_img') or '').strip()]
        product_images.extend(
            str(value).strip() for value in row.get('extra_imgs') or []
        )
        ordered = list(dict.fromkeys(value for value in product_images if value))
        safe_candidates: list[tuple[str, str, int]] = []
        for position, url in enumerate(ordered):
            original_role = 'main' if position == 0 else 'attachment'
            if assessment_status(
                effective_assessment(product_id, original_role, url)
            ) == 'safe':
                safe_candidates.append((url, original_role, position))
        candidates[product_id] = safe_candidates
        candidate_index[product_id] = 0

    unresolved = set(row_by_id)
    while unresolved:
        round_urls: list[str] = []
        exhausted: set[str] = set()
        for product_id in sorted(unresolved):
            position = candidate_index[product_id]
            values = candidates[product_id]
            if position >= len(values):
                exhausted.add(product_id)
                continue
            url = values[position][0]
            if url not in round_urls:
                round_urls.append(url)
        unresolved.difference_update(exhausted)
        if not unresolved:
            break
        _review_main_text_images(
            round_urls,
            assessments=main_text_assessments,
            cache=cache,
            cache_path=cache_path,
            provider_getter=provider_getter,
            concurrency_stats=concurrency_stats,
        )
        resolved_this_round: set[str] = set()
        for product_id in sorted(unresolved):
            candidate = candidates[product_id][candidate_index[product_id]]
            text_assessment = main_text_assessments.get(candidate[0])
            if assessment_status(text_assessment) == 'safe':
                rank = quality_rank.get(
                    str((text_assessment or {}).get('main_image_quality')),
                    0,
                )
                if rank > selected_rank.get(product_id, -1):
                    selected[product_id] = candidate
                    selected_rank[product_id] = rank
                if rank == quality_rank['preferred']:
                    resolved_this_round.add(product_id)
                else:
                    candidate_index[product_id] += 1
            else:
                candidate_index[product_id] += 1
        unresolved.difference_update(resolved_this_round)

    pending: list[dict[str, Any]] = []
    attachment_deleted = 0
    variant_deleted = 0
    main_reselected = 0
    main_original_retained = 0

    for product_id, row in row_by_id.items():
        original_main = str(row.get('main_img') or '').strip()
        original_extras = [
            str(value).strip()
            for value in row.get('extra_imgs') or []
            if str(value).strip()
        ]
        product_images = list(dict.fromkeys(
            [value for value in [original_main, *original_extras] if value]
        ))
        original_role = {
            url: ('main' if index == 0 else 'attachment')
            for index, url in enumerate(product_images)
        }
        original_position = {
            url: index for index, url in enumerate(product_images)
        }
        image_records: list[dict[str, Any]] = []

        def source_record(url: str, final_role: str) -> dict[str, Any]:
            source_role = original_role.get(url, final_role)
            general = effective_assessment(product_id, source_role, url)
            text = main_text_assessments.get(url)
            record = assessment_record(
                url=url,
                role=final_role,
                assessment=general,
                text_assessment=text,
                source='source',
            )
            record['original_role'] = source_role
            record['original_position'] = original_position.get(url, 0)
            record['main_eligible'] = (
                assessment_status(general) == 'safe'
                and assessment_status(text) == 'safe'
            )
            return record

        selected_value = selected.get(product_id)
        if selected_value is None:
            row['_main_selection_pending'] = True
            for url in product_images:
                record = source_record(url, original_role[url])
                record['image_action'] = 'pending_main_candidate'
                image_records.append(record)
            for position, url in enumerate(row.get('var_imgs') or []):
                url = str(url or '').strip()
                if not url:
                    continue
                general = effective_assessment(product_id, 'variant', url)
                record = assessment_record(
                    url=url,
                    role='variant',
                    assessment=general,
                    source='source',
                )
                record['original_role'] = 'variant'
                record['original_position'] = position
                record['main_eligible'] = False
                record['image_action'] = 'keep' if assessment_status(general) == 'safe' else 'delete_variant'
                image_records.append(record)
            row['_image_assessments'] = sorted(
                image_records,
                key=lambda item: (
                    ROLE_PRIORITY.get(item['role'], 9),
                    int(item.get('original_position') or 0),
                ),
            )
            pending.append({
                'product_id': product_id,
                'site': str(row.get('site') or row.get('产品站点') or 'US'),
                'title': str(row.get('title') or ''),
                'reason': 'missing_clean_main',
                'images': list(row['_image_assessments']),
            })
            continue

        selected_url, selected_original_role, _ = selected_value
        row.pop('_main_selection_pending', None)
        row['main_img'] = selected_url
        kept_extras: list[str] = []
        for url in product_images:
            if url == selected_url:
                continue
            source_role = original_role[url]
            if assessment_status(
                effective_assessment(product_id, source_role, url)
            ) == 'safe':
                kept_extras.append(url)
            else:
                attachment_deleted += 1
                record = source_record(url, 'attachment')
                record['image_action'] = 'delete_attachment'
                image_records.append(record)
        row['extra_imgs'] = kept_extras
        main_record = source_record(selected_url, 'main')
        main_record['selection_action'] = (
            'retain_main'
            if selected_original_role == 'main'
            else 'promote_to_main'
        )
        main_record['image_action'] = 'keep'
        image_records.append(main_record)
        for url in kept_extras:
            record = source_record(url, 'attachment')
            record['selection_action'] = (
                'demote_from_main'
                if original_role[url] == 'main'
                else 'keep_attachment'
            )
            record['image_action'] = 'keep'
            image_records.append(record)

        kept_variants: list[str] = []
        for position, value in enumerate(row.get('var_imgs') or []):
            url = str(value or '').strip()
            if not url:
                continue
            general = effective_assessment(product_id, 'variant', url)
            record = assessment_record(
                url=url,
                role='variant',
                assessment=general,
                source='source',
            )
            record['original_role'] = 'variant'
            record['original_position'] = position
            record['main_eligible'] = False
            if assessment_status(general) == 'safe':
                kept_variants.append(url)
                record['image_action'] = 'keep'
            else:
                variant_deleted += 1
                record['image_action'] = 'delete_variant'
            image_records.append(record)
        row['var_imgs'] = kept_variants
        row['var_img'] = kept_variants[0] if kept_variants else ''
        row['_image_assessments'] = sorted(
            image_records,
            key=lambda item: (
                ROLE_PRIORITY.get(item['role'], 9),
                int(item.get('original_position') or 0),
            ),
        )
        if selected_url == original_main:
            main_original_retained += 1
        else:
            main_reselected += 1

    if pending:
        quality_issues.append(
            f'{len(pending)} 个商品没有安全且无文字的原图主图，已转待人工审核'
        )
    runtime_metrics['pending_main_products'] = pending
    runtime_metrics['quarantined_products'] = []
    runtime_metrics['image_assessments'] = {
        url: dict(value) for url, value in assessments.items()
    }
    runtime_metrics['main_text_assessments'] = {
        url: dict(value) for url, value in main_text_assessments.items()
    }
    status_counts = {
        status: sum(
            1 for value in assessments.values()
            if assessment_status(value) == status
        )
        for status in ('safe', 'risk', 'unknown')
    }
    text_status_counts = {
        status: sum(
            1 for value in main_text_assessments.values()
            if assessment_status(value) == status
        )
        for status in ('safe', 'risk', 'unknown')
    }
    runtime_metrics['image_safety_gate'] = {
        'processing_mode': 'select_existing',
        'source_references': sum(len(items) for items in usage.values()),
        'unique_source_images': len(usage),
        'source_status_counts': status_counts,
        'main_text_status_counts': text_status_counts,
        'main_candidates_reviewed': len(main_text_assessments),
        'main_original_retained': main_original_retained,
        'main_reselected': main_reselected,
        'preferred_main_selected': sum(
            1 for rank in selected_rank.values()
            if rank == quality_rank['preferred']
        ),
        'attachment_deleted': attachment_deleted,
        'variant_deleted': variant_deleted,
        'pending_products': len(pending),
        'generated_main': 0,
        'generated_variant': 0,
        'generated_attachment': 0,
        'generation_requests': 0,
    }
    runtime_metrics['image_remediation'] = {
        'requested': 0,
        'succeeded': 0,
        'failed': 0,
        'generated_candidates_reviewed': 0,
    }
    save_cache(cache_path, cache)
    return list(data)


def _localized_image_context(row: dict[str, Any]) -> str:
    """Build a compact locale-aware context for the image editor."""
    from ..markets import get_market

    market = get_market(row.get('site') or 'US')
    title = str(row.get('title') or row.get('_source_title') or '').strip()
    return (
        f'TARGET MARKET: {market.code}; COUNTRY: {market.country}; '
        f'TARGET LANGUAGE: {market.language}; LOCALE: {market.locale}. '
        'Translate product information inside the image only into this '
        'language. Remove prohibited branding and overlays. '
        f'SOLD PRODUCT TITLE: {title[:160]}'
    )


def _main_quality_rank(value: object) -> int:
    if not isinstance(value, dict):
        return 0
    explicit = {
        'preferred': 3,
        'acceptable': 2,
        'fallback': 1,
    }.get(str(value.get('main_image_quality') or '').strip(), 0)
    if explicit:
        return explicit
    evidence = str(value.get('evidence') or '').lower()
    if any(token in evidence for token in ('white background', 'isolated product', 'single product')):
        return 3
    if any(token in evidence for token in ('collage', 'lifestyle', 'installation scene', 'dimension diagram')):
        return 1
    return 0


def _regenerate_all_localized_images(
    data: list[dict[str, Any]],
    *,
    usage: dict[str, list[dict[str, Any]]],
    row_by_id: dict[str, dict[str, Any]],
    assessments: dict[str, dict[str, Any]],
    main_text_assessments: dict[str, dict[str, Any]],
    cache: dict[str, Any],
    cache_path: str | None,
    provider_getter: Callable[[], object],
    concurrency_stats: dict[str, Any],
    runtime_metrics: dict[str, Any],
    quality_issues: list[str],
    generation_version: str,
) -> list[dict[str, Any]]:
    """Regenerate every source image once per role and target language."""
    from ..markets import get_market

    target_set: set[tuple[str, str]] = set()
    listing_contexts: dict[tuple[str, str], str] = {}
    for product_id, row in row_by_id.items():
        market = get_market(row.get('site') or 'US')
        locale = market.language_code.lower()
        context = _localized_image_context(row)
        for url, role, _position in row_image_roles(row):
            kind = f'{role}|{locale}'
            target_set.add((url, kind))
            listing_contexts.setdefault((url, kind), context)
    targets = sorted(target_set, key=lambda item: (item[0], item[1]))
    generate_safe_replacements(
        targets,
        cache=cache,
        cache_path=cache_path,
        generation_version=generation_version,
        main_text_version='',
        provider_getter=provider_getter,
        concurrency_stats=concurrency_stats,
        listing_contexts=listing_contexts,
    )
    # The caller supplies the current signature through runtime_metrics only
    # for reporting; generation lookup uses the cache's own current version.
    generation_version = str(cache.get('gen_prompt_version') or '')
    blockers: list[dict[str, Any]] = []
    generated_by_target: dict[tuple[str, str], str] = {}
    for source_url, kind in targets:
        replacement = cached_generation(
            cache,
            generation_version,
            kind,
            source_url,
            '',
        )
        if replacement:
            generated_by_target[(source_url, kind)] = replacement
            continue
        role = _generation_role(kind)
        blockers.extend(
            {
                'product_id': product_id,
                'role': role,
                'url': source_url,
                'locale': _generation_locale(kind),
                'message': '全图本地化编辑失败；已停止发布，正式表不覆盖',
                'generation_failure': cache.get('gen_failures', {}).get(
                    f'{kind}:{source_url}'
                ),
            }
            for product_id, row in row_by_id.items()
            if any(
                value == source_url and item_role == role
                for value, item_role, _ in row_image_roles(row)
            )
            and get_market(row.get('site') or 'US').language_code.lower()
            == _generation_locale(kind)
        )
    if blockers:
        runtime_metrics['image_publish_blockers'] = blockers
        quality_issues.append(f'图片本地化编辑失败 {len(blockers)} 项，已停止发布')
        save_cache(cache_path, cache)
        sample = ', '.join(
            f"{item['product_id']}:{item['role']}"
            for item in blockers[:5]
        )
        raise RuntimeError(
            f'图片本地化编辑失败，已停止发布，正式表未覆盖：'
            f'{len(blockers)} 项（{sample}）'
        )

    staged_rows: dict[str, dict[str, Any]] = {}
    for product_id, row in row_by_id.items():
        market = get_market(row.get('site') or 'US')
        locale = market.language_code.lower()
        source_products = [
            str(row.get('main_img') or '').strip(),
            *[
                str(value).strip()
                for value in row.get('extra_imgs') or []
                if str(value).strip()
            ],
        ]
        generated_products = [
            generated_by_target[(url, f"{'main' if index == 0 else 'attachment'}|{locale}")]
            for index, url in enumerate(source_products)
        ]
        ranked = sorted(
            enumerate(generated_products),
            key=lambda item: (
                -_main_quality_rank(
                    main_text_assessments.get(source_products[item[0]])
                    or assessments.get(source_products[item[0]])
                    or {}
                ),
                item[0],
            ),
        )
        selected_index = ranked[0][0]
        variants = [
            str(value).strip()
            for value in row.get('var_imgs') or []
            if str(value).strip()
        ]
        generated_variants = [
            generated_by_target[(url, f'variant|{locale}')]
            for url in variants
        ]
        staged = dict(row)
        staged['main_img'] = generated_products[selected_index]
        staged['extra_imgs'] = [
            value
            for index, value in enumerate(generated_products)
            if index != selected_index
        ]
        staged['var_imgs'] = generated_variants
        staged['var_img'] = generated_variants[0] if generated_variants else ''
        records: list[dict[str, Any]] = []
        for index, source_url in enumerate(source_products):
            role = 'main' if index == 0 else 'attachment'
            kind = f'{role}|{locale}'
            replacement = generated_by_target[(source_url, kind)]
            meta = cache.get('gen_meta', {}).get(f'{kind}:{source_url}') or {}
            record = assessment_record(
                url=replacement,
                role='main' if index == selected_index else 'attachment',
                assessment=meta.get('risk_assessment') or unknown_image_assessment(
                    'generated image accepted for human review'
                ),
                source='generated',
                source_url=source_url,
            )
            record['original_role'] = role
            record['original_position'] = index
            record['selection_action'] = (
                'promote_to_main' if index == selected_index and index else
                'retain_main' if index == selected_index else
                'keep_attachment'
            )
            record['accepted_without_machine_review'] = bool(
                meta.get('accepted_without_machine_review')
            )
            record['source_image_action'] = image_action(
                assessments.get(source_url)
            )
            record['source_text_assessment'] = dict(
                main_text_assessments.get(source_url) or {}
            )
            record['source_detected_text'] = list(dict.fromkeys(
                list((assessments.get(source_url) or {}).get('detected_text') or [])
                + list((main_text_assessments.get(source_url) or {}).get('detected_text') or [])
            ))
            record['edit_action'] = 'localized_edit'
            record['generation_route_offset'] = int(meta.get('route_offset') or 0)
            record['locale'] = locale
            records.append(record)
        for position, source_url in enumerate(variants):
            kind = f'variant|{locale}'
            replacement = generated_by_target[(source_url, kind)]
            meta = cache.get('gen_meta', {}).get(f'{kind}:{source_url}') or {}
            record = assessment_record(
                url=replacement,
                role='variant',
                assessment=meta.get('risk_assessment') or unknown_image_assessment(
                    'generated image accepted for human review'
                ),
                source='generated',
                source_url=source_url,
            )
            record['original_role'] = 'variant'
            record['original_position'] = position
            record['accepted_without_machine_review'] = bool(
                meta.get('accepted_without_machine_review')
            )
            record['source_image_action'] = image_action(
                assessments.get(source_url)
            )
            record['source_text_assessment'] = dict(
                main_text_assessments.get(source_url) or {}
            )
            record['source_detected_text'] = list(dict.fromkeys(
                list((assessments.get(source_url) or {}).get('detected_text') or [])
                + list((main_text_assessments.get(source_url) or {}).get('detected_text') or [])
            ))
            record['edit_action'] = 'localized_edit'
            record['generation_route_offset'] = int(meta.get('route_offset') or 0)
            record['locale'] = locale
            records.append(record)
        staged['_image_assessments'] = sorted(
            records,
            key=lambda item: (ROLE_PRIORITY.get(item['role'], 9), item.get('original_position', 0)),
        )
        staged_rows[product_id] = staged
    for product_id, staged in staged_rows.items():
        row_by_id[product_id].update(staged)
    generated_main = sum(1 for row in data for _ in [row.get('main_img')])
    generated_variants = sum(len(row.get('var_imgs') or []) for row in data)
    generated_attachments = sum(len(row.get('extra_imgs') or []) for row in data)
    runtime_metrics['image_safety_gate'] = {
        'processing_mode': 'regenerate_all_localized',
        'source_images': len(usage),
        'references': sum(len(items) for items in usage.values()),
        'reviewed': len(assessments),
        'safe': sum(assessment_status(value) == 'safe' for value in assessments.values()),
        'risk': sum(assessment_status(value) == 'risk' for value in assessments.values()),
        'unknown': sum(assessment_status(value) == 'unknown' for value in assessments.values()),
        'generated_main': generated_main,
        'generated_attachment': generated_attachments,
        'generated_variant': generated_variants,
        'generation_requests': len(targets),
        'generated_unique_targets': len(targets),
        'attachment_deleted': 0,
        'variant_deleted': 0,
        'publish_blockers': 0,
        'locale_targets': sorted({_generation_locale(kind) for _, kind in targets}),
    }
    runtime_metrics['image_remediation'] = {
        'reviewed': len(assessments),
        'flagged': sum(assessment_status(value) == 'risk' for value in assessments.values()),
        'generated_candidates_reviewed': 0,
        'generated_main': generated_main,
        'generated_attachment': generated_attachments,
        'generated_variant': generated_variants,
        'generation_url_checked': len(targets),
        'generation_url_valid': len(generated_by_target),
        'generation_url_invalid': 0,
        'attachment_deleted': 0,
        'variant_deleted': 0,
        'publish_blockers': 0,
    }
    runtime_metrics['image_assessments'] = {url: dict(value) for url, value in assessments.items()}
    runtime_metrics['main_text_assessments'] = {url: dict(value) for url, value in main_text_assessments.items()}
    save_cache(cache_path, cache)
    return list(data)

def run_structured_image_safety_gate(data: list[dict[str, Any]], cache_path: str | None=None, quality_issues: list[str] | None=None, progress: Callable[[int, int], None] | None=None, runtime_metrics: dict[str, Any] | None=None, provider_getter: Callable[[], object] | None=None) -> list[dict[str, Any]]:
    """Audit every source image, remediate risks, and fail closed."""
    if provider_getter is None:
        raise ValueError('structured image gate requires provider_getter')
    quality_issues = quality_issues if quality_issues is not None else []
    runtime_metrics = runtime_metrics if isinstance(runtime_metrics, dict) else {}
    concurrency_stats = runtime_metrics.setdefault('concurrency', {})
    (
        review_version,
        main_text_version,
        generation_version,
    ) = current_cache_versions()
    cache = load_cache(
        cache_path,
        review_version,
        main_text_version,
        generation_version,
    )
    assessments = cache['risk_assessments']
    confirmations = cache['risk_confirmations']
    main_text_assessments = cache['main_text_assessments']
    gen_meta = cache['gen_meta']
    gen_failures = cache['gen_failures']
    manual_overrides = load_manual_overrides(cache_path)

    def effective_assessment(product_id: str, role: str, url: str) -> dict[str, Any]:
        override = manual_overrides.get((str(product_id), str(role), str(url)))
        if override:
            return manual_safe_assessment(override)
        return assessments.get(url) or unknown_image_assessment()

    def effective_action(
        role: str,
        assessment: object,
        text_assessment: object = None,
    ) -> str:
        if role == 'attachment':
            if attachment_should_delete(assessment):
                return 'delete_attachment'
            if assessment_status(assessment) == 'safe':
                return 'keep'
            return 'keep_review'
        if (
            role == 'main'
            and assessment_status(text_assessment) == 'risk'
        ):
            return 'edit_remove'
        if image_action(assessment) == 'block_publish':
            return 'keep_review'
        return image_action(assessment)

    def requires_remediation(
        product_id: str,
        role: str,
        url: str,
    ) -> bool:
        if role == 'attachment':
            return False
        general = effective_assessment(product_id, role, url)
        if image_requires_edit(general):
            return True
        return (
            role == 'main'
            and assessment_status(main_text_assessments.get(url)) == 'risk'
        )

    usage, row_by_id = _index_images(data)
    urls = sorted(usage)
    _review_source_images(urls, assessments=assessments, cache=cache, cache_path=cache_path, provider_getter=provider_getter, concurrency_stats=concurrency_stats, progress=progress)
    if _image_processing_mode() == 'regenerate_all_localized':
        return _regenerate_all_localized_images(
            data,
            usage=usage,
            row_by_id=row_by_id,
            assessments=assessments,
            main_text_assessments=main_text_assessments,
            cache=cache,
            cache_path=cache_path,
            provider_getter=provider_getter,
            concurrency_stats=concurrency_stats,
            runtime_metrics=runtime_metrics,
            quality_issues=quality_issues,
            generation_version=generation_version,
        )
    if _image_processing_mode() == 'select_existing':
        return _select_existing_images(
            data,
            usage=usage,
            row_by_id=row_by_id,
            assessments=assessments,
            main_text_assessments=main_text_assessments,
            cache=cache,
            cache_path=cache_path,
            provider_getter=provider_getter,
            concurrency_stats=concurrency_stats,
            runtime_metrics=runtime_metrics,
            quality_issues=quality_issues,
            effective_assessment=effective_assessment,
        )
    main_urls = sorted(
        url
        for url, items in usage.items()
        if any(item['role'] == 'main' for item in items)
    )
    _review_main_text_images(
        main_urls,
        assessments=main_text_assessments,
        cache=cache,
        cache_path=cache_path,
        provider_getter=provider_getter,
        concurrency_stats=concurrency_stats,
    )
    review_failures = [
        {'url': url, 'assessment': value}
        for url, value in assessments.items()
        if image_action(value) == 'block_publish'
    ]
    if review_failures:
        runtime_metrics['image_review_warnings'] = review_failures
        quality_issues.append(
            f'图片初审暂不可用 {len(review_failures)} 张，已保留供人工复核'
        )
    main_text_failures = [
        {'url': url, 'assessment': main_text_assessments.get(url)}
        for url in main_urls
        if assessment_status(main_text_assessments.get(url)) == 'unknown'
    ]
    if main_text_failures:
        runtime_metrics['main_text_review_warnings'] = main_text_failures
        quality_issues.append(
            f'主图文字检查暂不可用 {len(main_text_failures)} 张，已保留供人工复核'
        )
    records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    intrinsic_confirmed = sum(
        1 for value in assessments.values()
        if assessment_is_intrinsic_brand(value)
    )
    confirmation_conflicts = 0
    for url in urls:
        for item in usage[url]:
            effective = effective_assessment(item['product_id'], item['role'], url)
            record = assessment_record(
                url=url,
                role=item['role'],
                assessment=effective,
                text_assessment=(
                    main_text_assessments.get(url)
                    if item['role'] == 'main' else None
                ),
                source='source',
            )
            record['image_action'] = effective_action(
                item['role'],
                effective,
                main_text_assessments.get(url),
            )
            records[item['product_id']].append(record)
    attachment_deleted = 0
    remediation_targets_set: set[tuple[str, str]] = set()
    for url in sorted(urls):
        for item in usage[url]:
            effective = effective_assessment(
                item['product_id'],
                item['role'],
                url,
            )
            if requires_remediation(
                item['product_id'],
                item['role'],
                url,
            ):
                remediation_targets_set.add((url, item['role']))
    remediation_targets = sorted(
        remediation_targets_set,
        key=lambda item: (item[0], ROLE_PRIORITY.get(item[1], 9)),
    )
    listing_contexts: dict[tuple[str, str], str] = {}
    for source_url, kind in remediation_targets:
        titles = [
            str(row_by_id[item['product_id']].get('title') or '').strip()
            for item in usage[source_url]
            if item['role'] == kind
        ]
        listing_contexts[(source_url, kind)] = next(
            (title for title in titles if title),
            '',
        )
    targets_to_generate = generate_safe_replacements(remediation_targets, cache=cache, cache_path=cache_path, generation_version=generation_version, main_text_version=main_text_version, provider_getter=provider_getter, concurrency_stats=concurrency_stats, listing_contexts=listing_contexts)
    generated_candidates_reviewed = sum(
        int(
            (
                gen_meta.get(f'{kind}:{source_url}')
                or gen_failures.get(f'{kind}:{source_url}')
                or {}
            ).get('candidates_reviewed')
            or 0
        )
        for source_url, kind in remediation_targets
    )
    generated_main = generated_variant = generated_attachment = 0
    failed_main = failed_variant = failed_attachment = 0
    publish_blockers: list[dict[str, Any]] = []

    def append_generated_record(
        product_id: str,
        *,
        role: str,
        source_url: str,
        replacement: str,
    ) -> None:
        meta = gen_meta[f'{role}:{source_url}']
        generated_record = assessment_record(
            url=replacement,
            role=role,
            assessment=meta['risk_assessment'],
            text_assessment=meta.get('text_assessment'),
            source='generated',
            source_url=source_url,
        )
        generated_record['source_image_action'] = effective_action(
            role,
            assessments.get(source_url),
            main_text_assessments.get(source_url),
        )
        generated_record['source_detected_text'] = list(dict.fromkeys(
            list(
                (main_text_assessments.get(source_url) or {}).get(
                    'detected_text'
                ) or []
            )
            + list(
                (assessments.get(source_url) or {}).get('detected_text') or []
            )
        ))
        generated_record['generation_route_offset'] = int(
            meta.get('route_offset') or 0
        )
        generated_record['generation_strategy'] = str(
            meta.get('strategy') or 'precise'
        )
        generated_record['generation_reference_free'] = bool(
            meta.get('reference_free')
        )
        generated_record['candidates_reviewed'] = int(
            meta.get('candidates_reviewed') or 0
        )
        generated_record['accepted_without_machine_review'] = bool(
            meta.get('accepted_without_machine_review')
        )
        records[product_id].append(generated_record)

    def add_publish_blocker(
        product_id: str,
        *,
        role: str,
        source_url: str,
    ) -> None:
        failure = gen_failures.get(f'{role}:{source_url}')
        publish_blockers.append({
            'product_id': product_id,
            'role': role,
            'url': source_url,
            'action': effective_action(
                role,
                assessments.get(source_url),
                main_text_assessments.get(source_url),
            ),
            'message': '图片需要局部编辑，但全部生图线路失败；已停止发布，正式表不覆盖',
            'assessment': assessments.get(source_url),
            'generation_failure': failure,
        })

    for product_id, row in row_by_id.items():
        main = str(row.get('main_img') or '')
        if requires_remediation(product_id, 'main', main):
            replacement = cached_generation(cache, generation_version, 'main', main, main_text_version)
            if replacement:
                row['main_img'] = replacement
                generated_main += 1
                append_generated_record(
                    product_id,
                    role='main',
                    source_url=main,
                    replacement=replacement,
                )
            else:
                failed_main += 1
                add_publish_blocker(
                    product_id,
                    role='main',
                    source_url=main,
                )
        before_variants = list(row.get('var_imgs') or [])
        remediated_variants: list[str] = []
        for source_url in before_variants:
            if not image_requires_edit(effective_assessment(product_id, 'variant', source_url)):
                remediated_variants.append(source_url)
                continue
            replacement = cached_generation(cache, generation_version, 'variant', source_url)
            if replacement:
                remediated_variants.append(replacement)
                generated_variant += 1
                append_generated_record(
                    product_id,
                    role='variant',
                    source_url=source_url,
                    replacement=replacement,
                )
            else:
                remediated_variants.append(source_url)
                failed_variant += 1
                add_publish_blocker(
                    product_id,
                    role='variant',
                    source_url=source_url,
                )
        row['var_imgs'] = remediated_variants
        row['var_img'] = remediated_variants[0] if remediated_variants else ''
        before_attachments = list(row.get('extra_imgs') or [])
        remediated_attachments: list[str] = []
        for source_url in before_attachments:
            if attachment_should_delete(
                effective_assessment(product_id, 'attachment', source_url)
            ):
                attachment_deleted += 1
                continue
            remediated_attachments.append(source_url)
        row['extra_imgs'] = remediated_attachments
        row['_image_assessments'] = sorted(records[product_id], key=lambda item: (ROLE_PRIORITY.get(item['role'], 9), item['source'] != 'source'))
    quarantine_records: list[dict[str, Any]] = []
    retained = list(data)
    status_counts = {status: sum((1 for value in assessments.values() if assessment_status(value) == status)) for status in ('safe', 'risk', 'unknown')}
    text_status_counts = {
        status: sum(
            1
            for value in main_text_assessments.values()
            if assessment_status(value) == status
        )
        for status in ('safe', 'risk', 'unknown')
    }
    action_counts = {
        action: sum(
            1
            for url, items in usage.items()
            for item in items
            if effective_action(
                item['role'],
                effective_assessment(
                    item['product_id'],
                    item['role'],
                    url,
                ),
                main_text_assessments.get(url),
            ) == action
        )
        for action in (
            'keep',
            'edit_translate',
            'edit_remove',
            'delete_attachment',
            'keep_review',
            'block_publish',
        )
    }
    safety_metrics = {'processing_mode': 'generate_replacements', 'policy_version': IMAGE_RISK_POLICY_VERSION, 'source_images': len(urls), 'reviewed': len(assessments), **status_counts, 'actions': action_counts, 'main_text_reviewed': len(main_text_assessments), 'main_text_safe': text_status_counts['safe'], 'main_text_risk': text_status_counts['risk'], 'main_text_unknown': text_status_counts['unknown'], 'intrinsic_brand_confirmed': intrinsic_confirmed, 'confirmation_conflicts': confirmation_conflicts, 'attachment_deleted': attachment_deleted, 'generated_reviewed': generated_candidates_reviewed, 'generated_main': generated_main, 'generated_variant': generated_variant, 'generated_attachment': generated_attachment, 'failed_main': failed_main, 'failed_variant': failed_variant, 'failed_attachment': failed_attachment, 'publish_blockers': len(publish_blockers), 'quarantined_products': len(quarantine_records), 'retained_products': len(retained), 'manual_overrides_applied': sum((1 for product_id, row in row_by_id.items() for url, role, _position in row_image_roles(row) if (product_id, role, url) in manual_overrides)), 'human_confirmed_quarantine': 0}
    runtime_metrics['image_safety_gate'] = safety_metrics
    runtime_metrics['quarantined_products'] = quarantine_records
    runtime_metrics['image_assessments'] = {url: dict(value) for url, value in assessments.items()}
    runtime_metrics['main_text_assessments'] = {
        url: dict(value)
        for url, value in main_text_assessments.items()
    }
    runtime_metrics['image_confirmations'] = {url: dict(value) for url, value in confirmations.items()}
    runtime_metrics['image_remediation'] = {'reviewed': len(assessments), 'flagged': status_counts['risk'], 'clean_retained': status_counts['safe'], 'unknown_retained': action_counts['keep_review'], 'attachment_reviewed': sum((1 for url, items in usage.items() if any((item['role'] == 'attachment' for item in items)))), 'attachment_flagged': sum((1 for url, items in usage.items() if any((item['role'] == 'attachment' for item in items)) and attachment_should_delete(assessments.get(url)))), 'attachment_deleted': attachment_deleted, 'generated_candidates_reviewed': generated_candidates_reviewed, 'generated_main': generated_main, 'generated_variant': generated_variant, 'generated_attachment': generated_attachment, 'failed_main': failed_main, 'failed_variant': failed_variant, 'failed_attachment': failed_attachment, 'generation_url_checked': len(targets_to_generate), 'generation_url_valid': generated_main + generated_variant + generated_attachment, 'generation_url_invalid': failed_main + failed_variant + failed_attachment, 'publish_blockers': len(publish_blockers)}
    if publish_blockers:
        runtime_metrics['image_publish_blockers'] = publish_blockers
        quality_issues.append(f'图片编辑失败 {len(publish_blockers)} 张，已停止发布')
        save_cache(cache_path, cache)
        sample = ', '.join(
            f"{item['product_id']}:{item['role']}"
            for item in publish_blockers[:5]
        )
        raise RuntimeError(
            f'图片编辑失败，已停止发布，正式表未覆盖：'
            f'{len(publish_blockers)} 张（{sample}）'
        )
    save_cache(cache_path, cache)
    print(f"图片安全门完成: 安全 {status_counts['safe']}，风险 {status_counts['risk']}，未知 {status_counts['unknown']}，需翻译 {action_counts['edit_translate']}，需去标 {action_counts['edit_remove']}，生成主图 {generated_main}，生成附图 {generated_attachment}，生成变种图 {generated_variant}，阻断 {len(publish_blockers)}", flush=True)
    if progress:
        progress(1, 1)
    return retained
