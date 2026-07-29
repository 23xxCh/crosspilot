"""Compatibility Adapter for the Amazon text-stage package."""
from __future__ import annotations

from scripts.model_provider import ProviderQuotaError
from scripts.pipelines.amazon_text import (
    clamp_title,
    clean_descriptions,
    generate_bullets_keywords,
    normalize_title,
    optimize_titles,
    parse_bullet_json,
    remove_dirty_descriptions,
    source_bullet_candidates,
    valid_bullet_payload,
)


QuotaExhaustedError = ProviderQuotaError
_clamp_title = clamp_title
_normalize_title = normalize_title
_parse_bullet_json = parse_bullet_json
_remove_dirty_descriptions = remove_dirty_descriptions
_source_bullet_candidates = source_bullet_candidates
_stage_clean_descs = clean_descriptions
_stage_generate_bullets_keywords = generate_bullets_keywords
_stage_optimize_titles = optimize_titles
_valid_bullet_payload = valid_bullet_payload


__all__ = [
    "QuotaExhaustedError",
    "_clamp_title",
    "_normalize_title",
    "_parse_bullet_json",
    "_remove_dirty_descriptions",
    "_source_bullet_candidates",
    "_stage_clean_descs",
    "_stage_generate_bullets_keywords",
    "_stage_optimize_titles",
    "_valid_bullet_payload",
]
