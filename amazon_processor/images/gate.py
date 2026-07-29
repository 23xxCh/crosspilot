"""Image remediation and the fail-closed Amazon image-safety gate."""
from __future__ import annotations
import time
from typing import Any, Callable
from .risk import assessment_status, unknown_image_assessment
from ..concurrency import adaptive_map
from ..providers.support import ProviderQuotaError
from ..quality import AMAZON_IMAGE_GEN_CONCURRENCY
from .risk import safe_assess, validate_image_url
from .cache import is_current_assessment, save_cache

def cached_generation(cache: dict[str, Any], generation_version: str, kind: str, source_url: str) -> str:
    """Return only a current generated URL that passed structured review."""
    key = f'{kind}:{source_url}'
    generated = str(cache['gen_results'].get(key) or '')
    meta = cache['gen_meta'].get(key) or {}
    generated_assessment = meta.get('risk_assessment')
    if generated and meta.get('prompt_version') == generation_version and is_current_assessment(generated_assessment) and (assessment_status(generated_assessment) == 'safe'):
        return generated
    return ''

def _generation_route_limit() -> int:
    from ..config.env import get_int
    return max(1, min(3, get_int('IMAGE_SAFETY_REGEN_LIMIT', 2)))

def generate_safe_replacements(targets: list[tuple[str, str]], *, cache: dict[str, Any], cache_path: str | None, generation_version: str, provider_getter: Callable[[], object], concurrency_stats: dict[str, Any]) -> list[tuple[str, str]]:
    """Generate missing replacements and accept only decoded, reviewed images."""
    targets_to_generate = [item for item in targets if not cached_generation(cache, generation_version, item[1], item[0])]
    if not targets_to_generate:
        return targets_to_generate
    generation_routes = _generation_route_limit()

    def generate_one(item: tuple[str, str]) -> dict[str, Any]:
        source_url, kind = item
        provider = provider_getter()
        last_reason = 'generation_failed'
        last_assessment = unknown_image_assessment('no generated candidate')
        candidates_reviewed = 0
        for route_offset in range(generation_routes):
            try:
                generated = str(provider.call_image_gen(source_url, is_variant=kind == 'variant', context='', route_offset=route_offset) or '')
            except ProviderQuotaError:
                raise
            except Exception as exc:
                last_reason = type(exc).__name__
                continue
            valid, reason = validate_image_url(generated)
            if not valid:
                last_reason = reason
                continue
            last_assessment = safe_assess(provider, generated)
            candidates_reviewed += 1
            if assessment_status(last_assessment) == 'safe':
                return {'url': generated, 'assessment': last_assessment, 'route_offset': route_offset, 'candidates_reviewed': candidates_reviewed}
            last_reason = 'generated_image_' + assessment_status(last_assessment)
        return {'url': '', 'assessment': last_assessment, 'failure_reason': last_reason, 'candidates_reviewed': candidates_reviewed}

    def generate_done(item: tuple[str, str], result: object) -> None:
        source_url, kind = item
        key = f'{kind}:{source_url}'
        if isinstance(result, Exception) or not isinstance(result, dict):
            result = {'url': '', 'assessment': unknown_image_assessment('generation worker failed'), 'failure_reason': 'generation_worker_failed'}
        generated = str(result.get('url') or '')
        if generated:
            cache['gen_results'][key] = generated
            cache['gen_meta'][key] = {'kind': kind, 'source_url': source_url, 'prompt_version': generation_version, 'risk_assessment': result['assessment'], 'route_offset': int(result.get('route_offset') or 0), 'candidates_reviewed': int(result.get('candidates_reviewed') or 0), 'ts': int(time.time())}
            cache['gen_failures'].pop(key, None)
        else:
            cache['gen_failures'][key] = {'kind': kind, 'source_url': source_url, 'prompt_version': generation_version, 'reason': str(result.get('failure_reason') or 'generation_failed')[:120], 'risk_assessment': result.get('assessment'), 'candidates_reviewed': int(result.get('candidates_reviewed') or 0), 'ts': int(time.time())}
        save_cache(cache_path, cache)
    print(f'风险主图/变种图修复并复审: {len(targets_to_generate)} 张...', flush=True)
    _, generation_stats = adaptive_map(targets_to_generate, generate_one, operation='amazon_safe_image_gen', initial_workers=AMAZON_IMAGE_GEN_CONCURRENCY, min_workers=2, is_success=lambda value: isinstance(value, dict) and bool(value.get('url')), on_result=generate_done, terminal_exceptions=(ProviderQuotaError,), backoff_s=2, max_backoff_s=15)
    concurrency_stats['amazon_safe_image_gen'] = generation_stats
    return targets_to_generate
