"""Image remediation and the fail-closed Amazon image-safety gate."""
from __future__ import annotations
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
from .risk import safe_assess, validate_image_url
from .cache import (
    is_current_assessment,
    save_cache,
)


def _generated_image_review_mode() -> str:
    from ..config.env import get

    return str(get("GENERATED_IMAGE_REVIEW_MODE", "strict")).strip().lower()

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

def _generation_attempt_plan() -> list[tuple[int, str]]:
    from ..config.env import get_int
    limit = max(1, min(10, get_int('IMAGE_SAFETY_REGEN_LIMIT', 3)))
    return [(route_offset, 'precise') for route_offset in range(limit)]


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
    targets = []
    action = image_action(general)
    if detected_text:
        if kind == 'main':
            targets.append(
                'all visible text to remove: '
                + ', '.join(repr(value) for value in detected_text)
            )
        elif action == 'edit_translate':
            targets.append(
                'non-English product information text to translate: '
                + ', '.join(repr(value) for value in detected_text)
            )
        else:
            targets.append(
                'visible prohibited or non-English text: '
                + ', '.join(repr(value) for value in detected_text)
            )
    if categories:
        targets.append('risk elements ' + ', '.join(categories))
    if evidence:
        targets.append('review evidence: ' + evidence)
    if not targets:
        targets.append(
            'all visible text and text-like marks'
            if kind == 'main'
            else 'only confirmed brand marks, logos, watermarks, seller marks, or non-English product text'
        )
    strategy_instruction = {
        'precise': (
            'REFERENCE-PRESERVING LOCAL EDIT: use the reference image as the '
            'base image. Make the smallest possible local edit only. Do not '
            'redesign, rebuild, recrop, recolor, replace, rearrange, or '
            'simplify the product.'
        ),
    }.get(strategy, '')
    if kind == 'main':
        localized_instruction = (
            'MAIN IMAGE ZERO-TEXT RULE: remove every visible letter, word, '
            'number, dimension, model marking, specification, label, brand '
            'name, vehicle emblem, logo, watermark, seller/store name, URL, '
            'marketplace mark, QR code, promotional label, and pseudo-text. '
            'Do not translate or keep existing English text. Inpaint only the '
            'affected local areas with matching color, texture, and material. '
            'The output must contain no visible text or text-like marks. Do '
            'not create new labels, fake logos, new text, people, hands, '
            'vehicles, or extra components.'
        )
    else:
        localized_instruction = (
            'EDIT RULES: remove brand names, vehicle emblems, logos, '
            'watermarks, seller/store names, URLs, marketplace marks, and '
            'promotional labels. Translate Chinese or other non-English '
            'product information, specs, dimensions, controls, and '
            'installation labels into natural English. Keep existing English '
            'product information unchanged. Fill removed logo or watermark '
            'areas with matching local color, texture, and material; use a '
            'white patch only when the original local surface is white. Do '
            'not create pseudo-text, new labels, fake logos, new model '
            'numbers, new brands, people, hands, vehicles, or extra '
            'components.'
        )
    title_text = str(listing_title or '').strip()[:120]
    if strategy in {'rebuild', 'minimal', 'fallback'}:
        standalone = _standalone_product_title(title_text)
        title_instruction = (
            f'STANDALONE SOLD ITEM: {standalone}. '
            'Words such as car or vehicle indicate compatibility only; '
            'never draw the compatible vehicle.'
            if standalone else ''
        )
    else:
        title_instruction = (
            f'SOLD ITEM TITLE: {title_text}.'
            if title_text else ''
        )
    parts = [
        title_instruction,
        strategy_instruction,
        'EDIT TARGETS: ' + '; '.join(targets) + '.',
        (
            'The previous candidate was rejected for '
            + str(previous_rejection).strip()
            + '. Do not repeat it.'
            if str(previous_rejection).strip()
            else ''
        ),
        'Preserve product geometry, count, layout, angle, background, color, material, and texture.',
        localized_instruction,
    ]
    return ' '.join(part for part in parts if part)[:1400]


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
    generation_plan = _generation_attempt_plan()
    listing_contexts = listing_contexts or {}
    disabled_routes: set[int] = set()
    disabled_routes_lock = Lock()

    def generate_one(item: tuple[str, str]) -> dict[str, Any]:
        source_url, kind = item
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
            reference_free = kind == 'main' and strategy in {
                'rebuild',
                'minimal',
                'fallback',
            }
            with disabled_routes_lock:
                route_disabled = route_offset in disabled_routes
            if route_disabled:
                attempts.append({
                    'attempt_index': attempt_index,
                    'route_offset': route_offset,
                    'strategy': strategy,
                    'reference_free': reference_free,
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
                generated = str(provider.call_image_gen(
                    source_url,
                    is_variant=kind in {'variant', 'attachment'},
                    context=context,
                    route_offset=route_offset,
                    reference_free=reference_free,
                ) or '')
            except ProviderCircuitOpenError:
                raise
            except ProviderQuotaError as exc:
                if str(getattr(exc, 'provider', '')).lower() == 'gpt':
                    with disabled_routes_lock:
                        disabled_routes.add(route_offset)
                    attempts.append({
                        'attempt_index': attempt_index,
                    'route_offset': route_offset,
                    'strategy': strategy,
                    'reference_free': reference_free,
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
                    'candidate_url': generated,
                    'error': reason,
                })
                continue
            if _generated_image_review_mode() == "human_review":
                last_assessment = unknown_image_assessment(
                    "generated image accepted for human review without "
                    "machine recheck"
                )
                if kind == 'main':
                    last_text_assessment = unknown_main_text_assessment(
                        "generated main image accepted for human review "
                        "without machine text recheck"
                    )
                attempts.append({
                    'attempt_index': attempt_index,
                    'route_offset': route_offset,
                    'strategy': strategy,
                    'reference_free': reference_free,
                    'candidate_url': generated,
                    'accepted_without_machine_review': True,
                })
                return {
                    'url': generated,
                    'assessment': last_assessment,
                    'text_assessment': (
                        last_text_assessment if kind == 'main' else None
                    ),
                    'strategy': strategy,
                    'reference_free': reference_free,
                    'route_offset': route_offset,
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
                    'candidate_url': generated,
                    'reason': last_reason,
                    'risk_assessment': last_assessment,
                })
                continue
            return {
                'url': generated,
                'assessment': last_assessment,
                'text_assessment': (
                    last_text_assessment if kind == 'main' else None
                ),
                'strategy': strategy,
                'reference_free': reference_free,
                'route_offset': route_offset,
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
            'attempts': attempts,
        }

    def generate_done(item: tuple[str, str], result: object) -> None:
        source_url, kind = item
        key = f'{kind}:{source_url}'
        if isinstance(result, Exception) or not isinstance(result, dict):
            result = {'url': '', 'assessment': unknown_image_assessment('generation worker failed'), 'failure_reason': 'generation_worker_failed'}
        generated = str(result.get('url') or '')
        if generated:
            cache['gen_results'][key] = generated
            cache['gen_meta'][key] = {'kind': kind, 'source_url': source_url, 'prompt_version': generation_version, 'main_text_prompt_version': main_text_version if kind == 'main' else '', 'risk_assessment': result['assessment'], 'text_assessment': result.get('text_assessment'), 'strategy': str(result.get('strategy') or 'precise'), 'reference_free': bool(result.get('reference_free')), 'route_offset': int(result.get('route_offset') or 0), 'candidates_reviewed': int(result.get('candidates_reviewed') or 0), 'attempts': list(result.get('attempts') or []), 'accepted_without_machine_review': bool(result.get('accepted_without_machine_review')), 'ts': int(time.time())}
            cache['gen_failures'].pop(key, None)
        else:
            cache['gen_failures'][key] = {'kind': kind, 'source_url': source_url, 'prompt_version': generation_version, 'main_text_prompt_version': main_text_version if kind == 'main' else '', 'reason': str(result.get('failure_reason') or 'generation_failed')[:120], 'risk_assessment': result.get('assessment'), 'text_assessment': result.get('text_assessment'), 'candidates_reviewed': int(result.get('candidates_reviewed') or 0), 'attempts': list(result.get('attempts') or []), 'ts': int(time.time())}
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
    safety_metrics = {'policy_version': IMAGE_RISK_POLICY_VERSION, 'source_images': len(urls), 'reviewed': len(assessments), **status_counts, 'actions': action_counts, 'main_text_reviewed': len(main_text_assessments), 'main_text_safe': text_status_counts['safe'], 'main_text_risk': text_status_counts['risk'], 'main_text_unknown': text_status_counts['unknown'], 'intrinsic_brand_confirmed': intrinsic_confirmed, 'confirmation_conflicts': confirmation_conflicts, 'attachment_deleted': attachment_deleted, 'generated_reviewed': generated_candidates_reviewed, 'generated_main': generated_main, 'generated_variant': generated_variant, 'generated_attachment': generated_attachment, 'failed_main': failed_main, 'failed_variant': failed_variant, 'failed_attachment': failed_attachment, 'publish_blockers': len(publish_blockers), 'quarantined_products': len(quarantine_records), 'retained_products': len(retained), 'manual_overrides_applied': sum((1 for product_id, row in row_by_id.items() for url, role, _position in row_image_roles(row) if (product_id, role, url) in manual_overrides)), 'human_confirmed_quarantine': 0}
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
