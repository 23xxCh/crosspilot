"""Deterministic Amazon Bullet Point and keyword quality policy."""
from __future__ import annotations

import re

from .audit import add_quality_issue
from .rules import (
    BRAND_RE,
    GENERIC_TERMS,
    META_TEXT_RE,
    OEM_RE,
    STOP_TERMS,
    extract_factual_markers,
    fingerprint_text,
    meaningful_tokens,
    plain_text,
    term_tokens,
    trim_words,
)


def is_weak_bullet(text):
    cleaned = plain_text(text)
    if len(cleaned) < 5 or META_TEXT_RE.search(cleaned):
        return True
    return (
        not meaningful_tokens(cleaned)
        and not extract_factual_markers(cleaned)
    )


def clean_bullet_text(text):
    cleaned = plain_text(text)
    cleaned = BRAND_RE.sub("", cleaned)
    cleaned = OEM_RE.sub("", cleaned)
    cleaned = re.sub(r"^[\s\-*•\d.)]+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ;,.-")
    return trim_words(cleaned, 200)


def normalize_bullets_for_row(row):
    raw_bullets = list(row.get("bullets") or [])[:5]
    raw_bullets.extend([""] * (5 - len(raw_bullets)))
    seen = set()
    normalized = []

    for raw in raw_bullets[:5]:
        bullet = clean_bullet_text(str(raw or ""))
        fingerprint = fingerprint_text(bullet)
        if bullet and (
            fingerprint in seen
            or is_weak_bullet(bullet)
        ):
            bullet = ""
        if fingerprint and bullet:
            seen.add(fingerprint)
        normalized.append(bullet)

    row["bullets"] = normalized
    has_real_issue = (
        any(
            not bullet or is_weak_bullet(bullet)
            for bullet in normalized
            if bullet
        )
        or len([
            bullet
            for bullet in normalized
            if bullet
        ]) < 5
    )
    if has_real_issue:
        add_quality_issue(
            row,
            "bullet_quality_warning",
            "Bullet 存在重复、泛词、品牌残留或疑似规格风险，"
            "已清洗后复核",
        )
    return row


def split_keywords(value):
    return [
        part.strip()
        for part in re.split(
            r"[,;，；\n]+",
            str(value or ""),
        )
        if part.strip()
    ]


def clean_keyword_term(term):
    cleaned = plain_text(term).lower()
    cleaned = BRAND_RE.sub("", cleaned)
    cleaned = OEM_RE.sub("", cleaned)
    cleaned = re.sub(r"[^a-z0-9/+\-\s]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -,+/")
    return cleaned


def is_weak_keyword(term):
    if (
        not term
        or len(term) < 2
        or META_TEXT_RE.search(term)
    ):
        return True
    tokens = term_tokens(term)
    if not tokens:
        return True
    return all(
        token in STOP_TERMS or token in GENERIC_TERMS
        for token in tokens
    )


def dedupe_terms(terms):
    result = []
    seen = set()
    for term in terms:
        cleaned = clean_keyword_term(term)
        fingerprint = fingerprint_text(cleaned)
        if (
            not cleaned
            or not fingerprint
            or fingerprint in seen
            or is_weak_keyword(cleaned)
        ):
            continue
        seen.add(fingerprint)
        result.append(cleaned)
    return result


def keyword_candidates_from_source(row):
    text = plain_text(
        f"{row.get('title', '')}. {row.get('desc', '')}"
    )
    words = [
        token.lower()
        for token in re.findall(
            r"[A-Za-z0-9]+(?:/[A-Za-z0-9]+)?",
            text,
        )
        if (
            token.lower() not in STOP_TERMS
            and token.lower() not in GENERIC_TERMS
            and (
                len(token) > 2
                or re.search(r"\d", token)
            )
        )
    ]
    candidates = list(extract_factual_markers(text))
    for size in (2, 3):
        for index in range(
            0,
            max(0, len(words) - size + 1),
        ):
            candidates.append(
                " ".join(words[index:index + size])
            )
    candidates.extend(words)
    return dedupe_terms(candidates)


def join_keywords(terms, limit=250):
    terms = dedupe_terms(terms)

    def greedy(excluded):
        selected = []
        for term in terms:
            if term in excluded:
                continue
            trial = ", ".join(selected + [term])
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
            score = (
                len(trial_selected),
                -len(", ".join(trial_selected)),
            )
            if len(trial_selected) <= len(selected):
                continue
            if best is None or score > best[0]:
                best = (score, term, trial_selected)
        if best is None:
            break
        excluded.add(best[1])
        selected = best[2]

    return ", ".join(selected)


def normalize_keywords_for_row(row):
    original_terms = split_keywords(row.get("keywords", ""))
    cleaned_original = dedupe_terms(original_terms)
    candidates = keyword_candidates_from_source(row)
    terms = dedupe_terms(cleaned_original + candidates)
    normalized = join_keywords(terms)
    row["keywords"] = normalized
    normalized_terms = dedupe_terms(
        split_keywords(normalized)
    )
    if (
        len(normalized_terms) != 10
        or len(normalized) > 250
    ):
        add_quality_issue(
            row,
            "keyword_quality_warning",
            "关键词不足 10 个、重复、过泛或含品牌，"
            "已按源内容补齐/清洗",
        )
    return row


__all__ = [
    "clean_bullet_text",
    "clean_keyword_term",
    "dedupe_terms",
    "is_weak_bullet",
    "is_weak_keyword",
    "join_keywords",
    "keyword_candidates_from_source",
    "normalize_bullets_for_row",
    "normalize_keywords_for_row",
    "split_keywords",
]
