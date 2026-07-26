#!/usr/bin/env python3
"""Amazon 采集表 → 回填表 管道（7 阶段）。使用 model_provider 进行所有 AI 调用。

用法: uv run python scripts/process_amazon.py "亚马逊表/跨境电商自动化采集表.xlsx"
"""
import sys, os, json, re, time, threading, inspect
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import openpyxl
from concurrent.futures import ThreadPoolExecutor, as_completed
from adapters import detect_adapter
from pipeline_log import log as _log, new_request_id, PipelineMetrics
from concurrency import adaptive_map, configured_concurrency
from services.constants import (
    IMAGE_POLICY_VERSION,
    IMAGE_REMEDIATION_REVIEW_PROMPT,
    compile_brand_pattern,
)
from services.amazon_json import load_columnar_json, write_output_json
from model_provider import (
    ProviderQuotaError,
    get_provider,
    reload_provider as _reload_provider,
)
import requests as _requests

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Backward-compatible local name used by the stage code.
QuotaExhaustedError = ProviderQuotaError

_CACHE_IO_LOCK = threading.Lock()
AMAZON_TITLE_CONCURRENCY = configured_concurrency('text', 10, maximum=50)
AMAZON_DESC_CONCURRENCY = configured_concurrency('text', 10, maximum=50)
AMAZON_BULLET_CONCURRENCY = configured_concurrency('text', 20, maximum=50)
AMAZON_REVIEW_CONCURRENCY = configured_concurrency('review', 100, maximum=150)
AMAZON_IMAGE_GEN_CONCURRENCY = configured_concurrency('image_gen', 15, maximum=30)

