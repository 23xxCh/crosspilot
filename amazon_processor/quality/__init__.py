"""Stable Interface for Amazon listing quality and validation policy."""
from __future__ import annotations

from ..concurrency import configured_concurrency

from .core import (
    ISSUE_AUDIT_META,
    MAX_ROW_AUDIT_ITEMS,
    MAX_VALIDATION_AUDIT_ITEMS,
    ROW_QUALITY_LABELS,
    add_audit,
    add_quality_issue,
    attach_audit_to_validation,
    audit_text,
    summarize_audit_trail,
    summarize_row_quality_issues,
)
from .core import (
    clean_bullet_text,
    clean_keyword_term,
    dedupe_terms,
    is_weak_bullet,
    is_weak_keyword,
    join_keywords,
    keyword_candidates_from_source,
    normalize_bullets_for_row,
    normalize_keywords_for_row,
    split_keywords,
)
from .core import (
    BRAND_RE,
    FACT_RE,
    GENERIC_TERMS,
    IMG_RE,
    LOGISTICS_RE,
    META_TEXT_RE,
    OEM_RE,
    RETURN_RE,
    STOP_TERMS,
    extract_factual_markers,
    fingerprint_text,
    meaningful_tokens,
    missing_factual_markers,
    normalize_fact_marker,
    plain_text,
    term_tokens,
    trim_words,
    unexpected_brand_markers,
)
from .core import validate_amazon_rows


AMAZON_TITLE_CONCURRENCY = configured_concurrency(
    "text",
    10,
    maximum=50,
)
AMAZON_DESC_CONCURRENCY = configured_concurrency(
    "text",
    10,
    maximum=50,
)
AMAZON_BULLET_CONCURRENCY = configured_concurrency(
    "text",
    20,
    maximum=50,
)
AMAZON_REVIEW_CONCURRENCY = configured_concurrency(
    "review",
    100,
    maximum=150,
)
AMAZON_IMAGE_GEN_CONCURRENCY = configured_concurrency(
    "image_gen",
    20,
    maximum=40,
)
AMAZON_IMAGE_GEN_ATTEMPTS = 1


__all__ = [
    "AMAZON_BULLET_CONCURRENCY",
    "AMAZON_DESC_CONCURRENCY",
    "AMAZON_IMAGE_GEN_ATTEMPTS",
    "AMAZON_IMAGE_GEN_CONCURRENCY",
    "AMAZON_REVIEW_CONCURRENCY",
    "AMAZON_TITLE_CONCURRENCY",
    "BRAND_RE",
    "FACT_RE",
    "GENERIC_TERMS",
    "IMG_RE",
    "ISSUE_AUDIT_META",
    "LOGISTICS_RE",
    "MAX_ROW_AUDIT_ITEMS",
    "MAX_VALIDATION_AUDIT_ITEMS",
    "META_TEXT_RE",
    "OEM_RE",
    "RETURN_RE",
    "ROW_QUALITY_LABELS",
    "STOP_TERMS",
    "add_audit",
    "add_quality_issue",
    "attach_audit_to_validation",
    "audit_text",
    "clean_bullet_text",
    "clean_keyword_term",
    "dedupe_terms",
    "extract_factual_markers",
    "fingerprint_text",
    "is_weak_bullet",
    "is_weak_keyword",
    "join_keywords",
    "keyword_candidates_from_source",
    "meaningful_tokens",
    "missing_factual_markers",
    "normalize_bullets_for_row",
    "normalize_fact_marker",
    "normalize_keywords_for_row",
    "plain_text",
    "split_keywords",
    "summarize_audit_trail",
    "summarize_row_quality_issues",
    "term_tokens",
    "trim_words",
    "unexpected_brand_markers",
    "validate_amazon_rows",
]
