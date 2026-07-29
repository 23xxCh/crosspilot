"""Deterministic Amazon title branding and compatibility formatting."""
from __future__ import annotations

import re
from dataclasses import dataclass

from services.constants import (
    COMPATIBILITY_BRAND_ALIASES,
    COMPATIBILITY_BRANDS,
    STRIP_ONLY_BRANDS,
    compile_brand_pattern,
)


TITLE_MAX_LENGTH = 75

_COMPATIBILITY_RE = compile_brand_pattern(COMPATIBILITY_BRANDS)
_STRIP_ONLY_RE = compile_brand_pattern(STRIP_ONLY_BRANDS)
_FIT_MARKER_RE = re.compile(
    r'(?<![A-Za-z0-9])'
    r'(?:fits?\s+for|fitment\s+for|compatible\s+with|for)'
    r'(?![A-Za-z0-9])\s*',
    re.IGNORECASE,
)
_LEADING_NOISE_RE = re.compile(
    r'^(?:(?:fits?\s+for|fitment\s+for|compatible\s+with|for)\s+)+',
    re.IGNORECASE,
)
_GENERIC_PREFIX_RE = re.compile(r'^\[?generic\]?\b[\s:,-]*', re.IGNORECASE)
_GENERIC_ANY_RE = re.compile(r'(?<![A-Za-z0-9])\[?generic\]?(?![A-Za-z0-9])', re.IGNORECASE)
_DANGLING_FOR_RE = re.compile(r'\s+\bfor\b[\s:,-]*$', re.IGNORECASE)
_SPACE_RE = re.compile(r'\s+')

# These words identify the beginning of the product phrase when a source title
# starts with "For Brand Model ...". Ambiguous brand-like ordinary words such
# as Seat and Mini are intentionally absent from the brand registry.
_PRODUCT_BOUNDARY_WORDS = {
    'accessory', 'accessories', 'adapter', 'antenna', 'assembly', 'auto',
    'automotive', 'badge', 'bracket', 'bumper', 'cable', 'camera', 'cap',
    'car', 'charger', 'clip', 'connector', 'control', 'cover', 'cushion',
    'dashboard', 'decal', 'diffuser', 'door', 'emblem', 'fender', 'filter',
    'frame', 'front', 'gasket', 'guard', 'handle', 'holder', 'hose', 'key',
    'kit', 'knob', 'lamp', 'left', 'light', 'lip', 'lock', 'mat', 'mirror',
    'molding', 'moulding', 'mount', 'nozzle', 'organiser', 'organizer',
    'panel', 'pedal', 'pin', 'pipe', 'plug', 'protector', 'rear',
    'replacement', 'right', 'rivet', 'seal', 'sensor', 'shade', 'spoiler',
    'spray', 'sticker', 'strip', 'switch', 'trim', 'vehicle', 'washer',
    'wheel', 'windshield', 'wire', 'wiper', 'universal',
}
_SPEC_TOKEN_RE = re.compile(
    r'^(?:'
    r'\d+(?:[.,]\d+)?(?:x|×|pcs?|pack|pin|mm|cm|m|in|inch|v|w|a|oz|lb)'
    r'|\d+\s*[-/]\s*\d+'
    r')$',
    re.IGNORECASE,
)
_COMPATIBILITY_CONTINUATION_RE = re.compile(
    r'^(?:'
    r'(?:19|20)\d{2}(?:\s*[-/]\s*(?:19|20)\d{2})?'
    r'|\d+(?:x|×|pcs?|pack)'
    r')$',
    re.IGNORECASE,
)
_LOW_PRIORITY_WORDS = {
    'accessories', 'accessory', 'automotive', 'auto', 'universal', 'style',
}


@dataclass(frozen=True)
class TitleNormalization:
    """Normalized title plus audit metadata used by delivery scripts."""

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
    return COMPATIBILITY_BRAND_ALIASES.get(
        alias,
        COMPATIBILITY_BRAND_ALIASES.get(match.group(0).lower(), match.group(0)),
    )


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
    for token_match in re.finditer(r'\S+', text[brand_end:]):
        token = token_match.group(0).strip(' ,;:()[]')
        lowered = token.lower()
        if _COMPATIBILITY_CONTINUATION_RE.match(lowered):
            end = brand_end + token_match.end()
            token_count += 1
            continue
        if (
            lowered in _PRODUCT_BOUNDARY_WORDS
            or _SPEC_TOKEN_RE.match(lowered)
        ):
            break
        end = brand_end + token_match.end()
        token_count += 1
        if token_count >= 6:
            break
    return end


def _extract_target_and_product(title: str) -> tuple[str, str]:
    """Return (compatibility target, product phrase) without fabricating data."""
    working = _clean_spaces(title)
    working = re.sub(
        r'^(?:for\s+)?\[?generic\]?\b[\s:,-]*',
        '',
        working,
        flags=re.IGNORECASE,
    )

    # Prefer an explicit trailing compatibility clause. It is already
    # structurally unambiguous and keeping the full suffix is idempotent.
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
            return target, product

        target_end = _target_prefix_end(working, brand.end())
        target = _canonicalize_target(working[brand.start():target_end])
        product = _clean_spaces(working[target_end:])
        return target, product

    brand = _COMPATIBILITY_RE.search(working)
    if not brand:
        return '', working

    target_end = _target_prefix_end(working, brand.end())
    target = _canonicalize_target(working[brand.start():target_end])
    product = _clean_spaces(
        working[:brand.start()] + ' ' + working[target_end:]
    )
    return target, product


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
    # Defensive last resort for an unusually long compatibility string.
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
    should_normalize = bool(
        _COMPATIBILITY_RE.search(original)
        or _STRIP_ONLY_RE.search(original)
        or _GENERIC_ANY_RE.search(original)
        or _LEADING_NOISE_RE.search(original)
        or _DANGLING_FOR_RE.search(original)
    )
    if not should_normalize:
        normalized = _clamp_plain_title(original)
        return TitleNormalization(
            title=normalized,
            compatibility='',
            changed=normalized != original,
        )
    target, product = _extract_target_and_product(original)
    product = _clean_product(product)
    if not product:
        # Keep a non-empty source phrase rather than inventing a product noun.
        product = _clean_product(original) or original
    normalized = _build_title(product, target)
    return TitleNormalization(
        title=normalized,
        compatibility=target,
        changed=normalized != original,
    )


def normalize_amazon_title(title: str) -> str:
    return normalize_amazon_title_details(title).title


__all__ = [
    'TITLE_MAX_LENGTH',
    'TitleNormalization',
    'normalize_amazon_title',
    'normalize_amazon_title_details',
]
