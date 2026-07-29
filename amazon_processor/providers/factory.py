"""Effective provider configuration and thread-safe singleton factory."""
from __future__ import annotations

import threading
from typing import Any, Optional

from ..config.prompts import reload_prompt_registry

from .composite import CompositeProvider


def load_provider_config() -> dict[str, Any]:
    """Map unified settings to the provider construction shape."""
    config: dict[str, Any] = {}
    from ..config.env import load_config

    cfg = load_config()
    mapping = {
            "DEEPSEEK_KEY": "deepseek_key",
            "AGNES_KEY": "agnes_key",
            "GPT_IMAGE_KEY": "gpt_image_key",
            "TEXT_PROVIDER": "text_provider",
            "VISION_PROVIDER": "vision_provider",
            "IMAGE_PROVIDER": "image_gen_provider",
            "DEEPSEEK_BASE_URL": "deepseek_base_url",
            "DEEPSEEK_TEXT_MODEL": "deepseek_text_model",
            "DEEPSEEK_TEXT_FALLBACK_MODEL": (
                "deepseek_text_fallback_model"
            ),
            "AGNES_BASE_URL": "agnes_base_url",
            "AGNES_TEXT_BASE_URL": "agnes_text_base_url",
            "AGNES_TEXT_MODEL": "agnes_text_model",
            "AGNES_VISION_BASE_URL": "agnes_vision_base_url",
            "AGNES_VISION_MODEL": "agnes_vision_model",
            "AGNES_IMAGE_BASE_URL": "agnes_image_base_url",
            "AGNES_IMAGE_MODEL": "agnes_image_model",
            "AGNES_IMAGE_FALLBACK_MODEL": (
                "agnes_image_fallback_model"
            ),
            "GPT_IMAGE_BASE_URL": "gpt_image_base_url",
            "GPT_IMAGE_MODEL": "gpt_image_model",
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
            if (
                not _KEYS.get("deepseek_key")
                and not _KEYS.get("agnes_key")
            ):
                raise ValueError(
                    "未配置 API 密钥。请复制 .env.example 为 .env，填写 "
                    "DEEPSEEK_KEY / AGNES_KEY。"
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
