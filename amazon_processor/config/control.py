"""Transactional storage for the local configuration manager."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Iterator
from uuid import uuid4

from .credentials import CredentialStore, read_env_values
from .locking import ProcessLock, processor_is_running
from .models import ModelRegistry
from .prompts import PromptRegistry


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"
SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.json"
PROMPT_ROOT = PROJECT_ROOT / "config" / "prompts"
MANIFEST_PATH = PROMPT_ROOT / "manifest.json"
RUNTIME_ROOT = PROJECT_ROOT / ".runtime"
BACKUP_ROOT = RUNTIME_ROOT / "config_backups"
STAGING_ROOT = RUNTIME_ROOT / "config_staging"
BACKUP_LIMIT = 10
_BACKUP_NAME_RE = re.compile(r"^[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$")

RUNTIME_FIELDS = (
    {
        "key": "text_concurrency",
        "label": "文本并发",
        "type": "int",
        "min": 1,
        "max": 300,
        "step": 1,
    },
    {
        "key": "review_concurrency",
        "label": "审图并发",
        "type": "int",
        "min": 1,
        "max": 100,
        "step": 1,
    },
    {
        "key": "image_concurrency",
        "label": "生图并发",
        "type": "int",
        "min": 1,
        "max": 100,
        "step": 1,
    },
    {
        "key": "max_rows",
        "label": "最多处理行数（0 为全部）",
        "type": "int",
        "min": 0,
        "max": 10000,
        "step": 1,
    },
    {
        "key": "max_input_rows",
        "label": "输入安全上限",
        "type": "int",
        "min": 1,
        "max": 100000,
        "step": 1,
    },
    {
        "key": "agnes_503_retry_limit",
        "label": "Agnes 503 快速重试次数",
        "type": "int",
        "min": 0,
        "max": 10,
        "step": 1,
    },
    {
        "key": "agnes_503_backoff_min_s",
        "label": "503 最短等待（秒）",
        "type": "float",
        "min": 0,
        "max": 300,
        "step": 0.5,
    },
    {
        "key": "agnes_503_backoff_max_s",
        "label": "503 最长等待（秒）",
        "type": "float",
        "min": 0,
        "max": 600,
        "step": 0.5,
    },
    {
        "key": "agnes_503_circuit_threshold",
        "label": "Agnes 熔断阈值",
        "type": "int",
        "min": 1,
        "max": 100,
        "step": 1,
    },
    {
        "key": "agnes_503_circuit_cooldown_s",
        "label": "Agnes 熔断冷却（秒）",
        "type": "int",
        "min": 1,
        "max": 3600,
        "step": 1,
    },
    {
        "key": "image_regeneration_routes",
        "label": "图片修复最大尝试数",
        "type": "int",
        "min": 1,
        "max": 10,
        "step": 1,
    },
    {
        "key": "adaptive_failure_rate",
        "label": "并发降级失败率",
        "type": "float",
        "min": 0.01,
        "max": 1,
        "step": 0.01,
    },
    {
        "key": "adaptive_recovery_batches",
        "label": "并发恢复批次数",
        "type": "int",
        "min": 1,
        "max": 100,
        "step": 1,
    },
    {
        "key": "circuit_failure_threshold",
        "label": "通用熔断阈值",
        "type": "int",
        "min": 1,
        "max": 100,
        "step": 1,
    },
    {
        "key": "circuit_cooldown_s",
        "label": "通用熔断冷却（秒）",
        "type": "int",
        "min": 1,
        "max": 3600,
        "step": 1,
    },
)


class ConfigurationError(ValueError):
    """Configuration input is invalid without containing secret values."""


class ConfigurationConflict(ConfigurationError):
    """The files changed after the browser loaded them."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"配置文件不存在: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"配置文件无法读取: {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"{path.name} 根节点必须是对象")
    return value


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def _revision_paths() -> list[Path]:
    paths = [SETTINGS_PATH, ENV_PATH, MANIFEST_PATH]
    if MANIFEST_PATH.is_file():
        try:
            registry = PromptRegistry(PROMPT_ROOT)
            paths.extend(PROMPT_ROOT / spec.path for spec in registry.specs())
        except Exception:
            paths.extend(PROMPT_ROOT.rglob("*.txt"))
    return paths


