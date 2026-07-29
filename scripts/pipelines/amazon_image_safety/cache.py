"""Versioned cache and manual-review overrides for image safety."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from typing import Any

from crosspilot.image_risk import (
    IMAGE_RISK_POLICY_VERSION,
    IMAGE_RISK_SCHEMA_VERSION,
    normalize_image_assessment,
)
from crosspilot.prompt_registry import build_runtime_signature
from ...pipeline_log import log as _log
from ...services.constants import IMAGE_POLICY_VERSION


_CACHE_LOCK = threading.Lock()


def current_cache_versions() -> tuple[str, str]:
    """Return signatures that invalidate stale review and generation data."""
    review_version = build_runtime_signature(
        f"{IMAGE_POLICY_VERSION}:{IMAGE_RISK_POLICY_VERSION}",
        "images.risk_assessment",
        "images.risk_confirmation",
    )
    generation_version = build_runtime_signature(
        f"{IMAGE_POLICY_VERSION}:{IMAGE_RISK_POLICY_VERSION}",
        "images.main_product",
        "images.variant",
    )
    return review_version, generation_version


def is_current_assessment(value: object) -> bool:
    """Return whether a cached assessment matches the current schema."""
    return bool(
        isinstance(value, dict)
        and value.get("schema_version") == IMAGE_RISK_SCHEMA_VERSION
        and value.get("policy_version") == IMAGE_RISK_POLICY_VERSION
        and normalize_image_assessment(value) is not None
    )


def load_cache(
    cache_path: str | None,
    review_version: str,
    generation_version: str,
) -> dict[str, Any]:
    """Load compatible cache sections and invalidate stale policy results."""
    raw: dict[str, Any] = {}
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as handle:
                raw = json.load(handle) or {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            _log.warn("结构化图审缓存读取失败", error=str(exc)[:100])
    assessments = {}
    confirmations = {}
    if raw.get("risk_prompt_version") == review_version:
        assessments = {
            str(url): value
            for url, value in (raw.get("risk_assessments") or {}).items()
            if is_current_assessment(value)
        }
        confirmations = {
            str(url): value
            for url, value in (raw.get("risk_confirmations") or {}).items()
            if is_current_assessment(value)
        }
    elif raw:
        print("图片风险策略已更新，旧 YES/NO 图审缓存已强制失效", flush=True)
    generation_is_current = raw.get("gen_prompt_version") == generation_version
    return {
        "risk_prompt_version": review_version,
        "gen_prompt_version": generation_version,
        "image_policy_version": IMAGE_POLICY_VERSION,
        "image_risk_policy_version": IMAGE_RISK_POLICY_VERSION,
        "risk_assessments": assessments,
        "risk_confirmations": confirmations,
        "gen_results": (
            dict(raw.get("gen_results") or {}) if generation_is_current else {}
        ),
        "gen_meta": (
            dict(raw.get("gen_meta") or {}) if generation_is_current else {}
        ),
        "gen_failures": (
            dict(raw.get("gen_failures") or {}) if generation_is_current else {}
        ),
    }


def save_cache(cache_path: str | None, cache: dict[str, Any]) -> None:
    """Atomically persist the image safety cache."""
    if not cache_path:
        return
    directory = os.path.dirname(os.path.abspath(cache_path))
    os.makedirs(directory, exist_ok=True)
    with _CACHE_LOCK:
        snapshot = {
            "risk_prompt_version": cache["risk_prompt_version"],
            "gen_prompt_version": cache["gen_prompt_version"],
            "image_policy_version": cache["image_policy_version"],
            "image_risk_policy_version": cache["image_risk_policy_version"],
            "risk_assessments": dict(cache["risk_assessments"]),
            "risk_confirmations": dict(cache["risk_confirmations"]),
            "gen_results": dict(cache["gen_results"]),
            "gen_meta": dict(cache["gen_meta"]),
            "gen_failures": dict(cache["gen_failures"]),
        }
        temp_path = f"{cache_path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, cache_path)
        finally:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass


def override_candidates(cache_path: str | None) -> list[Path]:
    """Locate optional human-review override files."""
    candidates = []
    configured = str(
        os.environ.get("CROSSPILOT_IMAGE_REVIEW_OVERRIDES") or ""
    ).strip()
    if configured:
        candidates.append(Path(configured))
    if cache_path:
        cache = Path(cache_path).resolve()
        for parent in cache.parents:
            if parent.name == "亚马逊表":
                candidates.append(parent / "检查图片文字" / "图片人工覆盖.json")
                break
    return candidates


def load_manual_overrides(
    cache_path: str | None,
) -> dict[tuple[str, str, str], dict]:
    """Load explicit false-positive decisions keyed by product, role and URL."""
    overrides = {}
    for path in override_candidates(cache_path):
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            _log.warn("图片人工覆盖读取失败", error=str(exc)[:100])
            continue
        for item in value.get("overrides") or []:
            if isinstance(item, dict) and item.get("action") == "false_positive":
                key = (
                    str(item.get("product_id") or ""),
                    str(item.get("role") or ""),
                    str(item.get("image_url") or ""),
                )
                if all(key):
                    overrides[key] = item
    return overrides


def manual_safe_assessment(override: dict) -> dict[str, Any]:
    """Convert a recorded human false-positive decision into a safe result."""
    result = normalize_image_assessment(
        {
            "status": "safe",
            "reasons": [],
            "placement": "none",
            "detected_text": [],
            "confidence": 1.0,
            "evidence": (
                "人工终审标记模型误判"
                + (
                    f"：{str(override.get('note') or '')[:300]}"
                    if override.get("note")
                    else ""
                )
            ),
        }
    )
    result["manual_override"] = True
    result["override_recorded_at"] = str(override.get("recorded_at") or "")
    return result


__all__ = [
    "current_cache_versions",
    "is_current_assessment",
    "load_cache",
    "load_manual_overrides",
    "manual_safe_assessment",
    "save_cache",
]
