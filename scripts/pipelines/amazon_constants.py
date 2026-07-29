#!/usr/bin/env python3
"""Shared Amazon listing rules, prompts, and quality helpers."""
from __future__ import annotations

import re

from crosspilot.prompt_registry import get_prompt_registry

from concurrency import configured_concurrency
from services.constants import compile_brand_pattern

AMAZON_TITLE_CONCURRENCY = configured_concurrency('text', 10, maximum=50)
AMAZON_DESC_CONCURRENCY = configured_concurrency('text', 10, maximum=50)
AMAZON_BULLET_CONCURRENCY = configured_concurrency('text', 20, maximum=50)
AMAZON_REVIEW_CONCURRENCY = configured_concurrency('review', 100, maximum=150)
AMAZON_IMAGE_GEN_CONCURRENCY = configured_concurrency(
    'image_gen',
    20,
    maximum=40,
)
AMAZON_IMAGE_GEN_ATTEMPTS = 1

# Brand patterns for cleaning
_BRAND_RE = compile_brand_pattern()
_OEM_RE = re.compile(r'\b(OEM|Original|Factory|原厂|原装|正品|Genuine)\b', re.IGNORECASE)
_LOGISTICS_RE = re.compile(r'(交货时间|发货时间|运输方式|快递|物流|Shipping|Delivery|Express|Freight|Carrier)[：:].*?(?=\n|$)', re.IGNORECASE)
_RETURN_RE = re.compile(r'(退货|退款|Return|Refund|Warranty|保修|Payment|支付).*?(?=\n|$)', re.IGNORECASE)
_IMG_RE = re.compile(r'<img[^>]*>', re.IGNORECASE)


_ROW_QUALITY_LABELS = {
    'missing_source_description': '源产品描述缺失',
    'title_ai_fallback': '标题 AI 优化降级为规则处理',
    'title_fact_loss': '标题关键规格可能丢失',
    'description_ai_fallback': '描述 AI 清洗降级为规则处理',
    'description_fact_loss': '描述关键规格可能丢失',
    'bullet_rule_fallback': 'Bullet/关键词由规则补全',
    'bullet_quality_warning': 'Bullet 内容质量需要复核',
    'keyword_quality_warning': '关键词内容质量需要复核',
    'main_image_generation_failed': '风险主图生成失败并保留原图',
    'variant_image_generation_failed': '风险变种图生成失败并保留原图',
}

_META_TEXT_RE = re.compile(
    r'(as an ai|i cannot|here are|optimized title|cleaned description|'
    r'bullet points|search keywords|json|markdown|rules?:|requirements?:)',
    re.IGNORECASE,
)
_STOP_TERMS = {
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'for', 'from', 'in',
    'into', 'is', 'it', 'its', 'of', 'on', 'or', 'our', 'that', 'the',
    'this', 'to', 'with', 'you', 'your',
}
_GENERIC_TERMS = {
    'best', 'excellent', 'generic', 'good', 'great', 'high', 'hot', 'item',
    'new', 'nice', 'perfect', 'premium', 'product', 'quality', 'sale',
    'useful', 'value',
}
_FACT_RE = re.compile(
    r'\b(?:19|20)\d{2}\b'
    r'|\b\d+(?:[./-]\d+)?\s*(?:mm|cm|m|inch|inches|in|ft|kg|g|lb|lbs|oz|'
    r'v|volt|volts|w|watt|watts|l|ml|pcs|pc|pack|packs|piece|pieces|'
    r'key|keys|pin|pins)\b'
    r'|\b[A-Z]{1,5}\d{1,5}[A-Z0-9-]*\b'
    r'|\b\d+[A-Z]{1,5}\b',
    re.IGNORECASE,
)


def _plain_text(value):
    text = str(value or '')
    text = _IMG_RE.sub(' ', text)
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


_MAX_ROW_AUDIT_ITEMS = 20
_MAX_VALIDATION_AUDIT_ITEMS = 120