def configuration_revision() -> str:
    """Hash file metadata only, so the browser never receives a secret digest."""
    rows = []
    for path in sorted(set(_revision_paths()), key=lambda item: str(item)):
        try:
            stat = path.stat()
            rows.append(
                (
                    str(path.relative_to(PROJECT_ROOT)),
                    stat.st_mtime_ns,
                    stat.st_size,
                )
            )
        except OSError:
            rows.append((str(path.relative_to(PROJECT_ROOT)), None, None))
    encoded = json.dumps(rows, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def configuration_signature(
    registry: ModelRegistry,
    prompts: PromptRegistry,
) -> str:
    payload = {
        "models": registry.signature(),
        "prompts": prompts.signature(),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _validate_runtime(settings: dict[str, Any]) -> None:
    runtime = settings.get("runtime")
    if not isinstance(runtime, dict):
        raise ConfigurationError("settings.runtime 必须是对象")
    for field in RUNTIME_FIELDS:
        key = str(field["key"])
        if key not in runtime:
            raise ConfigurationError(f"runtime 缺少 {key}")
        raw = runtime[key]
        expected = field["type"]
        if isinstance(raw, bool):
            raise ConfigurationError(f"runtime.{key} 必须是数字")
        try:
            value = int(raw) if expected == "int" else float(raw)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"runtime.{key} 必须是数字") from exc
        if expected == "int" and value != raw:
            raise ConfigurationError(f"runtime.{key} 必须是整数")
        if value < field["min"] or value > field["max"]:
            raise ConfigurationError(
                f"runtime.{key} 必须在 {field['min']}–{field['max']} 之间"
            )
    if (
        float(runtime["agnes_503_backoff_min_s"])
        > float(runtime["agnes_503_backoff_max_s"])
    ):
        raise ConfigurationError("503 最短等待不能大于最长等待")


def _prompt_payload(prompts: PromptRegistry) -> list[dict[str, object]]:
    result = []
    for spec in prompts.specs():
        result.append({**spec.as_dict(), "content": prompts.get(spec.prompt_id)})
    return result


def list_backups() -> list[dict[str, Any]]:
    if not BACKUP_ROOT.is_dir():
        return []
    result = []
    for directory in sorted(BACKUP_ROOT.iterdir(), reverse=True):
        if not directory.is_dir() or not _BACKUP_NAME_RE.fullmatch(directory.name):
            continue
        metadata_path = directory / "metadata.json"
        metadata = {}
        if metadata_path.is_file():
            try:
                metadata = _read_json(metadata_path)
            except ConfigurationError:
                metadata = {}
        result.append({
            "name": directory.name,
            "created_at": metadata.get("created_at", ""),
            "reason": metadata.get("reason", "配置保存"),
        })
    return result


def public_state() -> dict[str, Any]:
    registry = ModelRegistry.from_file(SETTINGS_PATH)
    prompts = PromptRegistry(PROMPT_ROOT)
    credentials = CredentialStore(registry, env_path=ENV_PATH)
    return {
        "revision": configuration_revision(),
        "signature": configuration_signature(registry, prompts),
        "processing": processor_is_running(RUNTIME_ROOT / "processor.lock"),
        "settings": _read_json(SETTINGS_PATH),
        "credentials": credentials.public_states(),
        "prompts": _prompt_payload(prompts),
        "runtime_fields": list(RUNTIME_FIELDS),
        "backups": list_backups(),
    }


def _validate_secret_value(value: object) -> str:
    if not isinstance(value, str):
        raise ConfigurationError("API 密钥必须是字符串")
    if "\r" in value or "\n" in value or "\0" in value:
        raise ConfigurationError("API 密钥不能包含换行或空字符")
    result = value.strip()
    if not result:
        raise ConfigurationError("替换 API 密钥时不能为空")
    if len(result) > 4096:
        raise ConfigurationError("API 密钥长度异常")
    return result


def _rewrite_env(
    *,
    old_registry: ModelRegistry,
    new_registry: ModelRegistry,
    actions: dict[str, Any],
) -> str:
    original = ENV_PATH.read_text(encoding="utf-8-sig") if ENV_PATH.is_file() else ""
    lines = original.splitlines()
    old_file_values = read_env_values(ENV_PATH)
    old_by_id = {
        item.credential_id: item
        for item in old_registry.credentials()
    }
    new_by_id = {
        item.credential_id: item
        for item in new_registry.credentials()
    }
    desired: dict[str, str | None] = {}

    for credential_id, definition in new_by_id.items():
        action = actions.get(credential_id) or {"action": "keep"}
        if not isinstance(action, dict):
            raise ConfigurationError(f"凭据操作格式错误: {credential_id}")
        operation = str(action.get("action") or "keep")
        if operation not in {"keep", "replace", "clear"}:
            raise ConfigurationError(f"未知凭据操作: {operation}")
        system_value = str(os.environ.get(definition.env) or "")
        if system_value and operation != "keep":
            raise ConfigurationError(
                f"{definition.label} 由系统环境变量覆盖，不能在页面替换或清空"
            )
        previous = old_by_id.get(credential_id)
        previous_value = (
            old_file_values.get(previous.env, "")
            if previous is not None
            else ""
        )
        if operation == "replace":
            desired[definition.env] = _validate_secret_value(action.get("value"))
        elif operation == "clear":
            desired[definition.env] = ""
        elif definition.env != getattr(previous, "env", None) and previous_value:
            desired[definition.env] = previous_value

    retained_envs = {item.env for item in new_by_id.values()}
    for old_definition in old_by_id.values():
        if old_definition.env not in retained_envs:
            desired[old_definition.env] = None

    positions: dict[str, int] = {}
    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        positions[key] = index

    for env_name, value in desired.items():
        if value is None:
            if env_name in positions:
                lines[positions[env_name]] = ""
            continue
        rendered = f"{env_name}={value}"
        if env_name in positions:
            lines[positions[env_name]] = rendered
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(rendered)
    compacted = []
    previous_blank = False
    for line in lines:
        blank = not line.strip()
        if blank and previous_blank:
            continue
        compacted.append(line)
        previous_blank = blank
    return "\n".join(compacted).rstrip() + "\n"


@contextmanager
def _staged_configuration(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    settings = payload.get("settings")
    prompt_values = payload.get("prompts")
    actions = payload.get("secrets") or {}
    if not isinstance(settings, dict):
        raise ConfigurationError("settings 必须是对象")
    if not isinstance(prompt_values, dict):
        raise ConfigurationError("prompts 必须是对象")
    if not isinstance(actions, dict):
        raise ConfigurationError("secrets 必须是对象")

    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="config-",
        dir=STAGING_ROOT,
    ) as temp:
        root = Path(temp)
        stage_settings = root / "settings.json"
        stage_env = root / ".env"
        stage_prompts = root / "prompts"
        shutil.copytree(PROMPT_ROOT, stage_prompts)
        stage_settings.write_bytes(_json_bytes(settings))

        old_registry = ModelRegistry.from_file(SETTINGS_PATH)
        new_registry = ModelRegistry.from_file(stage_settings)
        stage_env.write_text(
            _rewrite_env(
                old_registry=old_registry,
                new_registry=new_registry,
                actions=actions,
            ),
            encoding="utf-8",
        )

        stage_prompt_registry = PromptRegistry(stage_prompts)
        expected_ids = {
            spec.prompt_id for spec in stage_prompt_registry.specs()
        }
        if set(prompt_values) != expected_ids:
            missing = sorted(expected_ids - set(prompt_values))
            unknown = sorted(set(prompt_values) - expected_ids)
            details = []
            if missing:
                details.append("缺少 " + ", ".join(missing))
            if unknown:
                details.append("未知 " + ", ".join(unknown))
            raise ConfigurationError("Prompt 列表不完整: " + "；".join(details))
        for spec in stage_prompt_registry.specs():
            content = prompt_values[spec.prompt_id]
            if not isinstance(content, str):
                raise ConfigurationError(
                    f"{spec.prompt_id} 的内容必须是字符串"
                )
            (stage_prompts / spec.path).write_text(
                content.strip() + "\n",
                encoding="utf-8",
            )
        stage_prompt_registry.reload()
        for spec in stage_prompt_registry.specs():
            stage_prompt_registry.get(spec.prompt_id)

        _validate_runtime(settings)
        missing_routes = CredentialStore(
            new_registry,
            env_path=stage_env,
        ).missing_routes()
        if missing_routes:
            raise ConfigurationError(
                "以下模型线路缺少 API 密钥: " + ", ".join(missing_routes)
            )
        yield {
            "root": root,
            "settings": stage_settings,
            "env": stage_env,
            "prompts": stage_prompts,
            "registry": new_registry,
            "prompt_registry": stage_prompt_registry,
            "signature": configuration_signature(
                new_registry,
                stage_prompt_registry,
            ),
        }


def validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    with _staged_configuration(payload) as staged:
        return {
            "valid": True,
            "signature": staged["signature"],
            "processing": processor_is_running(RUNTIME_ROOT / "processor.lock"),
        }


def _copy_if_exists(source: Path, target: Path) -> bool:
    if not source.exists():
        return False
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return True


def _create_backup(reason: str) -> Path:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    directory = BACKUP_ROOT / f"{stamp}_{uuid4().hex[:6]}"
    directory.mkdir(parents=True)
    env_exists = _copy_if_exists(ENV_PATH, directory / ".env")
    _copy_if_exists(SETTINGS_PATH, directory / "settings.json")
    _copy_if_exists(PROMPT_ROOT, directory / "prompts")
    (directory / "metadata.json").write_bytes(_json_bytes({
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "reason": reason,
        "env_exists": env_exists,
        "revision": configuration_revision(),
    }))
    return directory


def _prune_backups() -> None:
    backups = [
        item for item in sorted(BACKUP_ROOT.iterdir(), reverse=True)
        if item.is_dir() and _BACKUP_NAME_RE.fullmatch(item.name)
    ]
    for expired in backups[BACKUP_LIMIT:]:
        shutil.rmtree(expired)


def _atomic_copy(source: Path, target: Path) -> None:
    data = source.read_bytes()
    if target.is_file() and target.read_bytes() == data:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _copy_prompt_tree(source: Path) -> None:
    source_files = {
        path.relative_to(source): path
        for path in source.rglob("*")
        if path.is_file()
    }
    for relative, path in source_files.items():
        _atomic_copy(path, PROMPT_ROOT / relative)


def _restore_snapshot(directory: Path) -> None:
    metadata = _read_json(directory / "metadata.json")
    _atomic_copy(directory / "settings.json", SETTINGS_PATH)
    _copy_prompt_tree(directory / "prompts")
    if metadata.get("env_exists"):
        _atomic_copy(directory / ".env", ENV_PATH)
    elif ENV_PATH.exists():
        ENV_PATH.unlink()


def _reload_live_configuration() -> None:
    from .env import reload_config
    from .prompts import reload_prompt_registry

    reload_config()
    reload_prompt_registry()
    try:
        from ..providers.factory import reload_provider

        reload_provider()
    except ImportError:
        pass


def save_payload(payload: dict[str, Any]) -> dict[str, Any]:
    revision = str(payload.get("revision") or "")
    with ProcessLock(RUNTIME_ROOT / "processor.lock"):
        if revision != configuration_revision():
            raise ConfigurationConflict(
                "配置已被其他程序修改，请刷新页面后重新编辑"
            )
        old_registry = ModelRegistry.from_file(SETTINGS_PATH)
        old_prompts = PromptRegistry(PROMPT_ROOT)
        old_signature = configuration_signature(old_registry, old_prompts)
        with _staged_configuration(payload) as staged:
            backup = _create_backup("配置保存")
            try:
                _atomic_copy(staged["settings"], SETTINGS_PATH)
                _atomic_copy(staged["env"], ENV_PATH)
                _copy_prompt_tree(staged["prompts"])
                _reload_live_configuration()
            except Exception:
                _restore_snapshot(backup)
                _reload_live_configuration()
                raise
            new_signature = staged["signature"]
            _prune_backups()
    return {
        "saved": True,
        "old_signature": old_signature,
        "new_signature": new_signature,
        "cache_invalidated": old_signature != new_signature,
        "state": public_state(),
    }


def restore_backup(name: str, revision: str) -> dict[str, Any]:
    if not _BACKUP_NAME_RE.fullmatch(name):
        raise ConfigurationError("备份名称不合法")
    source = (BACKUP_ROOT / name).resolve()
    try:
        source.relative_to(BACKUP_ROOT.resolve())
    except ValueError as exc:
        raise ConfigurationError("备份路径不合法") from exc
    if not source.is_dir():
        raise ConfigurationError(f"备份不存在: {name}")
    with ProcessLock(RUNTIME_ROOT / "processor.lock"):
        if revision != configuration_revision():
            raise ConfigurationConflict(
                "配置已被其他程序修改，请刷新页面后重试"
            )
        backup_registry = ModelRegistry.from_file(source / "settings.json")
        backup_prompts = PromptRegistry(source / "prompts")
        for spec in backup_prompts.specs():
            backup_prompts.get(spec.prompt_id)
        _validate_runtime(_read_json(source / "settings.json"))
        backup_env = source / ".env"
        missing = CredentialStore(
            backup_registry,
            env_path=backup_env,
        ).missing_routes()
        if missing:
            raise ConfigurationError(
                "备份缺少当前需要的 API 密钥: " + ", ".join(missing)
            )
        rollback = _create_backup(f"恢复 {name} 前")
        try:
            _restore_snapshot(source)
            _reload_live_configuration()
        except Exception:
            _restore_snapshot(rollback)
            _reload_live_configuration()
            raise
        _prune_backups()
    return {"restored": True, "state": public_state()}


__all__ = [
    "BACKUP_ROOT",
    "ConfigurationConflict",
    "ConfigurationError",
    "RUNTIME_FIELDS",
    "configuration_revision",
    "list_backups",
    "public_state",
    "restore_backup",
    "save_payload",
    "validate_payload",
]
