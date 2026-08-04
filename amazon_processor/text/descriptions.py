"""Amazon description extraction, formatting, and relevance protection."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import html
import json
import re

from ..config.prompts import get_prompt_registry
from ..providers import (
    ProviderQuotaError,
    get_provider as _default_get_provider,
)
from ..log import log as _log
from ..quality import (
    AMAZON_DESC_CONCURRENCY,
    BRAND_RE as _BRAND_RE,
    IMG_RE as _IMG_RE,
    OEM_RE as _OEM_RE,
    add_audit as _add_audit,
    add_quality_issue as _add_quality_issue,
    has_cross_sell_contamination as _has_cross_sell_contamination,
    is_text_relevant as _is_text_relevant,
    missing_factual_markers as _missing_factual_markers,
    relevant_token_overlap as _relevant_token_overlap,
    trim_words as _trim_words,
)
from .locale import description_label, market_prompt_values


QuotaExhaustedError = ProviderQuotaError
_prompts = get_prompt_registry()

DESCRIPTION_MAX_CHARS = 500
_PRICE_RE = re.compile(r"\b\d+(?:\.\d{1,2})?\s*USD\b", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_BREAK_RE = re.compile(
    r"<\s*(?:br|/p|/div|/li)\s*/?\s*>",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_PRODUCT_START_RE = re.compile(
    r"\b(?:product\s+description|description|features?|"
    r"specifications?|bullet\s+points?|item\s+description)\s*[:：]?",
    re.IGNORECASE,
)
_POLICY_STOP_RE = re.compile(
    r"(?:payment\s+policy|shipping\s+policy|returns?\s+policy|"
    r"terms?\s+of\s+sale|about\s+us|we\s+(?:only\s+)?accept\s+"
    r"(?:payment|paypal)|all\s+major\s+credit\s+cards|"
    r"payment\s+must\s+be\s+received|orders?\s+processed\s+within|"
    r"delivery\s+details?|your\s+satisfaction|"
    r"please\s+contact\s+us|negative\s+feedback|"
    r"visit\s+(?:our|my)\s+store|paypal|"
    r"item\s+will\s+be\s+shipped|delivery\s+time|we\s+ship)",
    re.IGNORECASE,
)
_STORE_PROMO_RES = (
    re.compile(
        r"international\s+buyers?\s+please\s+note:?.*?"
        r"(?:the\s+quality\s+is\s+guaranteed!|prior\s+to\s+bidding/buying[.!])",
        re.IGNORECASE,
    ),
    re.compile(
        r"welcome\s+to\s+my\s+store.*?(?:visit\s+(?:our|my)\s+store|"
        r"thanks?\s+for\s+your\s+understandings?)[.!^_\s]*",
        re.IGNORECASE,
    ),
    re.compile(
        r"please\s+contact\s+us\s+before.*?"
        r"(?:thanks?\s+for\s+your\s+understandings?|negative\s+feedback)[.!]*",
        re.IGNORECASE,
    ),
    re.compile(
        r"if\s+you\s+like\s+(?:the|our)\s+product.*?"
        r"(?:visit\s+(?:our|my)\s+store|favourite\s+seller)[.!^_\s]*",
        re.IGNORECASE,
    ),
    re.compile(
        r"if\s+the\s+products?\s+have\s+any\s+problems?.*?"
        r"thanks?\s+for\s+your\s+understandings?[.!]*",
        re.IGNORECASE,
    ),
    re.compile(
        r"please\s+leave\s+(?:a\s+)?message\s+before.*?"
        r"(?:bad|negative)\s+feedback.*?"
        r"(?:understanding|solution|[.!])",
        re.IGNORECASE,
    ),
)
_SELLER_INSTRUCTION_RE = re.compile(
    r"\(?\s*please\s+(?:tell|leave|send)\s+(?:us|me).*?"
    r"(?:otherwise|or)\s+we(?:'ll|\s+will).*?randomly\s*\)?",
    re.IGNORECASE,
)
_DETAIL_RE = re.compile(
    r"\b("
    r"material|dimensions?|size|colou?r|"
    r"compatibility|compatible\s+with|fitment|applicable\s+models?|"
    r"quantity|qty|package\s+(?:includes?|contents?)|packing\s+list|"
    r"specifications?|technical\s+specifications?|features?"
    r")\s*[:：\-]\s*",
    re.IGNORECASE,
)
_DETAIL_ALIASES = {
    "material": "Material",
    "dimension": "Size",
    "dimensions": "Size",
    "size": "Size",
    "color": "Color",
    "colour": "Color",
    "compatibility": "Compatibility",
    "compatible with": "Compatibility",
    "fitment": "Compatibility",
    "applicable model": "Compatibility",
    "applicable models": "Compatibility",
    "quantity": "Quantity",
    "qty": "Quantity",
    "package include": "Package Includes",
    "package includes": "Package Includes",
    "package content": "Package Includes",
    "package contents": "Package Includes",
    "packing list": "Package Includes",
    "specification": "Specifications",
    "specifications": "Specifications",
    "technical specification": "Specifications",
    "technical specifications": "Specifications",
    "feature": "Features",
    "features": "Features",
}
_DISPLAY_ORDER = (
    "Material",
    "Size",
    "Color",
    "Compatibility",
    "Quantity",
    "Specifications",
    "Features",
    "Package Includes",
)
_KEEP_PRIORITY = (
    "Compatibility",
    "Size",
    "Specifications",
    "Material",
    "Quantity",
    "Package Includes",
    "Color",
    "Features",
)


def _clean_line(value: object) -> str:
    text = html.unescape(str(value or ""))
    text = _BREAK_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = _IMG_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    text = _SELLER_INSTRUCTION_RE.sub(" ", text)
    text = _BRAND_RE.sub("", text)
    text = _OEM_RE.sub("", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+\d+[.)]?\s*$", "", text)
    text = text.strip(" \t\r\n:：;,-")
    return text


def _normalize_source_text(value: object) -> str:
    text = html.unescape(str(value or ""))
    text = _BREAK_RE.sub("\n", text)
    text = _IMG_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    text = text.replace("\xa0", " ")
    # Source pages commonly concatenate cross-sell cards. A price is the
    # reliable boundary; isolate and remove each priced listing line.
    text = re.sub(
        r"(\b\d+(?:\.\d{1,2})?\s*USD\b)\s*",
        r"\1\n",
        text,
        flags=re.IGNORECASE,
    )
    lines = [
        line
        for line in text.splitlines()
        if not _PRICE_RE.search(line)
    ]
    text = "\n".join(lines)
    for pattern in _STORE_PROMO_RES:
        text = pattern.sub(" ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _candidate_fragment(text: str, start: int) -> str:
    fragment = text[start:]
    stop = _POLICY_STOP_RE.search(fragment)
    if stop:
        fragment = fragment[:stop.start()]
    fragment = _PRODUCT_START_RE.sub(" ", fragment)
    for pattern in _STORE_PROMO_RES:
        fragment = pattern.sub(" ", fragment)
    fragment = _BRAND_RE.sub("", fragment)
    fragment = _OEM_RE.sub("", fragment)
    fragment = re.sub(r"\s+", " ", fragment).strip(" .,:;-")
    fragment = re.sub(r"\s+\d+[.)]?\s*$", "", fragment).strip()
    return fragment


def select_relevant_description_source(
    title: str,
    description: str,
) -> str:
    """Select the product block that best matches the current title."""
    text = _normalize_source_text(description)
    if not text:
        return ""
    starts = [0]
    starts.extend(match.start() for match in _PRODUCT_START_RE.finditer(text))
    candidates = []
    seen = set()
    for start in starts:
        candidate = _candidate_fragment(text, start)
        fingerprint = re.sub(r"[^a-z0-9]+", "", candidate.lower())
        if len(candidate) < 12 or not fingerprint or fingerprint in seen:
            continue
        seen.add(fingerprint)
        overlap = _relevant_token_overlap(title, candidate)
        product_signals = len(
            re.findall(
                r"\b(?:material|size|dimension|color|colour|compatible|"
                r"fitment|feature|package|quantity|install|protect|"
                r"waterproof|elastic|seal|adhesive|glue|sponge)\w*\b",
                candidate,
                re.IGNORECASE,
            )
        )
        contamination_penalty = (
            20 if _has_cross_sell_contamination(candidate) else 0
        )
        score = (
            len(overlap) * 12
            + min(product_signals, 8)
            + (2 if start else 0)
            - contamination_penalty
        )
        candidates.append(
            (
                score,
                len(overlap),
                min(product_signals, 8),
                -len(candidate),
                candidate,
            )
        )
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    best_record = candidates[0]
    best = best_record[4]
    if _has_cross_sell_contamination(best):
        return ""
    if not _is_text_relevant(title, best) and best_record[2] < 2:
        return ""
    return best[:8000].strip()


def partition_product_description_rows(
    data: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Remove rows whose source contains no usable product description."""
    retained = []
    rejected = []
    for row in data:
        product_id = str(row.get("id") or "")
        title = str(row.get("title") or "")
        source = str(row.get("desc") or "")
        if not source.strip():
            rejected.append({
                "product_id": product_id,
                "title": title,
                "code": "missing_source_description",
                "message": "源产品描述为空，商品已从正式回填表删除",
            })
            continue
        selected = select_relevant_description_source(title, source)
        if not selected:
            rejected.append({
                "product_id": product_id,
                "title": title,
                "code": "missing_product_description_content",
                "message": (
                    "源描述清除店铺和交易模板后没有产品内容，"
                    "商品已从正式回填表删除"
                ),
            })
            continue
        row["_description_source_block"] = selected
        retained.append(row)
    return retained, rejected