def _audit_text(value, limit=180):
    if isinstance(value, (list, tuple)):
        text = ' | '.join(
            str(item or '').strip()
            for item in value
            if str(item or '').strip()
        )
    else:
        text = str(value or '')
    text = re.sub(r'\s+', ' ', text).strip()
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _add_audit(
    row,
    stage,
    field,
    before,
    after,
    *,
    method='rule',
    reason='',
    severity='info',
    action='',
):
    """Attach a bounded per-row audit entry for review UI and CSV evidence."""
    before_text = _audit_text(before)
    after_text = _audit_text(after)
    reason = str(reason or '').strip()
    if before_text == after_text and severity not in {'warning', 'review', 'error'}:
        return
    audit = row.setdefault('_audit', [])
    if len(audit) >= _MAX_ROW_AUDIT_ITEMS:
        return
    key = (stage, field, method, reason, before_text, after_text)
    for item in audit:
        existing = (
            item.get('stage'),
            item.get('field'),
            item.get('method'),
            item.get('reason'),
            item.get('before'),
            item.get('after'),
        )
        if existing == key:
            return
    audit.append({
        'stage': str(stage or ''),
        'field': str(field or ''),
        'method': str(method or ''),
        'reason': reason,
        'before': before_text,
        'after': after_text,
        'severity': severity,
        'action': str(action or ''),
    })


_ISSUE_AUDIT_META = {
    'missing_source_description': ('读取表格', 'description', 'review'),
    'title_ai_fallback': ('标题优化', 'title', 'fallback'),
    'title_fact_loss': ('标题优化', 'title', 'review'),
    'description_ai_fallback': ('描述清洗', 'description', 'fallback'),
    'description_fact_loss': ('描述清洗', 'description', 'review'),
    'bullet_rule_fallback': ('Bullet+关键词', 'Bullet', 'fallback'),
    'bullet_quality_warning': ('Bullet+关键词', 'Bullet', 'review'),
    'keyword_quality_warning': ('Bullet+关键词', 'keywords', 'review'),
}


def _add_quality_issue(row, code, message):
    """Attach a de-duplicated internal quality issue to a source row."""
    issues = row.setdefault('_quality_issues', [])
    if not any(issue.get('code') == code for issue in issues):
        issues.append({'code': code, 'message': message})
        stage, field, method = _ISSUE_AUDIT_META.get(
            code,
            ('质量检查', '', 'review'),
        )
        if field:
            value = row.get('desc') if field == 'description' else row.get(field, '')
            _add_audit(
                row,
                stage,
                field,
                value,
                value,
                method=method,
                reason=code,
                severity='warning',
                action=message,
            )


def _summarize_audit_trail(data, max_items=_MAX_VALIDATION_AUDIT_ITEMS):
    items = []
    for row_number, row in enumerate(data, 1):
        for entry in row.get('_audit', []):
            if not isinstance(entry, dict):
                continue
            normalized = {
                'row': row_number,
                'stage': str(entry.get('stage') or ''),
                'field': str(entry.get('field') or ''),
                'method': str(entry.get('method') or ''),
                'reason': str(entry.get('reason') or ''),
                'before': _audit_text(entry.get('before')),
                'after': _audit_text(entry.get('after')),
                'severity': str(entry.get('severity') or 'info'),
                'action': str(entry.get('action') or ''),
            }
            if normalized['stage'] and normalized['field']:
                items.append(normalized)
            if len(items) >= max_items:
                return items
    return items


def _attach_audit_to_validation(validation, data):
    validation = dict(validation or {})
    audit = _summarize_audit_trail(data)
    if audit:
        validation['audit'] = audit
        validation['audit_truncated'] = len(audit) >= _MAX_VALIDATION_AUDIT_ITEMS
    return validation


def _summarize_row_quality_issues(data, max_examples=5):
    """Aggregate row-level degradations into bounded, user-facing messages."""
    grouped = {}
    for row_number, row in enumerate(data, 1):
        seen_codes = set()
        for issue in row.get('_quality_issues', []):
            if not isinstance(issue, dict):
                continue
            code = str(issue.get('code') or '').strip()
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            grouped.setdefault(code, []).append(row_number)

    summary = []
    for code, row_numbers in grouped.items():
        label = _ROW_QUALITY_LABELS.get(code, code)
        examples = '、'.join(f'第 {number} 行' for number in row_numbers[:max_examples])
        suffix = '等' if len(row_numbers) > max_examples else ''
        summary.append(
            f'{label}：{len(row_numbers)} 行（{examples}{suffix}），请抽样复核'
        )
    return summary