from collections import defaultdict
from typing import Any, Callable
from .risk import IMAGE_RISK_POLICY_VERSION, assessment_is_intrinsic_brand, assessment_status, load_confirmed_image_quarantine, unknown_image_assessment
from ..concurrency import adaptive_map
from ..providers.support import ProviderQuotaError
from ..quality import AMAZON_REVIEW_CONCURRENCY, add_audit as _add_audit, audit_text as _audit_text
from .risk import ROLE_PRIORITY, assessment_record, row_image_roles, safe_assess
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

def _review_source_images(urls: list[str], *, assessments: dict[str, dict[str, Any]], cache: dict[str, Any], cache_path: str | None, provider_getter: Callable[[], object], concurrency_stats: dict[str, Any], progress: Callable[[int, int], None] | None) -> None:
    missing = [url for url in urls if url not in assessments]
    total_work = max(1, len(missing))
    completed = 0

    def assess_one(url: str) -> dict[str, Any]:
        return safe_assess(provider_getter(), url)

    def assess_done(url: str, result: object) -> None:
        nonlocal completed
        if isinstance(result, Exception) or not isinstance(result, dict):
            result = unknown_image_assessment('assessment worker failed')
        assessments[url] = result
        completed += 1
        save_cache(cache_path, cache)
        if progress:
            progress(completed, total_work)
    if not missing:
        print(f'结构化图片初审全部缓存命中: {len(urls)} 张', flush=True)
        return
    print(f'结构化图片初审 {len(missing)} 张（{AMAZON_REVIEW_CONCURRENCY} 并发，自适应退避）...', flush=True)
    _, review_stats = adaptive_map(missing, assess_one, operation='amazon_structured_review', initial_workers=AMAZON_REVIEW_CONCURRENCY, min_workers=2, is_success=lambda value: isinstance(value, dict) and assessment_status(value) != 'unknown', on_result=assess_done, terminal_exceptions=(ProviderQuotaError,), backoff_s=2, max_backoff_s=15)
    concurrency_stats['amazon_structured_review'] = review_stats

def _confirm_intrinsic_brand_images(urls: list[str], *, assessments: dict[str, dict[str, Any]], confirmations: dict[str, dict[str, Any]], cache: dict[str, Any], cache_path: str | None, provider_getter: Callable[[], object], concurrency_stats: dict[str, Any]) -> None:
    intrinsic_urls = [url for url in urls if assessment_is_intrinsic_brand(assessments.get(url))]
    confirm_missing = [url for url in intrinsic_urls if url not in confirmations]
    if not confirm_missing:
        return

    def confirm_one(url: str) -> dict[str, Any]:
        return safe_assess(provider_getter(), url, confirmation=True)

    def confirm_done(url: str, result: object) -> None:
        if isinstance(result, Exception) or not isinstance(result, dict):
            result = unknown_image_assessment('confirmation worker failed')
        confirmations[url] = result
        save_cache(cache_path, cache)
    _, confirm_stats = adaptive_map(confirm_missing, confirm_one, operation='amazon_high_risk_confirmation', initial_workers=min(AMAZON_REVIEW_CONCURRENCY, max(1, len(confirm_missing))), min_workers=1, is_success=lambda value: isinstance(value, dict) and assessment_status(value) != 'unknown', on_result=confirm_done, terminal_exceptions=(ProviderQuotaError,), backoff_s=2, max_backoff_s=15)
    concurrency_stats['amazon_high_risk_confirmation'] = confirm_stats

