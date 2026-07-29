"""Amazon cleaning expressions, fact protection, and text fingerprints."""
from __future__ import annotations

import re

from scripts.services.constants import compile_brand_pattern


BRAND_RE = compile_brand_pattern()
OEM_RE = re.compile(
    r"\b(OEM|Original|Factory|原厂|原装|正品|Genuine)\b",
    re.IGNORECASE,
)
LOGISTICS_RE = re.compile(
    r"(交货时间|发货时间|运输方式|快递|物流|Shipping|Delivery|Express|"
    r"Freight|Carrier)[：:].*?(?=\n|$)",
    re.IGNORECASE,
)
RETURN_RE = re.compile(
    r"(退货|退款|Return|Refund|Warranty|保修|Payment|支付).*?(?=\n|$)",
    re.IGNORECASE,
)
IMG_RE = re.compile(r"<img[^>]*>", re.IGNORECASE)

META_TEXT_RE = re.compile(
    r"(as an ai|i cannot|here are|optimized title|cleaned description|"
    r"bullet points|search keywords|json|markdown|rules?:|requirements?:)",
    re.IGNORECASE,
)
STOP_TERMS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "into",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "our",
    "that",
    "the",
    "this",
    "to",
    "with",
    "you",
    "your",
}
GENERIC_TERMS = {
    "best",
    "excellent",
    "generic",
    "good",
    "great",
    "high",
    "hot",
    "item",
    "new",
    "nice",
    "perfect",
    "premium",
    "product",
    "quality",
    "sale",
    "useful",
    "value",
}
FACT_RE = re.compile(
    r"\b(?:19|20)\d{2}\b"
    r"|\b\d+(?:[./-]\d+)?\s*(?:mm|cm|m|inch|inches|in|ft|kg|g|lb|lbs|oz|"
    r"v|volt|volts|w|watt|watts|l|ml|pcs|pc|pack|packs|piece|pieces|"
    r"key|keys|pin|pins)\b"
    r"|\b[A-Z]{1,5}\d{1,5}[A-Z0-9-]*\b"
    r"|\b\d+[A-Z]{1,5}\b",
    re.IGNORECASE,
)


def plain_text(value):
    text = str(value or "")
    text = IMG_RE.sub(" ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_fact_marker(marker):
    marker = re.sub(
        r"\s+",
        " ",
        str(marker or "").strip().lower(),
    )
    marker = re.sub(r"\binches\b", "inch", marker)
    marker = re.sub(r"\bvolts\b", "v", marker)
    marker = re.sub(r"\bvolt\b", "v", marker)
    marker = re.sub(r"\bwatts\b", "w", marker)
    marker = re.sub(r"\bwatt\b", "w", marker)
    marker = re.sub(r"\bpacks\b", "pack", marker)
    marker = re.sub(r"\bpieces\b", "piece", marker)
    marker = re.sub(r"\bpins\b", "pin", marker)
    marker = re.sub(r"\bkeys\b", "key", marker)
    return marker


def extract_factual_markers(text):
    markers = []
    for match in FACT_RE.findall(plain_text(text)):
        marker = normalize_fact_marker(match)
        if marker and marker not in markers:
            markers.append(marker)
    return markers


def missing_factual_markers(source, candidate, limit=8):
    source_markers = extract_factual_markers(source)[:limit]
    if not source_markers:
        return []
    candidate_markers = set(extract_factual_markers(candidate))
    return [
        marker
        for marker in source_markers
        if marker not in candidate_markers
    ]


def unexpected_brand_markers(source, candidate):
    """Return model-added brands that were absent from the source."""
    source_brands = {
        match.group(0).strip().lower()
        for match in BRAND_RE.finditer(plain_text(source))
    }
    candidate_brands = {
        match.group(0).strip().lower()
        for match in BRAND_RE.finditer(plain_text(candidate))
    }
    return sorted(candidate_brands - source_brands)


def trim_words(text, limit):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    shortened = text[:limit].rsplit(" ", 1)[0].strip()
    return shortened or text[:limit].strip()


def fingerprint_text(text):
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def term_tokens(text):
    return [
        token.lower()
        for token in re.findall(
            r"[A-Za-z0-9]+(?:/[A-Za-z0-9]+)?",
            str(text or ""),
        )
    ]


def meaningful_tokens(text):
    return [
        token
        for token in term_tokens(text)
        if token not in STOP_TERMS
        and token not in GENERIC_TERMS
    ]


__all__ = [
    "BRAND_RE",
    "FACT_RE",
    "GENERIC_TERMS",
    "IMG_RE",
    "LOGISTICS_RE",
    "META_TEXT_RE",
    "OEM_RE",
    "RETURN_RE",
    "STOP_TERMS",
    "extract_factual_markers",
    "fingerprint_text",
    "meaningful_tokens",
    "missing_factual_markers",
    "normalize_fact_marker",
    "plain_text",
    "term_tokens",
    "trim_words",
    "unexpected_brand_markers",
]
