"""Deterministic Amazon title policy and AI-assisted title optimization."""
from __future__ import annotations
import re
from dataclasses import dataclass
from ..policy import COMPATIBILITY_BRAND_ALIASES, COMPATIBILITY_BRANDS, STRIP_ONLY_BRANDS, compile_brand_pattern
TITLE_MAX_LENGTH = 74
_COMPATIBILITY_RE = compile_brand_pattern(COMPATIBILITY_BRANDS)
_STRIP_ONLY_RE = compile_brand_pattern(STRIP_ONLY_BRANDS)
_FIT_MARKER_RE = re.compile('(?<![A-Za-z0-9])(?:fits?\\s+for|fitment\\s+for|compatible\\s+with|for)(?![A-Za-z0-9])\\s*', re.IGNORECASE)
_LEADING_NOISE_RE = re.compile('^(?:(?:fits?\\s+for|fitment\\s+for|compatible\\s+with|for)\\s+)+', re.IGNORECASE)
_GENERIC_PREFIX_RE = re.compile('^\\[?generic\\]?\\b[\\s:,-]*', re.IGNORECASE)
_GENERIC_ANY_RE = re.compile('(?<![A-Za-z0-9])\\[?generic\\]?(?![A-Za-z0-9])', re.IGNORECASE)
_DANGLING_FOR_RE = re.compile('\\s+\\bfor\\b[\\s:,-]*$', re.IGNORECASE)
_SPACE_RE = re.compile('\\s+')
_PRODUCT_BOUNDARY_WORDS = {'accessory', 'accessories', 'adapter', 'antenna', 'assembly', 'auto', 'automotive', 'badge', 'bracket', 'bumper', 'cable', 'camera', 'cap', 'car', 'charger', 'clip', 'connector', 'control', 'cover', 'cushion', 'dashboard', 'decal', 'diffuser', 'door', 'emblem', 'fender', 'filter', 'frame', 'front', 'gasket', 'guard', 'handle', 'holder', 'hose', 'key', 'kit', 'knob', 'lamp', 'left', 'light', 'lip', 'lock', 'mat', 'mirror', 'molding', 'moulding', 'mount', 'nozzle', 'organiser', 'organizer', 'panel', 'pedal', 'pin', 'pipe', 'plug', 'protector', 'rear', 'replacement', 'right', 'rivet', 'seal', 'sensor', 'shade', 'spoiler', 'spray', 'sticker', 'strip', 'switch', 'trim', 'vehicle', 'washer', 'wheel', 'windshield', 'wire', 'wiper', 'universal'}
_SPEC_TOKEN_RE = re.compile('^(?:\\d+(?:[.,]\\d+)?(?:x|×|pcs?|pack|pin|mm|cm|m|in|inch|v|w|a|oz|lb)|\\d+\\s*[-/]\\s*\\d+)$', re.IGNORECASE)
_COMPATIBILITY_CONTINUATION_RE = re.compile('^(?:(?:19|20)\\d{2}(?:\\s*[-/]\\s*(?:19|20)\\d{2})?|\\d+(?:x|×|pcs?|pack))$', re.IGNORECASE)
_LOW_PRIORITY_WORDS = {'accessories', 'accessory', 'automotive', 'auto', 'universal', 'style'}

@dataclass(frozen=True)
class TitleNormalization:
    """Normalized title plus audit metadata used by the delivery Module."""
    title: str
    compatibility: str
    changed: bool

def _clean_spaces(value: str) -> str:
    value = _SPACE_RE.sub(' ', str(value or '')).strip()
    return value.strip(' ,;:-')

def _canonical_brand(match: re.Match) -> str:
    alias = _SPACE_RE.sub(' ', match.group(0).replace('-', ' ')).lower()
    if alias == 'mercedes benz':
        alias = 'mercedes benz'
    return COMPATIBILITY_BRAND_ALIASES.get(alias, COMPATIBILITY_BRAND_ALIASES.get(match.group(0).lower(), match.group(0)))

def _canonicalize_target(target: str) -> str:
    target = _clean_spaces(target)
    match = _COMPATIBILITY_RE.search(target)
    if not match:
        return ''
    canonical = _canonical_brand(match)
    target = target[:match.start()] + canonical + target[match.end():]
    target = _LEADING_NOISE_RE.sub('', target)
    target = _GENERIC_PREFIX_RE.sub('', target)
    return _clean_spaces(target)

def _target_prefix_end(text: str, brand_end: int) -> int:
    """Find where a leading Brand/Model phrase ends and product words begin."""
    end = brand_end
    token_count = 0
    for token_match in re.finditer('\\S+', text[brand_end:]):
        token = token_match.group(0).strip(' ,;:()[]')
        lowered = token.lower()
        if _COMPATIBILITY_CONTINUATION_RE.match(lowered):
            end = brand_end + token_match.end()
            token_count += 1
            continue
        if lowered in _PRODUCT_BOUNDARY_WORDS or _SPEC_TOKEN_RE.match(lowered):
            break
        end = brand_end + token_match.end()
        token_count += 1
        if token_count >= 6:
            break
    return end

