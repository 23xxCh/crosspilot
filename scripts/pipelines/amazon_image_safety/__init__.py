"""Stable interface for the fail-closed Amazon image safety gate.

Callers should depend on this package interface rather than its internal
assessment, cache, remediation, or orchestration modules.
"""
from .assessment import (
    safe_assess,
    validate_image_url,
)
from .cache import (
    current_cache_versions,
    is_current_assessment,
    load_cache,
    save_cache,
)
from .gate import run_structured_image_safety_gate


# Transitional aliases keep existing scripts and third-party imports working.
_is_current_assessment = is_current_assessment
_load_cache = load_cache
_save_cache = save_cache
_safe_assess = safe_assess
_validate_image_url = validate_image_url


__all__ = [
    "current_cache_versions",
    "is_current_assessment",
    "load_cache",
    "run_structured_image_safety_gate",
    "safe_assess",
    "save_cache",
    "validate_image_url",
]
