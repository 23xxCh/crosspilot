"""Final multi-market validation, repair, and resumable text cache."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Callable

from ..config.prompts import build_runtime_signature, get_prompt_registry
from ..markets import market_signature
from ..policy import enforce_prohibited_listing_terms
from ..providers import (
    ProviderAuthError,
    ProviderQuotaError,
    get_provider as _default_get_provider,
)
from ..quality import (
    AMAZON_BULLET_CONCURRENCY,
    missing_factual_markers,
    split_keywords,
    unexpected_brand_markers,
)
from .locale import (
    description_label,
    localization_violations,
    market_prompt_values,
    normalize_localized_listing_fields,
    normalize_localized_title,
    sanitize_localized_subtitle,
)


LOCALIZATION_POLICY_VERSION = "multi-market-localization-v2"
TEXT_FIELDS = ("title", "subtitle", "desc", "bullets", "keywords")
_prompts = get_prompt_registry()
_cache_lock = threading.Lock()


class LocalizationValidationError(ValueError):
    """A model responded, but the listing still violates release rules."""


def _signature() -> str:
    runtime = build_runtime_signature(
        LOCALIZATION_POLICY_VERSION,
        "amazon.title_optimize",
        "amazon.description_clean",
        "amazon.bullet_keywords",
        "amazon.localization_repair",
    )
    return f"{runtime}-{market_signature()}"


def _row_key(row: dict) -> str:
    payload = {
        "id": str(row.get("id") or ""),
        "site": str(row.get("site") or "US"),
        "title": str(row.get("_source_title") or row.get("title") or ""),
        "description": str(row.get("_source_desc") or row.get("desc") or ""),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class LocalizationCache:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.signature = _signature()
        self.entries: dict[str, dict] = {}
        self.hits = 0
        self.writes = 0
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict) or raw.get("signature") != self.signature:
            return
        entries = raw.get("entries")
        if isinstance(entries, dict):
            self.entries = {
                str(key): value
                for key, value in entries.items()
                if isinstance(value, dict)
            }

    def restore(self, row: dict) -> bool:
        value = self.entries.get(_row_key(row))
        if not isinstance(value, dict):
            return False
        legacy = "fields" not in value
        fields = value if legacy else value.get("fields")
        if not isinstance(fields, dict):
            return False
        restored_fields = []
        for field in TEXT_FIELDS:
            if field in fields:
                row[field] = fields.get(field)
                restored_fields.append(field)
        row["_localization_partial_fields"] = restored_fields
        if not (legacy or value.get("complete") is True):
            return False
        if _all_violations(row):
            return False
        row["_localization_cache_hit"] = True
        self.hits += 1
        return True

    def store(self, row: dict) -> None:
        if _all_violations(row):
            raise LocalizationValidationError("拒绝缓存未通过本地化校验的文案")
        self.entries[_row_key(row)] = {
            "complete": True,
            "fields": {
                field: row.get(field)
                for field in TEXT_FIELDS
            },
        }
        self.writes += 1
        self._save()

    def store_partial(self, row: dict, fields: tuple[str, ...]) -> bool:
        selected = {field: row.get(field) for field in fields}
        if "title" in selected:
            title = str(selected.get("title") or "").strip()
            if not title or len(title) > 75:
                return False
        if "desc" in selected:
            description = str(selected.get("desc") or "").strip()
            if not description or len(description) > 500:
                return False
        if "subtitle" in selected and len(str(selected.get("subtitle") or "")) > 125:
            return False
        if "bullets" in selected:
            bullets = list(selected.get("bullets") or [])
            if len(bullets) != 5 or any(not str(item or "").strip() for item in bullets):
                return False
        if "keywords" in selected and len(split_keywords(selected.get("keywords") or "")) != 10:
            return False
        key = _row_key(row)
        current = self.entries.get(key) or {}
        current_fields = (
            dict(current.get("fields") or {})
            if "fields" in current
            else {
                field: current.get(field)
                for field in TEXT_FIELDS
                if field in current
            }
        )
        current_fields.update(selected)
        self.entries[key] = {
            "complete": False,
            "fields": current_fields,
        }
        self.writes += 1
        self._save()
        return True

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "version": 2,
            "signature": self.signature,
            "entries": self.entries,
        }
        temporary = self.path.with_suffix(
            self.path.suffix
            + f".{os.getpid()}.{threading.get_ident()}.tmp"
        )
        with _cache_lock:
            try:
                with temporary.open("w", encoding="utf-8") as handle:
                    json.dump(snapshot, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)


def _parse_object(raw: object) -> dict | None:
    text = str(raw or "").strip()
    if not text:
        return None
    candidates = [text]
    match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if match:
        candidates.append(match.group(1))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _candidate_payload(row: dict) -> dict:
    return {
        "title": str(row.get("title") or ""),
        "subtitle": str(row.get("subtitle") or ""),
        "description": str(row.get("desc") or ""),
        "bullets": list(row.get("bullets") or []),
        "keywords": str(row.get("keywords") or ""),
    }


def _compact_localized_description(value: object, limit: int = 500) -> str:
    lines = [
        re.sub(r"[^\S\r\n]+", " ", line).strip()
        for line in str(value or "").replace("\r", "").split("\n")
    ]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= limit:
        return text
    intro = next((line for line in lines if line), "")
    details = [line for line in lines if line and line != intro]
    kept = intro[: min(180, limit)].rstrip(" ,;:-")
    for line in details:
        separator = "\n\n" if kept and "\n" not in kept else "\n"
        remaining = limit - len(kept) - len(separator)
        if remaining <= 0:
            break
        fragment = line[:remaining].rstrip(" ,;:-")
        if fragment:
            kept += separator + fragment
    return kept[:limit].rstrip(" ,;:-")


def _localized_phrase_candidates(candidate: dict) -> list[str]:
    text = "\n".join([
        str(candidate.get("subtitle") or ""),
        str(candidate.get("desc") or ""),
        str(candidate.get("title") or ""),
    ])
    phrases = [
        part.strip(" \t\r\n,;:-")
        for part in re.split(r"[\n.!?。！？]+", text)
        if part.strip(" \t\r\n,;:-")
    ]
    words = re.findall(r"[^\W_]+(?:[-'][^\W_]+)*|\d+(?:[.,]\d+)?", text)
    for width in (3, 4, 2):
        for start in range(0, max(0, len(words) - width + 1), width):
            phrases.append(" ".join(words[start:start + width]))
    return list(dict.fromkeys(phrases))


def _complete_localized_shapes(candidate: dict) -> dict:
    normalize_localized_listing_fields(candidate)
    bullets = [item for item in candidate.get("bullets") or [] if item]
    for phrase in _localized_phrase_candidates(candidate):
        if len(bullets) >= 5:
            break
        trial = dict(candidate)
        trial["bullets"] = [*bullets, phrase]
        normalize_localized_listing_fields(trial)
        normalized = [item for item in trial["bullets"] if item]
        if len(normalized) > len(bullets):
            bullets = normalized
    candidate["bullets"] = bullets[:5]

    def short_keyword(value: object) -> str:
        words = re.findall(
            r"[^\W_]+(?:[-'][^\W_]+)*|\d+(?:[.,]\d+)?",
            str(value or ""),
        )
        return " ".join(words[:3])[:22].strip(" ,;:-")

    terms = [
        short_keyword(term)
        for term in split_keywords(candidate.get("keywords", ""))
    ]
    candidate["keywords"] = ", ".join(term for term in terms if term)
    normalize_localized_listing_fields(candidate)
    terms = split_keywords(candidate.get("keywords", ""))
    text = " ".join([
        str(candidate.get("title") or ""),
        str(candidate.get("subtitle") or ""),
        str(candidate.get("desc") or ""),
        *(str(item or "") for item in candidate.get("bullets") or []),
    ])
    words = re.findall(
        r"[^\W_]+(?:[-'][^\W_]+)*|\d+(?:[.,]\d+)?",
        text,
    )
    additions = [
        *_localized_phrase_candidates(candidate),
        *(" ".join(words[start:start + 2]) for start in range(len(words))),
        *words,
    ]
    for phrase in additions:
        if len(terms) >= 10:
            break
        short = short_keyword(phrase)
        if not short:
            continue
        trial = dict(candidate)
        trial["keywords"] = ", ".join([*terms, short])
        normalize_localized_listing_fields(trial)
        normalized = split_keywords(trial.get("keywords", ""))
        if len(normalized) > len(terms):
            terms = normalized
    candidate["keywords"] = ", ".join(terms[:10])
    return candidate


def _selected_source(row: dict) -> str:
    return " ".join([
        str(row.get("_source_title") or ""),
        str(
            row.get("_description_source_block")
            or row.get("_source_desc")
            or ""
        ),
    ])


def _append_missing_facts(row: dict, candidate: dict) -> None:
    combined = " ".join([
        str(candidate.get("title") or ""),
        str(candidate.get("subtitle") or ""),
        str(candidate.get("desc") or ""),
        *(str(item or "") for item in candidate.get("bullets") or []),
        str(candidate.get("keywords") or ""),
    ])
    missing = [
        value
        for value in missing_factual_markers(_selected_source(row), combined)
        if not re.fullmatch(r"\d+(?:st|nd|rd|th)", value, re.IGNORECASE)
    ]
    if not missing:
        return
    line = (
        f"{description_label('Specifications', row.get('site') or 'US')}: "
        + ", ".join(missing)
    )
    budget = max(80, 500 - len(line) - 1)
    description = _compact_localized_description(candidate.get("desc"), budget)
    candidate["desc"] = f"{description}\n{line}".strip()[:500]


def _apply_candidate(row: dict, value: dict) -> dict:
    candidate = dict(row)
    candidate["title"] = normalize_localized_title(
        value.get("title"),
        row.get("site") or "US",
    )
    candidate["subtitle"] = sanitize_localized_subtitle(
        value.get("subtitle"),
        row.get("site") or "US",
    )
    candidate["desc"] = _compact_localized_description(
        value.get("description")
    )
    candidate["bullets"] = [
        str(item or "").strip()
        for item in (value.get("bullets") or [])
    ][:5]
    candidate["keywords"] = str(value.get("keywords") or "").strip()
    enforce_prohibited_listing_terms([candidate])
    _complete_localized_shapes(candidate)
    _append_missing_facts(row, candidate)
    return candidate


def _fact_violations(row: dict) -> list[str]:
    source = _selected_source(row)
    candidate = " ".join([
        str(row.get("title") or ""),
        str(row.get("subtitle") or ""),
        str(row.get("desc") or ""),
        *(str(item or "") for item in row.get("bullets") or []),
        str(row.get("keywords") or ""),
    ])
    candidate_numbers = set(re.findall(r"\d+(?:[.,]\d+)?", candidate))
    missing = []
    for value in missing_factual_markers(source, candidate):
        if re.fullmatch(r"\d+(?:st|nd|rd|th)", value, re.IGNORECASE):
            continue
        quantity = re.fullmatch(
            r"(\d+)\s*(?:x|pcs?|pieces?|packs?)",
            value,
            re.IGNORECASE,
        )
        if quantity and quantity.group(1) in candidate_numbers:
            continue
        missing.append(value)
    violations = [f"missing_fact:{value}" for value in missing]
    violations.extend(
        f"unexpected_brand:{value}"
        for value in unexpected_brand_markers(source, candidate)
    )
    return violations


def _all_violations(row: dict) -> list[str]:
    return [*localization_violations(row), *_fact_violations(row)]


def _repair_one(
    row: dict,
    provider_getter: Callable[[], object],
    *,
    max_attempts: int = 3,
) -> tuple[dict | None, list[str], Exception | None, int]:
    current = dict(row)
    violations = _all_violations(current)
    if not violations:
        return current, [], None, 0
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            provider = provider_getter()
            raw = provider.call_text(
                _prompts.render(
                    "amazon.localization_repair",
                    source_title=str(row.get("_source_title") or ""),
                    source_description=str(
                        row.get("_description_source_block")
                        or row.get("_source_desc")
                        or ""
                    )[:4000],
                    candidate_json=json.dumps(
                        _candidate_payload(current),
                        ensure_ascii=False,
                    ),
                    violations=", ".join(violations),
                    **market_prompt_values(row),
                ),
                max_tokens=4096,
            )
        except (ProviderAuthError, ProviderQuotaError):
            raise
        except Exception as exc:
            last_error = exc
            continue
        value = _parse_object(raw)
        if value is None:
            violations = ["invalid_json"]
            continue
        current = _apply_candidate(row, value)
        violations = _all_violations(current)
        if not violations:
            return current, [], None, attempt
    return None, violations, last_error, max_attempts


def ensure_localized_rows(
    rows: list[dict],
    *,
    cache: LocalizationCache,
    provider_getter: Callable[[], object] | None = None,
    runtime_metrics: dict | None = None,
    progress=None,
    sleep: Callable[[float], None] = time.sleep,
    retry_delays: tuple[int, ...] = (30, 120, 300, 600),
) -> list[dict]:
    """Repair invalid rows; retry transient provider failures without publishing."""
    provider_getter = provider_getter or _default_get_provider
    metrics = runtime_metrics if isinstance(runtime_metrics, dict) else {}
    stats = metrics.setdefault("localization", {})
    pending = [row for row in rows if _all_violations(row)]
    pending_objects = {id(row) for row in pending}
    for row in rows:
        if id(row) not in pending_objects:
            cache.store(row)
    stats["initial_pending"] = len(pending)
    stats.setdefault("repair_attempts", 0)
    stats.setdefault("transient_retry_rounds", 0)
    delay_index = 0
    while pending:
        next_pending: list[dict] = []
        content_failures: list[tuple[dict, list[str]]] = []
        with ThreadPoolExecutor(
            max_workers=min(AMAZON_BULLET_CONCURRENCY, len(pending))
        ) as pool:
            futures = {
                pool.submit(_repair_one, row, provider_getter): row
                for row in pending
            }
            for future in as_completed(futures):
                row = futures[future]
                repaired, violations, error, attempts = future.result()
                stats["repair_attempts"] += attempts
                if repaired is not None:
                    for field in TEXT_FIELDS:
                        row[field] = repaired.get(field)
                    cache.store(row)
                elif error is not None:
                    next_pending.append(row)
                else:
                    content_failures.append((row, violations))
                if progress:
                    progress(
                        len(rows) - len(next_pending) - len(content_failures),
                        max(1, len(rows)),
                    )
        if content_failures:
            details = "; ".join(
                f"{row.get('id')}[{row.get('site')}]:{','.join(violations)}"
                for row, violations in content_failures[:10]
            )
            raise LocalizationValidationError(
                "多站点文案连续修复后仍不合格，正式表未发布: " + details
            )
        pending = next_pending
        if pending:
            stats["transient_retry_rounds"] += 1
            delay = retry_delays[min(delay_index, len(retry_delays) - 1)]
            delay_index += 1
            stats["last_retry_delay_s"] = delay
            sleep(delay)
    stats["cache_hits"] = cache.hits
    stats["cache_writes"] = cache.writes
    stats["completed"] = len(rows)
    return rows


__all__ = [
    "LOCALIZATION_POLICY_VERSION",
    "LocalizationCache",
    "LocalizationValidationError",
    "ensure_localized_rows",
]