def _normalize_fact_marker(marker):
    marker = re.sub(r'\s+', ' ', str(marker or '').strip().lower())
    marker = re.sub(r'\binches\b', 'inch', marker)
    marker = re.sub(r'\bvolts\b', 'v', marker)
    marker = re.sub(r'\bvolt\b', 'v', marker)
    marker = re.sub(r'\bwatts\b', 'w', marker)
    marker = re.sub(r'\bwatt\b', 'w', marker)
    marker = re.sub(r'\bpacks\b', 'pack', marker)
    marker = re.sub(r'\bpieces\b', 'piece', marker)
    marker = re.sub(r'\bpins\b', 'pin', marker)
    marker = re.sub(r'\bkeys\b', 'key', marker)
    return marker


def _extract_factual_markers(text):
    markers = []
    for match in _FACT_RE.findall(_plain_text(text)):
        marker = _normalize_fact_marker(match)
        if marker and marker not in markers:
            markers.append(marker)
    return markers


def _missing_factual_markers(source, candidate, limit=8):
    source_markers = _extract_factual_markers(source)[:limit]
    if not source_markers:
        return []
    candidate_markers = set(_extract_factual_markers(candidate))
    return [marker for marker in source_markers if marker not in candidate_markers]


def _unexpected_brand_markers(source, candidate):
    """Return brand tokens introduced by a model but absent from the source."""
    source_brands = {
        match.group(0).strip().lower()
        for match in _BRAND_RE.finditer(_plain_text(source))
    }
    candidate_brands = {
        match.group(0).strip().lower()
        for match in _BRAND_RE.finditer(_plain_text(candidate))
    }
    return sorted(candidate_brands - source_brands)


def _trim_words(text, limit):
    text = re.sub(r'\s+', ' ', str(text or '')).strip()
    if len(text) <= limit:
        return text
    shortened = text[:limit].rsplit(' ', 1)[0].strip()
    return shortened or text[:limit].strip()


def _fingerprint_text(text):
    return re.sub(r'[^a-z0-9]+', '', str(text or '').lower())


def _term_tokens(text):
    return [
        token.lower()
        for token in re.findall(r'[A-Za-z0-9]+(?:/[A-Za-z0-9]+)?', str(text or ''))
    ]


def _meaningful_tokens(text):
    return [
        token for token in _term_tokens(text)
        if token not in _STOP_TERMS and token not in _GENERIC_TERMS
    ]


def _is_weak_bullet(text):
    cleaned = _plain_text(text)
    if len(cleaned) < 5 or _META_TEXT_RE.search(cleaned):
        return True
    return not _meaningful_tokens(cleaned) and not _extract_factual_markers(cleaned)


def _clean_bullet_text(text):
    cleaned = _plain_text(text)
    cleaned = _BRAND_RE.sub('', cleaned)
    cleaned = _OEM_RE.sub('', cleaned)
    cleaned = re.sub(r'^[\s\-*•\d.)]+', '', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' ;,.-')
    return _trim_words(cleaned, 200)


def _normalize_bullets_for_row(row):
    raw_bullets = list(row.get('bullets') or [])[:5]
    raw_bullets.extend([''] * (5 - len(raw_bullets)))
    source_markers = set(_extract_factual_markers(
        f"{row.get('title', '')} {row.get('desc', '')}"
    ))
    seen = set()
    normalized = []
    warned = False

    for raw in raw_bullets[:5]:
        original = str(raw or '')
        bullet = _clean_bullet_text(original)
        if bullet != original.strip():
            warned = True
        marker_set = set(_extract_factual_markers(bullet))
        if marker_set and source_markers and not marker_set.issubset(source_markers):
            warned = True
        fp = _fingerprint_text(bullet)
        if bullet and (fp in seen or _is_weak_bullet(bullet)):
            bullet = ''
            warned = True
        if fp and bullet:
            seen.add(fp)
        normalized.append(bullet)

    row['bullets'] = normalized
    # 只在有实质问题时警告（弱Bullet/重复），格式化不算
    has_real_issue = any(
        not b or _is_weak_bullet(b)
        for b in normalized if b
    ) or len([b for b in normalized if b]) < 5
    if has_real_issue:
        _add_quality_issue(row, 'bullet_quality_warning',
            'Bullet 存在重复、泛词、品牌残留或疑似规格风险，已清洗后复核')
    return row