def _extract_target_and_product(title: str) -> tuple[str, str]:
    """Return (compatibility target, product phrase) without fabricating data."""
    working = _clean_spaces(title)
    working = re.sub('^(?:for\\s+)?\\[?generic\\]?\\b[\\s:,-]*', '', working, flags=re.IGNORECASE)
    explicit = []
    for marker in _FIT_MARKER_RE.finditer(working):
        brand = _COMPATIBILITY_RE.match(working, marker.end())
        if brand:
            explicit.append((marker, brand))
    if explicit:
        marker, brand = explicit[-1]
        if marker.start() > 0:
            target = _canonicalize_target(working[brand.start():])
            product = _clean_spaces(working[:marker.start()])
            return (target, product)
        target_end = _target_prefix_end(working, brand.end())
        target = _canonicalize_target(working[brand.start():target_end])
        product = _clean_spaces(working[target_end:])
        return (target, product)
    brand = _COMPATIBILITY_RE.search(working)
    if not brand:
        return ('', working)
    target_end = _target_prefix_end(working, brand.end())
    target = _canonicalize_target(working[brand.start():target_end])
    product = _clean_spaces(working[:brand.start()] + ' ' + working[target_end:])
    return (target, product)

def _clean_product(product: str) -> str:
    product = _COMPATIBILITY_RE.sub(' ', product)
    product = _STRIP_ONLY_RE.sub(' ', product)
    product = _GENERIC_ANY_RE.sub(' ', product)
    product = _clean_spaces(product)
    previous = None
    while product != previous:
        previous = product
        product = _GENERIC_PREFIX_RE.sub('', product)
        product = _LEADING_NOISE_RE.sub('', product)
        product = _clean_spaces(product)
    product = _DANGLING_FOR_RE.sub('', product)
    return _clean_spaces(product)

def _trim_product(product: str, limit: int) -> str:
    if len(product) <= limit:
        return product
    compacted = product.split()
    for index in range(len(compacted) - 1, -1, -1):
        if compacted[index].strip(' ,;:-').lower() in _LOW_PRIORITY_WORDS:
            compacted.pop(index)
            candidate = ' '.join(compacted)
            if len(candidate) <= limit:
                return candidate
    shortened = product[:max(1, limit)]
    if ' ' in shortened:
        shortened = shortened.rsplit(' ', 1)[0]
    return shortened.strip()

def _build_title(product: str, target: str) -> str:
    prefix = 'Generic'
    suffix = f' for {target}' if target else ''
    product_limit = TITLE_MAX_LENGTH - len(prefix) - len(suffix) - 1
    product = _trim_product(product, max(1, product_limit))
    result = _clean_spaces(f'{prefix} {product}{suffix}')
    if len(result) <= TITLE_MAX_LENGTH:
        return result
    shortened = result[:TITLE_MAX_LENGTH]
    if ' ' in shortened:
        shortened = shortened.rsplit(' ', 1)[0]
    return shortened[:TITLE_MAX_LENGTH].strip()

def _clamp_plain_title(title: str) -> str:
    if len(title) <= TITLE_MAX_LENGTH:
        return title
    shortened = title[:TITLE_MAX_LENGTH]
    if ' ' in shortened:
        shortened = shortened.rsplit(' ', 1)[0]
    return shortened[:TITLE_MAX_LENGTH].strip()

def normalize_amazon_title_details(title: str) -> TitleNormalization:
    """Apply the Generic product [for compatibility] title contract."""
    original = _clean_spaces(title)
    if not original:
        return TitleNormalization('', '', False)
    should_normalize = bool(_COMPATIBILITY_RE.search(original) or _STRIP_ONLY_RE.search(original) or _GENERIC_ANY_RE.search(original) or _LEADING_NOISE_RE.search(original) or _DANGLING_FOR_RE.search(original))
    if not should_normalize:
        normalized = _clamp_plain_title(original)
        return TitleNormalization(title=normalized, compatibility='', changed=normalized != original)
    target, product = _extract_target_and_product(original)
    product = _clean_product(product)
    if not product:
        product = _clean_product(original) or original
    normalized = _build_title(product, target)
    return TitleNormalization(title=normalized, compatibility=target, changed=normalized != original)

