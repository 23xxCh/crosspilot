"""Effective provider configuration and thread-safe singleton factory."""
from __future__ import annotations

import threading
from typing import Any, Optional

from ..config.credentials import CredentialStore
from ..config.models import get_model_registry
from ..config.prompts import reload_prompt_registry

from .composite import CompositeProvider


def load_provider_config() -> dict[str, Any]:
    """Map unified settings to the provider construction shape."""
    registry = get_model_registry()
    credentials = CredentialStore(registry)
    config: dict[str, Any] = {
        "routes": {
            operation: [
                {
                    **target.as_dict(),
                    "api_key": credentials.value(target.credential),
                }
                for target in registry.routes(operation)
            ]
            for operation in ("text", "vision", "image")
        }
    }
    from ..config.env import load_config

    cfg = load_config()
    mapping = {
            "AGNES_503_RETRY_LIMIT": "agnes_503_retry_limit",
            "AGNES_503_BACKOFF_MIN_S": (
                "agnes_503_backoff_min_s"
            ),
            "AGNES_503_BACKOFF_MAX_S": (
                "agnes_503_backoff_max_s"
            ),
            "AGNES_503_CIRCUIT_THRESHOLD": (
                "agnes_503_circuit_threshold"
            ),
            "AGNES_503_CIRCUIT_COOLDOWN_S": (
                "agnes_503_circuit_cooldown_s"
            ),
            "CIRCUIT_FAILURE_THRESHOLD": (
                "circuit_failure_threshold"
            ),
            "CIRCUIT_COOLDOWN_S": "circuit_cooldown_s",
    }
    for source, target in mapping.items():
        if cfg.get(source):
            config[target] = cfg[source]
    return config


_KEYS = load_provider_config()
_provider: Optional[CompositeProvider] = None
_provider_lock = threading.Lock()


def reload_keys() -> dict[str, Any]:
    global _KEYS
    _KEYS = load_provider_config()
    return _KEYS


def get_provider() -> CompositeProvider:
    global _provider
    with _provider_lock:
        if _provider is None:
            missing = [
                f"{operation}:{route.get('credential')}"
                for operation, routes in (_KEYS.get("routes") or {}).items()
                for route in routes
                if not route.get("api_key")
            ]
            if missing:
                raise ValueError(
                    "以下模型线路未配置 API 密钥: "
                    + ", ".join(missing)
                    + "。请双击 03_配置管理.bat。"
                )
            _provider = CompositeProvider(_KEYS)
        return _provider


def reload_provider() -> None:
    global _provider
    try:
        from ..config.env import reload_config

        reload_config()
    except ImportError:
        pass
    reload_prompt_registry()
    with _provider_lock:
        _provider = None
        reload_keys()
