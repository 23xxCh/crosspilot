"""Amazon Bullet Point and search-keyword generation."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import re

from ..config.prompts import get_prompt_registry
from ..providers import (
    ProviderQuotaError,
    get_provider as _default_get_provider,
)
from ..log import log as _log
from ..quality import (
    AMAZON_BULLET_CONCURRENCY,
    add_audit as _add_audit,
    add_quality_issue as _add_quality_issue,
    audit_text as _audit_text,
    clean_bullet_text as _clean_bullet_text,
    fingerprint_text as _fingerprint_text,
    is_weak_bullet as _is_weak_bullet,
    normalize_bullets_for_row as _normalize_bullets_for_row,
    normalize_keywords_for_row as _normalize_keywords_for_row,
    plain_text as _plain_text,
)
from .subtitles import normalize_subtitles_for_rows
from .locale import (
    market_for_row,
    market_prompt_values,
    normalize_localized_listing_fields,
)


QuotaExhaustedError = ProviderQuotaError
_prompts = get_prompt_registry()


def source_bullet_candidates(row) -> list[str]:
    """Build five distinct bullets from source title and description only."""
    title = _clean_bullet_text(row.get("title", ""))
    description = _plain_text(row.get("desc", ""))
    if not description:
        return []

    raw_parts = re.split(
        r"(?:\s+\d+[.)]\s+|[.!?;；•]\s*|[\r\n]+)",
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
            " ".join(words[start:start + 24])
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
        "Product identification",
        "Listing detail",
        "Catalog detail",
        "Product information",
        "Source specification",
    )
    candidates = []
    seen = set()
    for index in range(5):
        snippet = unique_snippets[index % len(unique_snippets)]
        candidate = _clean_bullet_text(
            f"{labels[index]}: {snippet}"
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


def generate_bullets_keywords(
    data,
    progress=None,
    provider_getter=None,
):
    """Generate, validate, and rule-fill five bullets and ten keywords."""
    for row in data:
        if not str(row.get("desc") or "").strip():
            row["bullets"] = [""] * 5
            row["keywords"] = ""
            row["subtitle"] = ""
    items = [
        (index, row)
        for index, row in enumerate(data)
        if row["title"] and str(row.get("desc") or "").strip()
    ]
    print(
        f"生成 Bullet Point + 关键词 {len(items)} 条"
        f"（DeepSeek，{AMAZON_BULLET_CONCURRENCY} 并发）...",
        flush=True,
    )

    def generate_one(index, row):
        try:
            provider = (
                provider_getter or _default_get_provider
            )()
            result = provider.call_text(
                _prompts.render(
                    "amazon.bullet_keywords",
                    title=row["title"],
                    description=row.get("desc", "")[:500],
                    **market_prompt_values(row),
                ),
                max_tokens=2048,
            )
            if result:
                parsed = parse_bullet_json(result)
                if parsed:
                    return (
                        index,
                        parsed.get("bullets", [""] * 5)[:5],
                        parsed.get("keywords", ""),
                        parsed.get("subtitle", ""),
                    )
        except QuotaExhaustedError:
            raise
        except Exception as exc:
            _log.warn(
                "Bullet/关键词生成异常",
                row=index,
                error=str(exc),
            )
        return index, [""] * 5, "", ""

    done = 0
    with ThreadPoolExecutor(
        max_workers=AMAZON_BULLET_CONCURRENCY
    ) as pool:
        futures = {
            pool.submit(generate_one, index, row): index
            for index, row in items
        }
        for future in as_completed(futures):
            try:
                index, bullets, keywords, subtitle = future.result()
            except QuotaExhaustedError:
                for pending in futures:
                    pending.cancel()
                raise
            before_bullets = list(
                data[index].get("bullets") or []
            )
            before_keywords = data[index].get("keywords", "")
            data[index]["bullets"] = bullets
            data[index]["keywords"] = keywords
            data[index]["subtitle"] = subtitle
            _add_audit(
                data[index],
                "Bullet+关键词",
                "Bullet",
                before_bullets,
                bullets,
                method="ai",
                reason="model_generate",
                action="确认 5 条 Bullet 真实、唯一、无品牌残留",
            )
            _add_audit(
                data[index],
                "Bullet+关键词",
                "keywords",
                before_keywords,
                keywords,
                method="ai",
                reason="model_generate",
                action="确认关键词正好 10 个且相关",
            )
            done += 1
            if progress:
                progress(done, max(1, len(items) * 2))
            if done % 20 == 0:
                print(
                    f"  进度: {done}/{len(items)}",
                    flush=True,
                )

    empty_rows = [
        (index, row)
        for index, row in enumerate(data)
        if (
            row["title"]
            and str(row.get("desc") or "").strip()
            and (
                not row.get("bullets")
                or not any(row["bullets"])
            )
        )
    ]
    if empty_rows:
        print(
            f"  重试 {len(empty_rows)} 行空 Bullet（10 并发）...",
            flush=True,
        )
        retry_done = 0
        with ThreadPoolExecutor(
            max_workers=min(10, AMAZON_BULLET_CONCURRENCY)
        ) as pool:
            futures = {
                pool.submit(generate_one, index, row): index
                for index, row in empty_rows
            }
            for future in as_completed(futures):
                try:
                    index, bullets, keywords, subtitle = future.result()
                except QuotaExhaustedError:
                    for pending in futures:
                        pending.cancel()
                    raise
                before_bullets = list(
                    data[index].get("bullets") or []
                )
                before_keywords = data[index].get(
                    "keywords",
                    "",
                )
                data[index]["bullets"] = bullets
                data[index]["keywords"] = keywords
                data[index]["subtitle"] = subtitle
                _add_audit(
                    data[index],
                    "Bullet+关键词",
                    "Bullet",
                    before_bullets,
                    bullets,
                    method="ai",
                    reason="model_retry",
                    action=(
                        "确认重试生成的 Bullet 真实、唯一、"
                        "无品牌残留"
                    ),
                )
                _add_audit(
                    data[index],
                    "Bullet+关键词",
                    "keywords",
                    before_keywords,
                    keywords,
                    method="ai",
                    reason="model_retry",
                    action=(
                        "确认重试生成的关键词正好 10 个且相关"
                    ),
                )
                retry_done += 1
                if progress:
                    progress(
                        len(items) + retry_done,
                        max(1, len(items) * 2),
                    )
                if retry_done % 10 == 0:
                    print(
                        f"    重试: {retry_done}/{len(empty_rows)}",
                        flush=True,
                    )

    for row in data:
        if not str(row.get("desc") or "").strip():
            row["bullets"] = [""] * 5
            row["keywords"] = ""
            row["subtitle"] = ""
            continue
        if "bullets" not in row:
            row["bullets"] = [""] * 5
        if "keywords" not in row:
            row["keywords"] = ""
        if "subtitle" not in row:
            row["subtitle"] = ""
        before_bullets = list(row.get("bullets") or [])
        before_keywords = row.get("keywords", "")
        if market_for_row(row).language_code == "en":
            _normalize_bullets_for_row(row)
            _normalize_keywords_for_row(row)
        else:
            normalize_localized_listing_fields(row)
        if _audit_text(before_bullets) != _audit_text(
            row.get("bullets")
        ):
            _add_audit(
                row,
                "Bullet+关键词",
                "Bullet",
                before_bullets,
                row.get("bullets"),
                method="rule",
                reason="normalize_bullets",
                action="确认 Bullet 清洗后仍真实、唯一",
            )
        if _audit_text(before_keywords) != _audit_text(
            row.get("keywords")
        ):
            _add_audit(
                row,
                "Bullet+关键词",
                "keywords",
                before_keywords,
                row.get("keywords"),
                method="rule",
                reason="normalize_keywords",
                action="确认关键词清洗后正好 10 个且相关",
            )

    filled = 0
    for row in data:
        bullets = list(row.get("bullets") or [])[:5]
        bullets.extend([""] * (5 - len(bullets)))
        non_empty = [
            bullet
            for bullet in bullets[:5]
            if str(bullet).strip()
        ]
        if (
            len(non_empty) < 5
            and row.get("title")
            and str(row.get("desc") or "").strip()
            and market_for_row(row).language_code == "en"
        ):
            before_bullets = list(row.get("bullets") or [])
            before_keywords = row.get("keywords", "")
            _add_quality_issue(
                row,
                "bullet_rule_fallback",
                "Bullet 模型结果不完整，已使用描述或标题规则补全",
            )
            source_candidates = source_bullet_candidates(row)
            used = {
                _fingerprint_text(bullet)
                for bullet in bullets
                if str(bullet).strip()
            }
            for index in range(5):
                if str(bullets[index]).strip():
                    continue
                while source_candidates:
                    candidate = source_candidates.pop(0)
                    fingerprint = _fingerprint_text(candidate)
                    if fingerprint and fingerprint not in used:
                        bullets[index] = candidate
                        used.add(fingerprint)
                        break
            row["bullets"] = bullets[:5]
            if not str(row.get("keywords", "")).strip():
                clean_description = _plain_text(
                    row.get("desc", "")
                )
                words = re.findall(
                    r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?",
                    clean_description,
                )
                excluded = (
                    "Store",
                    "Product",
                    "Description",
                    "Please",
                    "Contact",
                    "Welcome",
                    "Categories",
                    "About",
                    "Item",
                    "Price",
                    "Shipping",
                    "Payment",
                    "Return",
                )
                candidates = [
                    word
                    for word in words
                    if len(word) > 3
                    and word.lower() not in excluded
                ]
                row["keywords"] = ", ".join(
                    list(dict.fromkeys(candidates))[:10]
                )
            _normalize_bullets_for_row(row)
            _normalize_keywords_for_row(row)
            _add_audit(
                row,
                "Bullet+关键词",
                "Bullet",
                before_bullets,
                row.get("bullets"),
                method="fallback",
                reason="rule_fill_incomplete_bullets",
                severity="warning",
                action=(
                    "重点复核规则补全的 Bullet 是否真实且不重复"
                ),
            )
            if _audit_text(before_keywords) != _audit_text(
                row.get("keywords")
            ):
                _add_audit(
                    row,
                    "Bullet+关键词",
                    "keywords",
                    before_keywords,
                    row.get("keywords"),
                    method="fallback",
                    reason="rule_fill_keywords",
                    severity="warning",
                    action=(
                        "重点复核规则补全关键词是否相关且正好 10 个"
                    ),
                )
            filled += 1
    if filled:
        print(
            f"  规则补全: {filled} 行 Bullet/关键词",
            flush=True,
        )
    normalize_subtitles_for_rows(data)
    incomplete = [
        index + 1
        for index, row in enumerate(data)
        if (
            row.get("title")
            and str(row.get("desc") or "").strip()
            and (
                len([
                    bullet
                    for bullet in row.get("bullets", [])
                    if str(bullet).strip()
                ]) < 5
                or not str(row.get("keywords", "")).strip()
            )
        )
    ]
    if incomplete:
        print(
            f"\n[WARN] Bullet/关键词生成不完整："
            f"{len(incomplete)} 行未达到输出要求，已跳过",
            flush=True,
        )
    if progress:
        progress(1, 1)
    return data


def parse_bullet_json(raw):
    """Parse a Bullet/keyword JSON response, including code fences."""
    if not raw:
        return None
    try:
        items = json.loads(raw)
        if valid_bullet_payload(items):
            return items
    except json.JSONDecodeError:
        pass
    match = re.search(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        raw,
        re.DOTALL,
    )
    if match:
        try:
            items = json.loads(match.group(1))
            return items if valid_bullet_payload(items) else None
        except json.JSONDecodeError:
            pass
    return None


def valid_bullet_payload(items):
    if not isinstance(items, dict):
        return False
    bullets = items.get("bullets")
    keywords = items.get("keywords")
    subtitle = items.get("subtitle", "")
    return (
        isinstance(bullets, list)
        and all(isinstance(item, str) for item in bullets)
        and isinstance(keywords, str)
        and isinstance(subtitle, str)
    )


__all__ = [
    "generate_bullets_keywords",
    "parse_bullet_json",
    "source_bullet_candidates",
    "valid_bullet_payload",
]