def _split_keywords(value):
    return [
        part.strip()
        for part in re.split(r'[,;，；\n]+', str(value or ''))
        if part.strip()
    ]


def _clean_keyword_term(term):
    cleaned = _plain_text(term).lower()
    cleaned = _BRAND_RE.sub('', cleaned)
    cleaned = _OEM_RE.sub('', cleaned)
    cleaned = re.sub(r'[^a-z0-9/+\-\s]', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' -,+/')
    return cleaned


def _is_weak_keyword(term):
    if not term or len(term) < 2 or _META_TEXT_RE.search(term):
        return True
    tokens = _term_tokens(term)
    if not tokens:
        return True
    return all(token in _STOP_TERMS or token in _GENERIC_TERMS for token in tokens)


def _dedupe_terms(terms):
    result = []
    seen = set()
    for term in terms:
        cleaned = _clean_keyword_term(term)
        fp = _fingerprint_text(cleaned)
        if not cleaned or not fp or fp in seen or _is_weak_keyword(cleaned):
            continue
        seen.add(fp)
        result.append(cleaned)
    return result


def _keyword_candidates_from_source(row):
    text = _plain_text(f"{row.get('title', '')}. {row.get('desc', '')}")
    words = [
        token.lower()
        for token in re.findall(r'[A-Za-z0-9]+(?:/[A-Za-z0-9]+)?', text)
        if (
            token.lower() not in _STOP_TERMS
            and token.lower() not in _GENERIC_TERMS
            and (len(token) > 2 or re.search(r'\d', token))
        )
    ]
    candidates = list(_extract_factual_markers(text))
    for size in (2, 3):
        for index in range(0, max(0, len(words) - size + 1)):
            candidates.append(' '.join(words[index:index + size]))
    candidates.extend(words)
    return _dedupe_terms(candidates)


def _join_keywords(terms, limit=250):
    terms = _dedupe_terms(terms)

    def _greedy(excluded):
        selected = []
        for term in terms:
            if term in excluded:
                continue
            trial = ', '.join(selected + [term])
            if len(trial) > limit:
                continue
            selected.append(term)
            if len(selected) == 10:
                break
        return selected

    excluded = set()
    selected = _greedy(excluded)
    # Long model phrases can consume the whole 250-char budget while leaving
    # fewer than ten terms. Drop the longest blocking term only when doing so
    # increases the number of valid, source-derived terms.
    while len(selected) < 10 and selected:
        best = None
        for term in selected:
            trial_excluded = excluded | {term}
            trial_selected = _greedy(trial_excluded)
            score = (len(trial_selected), -len(', '.join(trial_selected)))
            if len(trial_selected) <= len(selected):
                continue
            if best is None or score > best[0]:
                best = (score, term, trial_selected)
        if best is None:
            break
        excluded.add(best[1])
        selected = best[2]

    return ', '.join(selected)


def _normalize_keywords_for_row(row):
    original_terms = _split_keywords(row.get('keywords', ''))
    cleaned_original = _dedupe_terms(original_terms)
    candidates = _keyword_candidates_from_source(row)
    terms = _dedupe_terms(cleaned_original + candidates)
    normalized = _join_keywords(terms)
    row['keywords'] = normalized
    normalized_terms = _dedupe_terms(_split_keywords(normalized))
    if (
        len(normalized_terms) != 10
        or len(normalized) > 250
    ):
        _add_quality_issue(
            row,
            'keyword_quality_warning',
            '关键词不足 10 个、重复、过泛或含品牌，已按源内容补齐/清洗',
        )
    return row


