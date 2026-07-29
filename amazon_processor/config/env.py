"""Load secrets from .env and all non-secret settings from settings.json."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from typing import Any

from .models import get_model_registry, reload_model_registry


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"
SECRET_KEYS = ("DEEPSEEK_KEY", "AGNES_KEY", "GPT_IMAGE_KEY")
_cache: dict[str, str] | None = None
_lock = threading.Lock()


def _read_env(path: Path = ENV_PATH) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in SECRET_KEYS:
            values[key] = value.strip().strip('"').strip("'")
    return values


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
        "image_concurrency": "IMAGE_GEN_CONCURRENCY",
        "max_rows": "MAX_ROWS",
        "max_input_rows": "MAX_INPUT_ROWS",
        "agnes_503_retry_limit": "AGNES_503_RETRY_LIMIT",
        "agnes_503_backoff_min_s": "AGNES_503_BACKOFF_MIN_S",
        "agnes_503_backoff_max_s": "AGNES_503_BACKOFF_MAX_S",
        "agnes_503_circuit_threshold": "AGNES_503_CIRCUIT_THRESHOLD",
        "agnes_503_circuit_cooldown_s": (
            "AGNES_503_CIRCUIT_COOLDOWN_S"
        ),
        "image_regeneration_routes": (
            "IMAGE_SAFETY_REGEN_LIMIT"
        ),
        "adaptive_failure_rate": "ADAPTIVE_FAILURE_RATE",
        "adaptive_recovery_batches": "ADAPTIVE_RECOVERY_BATCHES",
        "circuit_failure_threshold": "CIRCUIT_FAILURE_THRESHOLD",
        "circuit_cooldown_s": "CIRCUIT_COOLDOWN_S",
    }
    defaults: dict[str, Any] = {
        "text_concurrency": 100,
        "review_concurrency": 30,
        "image_concurrency": 20,
        "max_rows": 0,
        "max_input_rows": 10_000,
        "agnes_503_retry_limit": 1,
        "agnes_503_backoff_min_s": 3,
        "agnes_503_backoff_max_s": 8,
        "agnes_503_circuit_threshold": 3,
        "agnes_503_circuit_cooldown_s": 120,
        "image_regeneration_routes": 2,
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
        values = {
            key: os.environ.get(key) or value
            for key, value in _read_env().items()
        }
        for key in SECRET_KEYS:
            values.setdefault(key, os.environ.get(key, ""))
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
