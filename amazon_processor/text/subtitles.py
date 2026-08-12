"""Amazon search-result subtitle generation and normalization."""
from __future__ import annotations

import re

from ..quality import (
    BRAND_RE,
    GENERIC_TERMS,
    META_TEXT_RE,
    OEM_RE,
    STOP_TERMS,
    add_audit,
    add_quality_issue,
    audit_text,
    clean_keyword_term,
    fingerprint_text,
    has_cross_sell_contamination,
    plain_text,
    split_keywords,
    term_tokens,
    trim_words,
)
from .locale import (
    market_for_row,
    normalize_localized_title,
    sanitize_localized_subtitle,
)


TITLE_DISPLAY_LIMIT = 75
SUBTITLE_MAX_LENGTH = 125

_FORBIDDEN_RE = re.compile(
    r"\b("
    r"best\s*seller|free\s*shipping|discount|promotion|promo|"
    r"hot\s*sale|limited\s*time|cheap|lowest\s*price|"
    r"top\s*quality|high\s*quality|premium"
    r")\b",
    re.IGNORECASE,
)
_LABEL_RE = re.compile(r"^\s*([A-Za-z][A-Za-z /_-]{1,35})\s*[:：]\s*(.+)$")
_ALLOWED_CHARS_RE = re.compile(r"[^A-Za-z0-9 ,]")
_TRAILING_PUNCT_RE = re.compile(r"[\s,.;:!?]+$")
_LOCALIZED_SPEC_RE = re.compile(r"^\s*([^:：\n]{1,40})\s*[:：]\s*(.+)$")
_LOCALIZED_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_NOISE_WORD_RE = re.compile(
    r"\b("
    r"yes|number|product|applicable|style|type|item|items|"
    r"note|notes|website|picture|pictures|shown|actual|slight|"
    r"difference|screen|setting|payment|shipping|policy"
    r")\b",
    re.IGNORECASE,
)

_LABEL_ALIASES = {
    "material": "material",
    "materials": "material",
    "size": "size",
    "dimensions": "size",
    "dimension": "size",
    "color": "color",
    "colors": "color",
    "colour": "color",
    "colours": "color",
    "compatibility": "compatibility",
    "fitment": "compatibility",
    "application": "compatibility",
    "package": "package",
    "package includes": "package",
    "package included": "package",
    "included": "package",
    "installation": "installation",
    "install": "installation",
    "voltage": "spec",
    "power": "spec",
    "quantity": "package",
}

_PHRASE_STOPWORDS = STOP_TERMS | GENERIC_TERMS | {
    "generic",
    "product",
    "item",
    "items",
    "free",
    "accessory",
    "accessories",
    "car",
    "vehicle",
    "vehicles",
    "auto",
    "automotive",
    "replacement",
}


def _title_tokens(title: str) -> set[str]:
    return {
        token
        for token in term_tokens(title)
        if token not in _PHRASE_STOPWORDS and not token.isdigit()
    }


def _sanitize_phrase(value: str) -> str:
    text = plain_text(value)
    text = BRAND_RE.sub(" ", text)
    text = OEM_RE.sub(" ", text)
    text = text.replace("×", " x ").replace("&", " and ")
    text = re.sub(r"\b\d+\s*-\s*\d+\b", lambda m: m.group(0).replace("-", " to "), text)
    text = _NOISE_WORD_RE.sub(" ", text)
    text = re.sub(r"\bMade\s+from\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bDue\s+to\b.*$", " ", text, flags=re.IGNORECASE)
    text = _ALLOWED_CHARS_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _TRAILING_PUNCT_RE.sub("", text)
    return trim_words(text, 48)


