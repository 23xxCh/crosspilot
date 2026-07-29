#!/usr/bin/env python3
"""CrossPilot 配置系统 — 模型注册表 + .env + 旧 keys.json 兼容。"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from crosspilot.model_registry import (
    get_model_registry,
    reload_model_registry,
)

ROOT = Path(__file__).resolve().parent.parent

# 非模型默认值。模型/端点默认值只来自 model_profiles.json。
STATIC_DEFAULTS = {
    'DEEPSEEK_KEY': '',
    'AGNES_KEY': '',
    'GPT_IMAGE_KEY': '',
    'PROMPT_PROFILE': 'production',
    'SKIP_IMAGE_GEN': 'false',
    'IMAGE_GEN_CONCURRENCY': '20',
    'TEXT_CONCURRENCY': '100',
    'REVIEW_CONCURRENCY': '30',
    'MAX_ROWS': '0',
    'MAX_INPUT_ROWS': '10000',
    'QUALITY_GATE': 'false',
    'IMAGE_REMEDIATE_ONLY': 'false',
    'IMAGE_QUALITY_REGEN_LIMIT': '1',
    'VALIDATE_GENERATED_IMAGE': 'false',
    'IMAGE_VALIDATION_ROUTE_LIMIT': '3',
    'IMAGE_GEN_ATTEMPTS': '3',
    'OUTPUT_REPORT': 'true',
    'REPORT_LANGUAGE': 'zh',
    'CIRCUIT_FAILURE_THRESHOLD': '8',
    'CIRCUIT_COOLDOWN_S': '60',
    'AGNES_503_RETRY_LIMIT': '1',
    'AGNES_503_BACKOFF_MIN_S': '3',
    'AGNES_503_BACKOFF_MAX_S': '8',
    'AGNES_503_CIRCUIT_THRESHOLD': '3',
    'AGNES_503_CIRCUIT_COOLDOWN_S': '120',
}

MODEL_CONFIG_KEYS = {
    'MODEL_PROFILE',
    'TEXT_PROVIDER',
    'VISION_PROVIDER',
    'IMAGE_PROVIDER',
    'DEEPSEEK_BASE_URL',
    'DEEPSEEK_TEXT_MODEL',
    'DEEPSEEK_TEXT_FALLBACK_MODEL',
    'AGNES_BASE_URL',
    'AGNES_TEXT_BASE_URL',
    'AGNES_TEXT_MODEL',
    'AGNES_VISION_BASE_URL',
    'AGNES_VISION_MODEL',
    'AGNES_IMAGE_BASE_URL',
    'AGNES_IMAGE_MODEL',
    'AGNES_IMAGE_FALLBACK_MODEL',
    'GPT_IMAGE_BASE_URL',
    'GPT_IMAGE_MODEL',
}

# 兼容历史导入。load_config 会根据最终选择的 profile 重新计算模型默认值。
DEFAULTS = {
    **STATIC_DEFAULTS,
    **get_model_registry().as_config(),
}
CONFIG_KEYS = set(STATIC_DEFAULTS) | MODEL_CONFIG_KEYS

_config: dict[str, str] | None = None


def _load_env_file(path: Path) -> dict[str, str]:
    """解析 .env 文件，返回 key-value 字典。"""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                result[key] = value
    return result


def _load_keys_json(path: Path) -> dict[str, str]:
    """兼容旧 keys.json 格式，提取 provider、模型和 key。"""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        # Map old keys.json fields to new config names
        mapping = {
            'model_profile': 'MODEL_PROFILE',
            'prompt_profile': 'PROMPT_PROFILE',
            'text_provider': 'TEXT_PROVIDER',
            'vision_provider': 'VISION_PROVIDER',
            'image_gen_provider': 'IMAGE_PROVIDER',
            'deepseek_key': 'DEEPSEEK_KEY',
            'agnes_key': 'AGNES_KEY',
            'gpt_image_key': 'GPT_IMAGE_KEY',
            'deepseek_base_url': 'DEEPSEEK_BASE_URL',
            'deepseek_text_model': 'DEEPSEEK_TEXT_MODEL',
            'deepseek_text_fallback_model': (
                'DEEPSEEK_TEXT_FALLBACK_MODEL'
            ),
            'agnes_base_url': 'AGNES_BASE_URL',
            'agnes_text_model': 'AGNES_TEXT_MODEL',
            'agnes_vision_model': 'AGNES_VISION_MODEL',
            'agnes_image_model': 'AGNES_IMAGE_MODEL',
            'agnes_image_fallback_model': (
                'AGNES_IMAGE_FALLBACK_MODEL'
            ),
            'gpt_image_base_url': 'GPT_IMAGE_BASE_URL',
            'gpt_image_model': 'GPT_IMAGE_MODEL',
        }
        for old_key, new_key in mapping.items():
            if old_key in data and data[old_key]:
                result[new_key] = str(data[old_key])
    except (OSError, json.JSONDecodeError):
        pass
    return result


def load_config() -> dict[str, str]:
    """加载配置：系统环境变量 > .env > 旧 keys.json > 模型/静态默认值。"""
    global _config
    if _config is not None:
        return _config

    # 先读取各覆盖层，以便在加载模型默认值前确定活动 profile。
    keys_json_path = Path(
        os.environ.get('CROSSPILOT_KEYS_PATH', str(ROOT / 'keys.json'))
    )
    keys_json_config = _load_keys_json(keys_json_path)
    env_path = get_env_path()
    env_config = _load_env_file(env_path)

    selected_profile = (
        os.environ.get('CROSSPILOT_MODEL_PROFILE')
        or env_config.get('MODEL_PROFILE')
        or keys_json_config.get('MODEL_PROFILE')
        or None
    )
    registry = get_model_registry(profile=selected_profile)
    cfg = {
        **STATIC_DEFAULTS,
        **registry.as_config(),
    }

    # 1. 旧 keys.json（最低覆盖优先级）
    cfg.update(keys_json_config)

    # 2. .env 文件
    cfg.update({k: v for k, v in env_config.items() if v})

    # 3. 环境变量（最高优先级）
    for key in CONFIG_KEYS | set(cfg):
        env_val = os.environ.get(f'CROSSPILOT_{key}')
        if env_val:
            cfg[key] = env_val

    _config = cfg
    return cfg


def get(key: str, default: str = '') -> str:
    """获取单个配置值。"""
    return load_config().get(key, default)


def get_bool(key: str, default: bool = False) -> bool:
    """获取布尔配置值。"""
    val = load_config().get(key, '').lower()
    if not val:
        return default
    return val in ('true', '1', 'yes', 'on')


def get_int(key: str, default: int = 0) -> int:
    """获取整数配置值。"""
    try:
        return int(load_config().get(key, ''))
    except (ValueError, TypeError):
        return default


def get_float(key: str, default: float = 0.0) -> float:
    """获取浮点配置值。"""
    try:
        return float(load_config().get(key, ''))
    except (ValueError, TypeError):
        return default


def reload_config() -> dict[str, str]:
    """强制重载配置。"""
    global _config
    _config = None
    reload_model_registry()
    return load_config()


def get_env_path() -> Path:
    """返回当前生效的 .env 文件路径。"""
    return Path(os.environ.get('CROSSPILOT_ENV', str(ROOT / '.env')))


def save_env_values(
    updates: dict[str, str],
    path: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """原子更新 .env，保留未修改的配置和注释，然后热重载。

    该函数不会记录或返回密钥明文。
    """
    normalized: dict[str, str] = {}
    for raw_key, raw_value in updates.items():
        key = str(raw_key or '').strip().upper()
        if key not in CONFIG_KEYS:
            raise ValueError(f'不支持的配置项: {key or "<empty>"}')
        value = str(raw_value if raw_value is not None else '').strip()
        if '\n' in value or '\r' in value:
            raise ValueError(f'{key} 不允许包含换行')
        normalized[key] = value

    target = Path(path) if path is not None else get_env_path()
    try:
        existing = target.read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        existing = []

    output: list[str] = []
    pending = dict(normalized)
    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or '=' not in line:
            output.append(line)
            continue
        raw_key, _, _raw_value = line.partition('=')
        key = raw_key.strip()
        if key in pending:
            output.append(f'{key}={pending.pop(key)}')
        else:
            output.append(line)
    if pending and output and output[-1] != '':
        output.append('')
    for key in sorted(pending):
        output.append(f'{key}={pending[key]}')

    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f'.{target.name}.{uuid.uuid4().hex}.tmp')
    try:
        temp_path.write_text(
            '\n'.join(output).rstrip() + '\n',
            encoding='utf-8',
        )
        os.replace(temp_path, target)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return reload_config()


def print_config() -> None:
    """打印当前配置（隐藏 key）。"""
    cfg = load_config()
    print("Current configuration:")
    for k, v in sorted(cfg.items()):
        if 'KEY' in k and v:
            display = v[:8] + '...' + v[-4:] if len(v) > 12 else '***'
        else:
            display = v
        print(f"  {k} = {display}")


def ensure_dirs() -> None:
    """确保必要目录存在。"""
    data_dir = Path(os.environ.get('CROSSPILOT_DATA_DIR', str(ROOT / 'data')))
    data_dir.mkdir(parents=True, exist_ok=True)
    uploads_dir = data_dir / 'uploads'
    uploads_dir.mkdir(parents=True, exist_ok=True)
