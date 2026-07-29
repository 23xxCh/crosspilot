"""Effective provider configuration and thread-safe singleton factory."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

from crosspilot.prompt_registry import reload_prompt_registry

from .composite import CompositeProvider


_ROOT = Path(__file__).resolve().parents[2]


def load_provider_config() -> dict[str, Any]:
    """Map effective CrossPilot config to the legacy provider config shape."""
    config: dict[str, Any] = {}
    try:
        from crosspilot.config import load_config

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
        }
        for source, target in mapping.items():
            if cfg.get(source):
                config[target] = cfg[source]
        if config:
            return config
    except ImportError:
        pass

    keys_path = Path(
        os.environ.get(
            "CROSSPILOT_KEYS_PATH",
            str(_ROOT / "keys.json"),
        )
    )
    try:
        loaded = json.loads(keys_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            config.update(loaded)
    except (OSError, json.JSONDecodeError):
        pass

    env_overrides = {
        "text_provider": "CROSSPILOT_TEXT_PROVIDER",
        "vision_provider": "CROSSPILOT_VISION_PROVIDER",
        "image_gen_provider": "CROSSPILOT_IMAGE_GEN_PROVIDER",
        "deepseek_key": "CROSSPILOT_DEEPSEEK_KEY",
        "agnes_key": "CROSSPILOT_AGNES_KEY",
        "gpt_image_key": "CROSSPILOT_GPT_IMAGE_KEY",
    }
    for field, env_name in env_overrides.items():
        value = os.environ.get(env_name)
        if value:
            config[field] = value
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
                    "未配置 API 密钥。请在 Web 设置页保存，"
                    "或复制 .env.example 为 .env 后填写 "
                    "DEEPSEEK_KEY / AGNES_KEY。"
                )
            _provider = CompositeProvider(_KEYS)
        return _provider


def reload_provider() -> None:
    global _provider
    try:
        from crosspilot.config import reload_config

        reload_config()
    except ImportError:
        pass
    reload_prompt_registry()
    with _provider_lock:
        _provider = None
        reload_keys()
