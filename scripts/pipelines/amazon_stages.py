#!/usr/bin/env python3
"""Amazon title, description, bullet, and keyword processing stages."""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from crosspilot.prompt_registry import get_prompt_registry
from model_provider import ProviderQuotaError, get_provider as _default_get_provider
from pipeline_log import log as _log
from services.amazon_titles import normalize_amazon_title
from .amazon_constants import (
    AMAZON_BULLET_CONCURRENCY,
    AMAZON_DESC_CONCURRENCY,
    AMAZON_TITLE_CONCURRENCY,
    _BRAND_RE,
    _IMG_RE,
    _LOGISTICS_RE,
    _OEM_RE,
    _RETURN_RE,
    _add_audit,
    _add_quality_issue,
    _audit_text,
    _clean_bullet_text,
    _fingerprint_text,
    _is_weak_bullet,
    _missing_factual_markers,
    _normalize_bullets_for_row,
    _normalize_keywords_for_row,
    _plain_text,
    _unexpected_brand_markers,
)

QuotaExhaustedError = ProviderQuotaError
_prompts = get_prompt_registry()


def _source_bullet_candidates(row) -> list[str]:
    """Build five distinct bullets using only source title/description text."""
    title = _clean_bullet_text(row.get('title', ''))
    description = _plain_text(row.get('desc', ''))
    if not description:
        return []

    raw_parts = re.split(
        r'(?:\s+\d+[.)]\s+|[.!?;；•]\s*|[\r\n]+)',
        description,
    )
    snippets = []
    for raw in raw_parts:
        candidate = _clean_bullet_text(raw)
        if len(candidate) >= 20:
            snippets.append(candidate)

    words = description.split()
    for start in range(0, len(words), 12):
        candidate = _clean_bullet_text(
            ' '.join(words[start:start + 24])
        )
        if len(candidate) >= 20:
            snippets.append(candidate)
        if len(snippets) >= 10:
            break
    if title:
        snippets.insert(0, title)

    unique_snippets = []
    seen_snippets = set()
    for snippet in snippets:
        fingerprint = _fingerprint_text(snippet)
        if not fingerprint or fingerprint in seen_snippets:
            continue
        seen_snippets.add(fingerprint)
        unique_snippets.append(snippet)

    if not unique_snippets:
        return []

    labels = (
        'Product identification',
        'Listing detail',
        'Catalog detail',
        'Product information',
        'Source specification',
    )
    candidates = []
    seen = set()
    for index in range(5):
        snippet = unique_snippets[index % len(unique_snippets)]
        candidate = _clean_bullet_text(
            f'{labels[index]}: {snippet}'
        )
        fingerprint = _fingerprint_text(candidate)
        if (
            candidate
            and fingerprint not in seen
            and not _is_weak_bullet(candidate)
        ):
            seen.add(fingerprint)
            candidates.append(candidate)
    return candidates


def _stage_optimize_titles(data, progress=None, provider_getter=None):
    """标题优化：Generic 产品 [for 适配品牌/型号]，最长 75 字符。"""
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
                provider = (provider_getter or _default_get_provider)()
                result = provider.call_text(
                    _prompts.render(
                        "amazon.title_optimize",
                        title=title,
                    ),
                    max_tokens=128,
                )
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
                        unexpected_brands = _unexpected_brand_markers(
                            data[idx].get('title', ''),
                            normalized_title,
                        )
                        if missing or unexpected_brands:
                            reason = (
                                'title_fact_loss'
                                if missing
                                else 'title_brand_hallucination'
                            )
                            details = (
                                '丢失关键规格：' + ', '.join(missing[:5])
                                if missing
                                else '新增源标题没有的品牌：'
                                + ', '.join(unexpected_brands[:5])
                            )
                            _add_audit(
                                data[idx],
                                '标题优化',
                                'title',
                                data[idx].get('title', ''),
                                normalized_title,
                                method='ai_rejected',
                                reason=reason,
                                severity='warning',
                                action='已拒绝 AI 标题（' + details + '），保留规则结果',
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
    return normalize_amazon_title(title)


def _stage_clean_descs(data, progress=None, provider_getter=None):
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
            provider = (provider_getter or _default_get_provider)()
            result = provider.call_text(
                _prompts.render(
                    "amazon.description_clean",
                    description=desc,
                ),
                max_tokens=2048,
            )
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
                    # 原始描述含交叉销售时不检测 fact_loss（删除的就是垃圾）
                    orig_desc = str(data[idx].get('desc', '')).lower()
                    has_cross_sell = any(k in orig_desc for k in (' usd', 'store categories', 'welcome to', 'payment', 'shipping policy'))
                    if not has_cross_sell:
                        missing = _missing_factual_markers(data[idx].get('desc', ''), cleaned)
                        if missing:
                            _add_audit(
                                data[idx],
                                '描述清洗',
                                'description',
                                data[idx].get('desc', ''),
                                cleaned,
                                method='ai_rejected',
                                reason='description_fact_loss',
                                severity='warning',
                                action=(
                                    '已拒绝 AI 描述（丢失关键规格：'
                                    + ', '.join(missing[:5])
                                    + '），保留规则结果'
                                ),
                            )
                            cleaned = ''
                    if cleaned:
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


def _stage_generate_bullets_keywords(data, progress=None, provider_getter=None):
    """API 生成 Bullet Point 1-5 + 10 关键词。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    items = [(i, row) for i, row in enumerate(data) if row['title']]
    print(
        f"生成 Bullet Point + 关键词 {len(items)} 条（DeepSeek，{AMAZON_BULLET_CONCURRENCY} 并发）...",
        flush=True,
    )

    def _gen_one(idx, row):
        try:
            provider = (provider_getter or _default_get_provider)()
            result = provider.call_text(
                _prompts.render(
                    "amazon.bullet_keywords",
                    title=row['title'],
                    description=row.get('desc', '')[:500],
                ),
                max_tokens=2048,
            )
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
            source_candidates = _source_bullet_candidates(row)
            used = {
                _fingerprint_text(bullet)
                for bullet in bullets
                if str(bullet).strip()
            }
            for j in range(5):
                if not str(bullets[j]).strip():
                    while source_candidates:
                        candidate = source_candidates.pop(0)
                        fingerprint = _fingerprint_text(candidate)
                        if fingerprint and fingerprint not in used:
                            bullets[j] = candidate
                            used.add(fingerprint)
                            break
            row['bullets'] = bullets[:5]
            # Fill keywords if empty
            if not str(row.get('keywords', '')).strip():
                clean_desc = _plain_text(row.get('desc', ''))
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