def _extract_json_object(raw: str) -> dict | None:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _normalize_label(value: object) -> str:
    key = re.sub(r"\s+", " ", str(value or "").strip().lower())
    key = key.rstrip(":：")
    return _DETAIL_ALIASES.get(key, "")


def parse_structured_description(
    raw: str,
) -> tuple[str, list[tuple[str, str]]] | None:
    """Parse the model's structured description response."""
    value = _extract_json_object(raw)
    if not value:
        return None
    summary = _clean_line(
        value.get("summary")
        or value.get("intro")
        or value.get("description")
        or ""
    )
    raw_details = value.get("details") or value.get("specs") or []
    details: list[tuple[str, str]] = []
    if isinstance(raw_details, dict):
        raw_details = [
            {"label": label, "value": detail}
            for label, detail in raw_details.items()
        ]
    if isinstance(raw_details, list):
        for item in raw_details:
            if not isinstance(item, dict):
                continue
            label = _normalize_label(
                item.get("label") or item.get("name") or ""
            )
            detail = _clean_line(
                item.get("value") or item.get("text") or ""
            )
            if label and detail:
                details.append((label, detail))
    for key, label in (
        ("material", "Material"),
        ("size", "Size"),
        ("color", "Color"),
        ("colour", "Color"),
        ("compatibility", "Compatibility"),
        ("quantity", "Quantity"),
        ("specifications", "Specifications"),
        ("features", "Features"),
        ("package_includes", "Package Includes"),
    ):
        detail = _clean_line(value.get(key) or "")
        if detail:
            details.append((label, detail))
    if not summary and not details:
        return None
    return summary, details