def run_structured_image_safety_gate(data: list[dict[str, Any]], cache_path: str | None=None, quality_issues: list[str] | None=None, progress: Callable[[int, int], None] | None=None, runtime_metrics: dict[str, Any] | None=None, provider_getter: Callable[[], object] | None=None) -> list[dict[str, Any]]:
    """Audit every source image, remediate risks, and fail closed."""
    if provider_getter is None:
        raise ValueError('structured image gate requires provider_getter')
    quality_issues = quality_issues if quality_issues is not None else []
    runtime_metrics = runtime_metrics if isinstance(runtime_metrics, dict) else {}
    concurrency_stats = runtime_metrics.setdefault('concurrency', {})
    review_version, generation_version = current_cache_versions()
    cache = load_cache(cache_path, review_version, generation_version)
    assessments = cache['risk_assessments']
    confirmations = cache['risk_confirmations']
    gen_meta = cache['gen_meta']
    gen_failures = cache['gen_failures']
    manual_overrides = load_manual_overrides(cache_path)

    def effective_assessment(product_id: str, role: str, url: str) -> dict[str, Any]:
        override = manual_overrides.get((str(product_id), str(role), str(url)))
        if override:
            return manual_safe_assessment(override)
        return assessments.get(url) or unknown_image_assessment()
    usage, row_by_id = _index_images(data)
    urls = sorted(usage)
    _review_source_images(urls, assessments=assessments, cache=cache, cache_path=cache_path, provider_getter=provider_getter, concurrency_stats=concurrency_stats, progress=progress)
    _confirm_intrinsic_brand_images(urls, assessments=assessments, confirmations=confirmations, cache=cache, cache_path=cache_path, provider_getter=provider_getter, concurrency_stats=concurrency_stats)
    quarantine: dict[str, list[dict[str, Any]]] = defaultdict(list)
    records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    intrinsic_confirmed = 0
    confirmation_conflicts = 0
    confirmed_quarantine = load_confirmed_image_quarantine()
    for url in urls:
        first = assessments[url]
        for item in usage[url]:
            effective = effective_assessment(item['product_id'], item['role'], url)
            records[item['product_id']].append(assessment_record(url=url, role=item['role'], assessment=effective, source='source'))
        if not assessment_is_intrinsic_brand(first):
            continue
        second = confirmations.get(url) or unknown_image_assessment('second confirmation unavailable')
        if assessment_is_intrinsic_brand(second):
            intrinsic_confirmed += 1
            code = 'intrinsic_brand_product'
            message = '两次图审均确认商品本体/包装含品牌徽章、Logo 或品牌文字'
        else:
            confirmation_conflicts += 1
            code = 'high_risk_confirmation_conflict'
            message = '两次高危图审结果冲突或二次判断未知，已进入人工隔离区'
        for affected in usage[url]:
            product_id = affected['product_id']
            if (product_id, affected['role'], url) in manual_overrides:
                continue
            quarantine[product_id].append({'code': code, 'role': affected['role'], 'url': url, 'message': message, 'assessment': first, 'confirmation': second})
    for product_id, block in confirmed_quarantine.items():
        if product_id not in row_by_id:
            continue
        quarantine[product_id].append({'code': 'human_confirmed_image_risk', 'role': 'product', 'url': '', 'message': block.get('reason') or '人工确认图片含品牌/Logo 高危视觉', 'assessment': {'status': 'risk', 'risk_categories': ['brand_logo'], 'placement': 'unknown', 'confidence': 1.0, 'evidence': '历史人工终审确认', 'manual_confirmation': True, 'source': block.get('source', '')}})
    attachment_deleted = 0
    for product_id, row in row_by_id.items():
        before = list(row.get('extra_imgs') or [])
        kept = [url for url in before if assessment_status(effective_assessment(product_id, 'attachment', url)) == 'safe']
        attachment_deleted += len(before) - len(kept)
        row['extra_imgs'] = kept
        if _audit_text(before) != _audit_text(kept):
            _add_audit(row, '图片安全门', 'attachment_image', before, kept, method='structured_vision', reason='drop_risk_or_unknown_attachment', severity='warning', action='终审确认被删除附图的风险标签与证据')
    remediation_targets: list[tuple[str, str]] = []
    for url in sorted(urls):
        status = assessment_status(assessments.get(url))
        roles = {item['role'] for item in usage[url] if assessment_status(effective_assessment(item['product_id'], item['role'], url)) != 'safe'}
        if status == 'risk' and (not assessment_is_intrinsic_brand(assessments.get(url))):
            if 'main' in roles:
                remediation_targets.append((url, 'main'))
            if 'variant' in roles:
                remediation_targets.append((url, 'variant'))
        elif status == 'unknown':
            for item in usage[url]:
                if item['role'] not in {'main', 'variant'}:
                    continue
                if assessment_status(effective_assessment(item['product_id'], item['role'], url)) == 'safe':
                    continue
                quarantine[item['product_id']].append({'code': f"unknown_{item['role']}_image", 'role': item['role'], 'url': url, 'message': '主图/变种图无法可靠判断，按高安全策略隔离', 'assessment': assessments[url]})
    targets_to_generate = generate_safe_replacements(remediation_targets, cache=cache, cache_path=cache_path, generation_version=generation_version, provider_getter=provider_getter, concurrency_stats=concurrency_stats)
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
    generated_main = generated_variant = 0
    failed_main = failed_variant = 0
    for product_id, row in row_by_id.items():
        main = str(row.get('main_img') or '')
        main_status = assessment_status(effective_assessment(product_id, 'main', main))
        if main_status == 'risk':
            replacement = cached_generation(cache, generation_version, 'main', main)
            if replacement:
                row['main_img'] = replacement
                generated_main += 1
                meta = gen_meta[f'main:{main}']
                records[product_id].append(assessment_record(url=replacement, role='main', assessment=meta['risk_assessment'], source='generated', source_url=main))
            elif not assessment_is_intrinsic_brand(effective_assessment(product_id, 'main', main)):
                failed_main += 1
                quarantine[product_id].append({'code': 'main_image_remediation_failed', 'role': 'main', 'url': main, 'message': '风险主图生成失败或生成后复审仍有风险', 'assessment': assessments.get(main), 'generation_failure': gen_failures.get(f'main:{main}')})
        before_variants = list(row.get('var_imgs') or [])
        remediated_variants: list[str] = []
        for source_url in before_variants:
            status = assessment_status(effective_assessment(product_id, 'variant', source_url))
            if status != 'risk':
                remediated_variants.append(source_url)
                continue
            replacement = cached_generation(cache, generation_version, 'variant', source_url)
            if replacement:
                remediated_variants.append(replacement)
                generated_variant += 1
                meta = gen_meta[f'variant:{source_url}']
                records[product_id].append(assessment_record(url=replacement, role='variant', assessment=meta['risk_assessment'], source='generated', source_url=source_url))
            else:
                remediated_variants.append(source_url)
                if not assessment_is_intrinsic_brand(effective_assessment(product_id, 'variant', source_url)):
                    failed_variant += 1
                    quarantine[product_id].append({'code': 'variant_image_remediation_failed', 'role': 'variant', 'url': source_url, 'message': '风险变种图生成失败或生成后复审仍有风险', 'assessment': assessments.get(source_url), 'generation_failure': gen_failures.get(f'variant:{source_url}')})
        row['var_imgs'] = remediated_variants
        row['var_img'] = remediated_variants[0] if remediated_variants else ''
        row['_image_assessments'] = sorted(records[product_id], key=lambda item: (ROLE_PRIORITY.get(item['role'], 9), item['source'] != 'source'))
    quarantine_records: list[dict[str, Any]] = []
    for product_id, reasons in quarantine.items():
        if not reasons:
            continue
        row = row_by_id[product_id]
        row['_quarantined'] = True
        row['_quarantine_reasons'] = reasons
        bullets = list(row.get('bullets') or [])
        quarantine_records.append({'product_id': product_id, 'title': str(row.get('title') or ''), 'reasons': reasons, 'images': row.get('_image_assessments') or [], 'source_row': {'title': str(row.get('title') or ''), 'description': str(row.get('desc') or ''), 'bullets': [str(bullets[index] or '') if index < len(bullets) else '' for index in range(5)], 'keywords': str(row.get('keywords') or '')}})
    retained = [row for row in data if not row.get('_quarantined')]
    status_counts = {status: sum((1 for value in assessments.values() if assessment_status(value) == status)) for status in ('safe', 'risk', 'unknown')}
    safety_metrics = {'policy_version': IMAGE_RISK_POLICY_VERSION, 'source_images': len(urls), 'reviewed': len(assessments), **status_counts, 'intrinsic_brand_confirmed': intrinsic_confirmed, 'confirmation_conflicts': confirmation_conflicts, 'attachment_deleted': attachment_deleted, 'generated_reviewed': generated_candidates_reviewed, 'generated_main': generated_main, 'generated_variant': generated_variant, 'failed_main': failed_main, 'failed_variant': failed_variant, 'quarantined_products': len(quarantine_records), 'retained_products': len(retained), 'manual_overrides_applied': sum((1 for product_id, row in row_by_id.items() for url, role, _position in row_image_roles(row) if (product_id, role, url) in manual_overrides)), 'human_confirmed_quarantine': sum((product_id in row_by_id for product_id in confirmed_quarantine))}
    runtime_metrics['image_safety_gate'] = safety_metrics
    runtime_metrics['quarantined_products'] = quarantine_records
    runtime_metrics['image_assessments'] = {url: dict(value) for url, value in assessments.items()}
    runtime_metrics['image_confirmations'] = {url: dict(value) for url, value in confirmations.items()}
    runtime_metrics['image_remediation'] = {'reviewed': len(assessments), 'flagged': status_counts['risk'], 'clean_retained': status_counts['safe'], 'unknown_retained': 0, 'attachment_reviewed': sum((1 for url, items in usage.items() if any((item['role'] == 'attachment' for item in items)))), 'attachment_flagged': sum((1 for url, items in usage.items() if any((item['role'] == 'attachment' for item in items)) and assessment_status(assessments.get(url)) != 'safe')), 'attachment_deleted': attachment_deleted, 'generated_candidates_reviewed': generated_candidates_reviewed, 'generated_main': generated_main, 'generated_variant': generated_variant, 'failed_main': failed_main, 'failed_variant': failed_variant, 'generation_url_checked': len(targets_to_generate), 'generation_url_valid': generated_main + generated_variant, 'generation_url_invalid': failed_main + failed_variant}
    if quarantine_records:
        quality_issues.append(f'图片安全门隔离 {len(quarantine_records)} 个商品，未写入正式回填表')
    save_cache(cache_path, cache)
    print(f"图片安全门完成: 安全 {status_counts['safe']}，风险 {status_counts['risk']}，未知 {status_counts['unknown']}，删附图 {attachment_deleted}，隔离商品 {len(quarantine_records)}", flush=True)
    if progress:
        progress(1, 1)
    return retained