def _validate_amazon_rows(rows, extra_issues=None, row_offset=0):
    issues = list(extra_issues or [])
    for index, row in enumerate(rows, 1):
        row_number = row_offset + index
        label = f'第 {row_number} 行'
        title = str(row.get('title') or '').strip()
        desc = _plain_text(row.get('desc') or '')
        main_img = str(row.get('main_img') or '').strip()
        bullets = [str(item or '').strip() for item in list(row.get('bullets') or [])[:5]]
        bullets.extend([''] * (5 - len(bullets)))
        keywords = str(row.get('keywords') or '').strip()
        keyword_terms = _dedupe_terms(_split_keywords(keywords))

        if not title or len(title) > 75 or _META_TEXT_RE.search(title):
            issues.append(f'{label}标题为空、超过 75 字符或疑似模型说明文本')
        if (
            not desc
            or _BRAND_RE.search(desc)
            or _OEM_RE.search(desc)
            or _META_TEXT_RE.search(desc)
        ):
            issues.append(f'{label}描述为空、含品牌残留或疑似模型说明文本')
        if not re.match(r'^https?://', main_img, re.IGNORECASE):
            issues.append(f'{label}主图 URL 无效')

        non_empty_bullets = [bullet for bullet in bullets if bullet]
        if len(non_empty_bullets) < 5:
            issues.append(f'{label} Bullet 不足 5 条')
        bullet_fps = [_fingerprint_text(bullet) for bullet in non_empty_bullets]
        if len(set(bullet_fps)) < len(bullet_fps):
            issues.append(f'{label} Bullet 存在重复内容')
        if any(len(bullet) > 200 for bullet in non_empty_bullets):
            issues.append(f'{label} Bullet 超过 200 字符')
        if any(_BRAND_RE.search(bullet) or _OEM_RE.search(bullet) for bullet in non_empty_bullets):
            issues.append(f'{label} Bullet 含品牌或 OEM 残留')
        if any(_is_weak_bullet(bullet) for bullet in non_empty_bullets):
            issues.append(f'{label} Bullet 过泛，缺少可检索的产品信息')

        if len(keyword_terms) != 10:
            issues.append(f'{label}关键词需为 10 个有效搜索词')
        if keywords and len(keywords) > 250:
            issues.append(f'{label}关键词超过 250 字符')
        if _BRAND_RE.search(keywords) or len(keyword_terms) != len(_split_keywords(keywords)):
            issues.append(f'{label}关键词为空、重复、过泛或含品牌')
        if len(issues) >= 20:
            break
    return {
        'passed': not issues,
        'issues': issues[:20],
        'truncated': len(issues) >= 20,
    }


# 兼容旧导入名；模板正文统一由 PromptRegistry 管理。
_prompts = get_prompt_registry()
DESC_CLEAN_PROMPT = _prompts.get("amazon.description_clean")
TITLE_OPTIMIZE_PROMPT = _prompts.get("amazon.title_optimize")
BULLET_KEYWORD_PROMPT = _prompts.get("amazon.bullet_keywords")

__all__ = ['AMAZON_TITLE_CONCURRENCY', 'AMAZON_DESC_CONCURRENCY', 'AMAZON_BULLET_CONCURRENCY', 'AMAZON_REVIEW_CONCURRENCY', 'AMAZON_IMAGE_GEN_CONCURRENCY', 'AMAZON_IMAGE_GEN_ATTEMPTS', '_BRAND_RE', '_OEM_RE', '_LOGISTICS_RE', '_RETURN_RE', '_IMG_RE', '_ROW_QUALITY_LABELS', '_META_TEXT_RE', '_STOP_TERMS', '_GENERIC_TERMS', '_FACT_RE', '_plain_text', '_MAX_ROW_AUDIT_ITEMS', '_MAX_VALIDATION_AUDIT_ITEMS', '_audit_text', '_add_audit', '_ISSUE_AUDIT_META', '_add_quality_issue', '_summarize_audit_trail', '_attach_audit_to_validation', '_summarize_row_quality_issues', '_normalize_fact_marker', '_extract_factual_markers', '_missing_factual_markers', '_unexpected_brand_markers', '_trim_words', '_fingerprint_text', '_term_tokens', '_meaningful_tokens', '_is_weak_bullet', '_clean_bullet_text', '_normalize_bullets_for_row', '_split_keywords', '_clean_keyword_term', '_is_weak_keyword', '_dedupe_terms', '_keyword_candidates_from_source', '_join_keywords', '_normalize_keywords_for_row', '_validate_amazon_rows', 'DESC_CLEAN_PROMPT', 'TITLE_OPTIMIZE_PROMPT', 'BULLET_KEYWORD_PROMPT']