def _label_kind(label: str) -> str:
    normalized = re.sub(r"[^a-z ]+", " ", label.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return _LABEL_ALIASES.get(normalized, "")


def _format_labeled_phrase(kind: str, value: str) -> str:
    first_segment = re.split(
        r"\b(?:Notes?|Package|Color|Compatibility|Fit Type|Application)\b|[;。]",
        str(value or ""),
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    cleaned = _sanitize_phrase(first_segment)
    if not cleaned:
        return ""
    lowered = cleaned.lower()
    if kind == "material" and "material" not in lowered:
        return f"{cleaned} material"
    if kind == "color" and not re.search(r"\bcolor|colors\b", lowered):
        return f"{cleaned} colors"
    if kind == "package" and not re.search(r"\bincluded|includes|pack\b", lowered):
        return f"{cleaned} included"
    if kind == "installation" and "install" not in lowered:
        return f"{cleaned} installation"
    return cleaned


def _keyword_candidates(row: dict) -> list[str]:
    return [
        clean_keyword_term(term)
        for term in split_keywords(row.get("keywords", ""))
    ]


def subtitle_candidates_from_source(row: dict) -> list[str]:
    """Extract subtitle phrase candidates from cleaned product facts."""
    candidates: list[str] = []
    for keyword in _keyword_candidates(row):
        if keyword:
            candidates.append(keyword)

    description = str(row.get("desc") or "")
    for line in description.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        match = _LABEL_RE.match(line)
        if not match:
            continue
        kind = _label_kind(match.group(1))
        if not kind:
            continue
        phrase = _format_labeled_phrase(kind, match.group(2))
        if phrase:
            candidates.append(phrase)

    for bullet in list(row.get("bullets") or [])[:5]:
        bullet_text = _sanitize_phrase(str(bullet or ""))
        if bullet_text and len(bullet_text.split()) <= 6:
            candidates.append(bullet_text)
    return candidates


def _is_valid_phrase(phrase: str, title_tokens: set[str]) -> bool:
    if not phrase or len(phrase) > 55:
        return False
    if _FORBIDDEN_RE.search(phrase) or META_TEXT_RE.search(phrase):
        return False
    if has_cross_sell_contamination(phrase):
        return False
    if _ALLOWED_CHARS_RE.search(phrase):
        return False
    tokens = {
        token
        for token in term_tokens(phrase)
        if token not in _PHRASE_STOPWORDS
    }
    if not tokens:
        return False
    return bool(tokens - title_tokens)


def build_subtitle(title: str, candidates: list[str]) -> str:
    """Build a comma-separated phrase list that obeys Amazon subtitle rules."""
    if len(str(title or "")) >= TITLE_DISPLAY_LIMIT:
        return ""
    title_tokens = _title_tokens(title)
    selected: list[str] = []
    seen = set()
    for raw in candidates:
        phrase = _sanitize_phrase(raw)
        fingerprint = fingerprint_text(phrase)
        if not fingerprint or fingerprint in seen:
            continue
        if not _is_valid_phrase(phrase, title_tokens):
            continue
        trial = ", ".join([*selected, phrase])
        if len(trial) > SUBTITLE_MAX_LENGTH:
            continue
        selected.append(phrase)
        seen.add(fingerprint)
        if len(selected) >= 4:
            break
    return ", ".join(selected)


def _localized_tokens(value: object) -> set[str]:
    return {
        token.casefold()
        for token in _LOCALIZED_WORD_RE.findall(str(value or ""))
        if len(token) > 1 and not token.isdigit()
    }


def _localized_subtitle_candidates(row: dict) -> list[str]:
    """Collect already-localized facts without translating or inventing them."""
    candidates: list[str] = []
    description = str(row.get("desc") or "")
    for line in description.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        match = _LOCALIZED_SPEC_RE.match(line)
        if not match:
            continue
        label = re.sub(r"\s+", " ", match.group(1)).strip()
        value = re.sub(r"\s+", " ", match.group(2)).strip()
        if label and value:
            candidates.append(f"{label} {value}")
    candidates.extend(split_keywords(row.get("keywords") or ""))
    for bullet in list(row.get("bullets") or [])[:5]:
        text = str(bullet or "").strip()
        if text and len(text.split()) <= 7:
            candidates.append(text)
    return candidates


def build_localized_subtitle(row: dict) -> str:
    """Build a localized fallback when the model returned no subtitle."""
    site = row.get("site") or "US"
    title_tokens = _localized_tokens(row.get("title"))
    selected: list[str] = []
    seen: set[str] = set()
    for raw in _localized_subtitle_candidates(row):
        phrase = sanitize_localized_subtitle(raw, site)
        fingerprint = phrase.casefold()
        if (
            not phrase
            or fingerprint in seen
            or len(phrase) > 55
            or _FORBIDDEN_RE.search(phrase)
            or has_cross_sell_contamination(phrase)
        ):
            continue
        phrase_tokens = _localized_tokens(phrase)
        if not phrase_tokens or not (phrase_tokens - title_tokens):
            continue
        trial = ", ".join([*selected, phrase])
        if len(trial) > SUBTITLE_MAX_LENGTH:
            continue
        selected.append(phrase)
        seen.add(fingerprint)
        if len(selected) >= 4:
            break
    return sanitize_localized_subtitle(", ".join(selected), site)


def normalize_subtitle_for_row(row: dict) -> dict:
    """Normalize or rule-fill one row's subtitle."""
    row["title"] = normalize_localized_title(
        row.get("title"),
        row.get("site") or "US",
    )
    title = str(row.get("title") or "").strip()
    before = str(row.get("subtitle") or "")
    market = market_for_row(row)
    if not title or len(title) >= TITLE_DISPLAY_LIMIT:
        row["subtitle"] = ""
    elif not str(row.get("desc") or "").strip():
        row["subtitle"] = ""
    elif market.language_code != "en":
        row["subtitle"] = sanitize_localized_subtitle(
            before,
            row.get("site") or "US",
        )
        if not row["subtitle"]:
            row["subtitle"] = build_localized_subtitle(row)
    else:
        row["subtitle"] = ""
        if before.strip():
            row["subtitle"] = build_subtitle(title, split_keywords(before))
        if not row["subtitle"]:
            row["subtitle"] = build_subtitle(
                title,
                subtitle_candidates_from_source(row),
            )

    after = str(row.get("subtitle") or "")
    if audit_text(before) != audit_text(after):
        add_audit(
            row,
            "副标题",
            "subtitle",
            before,
            after,
            method="rule",
            reason="normalize_subtitle",
            action="确认副标题未重复标题核心词且不含促销词",
        )
    if after and (
        len(after) > SUBTITLE_MAX_LENGTH
        or _FORBIDDEN_RE.search(after)
        or _ALLOWED_CHARS_RE.search(after)
        or has_cross_sell_contamination(after)
    ):
        add_quality_issue(
            row,
            "subtitle_quality_warning",
            "副标题超过 125 字符、含促销词、特殊符号或脏文案，需复核",
        )
    return row


def normalize_subtitles_for_rows(rows: list[dict]) -> list[dict]:
    for row in rows:
        normalize_subtitle_for_row(row)
    return rows


__all__ = [
    "SUBTITLE_MAX_LENGTH",
    "TITLE_DISPLAY_LIMIT",
    "build_localized_subtitle",
    "build_subtitle",
    "normalize_subtitle_for_row",
    "normalize_subtitles_for_rows",
    "subtitle_candidates_from_source",
]