def normalize_amazon_title(title: str) -> str:
    return normalize_amazon_title_details(title).title
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
from ..config.prompts import get_prompt_registry
from ..providers import ProviderQuotaError, get_provider as _default_get_provider
from ..log import log as _log
from ..quality import AMAZON_TITLE_CONCURRENCY, add_audit as _add_audit, add_quality_issue as _add_quality_issue, missing_factual_markers as _missing_factual_markers, unexpected_brand_markers as _unexpected_brand_markers
from .locale import market_prompt_values, normalize_localized_title
QuotaExhaustedError = ProviderQuotaError
_prompts = get_prompt_registry()

def optimize_titles(data, progress=None, provider_getter=None):
    """Apply the Generic compatibility contract and refine search ordering."""
    print(f'标题优化 {len(data)} 条...', flush=True)
    changed = 0
    for index, row in enumerate(data):
        original_title = str(row['title'] or '').strip()
        title = original_title
        if not title:
            continue
        title = normalize_title(title)
        if title != row['title']:
            row['title'] = title
            changed += 1
            _add_audit(row, '标题优化', 'title', original_title, title, method='rule', reason='normalize_title', action='确认标题为副标题预留展示空间')
        if progress:
            progress(index + 1, max(1, len(data) * 2))
    print(f'  规则优化: {changed} 行', flush=True)
    to_optimize = [(index, row) for index, row in enumerate(data) if row['title']]
    if to_optimize:
        print(f'  API 优化 {len(to_optimize)} 条（DeepSeek，{AMAZON_TITLE_CONCURRENCY} 并发）...', flush=True)

        def optimize_one(index, row):
            try:
                provider = (provider_getter or _default_get_provider)()
                result = provider.call_text(
                    _prompts.render(
                        'amazon.title_optimize',
                        title=row['title'],
                        **market_prompt_values(row),
                    ),
                    max_tokens=128,
                )
                if result:
                    return (index, result.strip())
            except QuotaExhaustedError:
                raise
            except Exception as exc:
                _log.warn('标题优化异常', error=str(exc))
            return (index, None)
        done = 0
        with ThreadPoolExecutor(max_workers=AMAZON_TITLE_CONCURRENCY) as pool:
            futures = {pool.submit(optimize_one, index, row): index for index, row in to_optimize}
            for future in as_completed(futures):
                try:
                    index, new_title = future.result()
                except QuotaExhaustedError:
                    for pending in futures:
                        pending.cancel()
                    raise
                if new_title:
                    if re.search('(optimize|need to|brand is generic|Rules:|这里|以下)', new_title, re.IGNORECASE):
                        _log.warn('标题优化返回meta文本，回退规则处理', row=index)
                        _add_quality_issue(data[index], 'title_ai_fallback', '标题模型返回了说明性文本，已使用规则结果')
                    else:
                        normalized = normalize_localized_title(
                            new_title,
                            data[index].get('site') or 'US',
                        )
                        missing = _missing_factual_markers(data[index].get('title', ''), normalized)
                        unexpected_brands = _unexpected_brand_markers(data[index].get('title', ''), normalized)
                        if missing or unexpected_brands:
                            reason = 'title_fact_loss' if missing else 'title_brand_hallucination'
                            details = '丢失关键规格：' + ', '.join(missing[:5]) if missing else '新增源标题没有的品牌：' + ', '.join(unexpected_brands[:5])
                            _add_audit(data[index], '标题优化', 'title', data[index].get('title', ''), normalized, method='ai_rejected', reason=reason, severity='warning', action='已拒绝 AI 标题（' + details + '），保留规则结果')
                        else:
                            before_title = data[index].get('title', '')
                            data[index]['title'] = normalized
                            _add_audit(data[index], '标题优化', 'title', before_title, normalized, method='ai', reason='model_refine', action='抽样确认 AI 标题未丢失规格/数量/适配信息')
                else:
                    _add_quality_issue(data[index], 'title_ai_fallback', '标题模型未返回有效结果，已使用规则结果')
                done += 1
                if progress:
                    progress(len(data) + done, max(1, len(data) * 2))
                if done % 10 == 0:
                    print(f'    API标题: {done}/{len(to_optimize)}', flush=True)
    for row in data:
        before_title = row.get('title', '')
        row['title'] = normalize_localized_title(
            before_title,
            row.get('site') or 'US',
        )
        if row['title'] != before_title:
            _add_audit(row, '标题优化', 'title', before_title, row['title'], method='rule', reason='final_normalize', action='确认最终标题仍保留硬规格')
    if progress:
        progress(1, 1)
    return data

def clamp_title(title):
    title = str(title or '').strip()
    if len(title) <= TITLE_MAX_LENGTH:
        return title
    shortened = title[:TITLE_MAX_LENGTH]
    if ' ' in shortened:
        shortened = shortened.rsplit(' ', 1)[0]
    return shortened[:TITLE_MAX_LENGTH].strip()

def normalize_title(title):
    return normalize_amazon_title(title)
