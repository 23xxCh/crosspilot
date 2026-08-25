"""Load secrets from .env and all non-secret settings from settings.json."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from typing import Any

from .credentials import CredentialStore
from .models import get_model_registry, reload_model_registry


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"
_cache: dict[str, str] | None = None
_lock = threading.Lock()


def _read_runtime() -> dict[str, str]:
    try:
        payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"统一配置不存在: {SETTINGS_PATH}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"统一配置无法读取: {exc}") from exc
    runtime = payload.get("runtime") or {}
    if not isinstance(runtime, dict):
        raise ValueError("config/settings.json 的 runtime 必须是对象")
    mapping = {
        "text_concurrency": "TEXT_CONCURRENCY",
        "review_concurrency": "REVIEW_CONCURRENCY",
        "review_batch_size": "REVIEW_BATCH_SIZE",
        "max_rows": "MAX_ROWS",
        "max_input_rows": "MAX_INPUT_ROWS",
        "adaptive_failure_rate": "ADAPTIVE_FAILURE_RATE",
        "adaptive_recovery_batches": "ADAPTIVE_RECOVERY_BATCHES",
        "circuit_failure_threshold": "CIRCUIT_FAILURE_THRESHOLD",
        "circuit_cooldown_s": "CIRCUIT_COOLDOWN_S",
    }
    defaults: dict[str, Any] = {
        "text_concurrency": 100,
        "review_concurrency": 30,
        "review_batch_size": 3,
        "max_rows": 0,
        "max_input_rows": 10_000,
        "adaptive_failure_rate": 0.25,
        "adaptive_recovery_batches": 3,
        "circuit_failure_threshold": 8,
        "circuit_cooldown_s": 60,
    }
    return {
        mapping[key]: str(runtime.get(key, default))
        for key, default in defaults.items()
    }


def load_config() -> dict[str, str]:
    """Return the complete effective configuration."""
    global _cache
    with _lock:
        if _cache is not None:
            return dict(_cache)
        registry = get_model_registry()
        values = CredentialStore(
            registry,
            env_path=ENV_PATH,
            environ=os.environ,
        ).values_by_env()
        values.update(registry.as_config())
        values.update(_read_runtime())
        values["MODEL_PROFILE"] = registry.profile_name
        values["PROMPT_PROFILE"] = "production"
        values["DATA_DIR"] = str(PROJECT_ROOT / ".runtime")
        _cache = {key: str(value) for key, value in values.items()}
        return dict(_cache)


def reload_config() -> dict[str, str]:
    global _cache
    with _lock:
        _cache = None
    reload_model_registry()
    return load_config()


def get(key: str, default: str = "") -> str:
    return load_config().get(key, default)


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(get(key, str(default)))
    except (TypeError, ValueError):
        return default


def get_float(key: str, default: float = 0.0) -> float:
    try:
        return float(get(key, str(default)))
    except (TypeError, ValueError):
        return default


def get_bool(key: str, default: bool = False) -> bool:
    value = get(key, "true" if default else "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


__all__ = [
    "ENV_PATH",
    "SETTINGS_PATH",
    "get",
    "get_bool",
    "get_float",
    "get_int",
    "load_config",
    "reload_config",
]
