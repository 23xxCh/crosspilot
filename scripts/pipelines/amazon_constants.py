"""Compatibility Adapter for the Amazon quality-policy package."""
from __future__ import annotations

from crosspilot.prompt_registry import get_prompt_registry
from scripts.pipelines import amazon_quality as _quality


AMAZON_TITLE_CONCURRENCY = _quality.AMAZON_TITLE_CONCURRENCY
AMAZON_DESC_CONCURRENCY = _quality.AMAZON_DESC_CONCURRENCY
AMAZON_BULLET_CONCURRENCY = _quality.AMAZON_BULLET_CONCURRENCY
AMAZON_REVIEW_CONCURRENCY = _quality.AMAZON_REVIEW_CONCURRENCY
AMAZON_IMAGE_GEN_CONCURRENCY = (
    _quality.AMAZON_IMAGE_GEN_CONCURRENCY
)
AMAZON_IMAGE_GEN_ATTEMPTS = _quality.AMAZON_IMAGE_GEN_ATTEMPTS

_BRAND_RE = _quality.BRAND_RE
_OEM_RE = _quality.OEM_RE
_LOGISTICS_RE = _quality.LOGISTICS_RE
_RETURN_RE = _quality.RETURN_RE
_IMG_RE = _quality.IMG_RE
_ROW_QUALITY_LABELS = _quality.ROW_QUALITY_LABELS
_META_TEXT_RE = _quality.META_TEXT_RE
_STOP_TERMS = _quality.STOP_TERMS
_GENERIC_TERMS = _quality.GENERIC_TERMS
_FACT_RE = _quality.FACT_RE
_MAX_ROW_AUDIT_ITEMS = _quality.MAX_ROW_AUDIT_ITEMS
_MAX_VALIDATION_AUDIT_ITEMS = (
    _quality.MAX_VALIDATION_AUDIT_ITEMS
)
_ISSUE_AUDIT_META = _quality.ISSUE_AUDIT_META

_plain_text = _quality.plain_text
_audit_text = _quality.audit_text
_add_audit = _quality.add_audit
_add_quality_issue = _quality.add_quality_issue
_summarize_audit_trail = _quality.summarize_audit_trail
_attach_audit_to_validation = _quality.attach_audit_to_validation
_summarize_row_quality_issues = (
    _quality.summarize_row_quality_issues
)
_normalize_fact_marker = _quality.normalize_fact_marker
_extract_factual_markers = _quality.extract_factual_markers
_missing_factual_markers = _quality.missing_factual_markers
_unexpected_brand_markers = _quality.unexpected_brand_markers
_trim_words = _quality.trim_words
_fingerprint_text = _quality.fingerprint_text
_term_tokens = _quality.term_tokens
_meaningful_tokens = _quality.meaningful_tokens
_is_weak_bullet = _quality.is_weak_bullet
_clean_bullet_text = _quality.clean_bullet_text
_normalize_bullets_for_row = _quality.normalize_bullets_for_row
_split_keywords = _quality.split_keywords
_clean_keyword_term = _quality.clean_keyword_term
_is_weak_keyword = _quality.is_weak_keyword
_dedupe_terms = _quality.dedupe_terms
_keyword_candidates_from_source = (
    _quality.keyword_candidates_from_source
)
_join_keywords = _quality.join_keywords
_normalize_keywords_for_row = _quality.normalize_keywords_for_row
_validate_amazon_rows = _quality.validate_amazon_rows

_prompts = get_prompt_registry()
DESC_CLEAN_PROMPT = _prompts.get("amazon.description_clean")
TITLE_OPTIMIZE_PROMPT = _prompts.get("amazon.title_optimize")
BULLET_KEYWORD_PROMPT = _prompts.get("amazon.bullet_keywords")


__all__ = [
    "AMAZON_TITLE_CONCURRENCY",
    "AMAZON_DESC_CONCURRENCY",
    "AMAZON_BULLET_CONCURRENCY",
    "AMAZON_REVIEW_CONCURRENCY",
    "AMAZON_IMAGE_GEN_CONCURRENCY",
    "AMAZON_IMAGE_GEN_ATTEMPTS",
    "_BRAND_RE",
    "_OEM_RE",
    "_LOGISTICS_RE",
    "_RETURN_RE",
    "_IMG_RE",
    "_ROW_QUALITY_LABELS",
    "_META_TEXT_RE",
    "_STOP_TERMS",
    "_GENERIC_TERMS",
    "_FACT_RE",
    "_plain_text",
    "_MAX_ROW_AUDIT_ITEMS",
    "_MAX_VALIDATION_AUDIT_ITEMS",
    "_audit_text",
    "_add_audit",
    "_ISSUE_AUDIT_META",
    "_add_quality_issue",
    "_summarize_audit_trail",
    "_attach_audit_to_validation",
    "_summarize_row_quality_issues",
    "_normalize_fact_marker",
    "_extract_factual_markers",
    "_missing_factual_markers",
    "_unexpected_brand_markers",
    "_trim_words",
    "_fingerprint_text",
    "_term_tokens",
    "_meaningful_tokens",
    "_is_weak_bullet",
    "_clean_bullet_text",
    "_normalize_bullets_for_row",
    "_split_keywords",
    "_clean_keyword_term",
    "_is_weak_keyword",
    "_dedupe_terms",
    "_keyword_candidates_from_source",
    "_join_keywords",
    "_normalize_keywords_for_row",
    "_validate_amazon_rows",
    "DESC_CLEAN_PROMPT",
    "TITLE_OPTIMIZE_PROMPT",
    "BULLET_KEYWORD_PROMPT",
]