def _atomic_save_cache(cache_path: str, cache: dict) -> None:
    """原子写缓存，带重试（Windows 文件锁兼容）。"""
    if not cache_path:
        return
    with _CACHE_IO_LOCK:
        snapshot = {
            'review_results': dict(cache.get('review_results') or {}),
            'gen_results': dict(cache.get('gen_results') or {}),
            'gen_prompt_version': cache.get('gen_prompt_version', 'main1600_var_clean_v1'),
            'gen_meta': dict(cache.get('gen_meta') or {}),
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


def reload_credentials():
    """热加载 keys.json，供 Web 设置保存后立即生效。"""
    _reload_provider()

AMAZON_STAGES = ['读取表格', '审图+生图', '标题优化', '描述清洗', 'Bullet+关键词', '写回填表']


class AmazonStatusReporter:
    def __init__(self, table_path):
        self.status_path = os.path.splitext(table_path)[0] + '_status.json'
        self.started_at = time.time()
        self.stage_index = 0
        self.stage_started_at = self.started_at
        self.current_stage = AMAZON_STAGES[0]
        self.total = 0

    def stage(self, name, current=0, total=0):
        self.stage_index = AMAZON_STAGES.index(name)
        self.current_stage = name
        self.stage_started_at = time.time()
        self.total = total
        self.update(current, total)

    def update(self, current, total=None):
        if total is not None:
            self.total = total
        elapsed = time.time() - self.stage_started_at
        eta = int(elapsed / current * (self.total - current)) if current and self.total else 0
        self._write({
            'status': 'running',
            'stage': self.current_stage,
            'stage_index': self.stage_index + 1,
            'stage_total': len(AMAZON_STAGES),
            'current': current,
            'total': self.total,
            'percent': int(current / self.total * 100) if self.total else 0,
            'eta_s': eta,
        })

    def failed(self, name, error):
        self._write({
            'status': 'failed',
            'stage': '错误',
            'stage_index': AMAZON_STAGES.index(name) + 1 if name in AMAZON_STAGES else self.stage_index + 1,
            'stage_total': len(AMAZON_STAGES),
            'error': str(error),
        })

    def finish(self, output, validation=None, metrics=None):
        validation = validation or {'passed': True, 'issues': []}
        needs_review = not validation.get('passed', False)
        self._write({
            'status': 'needs_review' if needs_review else 'done',
            'stage': '待人工复核' if needs_review else '完成',
            'stage_index': len(AMAZON_STAGES),
            'stage_total': len(AMAZON_STAGES),
            'current': 1,
            'total': 1,
            'percent': 100,
            'eta_s': 0,
            'output': output,
            'validation': validation,
            'metrics': metrics or {},
            'error': (
                f"输出存在 {len(validation.get('issues', []))} 项质量问题，请复核后使用"
                if needs_review else None
            ),
        })

    def _write(self, data):
        data['total_elapsed_s'] = int(time.time() - self.started_at)
        data['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        temp_path = self.status_path + f'.{threading.get_ident()}.tmp'
        for _ in range(3):
            try:
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(temp_path, self.status_path)
                break
            except (PermissionError, OSError):
                time.sleep(0.1)

# Brand patterns for cleaning
_BRAND_RE = compile_brand_pattern()
_OEM_RE = re.compile(r'\b(OEM|Original|Factory|原厂|原装|正品|Genuine)\b', re.IGNORECASE)
_LOGISTICS_RE = re.compile(r'(交货时间|发货时间|运输方式|快递|物流|Shipping|Delivery|Express|Freight|Carrier)[：:].*?(?=\n|$)', re.IGNORECASE)
_RETURN_RE = re.compile(r'(退货|退款|Return|Refund|Warranty|保修|Payment|支付).*?(?=\n|$)', re.IGNORECASE)
_IMG_RE = re.compile(r'<img[^>]*>', re.IGNORECASE)


_ROW_QUALITY_LABELS = {
    'title_ai_fallback': '标题 AI 优化降级为规则处理',
    'title_fact_loss': '标题关键规格可能丢失',
    'description_ai_fallback': '描述 AI 清洗降级为规则处理',
    'description_fact_loss': '描述关键规格可能丢失',
    'bullet_rule_fallback': 'Bullet/关键词由规则补全',
    'bullet_quality_warning': 'Bullet 内容质量需要复核',
    'keyword_quality_warning': '关键词内容质量需要复核',
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
    if warned:
        _add_quality_issue(
            row,
            'bullet_quality_warning',
            'Bullet 存在重复、泛词、品牌残留或疑似规格风险，已清洗后复核',
        )
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
    selected = []
    for term in terms:
        trial = ', '.join(selected + [term])
        if len(trial) > limit:
            continue
        selected.append(term)
        if len(selected) == 10:
            break
    return ', '.join(selected)


def _normalize_keywords_for_row(row):
    original_terms = _split_keywords(row.get('keywords', ''))
    cleaned_original = _dedupe_terms(original_terms)
    terms = cleaned_original
    candidates = _keyword_candidates_from_source(row)
    terms = _dedupe_terms(terms + candidates)[:10]
    normalized = _join_keywords(terms)
    row['keywords'] = normalized
    if (
        len(terms) != 10
        or len(original_terms) != 10
        or len(cleaned_original) != len(original_terms)
        or any(_BRAND_RE.search(term) for term in original_terms)
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


# Prompt templates
DESC_CLEAN_PROMPT = (
    "Clean this product description for Amazon listing.\n"
    "Rules:\n"
    "1. Brand is Generic. Delete ALL brand names (BMW, Toyota, Honda, etc.)\n"
    "2. Delete: OEM, Original, Factory, Genuine, 原厂, 正品, 原装 references\n"
    "3. Delete: cross-sell items with prices (XXX USD), shipping/delivery, return/refund, payment, warranty, FAQ, store info, contact\n"
    "4. Delete: all image <img> tags\n"
    "5. Keep ONLY: product name, features, material, size, color, quantity, compatibility, use cases\n"
    "6. Output plain English under 500 chars. Be concise. No HTML, no markdown.\n\n"
    "Description:\n{}"
)

TITLE_OPTIMIZE_PROMPT = (
    "CRITICAL: Output ONLY the optimized title text, nothing else. No explanations, no meta-text.\n\n"
    "Optimize this product title for Amazon. Rules:\n"
    "- Brand: Generic. Add \"For\" prefix before vehicle brands (e.g. \"For BMW\")\n"
    "- Delete ALL brand names from title\n"
    "- Max 100 characters. Trim colors > decorative words > dimensions.\n"
    "- Keep model numbers, years, specs.\n\n"
    "Title: {}"
)

BULLET_KEYWORD_PROMPT = (
    "Based on this product title and description, generate:\n\n"
    "1. BULLET POINTS (exactly 5):\n"
    "   - Each under 200 chars, English\n"
    "   - Cover: material, dimensions, features, compatibility, package contents\n"
    "   - Natural language for Amazon buyers\n"
    "   - NO brand names allowed\n\n"
    "2. SEARCH KEYWORDS (exactly 10):\n"
    "   - 10 relevant search terms, comma-separated\n"
    "   - NO brand names\n\n"
    "Return EXACTLY: {{\"bullets\": [\"point1\",\"point2\",\"point3\",\"point4\",\"point5\"], \"keywords\": \"kw1,kw2,...,kw10\"}}\n"
    "No markdown code fences. No extra text.\n\n"
    "Title: {title}\n"
    "Description: {desc}"
)


# === Pipeline ===

def _stage_read_json(ws, tp, progress=None):
    """读 JSON 格式采集表（列名: [值列表]）。"""
    max_rows = max(1, int(os.environ.get('CROSSPILOT_MAX_ROWS', '10000')))
    raw = load_columnar_json(tp, max_rows=max_rows)
    titles = raw.get('产品标题', [])
    descs = raw.get('产品描述', [])
    img_urls_list = raw.get('产品图片链接', [])
    var_urls_list = raw.get('变种图片链接', [])
    ids = raw.get('商品id', [])
    n = len(titles)
    print(f'读取 JSON: {n} 行', flush=True)
    data = []
    for i in range(n):
        img_urls = [
            url.strip() for url in img_urls_list[i]
            if url.strip().startswith(('http://', 'https://'))
        ]
        var_urls = [
            url.strip() for url in var_urls_list[i]
            if url.strip().startswith(('http://', 'https://'))
        ]
        data.append({
            'id': str(ids[i] or ''),
            'title': str(titles[i] or '').strip() if i < len(titles) else '',
            'desc': str(descs[i] or '') if i < len(descs) else '',
            'main_img': img_urls[0] if img_urls else '',
            'extra_imgs': img_urls[1:] if len(img_urls) > 1 else [],
            'var_imgs': var_urls,
            'var_img': var_urls[0] if var_urls else '',
        })
        if progress:
            progress(i + 1, n)
    return data


def _stage_read(ws, adapter, progress=None):
    """读采集表数据到内存，处理多 URL 图片列（换行分隔）。"""
    total = ws.max_row - 1
    print(f"读取 {total} 行...", flush=True)
    data = []
    for r in range(2, ws.max_row + 1):
        img_raw = str(ws.cell(r, adapter.cols['main_image']).value or '').strip()
        var_raw = str(ws.cell(r, adapter.cols['variant']).value or '').strip()
        # 多 URL 用换行分隔 → 拆成列表
        img_urls = [u.strip() for u in img_raw.replace('\r', '').split('\n') if u.strip().startswith('http')]
        var_urls = [u.strip() for u in var_raw.replace('\r', '').split('\n') if u.strip().startswith('http')]
        main_img = img_urls[0] if img_urls else ''
        extra_imgs = img_urls[1:] if len(img_urls) > 1 else []
        data.append({
            'id': str(ws.cell(r, 1).value or '').strip(),
            'title': str(ws.cell(r, adapter.cols['title']).value or '').strip(),
            'desc': str(ws.cell(r, adapter.cols['desc']).value or ''),
            'main_img': main_img,
            'var_imgs': var_urls,
            'var_img': var_urls[0] if var_urls else '',
            'extra_imgs': extra_imgs,
        })
        if progress:
            progress(r - 1, total)
    return data


def _validate_amazon_input(data):
    """拒绝空表、超大表和缺少核心商品字段的数据。"""
    if not data:
        raise ValueError('Amazon 输入表没有商品数据')
    max_rows = max(1, int(os.environ.get('CROSSPILOT_MAX_ROWS', '10000')))
    if len(data) > max_rows:
        raise ValueError(f'Amazon 输入表有 {len(data)} 行，超过上限 {max_rows} 行')

    issues = []
    for index, row in enumerate(data, 1):
        if not str(row.get('title', '')).strip():
            issues.append(f'第 {index} 行缺少产品标题')
        if not str(row.get('desc', '')).strip():
            issues.append(f'第 {index} 行缺少产品描述')
        main_img = str(row.get('main_img', '')).strip()
        if not re.match(r'^https?://', main_img, re.IGNORECASE):
            issues.append(f'第 {index} 行缺少有效的主图 URL')
        if len(issues) >= 20:
            break
    if issues:
        suffix = '（仅显示前 20 项）' if len(issues) >= 20 else ''
        raise ValueError('Amazon 输入质量检查失败' + suffix + '：' + '；'.join(issues))


def _stage_optimize_titles(data, progress=None):
    """标题优化：≤75 字符 + For/Compatible with 前缀。"""
    print(f"标题优化 {len(data)} 条...", flush=True)
    changed = 0
    for i, row in enumerate(data):
        original_title = str(row['title'] or '').strip()
        title = original_title
        if not title:
            continue
        title = _normalize_title(title)
        if title != row['title']:
            row['title'] = title
            changed += 1
            _add_audit(
                row,
                '标题优化',
                'title',
                original_title,
                title,
                method='rule',
                reason='normalize_title',
                action='确认标题兼容表达和 75 字符限制',
            )
        if progress:
            progress(i + 1, max(1, len(data) * 2))
    print(f"  规则优化: {changed} 行", flush=True)

    # API-based refinement for long titles.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    to_optimize = [(i, row) for i, row in enumerate(data) if row['title'] and len(row['title']) > 65]
    if to_optimize:
        print(
            f"  API 优化 {len(to_optimize)} 条（DeepSeek，{AMAZON_TITLE_CONCURRENCY} 并发）...",
            flush=True,
        )

        def _opt_one(idx, title):
            try:
                provider = get_provider()
                result = provider.call_text(TITLE_OPTIMIZE_PROMPT.format(title), max_tokens=128)
                if result:
                    return idx, result.strip()
            except QuotaExhaustedError:
                raise
            except Exception as e:
                _log.warn("标题优化异常", error=str(e))
            return idx, None

        done = 0
        with ThreadPoolExecutor(max_workers=AMAZON_TITLE_CONCURRENCY) as pool:
            futures = {pool.submit(_opt_one, i, row['title']): i for i, row in to_optimize}
            for future in as_completed(futures):
                try:
                    idx, new_title = future.result()
                except QuotaExhaustedError:
                    for pending in futures:
                        pending.cancel()
                    raise
                if new_title:
                    # 检测 meta 文本（API 返回了说明而非标题）
                    if re.search(r'(optimize|need to|brand is generic|Rules:|这里|以下)', new_title, re.IGNORECASE):
                        _log.warn("标题优化返回meta文本，回退规则处理", row=idx)
                        _add_quality_issue(
                            data[idx],
                            'title_ai_fallback',
                            '标题模型返回了说明性文本，已使用规则结果',
                        )
                    else:
                        normalized_title = _normalize_title(new_title)
                        missing = _missing_factual_markers(data[idx].get('title', ''), normalized_title)
                        if missing:
                            _add_quality_issue(
                                data[idx],
                                'title_fact_loss',
                                '标题模型结果丢失关键规格：' + ', '.join(missing[:5]),
                            )
                        else:
                            before_title = data[idx].get('title', '')
                            data[idx]['title'] = normalized_title
                            _add_audit(
                                data[idx],
                                '标题优化',
                                'title',
                                before_title,
                                normalized_title,
                                method='ai',
                                reason='model_refine',
                                action='抽样确认 AI 标题未丢失规格/数量/适配信息',
                            )
                else:
                    _add_quality_issue(
                        data[idx],
                        'title_ai_fallback',
                        '标题模型未返回有效结果，已使用规则结果',
                    )
                done += 1
                if progress:
                    progress(len(data) + done, max(1, len(data) * 2))
                if done % 10 == 0:
                    print(f"    API标题: {done}/{len(to_optimize)}", flush=True)
    for row in data:
        before_title = row.get('title', '')
        row['title'] = _normalize_title(before_title)
        if row['title'] != before_title:
            _add_audit(
                row,
                '标题优化',
                'title',
                before_title,
                row['title'],
                method='rule',
                reason='final_normalize',
                action='确认最终标题仍保留硬规格',
            )
    if progress:
        progress(1, 1)
    return data


def _clamp_title(title):
    title = str(title or '').strip()
    if len(title) <= 75:
        return title
    shortened = title[:75]
    if ' ' in shortened:
        shortened = shortened.rsplit(' ', 1)[0]
    return shortened[:75].strip()


def _normalize_title(title):
    title = str(title or '').strip()
    if (
        title
        and _BRAND_RE.search(title)
        and not re.match(r'^(For|Compatible with)\b', title, re.IGNORECASE)
    ):
        title = 'For ' + title
    return _clamp_title(title)


def _stage_clean_descs(data, progress=None):
    """先规则清洗，再用 DeepSeek 清理物流、政策和店铺模板内容。"""
    print(f"描述清洗 {len(data)} 条...", flush=True)
    changed = 0
    for index, row in enumerate(data):
        desc = row['desc']
        if desc:
            original = desc
            desc = _BRAND_RE.sub('', desc)
            desc = _OEM_RE.sub('', desc)
            desc = _LOGISTICS_RE.sub('', desc)
            desc = _RETURN_RE.sub('', desc)
            desc = _IMG_RE.sub('', desc)
            desc = re.sub(r'\n{3,}', '\n\n', desc).strip()
            if desc != original:
                row['desc'] = desc
                changed += 1
                _add_audit(
                    row,
                    '描述清洗',
                    'description',
                    original,
                    desc,
                    method='rule',
                    reason='remove_brand_policy_or_images',
                    action='确认描述仍保留尺寸/型号/数量等硬规格',
                )
        if progress:
            progress(index + 1, max(1, len(data) * 2))
    print(f"  规则清洗: {changed} 行", flush=True)

    def _clean_one(idx, desc):
        try:
            provider = get_provider()
            result = provider.call_text(DESC_CLEAN_PROMPT.format(desc), max_tokens=2048)
            if result:
                return idx, result.strip()
        except QuotaExhaustedError:
            raise
        except Exception as e:
            _log.warn("Amazon描述AI清洗异常", row=idx, error=str(e))
        return idx, ''

    items = [(i, row['desc']) for i, row in enumerate(data) if row.get('desc')]
    done = 0
    with ThreadPoolExecutor(
        max_workers=min(AMAZON_DESC_CONCURRENCY, max(1, len(items)))
    ) as pool:
        futures = {pool.submit(_clean_one, i, desc): i for i, desc in items}
        for future in as_completed(futures):
            try:
                idx, cleaned = future.result()
            except QuotaExhaustedError:
                for pending in futures:
                    pending.cancel()
                raise
            except Exception as e:
                idx = futures[future]
                cleaned = ''
                _log.warn("Amazon描述AI清洗异常", row=idx, error=str(e)[:100])
            if cleaned:
                cleaned = _IMG_RE.sub('', cleaned).replace('__IMG__', '')
                cleaned = _BRAND_RE.sub('', cleaned).strip()
                if cleaned:
                    missing = _missing_factual_markers(data[idx].get('desc', ''), cleaned)
                    if missing:
                        _add_quality_issue(
                            data[idx],
                            'description_fact_loss',
                            '描述模型结果丢失关键规格：' + ', '.join(missing[:5]),
                        )
                    else:
                        before_desc = data[idx].get('desc', '')
                        data[idx]['desc'] = cleaned
                        _add_audit(
                            data[idx],
                            '描述清洗',
                            'description',
                            before_desc,
                            cleaned,
                            method='ai',
                            reason='model_clean',
                            action='抽样确认 AI 清洗未删除关键规格',
                        )
                else:
                    _add_quality_issue(
                        data[idx],
                        'description_ai_fallback',
                        '描述模型结果清洗后为空，已保留规则清洗结果',
                    )
            else:
                _add_quality_issue(
                    data[idx],
                    'description_ai_fallback',
                    '描述模型未返回有效结果，已保留规则清洗结果',
                )
            done += 1
            if progress:
                progress(len(data) + done, max(1, len(data) * 2))
    # 后处理：正则强制清品牌残留
    for row in data:
        if row.get('desc'):
            before_desc = row['desc']
            row['desc'] = _BRAND_RE.sub('', row['desc']).strip()
            if row['desc'] != before_desc:
                _add_audit(
                    row,
                    '描述清洗',
                    'description',
                    before_desc,
                    row['desc'],
                    method='rule',
                    reason='final_brand_strip',
                    action='确认品牌清理没有误删兼容车型信息',
                )
        if row.get('keywords'):
            before_keywords = row['keywords']
            row['keywords'] = _BRAND_RE.sub('', row['keywords']).strip()
            if row['keywords'] != before_keywords:
                _add_audit(
                    row,
                    'Bullet+关键词',
                    'keywords',
                    before_keywords,
                    row['keywords'],
                    method='rule',
                    reason='final_brand_strip',
                    action='确认关键词仍有 10 个有效搜索词',
                )
    if progress:
        progress(1, 1)
    return data


def _stage_generate_bullets_keywords(data, progress=None):
    """API 生成 Bullet Point 1-5 + 10 关键词。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    items = [(i, row) for i, row in enumerate(data) if row['title']]
    print(
        f"生成 Bullet Point + 关键词 {len(items)} 条（DeepSeek，{AMAZON_BULLET_CONCURRENCY} 并发）...",
        flush=True,
    )

    def _gen_one(idx, row):
        try:
            provider = get_provider()
            result = provider.call_text(BULLET_KEYWORD_PROMPT.format(
                title=row['title'], desc=row.get('desc', '')[:500]
            ), max_tokens=2048)
            if result:
                parsed = _parse_bullet_json(result)
                if parsed:
                    return idx, parsed.get('bullets', [''] * 5)[:5], parsed.get('keywords', '')
        except QuotaExhaustedError:
            raise
        except Exception as e:
            _log.warn("Bullet/关键词生成异常", row=idx, error=str(e))
        return idx, [''] * 5, ''

    done = 0
    with ThreadPoolExecutor(max_workers=AMAZON_BULLET_CONCURRENCY) as pool:
        futures = {pool.submit(_gen_one, i, row): i for i, row in items}
        for future in as_completed(futures):
            try:
                idx, bullets, keywords = future.result()
            except QuotaExhaustedError:
                for pending in futures:
                    pending.cancel()
                raise
            before_bullets = list(data[idx].get('bullets') or [])
            before_keywords = data[idx].get('keywords', '')
            data[idx]['bullets'] = bullets
            data[idx]['keywords'] = keywords
            _add_audit(
                data[idx],
                'Bullet+关键词',
                'Bullet',
                before_bullets,
                bullets,
                method='ai',
                reason='model_generate',
                action='确认 5 条 Bullet 真实、唯一、无品牌残留',
            )
            _add_audit(
                data[idx],
                'Bullet+关键词',
                'keywords',
                before_keywords,
                keywords,
                method='ai',
                reason='model_generate',
                action='确认关键词正好 10 个且相关',
            )
            done += 1
            if progress:
                progress(done, max(1, len(items) * 2))
            if done % 20 == 0:
                print(f"  进度: {done}/{len(items)}", flush=True)

    # Fill defaults + retry empty bullets
    empty_rows = [(i, row) for i, row in enumerate(data)
                  if row['title'] and (not row.get('bullets') or not any(row['bullets']))]
    if empty_rows:
        print(f"  重试 {len(empty_rows)} 行空 Bullet（10 并发）...", flush=True)
        retry_done = 0
        with ThreadPoolExecutor(max_workers=min(10, AMAZON_BULLET_CONCURRENCY)) as pool:
            futures = {pool.submit(_gen_one, i, row): i for i, row in empty_rows}
            for future in as_completed(futures):
                try:
                    idx, bullets, keywords = future.result()
                except QuotaExhaustedError:
                    for pending in futures:
                        pending.cancel()
                    raise
                before_bullets = list(data[idx].get('bullets') or [])
                before_keywords = data[idx].get('keywords', '')
                data[idx]['bullets'] = bullets
                data[idx]['keywords'] = keywords
                _add_audit(
                    data[idx],
                    'Bullet+关键词',
                    'Bullet',
                    before_bullets,
                    bullets,
                    method='ai',
                    reason='model_retry',
                    action='确认重试生成的 Bullet 真实、唯一、无品牌残留',
                )
                _add_audit(
                    data[idx],
                    'Bullet+关键词',
                    'keywords',
                    before_keywords,
                    keywords,
                    method='ai',
                    reason='model_retry',
                    action='确认重试生成的关键词正好 10 个且相关',
                )
                retry_done += 1
                if progress:
                    progress(len(items) + retry_done, max(1, len(items) * 2))
                if retry_done % 10 == 0:
                    print(f"    重试: {retry_done}/{len(empty_rows)}", flush=True)

    for row in data:
        if 'bullets' not in row:
            row['bullets'] = [''] * 5
        if 'keywords' not in row:
            row['keywords'] = ''
        before_bullets = list(row.get('bullets') or [])
        before_keywords = row.get('keywords', '')
        _normalize_bullets_for_row(row)
        _normalize_keywords_for_row(row)
        if _audit_text(before_bullets) != _audit_text(row.get('bullets')):
            _add_audit(
                row,
                'Bullet+关键词',
                'Bullet',
                before_bullets,
                row.get('bullets'),
                method='rule',
                reason='normalize_bullets',
                action='确认 Bullet 清洗后仍真实、唯一',
            )
        if _audit_text(before_keywords) != _audit_text(row.get('keywords')):
            _add_audit(
                row,
                'Bullet+关键词',
                'keywords',
                before_keywords,
                row.get('keywords'),
                method='rule',
                reason='normalize_keywords',
                action='确认关键词清洗后正好 10 个且相关',
            )
    # Fill any remaining incomplete bullets with rule-based fallback
    filled = 0
    for i, row in enumerate(data):
        bullets = list(row.get('bullets') or [])[:5]
        bullets.extend([''] * (5 - len(bullets)))
        non_empty = [b for b in bullets[:5] if str(b).strip()]
        if len(non_empty) < 5 and row.get('title'):
            before_bullets = list(row.get('bullets') or [])
            before_keywords = row.get('keywords', '')
            _add_quality_issue(
                row,
                'bullet_rule_fallback',
                'Bullet 模型结果不完整，已使用描述或标题规则补全',
            )
            desc = str(row.get('desc', ''))
            # Clean HTML for sentence extraction
            clean_desc = re.sub(r'<[^>]+>', ' ', desc)
            clean_desc = re.sub(r'\s+', ' ', clean_desc).strip()
            # Extract sentences > 20 chars
            sentences = [s.strip() for s in re.split(r'[.!?]\s*', clean_desc) if len(s.strip()) > 30]
            # Only fill from real source content. Fabricated placeholder bullets
            # make incomplete data look valid and are worse than explicit blanks.
            for j in range(5):
                if not str(bullets[j]).strip():
                    if sentences:
                        bullets[j] = _clean_bullet_text(sentences.pop(0))
            row['bullets'] = bullets[:5]
            # Fill keywords if empty
            if not str(row.get('keywords', '')).strip():
                words = re.findall(r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?', clean_desc)
                car_words = [w for w in words if len(w) > 3 and w.lower() not in
                    ('Store', 'Product', 'Description', 'Please', 'Contact', 'Welcome',
                     'Categories', 'About', 'Item', 'Price', 'Shipping', 'Payment', 'Return')]
                row['keywords'] = ', '.join(list(dict.fromkeys(car_words))[:10])
            _normalize_bullets_for_row(row)
            _normalize_keywords_for_row(row)
            _add_audit(
                row,
                'Bullet+关键词',
                'Bullet',
                before_bullets,
                row.get('bullets'),
                method='fallback',
                reason='rule_fill_incomplete_bullets',
                severity='warning',
                action='重点复核规则补全的 Bullet 是否真实且不重复',
            )
            if _audit_text(before_keywords) != _audit_text(row.get('keywords')):
                _add_audit(
                    row,
                    'Bullet+关键词',
                    'keywords',
                    before_keywords,
                    row.get('keywords'),
                    method='fallback',
                    reason='rule_fill_keywords',
                    severity='warning',
                    action='重点复核规则补全关键词是否相关且正好 10 个',
                )
            filled += 1
    if filled:
        print(f'  规则补全: {filled} 行 Bullet/关键词', flush=True)
    incomplete = [
        i + 1 for i, row in enumerate(data)
        if row.get('title') and (
            len([b for b in row.get('bullets', []) if str(b).strip()]) < 5
            or not str(row.get('keywords', '')).strip()
        )
    ]
    if incomplete:
        print(f'\n[WARN] Bullet/关键词生成不完整：{len(incomplete)} 行未达到输出要求，已跳过', flush=True)
    if progress:
        progress(1, 1)
    return data


def _parse_bullet_json(raw):
    """解析 Bullet + Keyword JSON 响应。"""
    if not raw:
        return None
    try:
        items = json.loads(raw)
        if _valid_bullet_payload(items):
            return items
    except json.JSONDecodeError:
        pass
    # Try markdown code block
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
    if m:
        try:
            items = json.loads(m.group(1))
            return items if _valid_bullet_payload(items) else None
        except json.JSONDecodeError:
            pass
    return None


def _valid_bullet_payload(items):
    if not isinstance(items, dict):
        return False
    bullets = items.get('bullets')
    keywords = items.get('keywords')
    return (
        isinstance(bullets, list)
        and all(isinstance(item, str) for item in bullets)
        and isinstance(keywords, str)
    )


def _stage_review_and_gen(
    data,
    cache_path=None,
    quality_issues=None,
    progress=None,
    runtime_metrics=None,
):
    """主图/变种全部重生并移除人物；问题附图直接删除。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    VER = f'main1600_var_{IMAGE_POLICY_VERSION}'
    quality_issues = quality_issues if quality_issues is not None else []
    runtime_metrics = runtime_metrics if isinstance(runtime_metrics, dict) else {}
    concurrency_stats = runtime_metrics.setdefault('concurrency', {})

    main_urls, var_urls, extra_urls = set(), set(), set()
    for row in data:
        if row['main_img']:
            main_urls.add(row['main_img'])
        var_urls.update(u for u in row.get('var_imgs', []) if u)

    # Load cache
    cache = {}
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding='utf-8') as f:
                cache = json.load(f) or {}
        except Exception as e:
            _log.warn('缓存读取失败', error=str(e)[:100])

    if cache.get('image_policy_version') == IMAGE_POLICY_VERSION:
        review_results = cache.get('review_results', {}) or {}
    else:
        review_results = {}
        print("图片策略已更新，旧附图图审缓存已失效", flush=True)
    if cache.get('gen_prompt_version') == VER:
        gen_results = cache.get('gen_results', {}) or {}
        gen_meta = cache.get('gen_meta', {}) or {}
    else:
        gen_results = {}
        gen_meta = {}
    cache['gen_prompt_version'] = VER
    cache['image_policy_version'] = IMAGE_POLICY_VERSION
    cache.setdefault('gen_meta', gen_meta)
    mem_lock = threading.Lock()

    def _persist():
        with mem_lock:
            cache['review_results'] = {u: r for u, r in review_results.items() if r is not None}
            cache['gen_results'] = dict(gen_results)
            cache['gen_meta'] = dict(gen_meta)
        _atomic_save_cache(cache_path, cache)

    # Phase 1: 只审附图 (主图/变种全部必生)
    for row in data:
        for u in row.get('extra_imgs', []):
            if u:
                extra_urls.add(u)
    extra_to_review = [u for u in extra_urls if u not in review_results]
    to_gen_main = [u for u in main_urls if u and f'main:{u}' not in gen_results]
    to_gen_var = [u for u in var_urls if u and f'variant:{u}' not in gen_results]
    progress_total = len(extra_to_review) + len(to_gen_main) + len(to_gen_var)
    progress_done = 0
    extra_cached = len(extra_urls) - len(extra_to_review)
    if extra_cached:
        print(f'附图审图缓存命中: {extra_cached}/{len(extra_urls)}', flush=True)
    if extra_to_review:
        print(
            f'附图审图 {len(extra_to_review)} 张（Agnes {AMAZON_REVIEW_CONCURRENCY} 并发，自适应退避）...',
            flush=True,
        )
        done = none_streak = 0
        quota_hit = False

        def _review_image(url):
            """使用 model_provider 进行图审。"""
            try:
                provider = get_provider()
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
                extra_to_review,
                _review_image,
                operation='amazon_review',
                initial_workers=AMAZON_REVIEW_CONCURRENCY,
                min_workers=10,
                is_success=lambda result: result is not None and not isinstance(result, Exception),
                on_result=_review_done,
                terminal_exceptions=(ProviderQuotaError,),
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
        reviewed_ok = sum(1 for u in extra_urls if u in review_results)
        watermarked = sum(1 for v in review_results.values() if v is True)
        print(f'附图审图完成: 已缓存 {reviewed_ok}/{len(extra_urls)}, 需删除 {watermarked}', flush=True)
    else:
        print(f'附图审图全部缓存命中: {len(extra_urls)} 张', flush=True)
    unreviewed_extra = [u for u in extra_urls if u not in review_results]
    if unreviewed_extra:
        print(f'\n[WARN] 附图仍有 {len(unreviewed_extra)} 张未完成图审，已跳过（继续处理）', flush=True)
        quality_issues.append(
            f'有 {len(unreviewed_extra)} 张附图未完成图审，必须人工复核'
        )

    print(f'主图 {len(main_urls)} + 变种 {len(var_urls)} 全部必生 (无需审图)', flush=True)

    # Phase 2: 生图 (主图+变种总是生成, 附图不生)
    print(f'生图缓存命中: 主图 {len(main_urls)-len(to_gen_main)}/{len(main_urls)}, 变种 {len(var_urls)-len(to_gen_var)}/{len(var_urls)}', flush=True)
    total_to_gen = len(to_gen_main) + len(to_gen_var)
    if total_to_gen:
        print(f'待生成: 主图 {len(to_gen_main)} + 变种 {len(to_gen_var)} = {total_to_gen} (每张实时落盘, 限额不够自动停)', flush=True)
    done = ok = fail_streak = quota_hit = 0

    def _gen_one(url, kind):
        """使用 model_provider 进行图生图。"""
        try:
            provider = get_provider()
            is_variant = kind != 'main'
            return provider.call_image_gen(url, is_variant=is_variant) or ''
        except ProviderQuotaError:
            raise
        except Exception as e:
            _log.warn('生图异常', error=str(e)[:100])
            return ''

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
                r = ''
            done += 1
            progress_done += 1
            if r:
                with mem_lock:
                    cache_key = f'{kind}:{u}'
                    gen_results[cache_key] = r
                    gen_meta[cache_key] = {
                        'kind': kind,
                        'prompt_version': VER,
                        'ts': int(time.time()),
                    }
                    ok += 1
                    fail_streak = 0
                _persist()
            else:
                with mem_lock:
                    fail_streak += 1
            if done % 10 == 0 or (r and done % 5 == 0):
                print(f'  生图: {done}/{total_to_gen} (成功{ok}){" [已缓存]" if r else ""}', flush=True)
            if progress:
                progress(progress_done, max(1, progress_total))

        try:
            _gen_batch, gen_stats = adaptive_map(
                gen_items,
                _gen_item,
                operation='amazon_image_gen',
                initial_workers=AMAZON_IMAGE_GEN_CONCURRENCY,
                min_workers=2,
                is_success=lambda result: bool(result) and not isinstance(result, Exception),
                on_result=_gen_done,
                terminal_exceptions=(ProviderQuotaError,),
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
    missing_main = [u for u in main_urls if not gen_results.get(f'main:{u}')]
    missing_var = [u for u in var_urls if not gen_results.get(f'variant:{u}')]
    if missing_main or missing_var:
        print(f'\n[WARN] 图片生成不完整：主图缺 {len(missing_main)} 张，变种图缺 {len(missing_var)} 张，保留原图继续', flush=True)
        quality_issues.append(
            f'图片生成不完整：主图缺 {len(missing_main)} 张，变种图缺 {len(missing_var)} 张'
        )

    for row in data:
        if row['main_img']:
            before_main = row['main_img']
            row['main_img'] = gen_results.get(f"main:{row['main_img']}", row['main_img'])
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
        row['var_imgs'] = [gen_results.get(f'variant:{u}', u) for u in row.get('var_imgs', []) if u]
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


def _validate_amazon_output(output, expected_rows, extra_issues=None):
    """重新打开输出文件，返回结构化交付质量结果。"""
    wb = openpyxl.load_workbook(output, read_only=True, data_only=True)
    try:
        ws = wb.active
        actual_rows = max(0, ws.max_row - 2)
        if actual_rows != expected_rows:
            raise RuntimeError(
                f'Amazon 输出行数不一致：预期 {expected_rows} 行，实际 {actual_rows} 行'
            )

        rows = []
        for row_number in range(3, ws.max_row + 1):
            rows.append({
                'title': ws.cell(row_number, 1).value,
                'desc': ws.cell(row_number, 2).value,
                'main_img': ws.cell(row_number, 3).value,
                'bullets': [
                    ws.cell(row_number, column).value
                    for column in range(18, 23)
                ],
                'keywords': ws.cell(row_number, 23).value,
            })
        validation = _validate_amazon_rows(rows, extra_issues=extra_issues, row_offset=2)
        if validation['issues']:
            suffix = '（仅显示前 20 项）' if validation.get('truncated') else ''
            print(
                f'\n[WARN] Amazon 输出质量检查：{suffix}'
                + '；'.join(validation['issues']),
                flush=True,
            )
        return validation
    finally:
        wb.close()


def _stage_write_output(data, input_path, progress=None):
    """按输入格式写回填 JSON 或 24 列 XLSX。"""
    if input_path.lower().endswith('.json'):
        output = write_output_json(data, input_path)
        if progress:
            progress(len(data), len(data))
        print(f"完成! 保存 JSON: {output}", flush=True)
        return output

    output = os.path.splitext(input_path)[0] + '_回填.xlsx'
    if os.path.exists(output):
        output = os.path.splitext(input_path)[0] + time.strftime('_回填_%H%M%S_') + \
            str(int(time.time() * 1000) % 1000).zfill(3) + '.xlsx'
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Sheet1'

    # Row 1: Headers (exact match to template)
    headers = [
        '产品标题', '产品描述', '产品图片(本地地址)', '变体图片(本地地址)',
        '制造商', 'Model Number(型号)', 'Model Name(型号名称)',
        'Item Package Length(包装长度)', 'Package Length Unit(包装长度单位)',
        'Item Package Width(包装宽度)', 'Package Width Unit(包装宽度单位)',
        'Item Package Height(包装高度)', 'Package Height Unit(包装高度单位)',
        'Package Weight(包装重量)', 'Package Weight Unit(包装重量单位)',
        'MPN', '促销价 (USD)', 'Bullet Point1', 'Bullet Point2',
        'Bullet Point3', 'Bullet Point4', 'Bullet Point5',
        '关键词信息', 'UPC豁免:'
    ]
    for c, h in enumerate(headers, 1):
        ws.cell(1, c).value = h

    # Row 2: Default values (template reference row)
    defaults = [
        '同事提供', '同事提供', '同事提供', '同事提供',
        'Generic', '随机生成数值', '随机生成',
        '0.1', 'Inches(英寸)', '0.1', 'Inches(英寸)',
        '0.1', 'Inches(英寸)', '0.1', 'Kilograms(公斤）',
        '保存与SKU一致', '数值清空', '同事提供', '同事提供',
        '同事提供', '同事提供', '同事提供',
        '同事提供', '是'
    ]
    for c, v in enumerate(defaults, 1):
        ws.cell(2, c).value = v

    # Data rows start from row 3
    for i, row in enumerate(data):
        r = i + 3
        ws.cell(r, 1).value = row['title']
        ws.cell(r, 2).value = row['desc']
        # 主图 → Col3, 所有附图 → Col4（换行分隔）
        ws.cell(r, 3).value = row['main_img']
        image_urls = list(dict.fromkeys(row.get('extra_imgs', []) + row.get('var_imgs', [])))
        ws.cell(r, 4).value = '\n'.join(image_urls) if image_urls else ''
        # Cols 5-17: left as template defaults (manufacturer, package, MPN, etc.)
        for bi in range(5):
            ws.cell(r, 18 + bi).value = row.get('bullets', [''] * 5)[bi] if bi < len(row.get('bullets', [])) else ''
        ws.cell(r, 23).value = row.get('keywords', '')
        if progress:
            progress(i + 1, len(data))

    # Column widths (all 24 columns)
    widths = {1: 50, 2: 80, 3: 60, 4: 60, 5: 12, 6: 18, 7: 22, 8: 21, 9: 25,
              10: 22, 11: 27, 12: 25, 13: 23, 14: 23, 15: 30, 16: 18, 17: 13,
              18: 60, 19: 60, 20: 60, 21: 60, 22: 60, 23: 50, 24: 12}
    wrap_cols = {1, 2, 3, 4, 18, 19, 20, 21, 22, 23}  # text-heavy columns
    for col, w in widths.items():
        letter = openpyxl.utils.get_column_letter(col)
        ws.column_dimensions[letter].width = w
    # Text wrap for content columns
    from openpyxl.styles import Alignment
    wrap_align = Alignment(wrap_text=True, vertical='top')
    for r in range(1, ws.max_row + 1):
        for c in wrap_cols:
            ws.cell(r, c).alignment = wrap_align
    # Header row bold
    from openpyxl.styles import Font
    for c in range(1, 25):
        ws.cell(1, c).font = Font(bold=True)

    wb.save(output)
    wb.close()
    print(f"完成! 保存: {output}", flush=True)
    return output


def _main(tp: str) -> str:
    """Web 层调用入口：接收文件路径，返回输出路径。"""
    return _main_impl(tp)


def _main_impl(tp: str) -> str:
    """Amazon 管道：支持模板 JSON/XLSX，处理图片、文本并按原格式回填。"""
    rid = new_request_id()
    _log.info("Amazon管道启动", request_id=rid, file=os.path.basename(tp))
    print(f"=== Amazon 采集表 → 回填表 === [rid={rid}]")
    print(f"输入: {tp}")

    reload_credentials()
    # model_provider 会在首次调用时自动检查配置
    try:
        provider = get_provider()
    except ValueError as e:
        raise ValueError(f"配置错误: {e}")

    status = AmazonStatusReporter(tp)
    status.stage('读取表格')

    try:
        is_json = tp.lower().endswith('.json')
        if is_json:
            print("表格格式: JSON | 读取中...")
            data = _stage_read_json(None, tp, progress=status.update)
        else:
            wb = openpyxl.load_workbook(tp, data_only=True)
            try:
                ws = wb.active
                adapter = detect_adapter(ws)
                if not adapter or 'Amazon' not in adapter.name:
                    raise ValueError("无法识别为 Amazon 采集表格式")
                print(f"表格格式: {adapter.name} | {ws.max_row - 1} 行")
                data = _stage_read(ws, adapter, progress=status.update)
            finally:
                wb.close()
        _validate_amazon_input(data)
    except Exception as e:
        _log.error("Amazon阶段 [读取表格] 失败", error=str(e), exc_info=True)
        status.failed('读取表格', e)
        raise

    metrics = PipelineMetrics()

    def _run(name, fn, *args, **kwargs):
        t0 = time.time()
        status.stage(name, 0, len(data))
        try:
            call_kwargs = {'progress': status.update}
            try:
                params = inspect.signature(fn).parameters
                accepts_any = any(
                    param.kind == inspect.Parameter.VAR_KEYWORD
                    for param in params.values()
                )
                if accepts_any:
                    call_kwargs.update(kwargs)
                else:
                    call_kwargs.update({
                        key: value for key, value in kwargs.items()
                        if key in params
                    })
            except (TypeError, ValueError):
                call_kwargs.update(kwargs)
            result = fn(*args, **call_kwargs)
            issue_code = {
                '标题优化': 'title_ai_fallback',
                '描述清洗': 'description_ai_fallback',
                'Bullet+关键词': 'bullet_rule_fallback',
            }.get(name)
            degraded = (
                sum(
                    1 for row in data
                    if any(
                        issue.get('code') == issue_code
                        for issue in row.get('_quality_issues', [])
                    )
                )
                if issue_code else 0
            )
            metrics.record_stage(
                name,
                time.time() - t0,
                len(data),
                max(0, len(data) - degraded),
            )
            return result
        except Exception as e:
            _log.error(f"Amazon阶段 [{name}] 失败", error=str(e), exc_info=True)
            status.failed(name, e)
            raise

    cache_path = os.path.splitext(tp)[0] + '_amz_cache.json'
    # 主图/变种全部必生 (不审), 附图有水印则删 (可选审图, 配额不够自动跳过)
    quality_issues = []
    runtime_metrics = {}
    data = _run(
        '审图+生图',
        _stage_review_and_gen,
        data,
        cache_path,
        quality_issues,
        runtime_metrics=runtime_metrics,
    )
    data = _run('标题优化', _stage_optimize_titles, data)
    data = _run('描述清洗', _stage_clean_descs, data)
    data = _run('Bullet+关键词', _stage_generate_bullets_keywords, data)
    quality_issues.extend(_summarize_row_quality_issues(data))
    output = _run('写回填表', _stage_write_output, data, tp)
    if output.lower().endswith('.xlsx'):
        validation = _validate_amazon_output(
            output,
            len(data),
            extra_issues=quality_issues,
        )
    else:
        validation = _validate_amazon_rows(data, extra_issues=quality_issues)
    validation = _attach_audit_to_validation(validation, data)
    if hasattr(provider, 'metrics_snapshot'):
        metrics.set_provider_metrics(provider.metrics_snapshot())
    metrics.set_concurrency_metrics(runtime_metrics.get('concurrency'))
    metrics.set_quality_metrics(validation)
    metrics_data = metrics.to_dict()
    status.finish(output, validation, metrics_data)

    try:
        metrics_path = os.path.splitext(output)[0] + '_metrics.json'
        with open(metrics_path, 'w', encoding='utf-8') as f:
            json.dump(metrics_data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    print(f"输出: {output}")
    return output


def main():
    if len(sys.argv) < 2:
        print("用法: uv run python scripts/process_amazon.py \"<采集表.xlsx|json>\"")
        sys.exit(1)
    tp = sys.argv[1]
    _main_impl(tp)


if __name__ == '__main__':
    main()