def _extract_details(text: str) -> list[tuple[str, str]]:
    matches = list(_DETAIL_RE.finditer(text))
    details: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        label = _normalize_label(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = _clean_line(text[match.end():end])
        if not label or not value:
            continue
        details.append((label, _trim_words(value, 180)))
    return details


def _dedupe_details(
    details: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    result = []
    seen = set()
    for label, value in details:
        normalized_label = _normalize_label(label) or str(label)
        cleaned = _clean_line(value)
        if (
            normalized_label not in _DISPLAY_ORDER
            or not cleaned
            or cleaned.lower() in {"n/a", "na", "none", "unknown"}
            or _has_cross_sell_contamination(cleaned)
        ):
            continue
        key = (normalized_label, cleaned.lower())
        if key in seen:
            continue
        seen.add(key)
        result.append((normalized_label, cleaned))
    return result


def format_description(
    summary: str,
    details: list[tuple[str, str]] | tuple[tuple[str, str], ...],
    *,
    limit: int = DESCRIPTION_MAX_CHARS,
    site: str = "US",
) -> tuple[str, bool]:
    """Render a stable intro + blank line + labeled-lines description."""
    cleaned_summary = _clean_line(summary)
    if _has_cross_sell_contamination(cleaned_summary):
        cleaned_summary = ""
    normalized = _dedupe_details(list(details))
    grouped: dict[str, list[str]] = {}
    for label, value in normalized:
        grouped.setdefault(label, [])
        if value not in grouped[label]:
            grouped[label].append(value)
    lines_by_label = {
        label: (
            f"{description_label(label, site)}: "
            f"{'; '.join(grouped[label])}"
        )
        for label in _DISPLAY_ORDER
        if grouped.get(label)
    }
    ordered_lines = [
        lines_by_label[label]
        for label in _DISPLAY_ORDER
        if label in lines_by_label
    ]
    full = (
        cleaned_summary
        + ("\n\n" if cleaned_summary and ordered_lines else "")
        + "\n".join(ordered_lines)
    ).strip()
    if len(full) <= limit:
        return full, False

    compacted = True
    summary_budget = min(180, max(60, limit // 3))
    cleaned_summary = _trim_words(cleaned_summary, summary_budget)
    selected_labels = []
    used = len(cleaned_summary) + (2 if cleaned_summary else 0)
    for label in _KEEP_PRIORITY:
        line = lines_by_label.get(label)
        if not line:
            continue
        separator = 1 if selected_labels else 0
        if used + separator + len(line) <= limit:
            selected_labels.append(label)
            used += separator + len(line)
    selected_set = set(selected_labels)
    selected_lines = [
        lines_by_label[label]
        for label in _DISPLAY_ORDER
        if label in selected_set
    ]
    result = (
        cleaned_summary
        + ("\n\n" if cleaned_summary and selected_lines else "")
        + "\n".join(selected_lines)
    ).strip()
    if len(result) <= limit:
        return result, compacted
    return _trim_words(result, limit), compacted


def build_rule_description(
    title: str,
    source: str,
    *,
    site: str = "US",
) -> tuple[str, bool]:
    """Build a non-invented fallback using title and extracted source facts."""
    summary = _clean_line(title)
    if summary and summary[-1:] not in ".!?":
        summary += "."
    details = _extract_details(source)
    return format_description(summary, details, site=site)


def _valid_description(
    title: str,
    source: str,
    candidate: str,
) -> tuple[bool, list[str]]:
    if (
        not candidate
        or len(candidate) > DESCRIPTION_MAX_CHARS
        or _has_cross_sell_contamination(candidate)
        or not _is_text_relevant(title, candidate)
    ):
        return False, []
    missing = _missing_factual_markers(source, candidate)
    return not missing, missing


def clean_descriptions(data, progress=None, provider_getter=None):
    """Extract relevant source blocks and render structured descriptions."""
    print(f"描述提取与分段 {len(data)} 条...", flush=True)
    items = []
    for index, row in enumerate(data):
        original = str(row.get("desc") or "")
        if not original:
            if progress:
                progress(index + 1, max(1, len(data) * 2))
            continue
        selected = str(row.get("_description_source_block") or "")
        if not selected:
            selected = select_relevant_description_source(
                str(row.get("title") or ""),
                original,
            )
        row["_description_source_block"] = selected
        if selected:
            row["desc"] = selected
            if selected != original:
                _add_audit(
                    row,
                    "描述清洗",
                    "description",
                    original,
                    selected,
                    method="rule",
                    reason="select_title_relevant_product_block",
                    action="已删除交叉销售、店铺和交易模板",
                )
            items.append((index, selected))
        else:
            fallback, compacted = build_rule_description(
                str(row.get("title") or ""),
                "",
                site=str(row.get("site") or "US"),
            )
            row["desc"] = fallback
            _add_quality_issue(
                row,
                "description_relevance_warning",
                "源描述未找到可靠产品块，已仅按标题生成保守描述",
            )
            if compacted:
                _add_quality_issue(
                    row,
                    "description_compacted",
                    "描述超过 500 字符，已按硬规格优先压缩",
                )
        if progress:
            progress(index + 1, max(1, len(data) * 2))

    def clean_one(index: int, source: str):
        title = str(data[index].get("title") or "")
        last_missing: list[str] = []
        for attempt in range(2):
            strict_instruction = ""
            if attempt:
                strict_instruction = (
                    "The previous result was rejected. Use only facts that "
                    "match the title. Remove every unrelated product, price, "
                    "store, payment, shipping, return, and feedback phrase."
                )
            try:
                provider = (
                    provider_getter or _default_get_provider
                )()
                raw = provider.call_text(
                    _prompts.render(
                        "amazon.description_clean",
                        title=title,
                        description=source,
                        strict_instruction=strict_instruction,
                        **market_prompt_values(data[index]),
                    ),
                    max_tokens=3000,
                )
            except QuotaExhaustedError:
                raise
            except Exception as exc:
                _log.warn(
                    "Amazon描述AI清洗异常",
                    row=index,
                    attempt=attempt + 1,
                    error=str(exc),
                )
                continue
            parsed = parse_structured_description(raw)
            if not parsed:
                continue
            candidate, compacted = format_description(
                parsed[0],
                parsed[1],
                site=str(data[index].get("site") or "US"),
            )
            valid, missing = _valid_description(
                title,
                source,
                candidate,
            )
            last_missing = missing
            if valid:
                return index, candidate, compacted, "ai", []
        fallback, compacted = build_rule_description(
            title,
            source,
            site=str(data[index].get("site") or "US"),
        )
        return index, fallback, compacted, "fallback", last_missing

    done = 0
    with ThreadPoolExecutor(
        max_workers=min(
            AMAZON_DESC_CONCURRENCY,
            max(1, len(items)),
        )
    ) as pool:
        futures = {
            pool.submit(clean_one, index, source): index
            for index, source in items
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                (
                    _index,
                    cleaned,
                    compacted,
                    method,
                    missing,
                ) = future.result()
            except QuotaExhaustedError:
                for pending in futures:
                    pending.cancel()
                raise
            except Exception as exc:
                source = str(
                    data[index].get("_description_source_block") or ""
                )
                cleaned, compacted = build_rule_description(
                    str(data[index].get("title") or ""),
                    source,
                    site=str(data[index].get("site") or "US"),
                )
                method = "fallback"
                missing = []
                _log.warn(
                    "Amazon描述AI清洗异常",
                    row=index,
                    error=str(exc)[:100],
                )
            before = str(data[index].get("desc") or "")
            data[index]["desc"] = cleaned
            _add_audit(
                data[index],
                "描述清洗",
                "description",
                before,
                cleaned,
                method=method,
                reason=(
                    "model_structured_description"
                    if method == "ai"
                    else "description_relevance_fallback"
                ),
                severity="warning" if method != "ai" else "info",
                action="确认简介、规格分段和事实准确性",
            )
            if method != "ai":
                _add_quality_issue(
                    data[index],
                    "description_relevance_warning",
                    "模型结果不相关或格式不合格，已使用标题和源规格回退",
                )
            if missing:
                _add_quality_issue(
                    data[index],
                    "description_fact_loss",
                    "模型描述缺少关键规格，已拒绝并使用规则回退",
                )
            if compacted:
                _add_quality_issue(
                    data[index],
                    "description_compacted",
                    "描述超过 500 字符，已按硬规格优先压缩",
                )
            done += 1
            if progress:
                progress(
                    len(data) + done,
                    max(1, len(data) * 2),
                )
    if progress:
        progress(1, 1)
    return data


def enforce_description_safety(data):
    """Retain rows while replacing any unsafe generated description."""
    for row in data:
        description = str(row.get("desc") or "")
        title = str(row.get("title") or "")
        if not description:
            continue
        if (
            len(description) <= DESCRIPTION_MAX_CHARS
            and not _has_cross_sell_contamination(description)
            and _is_text_relevant(title, description)
        ):
            continue
        replacement, _compacted = build_rule_description(
            title,
            str(row.get("_description_source_block") or ""),
            site=str(row.get("site") or "US"),
        )
        row["desc"] = replacement
        _add_quality_issue(
            row,
            "description_relevance_warning",
            "最终描述相关性检查未通过，已使用安全规则描述替换",
        )
    return data


__all__ = [
    "DESCRIPTION_MAX_CHARS",
    "build_rule_description",
    "clean_descriptions",
    "enforce_description_safety",
    "format_description",
    "parse_structured_description",
    "partition_product_description_rows",
    "select_relevant_description_source",
]
