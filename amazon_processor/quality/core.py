"""Amazon text-quality rules, normalization, auditing, and validation."""
from __future__ import annotations
import re
from ..policy import (
    COMPATIBILITY_BRAND_ALIASES,
    PROHIBITED_LISTING_TERMS_RE,
    compile_brand_pattern,
)
BRAND_RE = compile_brand_pattern()
OEM_RE = re.compile('\\b(OEM|Original|Factory|原厂|原装|正品|Genuine)\\b', re.IGNORECASE)
LOGISTICS_RE = re.compile('(交货时间|发货时间|运输方式|快递|物流|Shipping|Delivery|Express|Freight|Carrier)[：:].*?(?=\\n|$)', re.IGNORECASE)
RETURN_RE = re.compile('(退货|退款|Return|Refund|Warranty|保修|Payment|支付).*?(?=\\n|$)', re.IGNORECASE)
IMG_RE = re.compile('<img[^>]*>', re.IGNORECASE)
META_TEXT_RE = re.compile('(as an ai|i cannot|here are|optimized title|cleaned description|bullet points|search keywords|json|markdown|rules?:|requirements?:)', re.IGNORECASE)
CROSS_SELL_RE = re.compile(
    r'(?:credit\s+card\s+knife|'
    r'\b\d+(?:\.\d{1,2})?\s*usd\b|'
    r'add\s+(?:me|us)\s+to\s+favourite|'
    r'visit\s+(?:our|my)\s+store|'
    r'welcome\s+to\s+my\s+store|'
    r'please\s+contact\s+us\s+before|'
    r'negative\s+feedback|'
    r'payment\s+policy|shipping\s+policy|returns?\s+policy|'
    r'terms?\s+of\s+sale|'
    r'we\s+(?:only\s+)?accept\s+payment|'
    r'orders?\s+processed\s+within|'
    r'ebay\s+address(?:es)?|'
    r'paypal)',
    re.IGNORECASE,
)
STOP_TERMS = {'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'for', 'from', 'in', 'into', 'is', 'it', 'its', 'of', 'on', 'or', 'our', 'that', 'the', 'this', 'to', 'with', 'you', 'your'}
GENERIC_TERMS = {'best', 'excellent', 'generic', 'good', 'great', 'high', 'hot', 'item', 'new', 'nice', 'perfect', 'premium', 'product', 'quality', 'sale', 'useful', 'value'}
DOMAIN_GENERIC_TERMS = {
    'accessories',
    'accessory',
    'auto',
    'car',
    'cars',
    'part',
    'parts',
    'universal',
    'vehicle',
    'vehicles',
}
FACT_RE = re.compile(
    r'(?<![A-Za-z0-9])(?:19|20)\d{2}(?![A-Za-z0-9])|'
    r'(?<![A-Za-z0-9])\d+(?:[./-]\d+)?\s*'
    r'(?i:mm|cm|m|inch|inches|in|ft|kg|g|lb|lbs|oz|v|volt|volts|w|watt|watts|l|ml|pcs|pc|pack|packs|piece|pieces|key|keys|pin|pins)'
    r'(?![A-Za-z])|'
    r'(?<![A-Za-z0-9])[A-Z]{1,5}\d{1,5}[A-Z0-9-]*(?![A-Za-z0-9-])|'
    r'(?<![A-Za-z0-9])\d+[A-Z]{1,5}(?![A-Za-z0-9])'
)

def plain_text(value):
    text = str(value or '')
    text = IMG_RE.sub(' ', text)
    text = re.sub('<[^>]+>', ' ', text)
    return re.sub('\\s+', ' ', text).strip()

def normalize_fact_marker(marker):
    marker = re.sub('\\s+', ' ', str(marker or '').strip().lower())
    marker = re.sub('\\binches\\b', 'inch', marker)
    marker = re.sub('\\bvolts\\b', 'v', marker)
    marker = re.sub('\\bvolt\\b', 'v', marker)
    marker = re.sub('\\bwatts\\b', 'w', marker)
    marker = re.sub('\\bwatt\\b', 'w', marker)
    marker = re.sub('\\bpacks\\b', 'pack', marker)
    marker = re.sub('\\bpieces\\b', 'piece', marker)
    marker = re.sub('\\bpins\\b', 'pin', marker)
    marker = re.sub('\\bkeys\\b', 'key', marker)
    return marker

def extract_factual_markers(text):
    text = plain_text(text)
    markers = []
    for match in FACT_RE.finditer(text):
        marker = normalize_fact_marker(match.group(0))
        if (
            match.start() > 0
            and text[match.start() - 1] == ':'
            or marker[:1].isalpha()
            and re.search(r'(?:19|20)\d{2}', marker)
        ):
            continue
        if marker and marker not in markers:
            markers.append(marker)
    return markers

def missing_factual_markers(source, candidate, limit=8):
    source_markers = extract_factual_markers(source)[:limit]
    if not source_markers:
        return []
    candidate_markers = set(extract_factual_markers(candidate))
    candidate_text = plain_text(candidate).lower()
    return [
        marker
        for marker in source_markers
        if marker not in candidate_markers
        and not re.search(
            rf'(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])',
            candidate_text,
        )
    ]

def unexpected_brand_markers(source, candidate):
    """Return model-added brands that were absent from the source."""
    def canonical(value):
        key = str(value or '').strip().lower()
        return str(COMPATIBILITY_BRAND_ALIASES.get(key, key)).lower()

    source_brands = {
        canonical(match.group(0))
        for match in BRAND_RE.finditer(plain_text(source))
    }
    candidate_brands = {
        canonical(match.group(0))
        for match in BRAND_RE.finditer(plain_text(candidate))
    }
    return sorted(candidate_brands - source_brands)

def trim_words(text, limit):
    text = re.sub('\\s+', ' ', str(text or '')).strip()
    if len(text) <= limit:
        return text
    shortened = text[:limit].rsplit(' ', 1)[0].strip()
    return shortened or text[:limit].strip()

def fingerprint_text(text):
    return re.sub('[^a-z0-9]+', '', str(text or '').lower())

def term_tokens(text):
    return [token.lower() for token in re.findall('[A-Za-z0-9]+(?:/[A-Za-z0-9]+)?', str(text or ''))]

def meaningful_tokens(text):
    return [token for token in term_tokens(text) if token not in STOP_TERMS and token not in GENERIC_TERMS]

def has_cross_sell_contamination(text):
    return bool(CROSS_SELL_RE.search(plain_text(text)))

def relevant_token_overlap(source, candidate):
    source_tokens = set(meaningful_tokens(source)) - DOMAIN_GENERIC_TERMS
    candidate_tokens = set(meaningful_tokens(candidate)) - DOMAIN_GENERIC_TERMS
    if not source_tokens or not candidate_tokens:
        return set()
    return source_tokens & candidate_tokens

def is_text_relevant(source, candidate):
    candidate_text = plain_text(candidate)
    if not candidate_text or has_cross_sell_contamination(candidate_text):
        return False
    source_tokens = set(meaningful_tokens(source)) - DOMAIN_GENERIC_TERMS
    if not source_tokens:
        return True
    return bool(relevant_token_overlap(source, candidate_text))
import re
ROW_QUALITY_LABELS = {'missing_source_description': '源产品描述缺失', 'title_ai_fallback': '标题 AI 优化降级为规则处理', 'title_fact_loss': '标题关键规格可能丢失', 'description_ai_fallback': '描述 AI 清洗降级为规则处理', 'description_fact_loss': '描述关键规格可能丢失', 'description_relevance_warning': '描述相关性需要复核', 'description_compacted': '描述因 500 字符限制被压缩', 'bullet_rule_fallback': 'Bullet/关键词由规则补全', 'bullet_quality_warning': 'Bullet 内容质量需要复核', 'keyword_quality_warning': '关键词内容需要复核', 'subtitle_quality_warning': '副标题内容质量需要复核', 'localization_language_warning': '标题/副标题/关键词语言检测提示'}
MAX_ROW_AUDIT_ITEMS = 20
MAX_VALIDATION_AUDIT_ITEMS = 120
ISSUE_AUDIT_META = {'missing_source_description': ('读取表格', 'description', 'review'), 'title_ai_fallback': ('标题优化', 'title', 'fallback'), 'title_fact_loss': ('标题优化', 'title', 'review'), 'description_ai_fallback': ('描述清洗', 'description', 'fallback'), 'description_fact_loss': ('描述清洗', 'description', 'review'), 'description_relevance_warning': ('描述清洗', 'description', 'review'), 'description_compacted': ('描述清洗', 'description', 'review'), 'bullet_rule_fallback': ('Bullet+关键词', 'Bullet', 'fallback'), 'bullet_quality_warning': ('Bullet+关键词', 'Bullet', 'review'), 'keyword_quality_warning': ('Bullet+关键词', 'keywords', 'review'), 'subtitle_quality_warning': ('副标题', 'subtitle', 'review'), 'localization_language_warning': ('多站点文案校验', 'title', 'review')}

def audit_text(value, limit=180):
    if isinstance(value, (list, tuple)):
        text = ' | '.join((str(item or '').strip() for item in value if str(item or '').strip()))
    else:
        text = str(value or '')
    text = re.sub('\\s+', ' ', text).strip()
    return text if len(text) <= limit else text[:limit - 1] + '…'

def add_audit(row, stage, field, before, after, *, method='rule', reason='', severity='info', action=''):
    """Attach one de-duplicated, bounded audit entry to a row."""
    before_text = audit_text(before)
    after_text = audit_text(after)
    reason = str(reason or '').strip()
    if before_text == after_text and severity not in {'warning', 'review', 'error'}:
        return
    audit = row.setdefault('_audit', [])
    if len(audit) >= MAX_ROW_AUDIT_ITEMS:
        return
    key = (stage, field, method, reason, before_text, after_text)
    for item in audit:
        existing = (item.get('stage'), item.get('field'), item.get('method'), item.get('reason'), item.get('before'), item.get('after'))
        if existing == key:
            return
    audit.append({'stage': str(stage or ''), 'field': str(field or ''), 'method': str(method or ''), 'reason': reason, 'before': before_text, 'after': after_text, 'severity': severity, 'action': str(action or '')})

def add_quality_issue(row, code, message):
    """Attach an issue and its corresponding audit evidence once."""
    issues = row.setdefault('_quality_issues', [])
    if any((issue.get('code') == code for issue in issues)):
        return
    issues.append({'code': code, 'message': message})
    stage, field, method = ISSUE_AUDIT_META.get(code, ('质量检查', '', 'review'))
    if field:
        value = row.get('desc') if field == 'description' else row.get(field, '')
        add_audit(row, stage, field, value, value, method=method, reason=code, severity='warning', action=message)

def summarize_audit_trail(data, max_items=MAX_VALIDATION_AUDIT_ITEMS):
    items = []
    for row_number, row in enumerate(data, 1):
        for entry in row.get('_audit', []):
            if not isinstance(entry, dict):
                continue
            normalized = {'row': row_number, 'stage': str(entry.get('stage') or ''), 'field': str(entry.get('field') or ''), 'method': str(entry.get('method') or ''), 'reason': str(entry.get('reason') or ''), 'before': audit_text(entry.get('before')), 'after': audit_text(entry.get('after')), 'severity': str(entry.get('severity') or 'info'), 'action': str(entry.get('action') or '')}
            if normalized['stage'] and normalized['field']:
                items.append(normalized)
            if len(items) >= max_items:
                return items
    return items

def attach_audit_to_validation(validation, data):
    validation = dict(validation or {})
    audit = summarize_audit_trail(data)
    if audit:
        validation['audit'] = audit
        validation['audit_truncated'] = len(audit) >= MAX_VALIDATION_AUDIT_ITEMS
    return validation

def summarize_row_quality_issues(data, max_examples=5):
    """Aggregate row degradations into bounded review messages."""
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
        label = ROW_QUALITY_LABELS.get(code, code)
        examples = '、'.join((f'第 {number} 行' for number in row_numbers[:max_examples]))
        suffix = '等' if len(row_numbers) > max_examples else ''
        summary.append(f'{label}：{len(row_numbers)} 行（{examples}{suffix}），请抽样复核')
    return summary
import re

def is_weak_bullet(text):
    cleaned = plain_text(text)
    if len(cleaned) < 5 or META_TEXT_RE.search(cleaned):
        return True
    return not meaningful_tokens(cleaned) and (not extract_factual_markers(cleaned))

def clean_bullet_text(text):
    cleaned = plain_text(text)
    cleaned = BRAND_RE.sub('', cleaned)
    cleaned = OEM_RE.sub('', cleaned)
    cleaned = re.sub('^[\\s\\-*•\\d.)]+', '', cleaned)
    cleaned = re.sub('\\s+', ' ', cleaned).strip(' ;,.-')
    return trim_words(cleaned, 200)

def normalize_bullets_for_row(row):
    raw_bullets = list(row.get('bullets') or [])[:5]
    raw_bullets.extend([''] * (5 - len(raw_bullets)))
    seen = set()
    normalized = []
    source_text = f"{row.get('title', '')}. {row.get('desc', '')}"
    for raw in raw_bullets[:5]:
        bullet = clean_bullet_text(str(raw or ''))
        if bullet and not is_text_relevant(source_text, bullet):
            bullet = ''
        fingerprint = fingerprint_text(bullet)
        if bullet and (fingerprint in seen or is_weak_bullet(bullet)):
            bullet = ''
        if fingerprint and bullet:
            seen.add(fingerprint)
        normalized.append(bullet)
    row['bullets'] = normalized
    has_real_issue = any((not bullet or is_weak_bullet(bullet) for bullet in normalized if bullet)) or len([bullet for bullet in normalized if bullet]) < 5
    if has_real_issue:
        add_quality_issue(row, 'bullet_quality_warning', 'Bullet 存在重复、泛词、品牌残留或疑似规格风险，已清洗后复核')
    return row

def split_keywords(value):
    return [part.strip() for part in re.split('[,;，；\\n]+', str(value or '')) if part.strip()]

def clean_keyword_term(term):
    cleaned = plain_text(term).lower()
    cleaned = BRAND_RE.sub('', cleaned)
    cleaned = OEM_RE.sub('', cleaned)
    cleaned = re.sub('[^a-z0-9/+\\-\\s]', ' ', cleaned)
    cleaned = re.sub('\\s+', ' ', cleaned).strip(' -,+/')
    return cleaned

def is_weak_keyword(term):
    if not term or len(term) < 2 or META_TEXT_RE.search(term):
        return True
    tokens = term_tokens(term)
    if not tokens:
        return True
    return all((token in STOP_TERMS or token in GENERIC_TERMS for token in tokens))

def dedupe_terms(terms):
    result = []
    seen = set()
    for term in terms:
        cleaned = clean_keyword_term(term)
        fingerprint = fingerprint_text(cleaned)
        if not cleaned or not fingerprint or fingerprint in seen or is_weak_keyword(cleaned):
            continue
        seen.add(fingerprint)
        result.append(cleaned)
    return result

def keyword_candidates_from_source(row):
    text = plain_text(f"{row.get('title', '')}. {row.get('desc', '')}")
    words = [token.lower() for token in re.findall('[A-Za-z0-9]+(?:/[A-Za-z0-9]+)?', text) if token.lower() not in STOP_TERMS and token.lower() not in GENERIC_TERMS and (len(token) > 2 or re.search('\\d', token))]
    candidates = list(extract_factual_markers(text))
    for size in (2, 3):
        for index in range(0, max(0, len(words) - size + 1)):
            candidates.append(' '.join(words[index:index + size]))
    candidates.extend(words)
    return dedupe_terms(candidates)

def join_keywords(terms, limit=250):
    terms = dedupe_terms(terms)

    def greedy(excluded):
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
    selected = greedy(excluded)
    while len(selected) < 10 and selected:
        best = None
        for term in selected:
            trial_excluded = excluded | {term}
            trial_selected = greedy(trial_excluded)
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

def normalize_keywords_for_row(row):
    original_terms = split_keywords(row.get('keywords', ''))
    source_text = f"{row.get('title', '')}. {row.get('desc', '')}"
    cleaned_original = [
        term
        for term in dedupe_terms(original_terms)
        if is_text_relevant(source_text, term)
    ]
    candidates = keyword_candidates_from_source(row)
    terms = dedupe_terms(cleaned_original + candidates)
    normalized = join_keywords(terms)
    row['keywords'] = normalized
    normalized_terms = dedupe_terms(split_keywords(normalized))
    if len(normalized_terms) != 10 or len(normalized) > 250:
        add_quality_issue(row, 'keyword_quality_warning', '关键词不足 10 个、重复、过泛或含品牌，已按源内容补齐/清洗')
    return row
import re

def validate_amazon_rows(rows, extra_issues=None, row_offset=0):
    """Validate final rows and return a bounded review result."""
    issues = list(extra_issues or [])
    for index, row in enumerate(rows, 1):
        from ..text.locale import market_for_row, sanitize_localized_subtitle

        is_english = market_for_row(row).language_code == 'en'
        row_number = row_offset + index
        label = f'第 {row_number} 行'
        title = str(row.get('title') or '').strip()
        subtitle = str(row.get('subtitle') or '').strip()
        description = plain_text(row.get('desc') or '')
        main_image = str(row.get('main_img') or '').strip()
        bullets = [str(item or '').strip() for item in list(row.get('bullets') or [])[:5]]
        bullets.extend([''] * (5 - len(bullets)))
        keywords = str(row.get('keywords') or '').strip()
        raw_keyword_terms = split_keywords(keywords)
        keyword_terms = (
            dedupe_terms(raw_keyword_terms)
            if is_english
            else list(dict.fromkeys(
                term.casefold() for term in raw_keyword_terms if term.strip()
            ))
        )
        if any(
            PROHIBITED_LISTING_TERMS_RE.search(value)
            for value in [title, subtitle, description, *bullets, keywords]
        ):
            issues.append(f'{label}文案含平台违规词')
        if not title or len(title) > 74 or META_TEXT_RE.search(title):
            issues.append(f'{label}标题为空、未给副标题预留展示空间或疑似模型说明文本')
        if title and not subtitle:
            issues.append(f'{label}副标题不能为空')
        if subtitle:
            if len(subtitle) > 125:
                issues.append(f'{label}副标题超过 125 字符')
            if re.search('[.!?;:：；！。？]', subtitle):
                issues.append(f'{label}副标题应使用逗号分隔短语，不能写完整句')
            if is_english and re.search('[^A-Za-z0-9 ,]', subtitle):
                issues.append(f'{label}副标题含特殊符号')
            if not is_english and sanitize_localized_subtitle(subtitle) != subtitle:
                issues.append(f'{label}副标题含特殊符号')
            if re.search(
                '\\b(best\\s*seller|free\\s*shipping|discount|promotion|promo|hot\\s*sale|limited\\s*time)\\b',
                subtitle,
                re.IGNORECASE,
            ):
                issues.append(f'{label}副标题含主观评价或促销词')
            subtitle_terms = [
                term for term in split_keywords(subtitle) if term
            ]
            if not subtitle_terms:
                issues.append(f'{label}副标题格式无有效短语')
        if not description or len(str(row.get('desc') or '')) > 500 or BRAND_RE.search(description) or OEM_RE.search(description) or META_TEXT_RE.search(description) or has_cross_sell_contamination(description) or not is_text_relevant(title, description):
            issues.append(f'{label}描述为空、超过 500 字符、与标题无关或含脏文案')
        if not re.match('^https?://', main_image, re.IGNORECASE):
            issues.append(f'{label}主图 URL 无效')
        non_empty_bullets = [bullet for bullet in bullets if bullet]
        if len(non_empty_bullets) < 5:
            issues.append(f'{label} Bullet 不足 5 条')
        bullet_fingerprints = [
            (
                fingerprint_text(bullet)
                if is_english
                else ''.join(char for char in bullet.casefold() if char.isalnum())
            )
            for bullet in non_empty_bullets
        ]
        if len(set(bullet_fingerprints)) < len(bullet_fingerprints):
            issues.append(f'{label} Bullet 存在重复内容')
        if any((len(bullet) > 200 for bullet in non_empty_bullets)):
            issues.append(f'{label} Bullet 超过 200 字符')
        if any((BRAND_RE.search(bullet) or OEM_RE.search(bullet) for bullet in non_empty_bullets)):
            issues.append(f'{label} Bullet 含品牌或 OEM 残留')
        if any(((is_english and is_weak_bullet(bullet)) or has_cross_sell_contamination(bullet) or not is_text_relevant(f'{title}. {description}', bullet) for bullet in non_empty_bullets)):
            issues.append(f'{label} Bullet 过泛，缺少可检索的产品信息')
        if len(keyword_terms) != 10:
            issues.append(f'{label}关键词需为 10 个有效搜索词')
        if keywords and len(keywords) > 250:
            issues.append(f'{label}关键词超过 250 字符')
        if BRAND_RE.search(keywords) or has_cross_sell_contamination(keywords) or len(keyword_terms) != len(raw_keyword_terms):
            issues.append(f'{label}关键词为空、重复、过泛或含品牌')
        if len(issues) >= 20:
            break
    return {'passed': not issues, 'issues': issues[:20], 'truncated': len(issues) >= 20}
