"""Stable facade for the modular provider implementation."""
from __future__ import annotations

from .providers import (
    AgnesProvider,
    CompositeProvider,
    DeepSeekProvider,
    GPTImageProvider,
    ModelProvider,
    ProviderAuthError,
    ProviderError,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    classify_http_error,
    get_provider,
    is_quota_error,
    load_provider_config,
    reload_keys as _reload_keys_impl,
    reload_provider as _reload_provider_impl,
)
from .providers import factory as _factory


_load_keys = load_provider_config
_is_quota_error = is_quota_error
_KEYS = _factory._KEYS


def reload_keys():
    """Compatibility wrapper that refreshes the facade snapshot."""
    global _KEYS
    _KEYS = _reload_keys_impl()
    return _KEYS


def reload_provider():
    """Reload effective config, Prompt files, and provider singleton."""
    global _KEYS
    _reload_provider_impl()
    _KEYS = _factory._KEYS


__all__ = [
    "ModelProvider",
    "DeepSeekProvider",
    "AgnesProvider",
    "GPTImageProvider",
    "CompositeProvider",
    "ProviderError",
    "ProviderAuthError",
    "ProviderQuotaError",
    "ProviderRateLimitError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "ProviderResponseError",
    "classify_http_error",
    "is_quota_error",
    "get_provider",
    "reload_keys",
    "reload_provider",
]
