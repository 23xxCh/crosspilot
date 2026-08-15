"""Validated, non-secret model routing configuration."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit


DEFAULT_MODEL_CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "settings.json"
)
OPERATIONS = ("text", "vision", "image")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PROVIDERS_BY_OPERATION = {
    "text": {"deepseek", "agnes"},
    "vision": {"agnes", "ollama"},
    "image": {"agnes", "gpt"},
}


class ModelConfigError(ValueError):
    """The model registry file is missing or invalid."""


@dataclass(frozen=True)
class CredentialDefinition:
    """A named, non-secret pointer to one environment variable."""

    credential_id: str
    label: str
    env: str

    @classmethod
    def from_mapping(
        cls,
        credential_id: str,
        value: Mapping[str, Any],
    ) -> "CredentialDefinition":
        location = f"credentials.{credential_id}"
        if not _NAME_RE.fullmatch(credential_id):
            raise ModelConfigError(
                f"{location} 名称只能使用小写字母、数字和下划线"
            )
        if not isinstance(value, Mapping):
            raise ModelConfigError(f"{location} 必须是对象")
        label = value.get("label")
        env = value.get("env")
        if not isinstance(label, str) or not label.strip():
            raise ModelConfigError(f"{location}.label 不能为空")
        if not isinstance(env, str) or not _ENV_RE.fullmatch(env.strip()):
            raise ModelConfigError(
                f"{location}.env 必须是大写环境变量名"
            )
        return cls(
            credential_id=credential_id,
            label=label.strip(),
            env=env.strip(),
        )

    def as_dict(self) -> dict[str, str]:
        return {"label": self.label, "env": self.env}


@dataclass(frozen=True)
class ModelTarget:
    """One concrete provider/model endpoint."""

    provider: str
    credential: str
    model: str
    base_url: str
    params: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({}),
    )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        location: str,
    ) -> "ModelTarget":
        if not isinstance(value, Mapping):
            raise ModelConfigError(f"{location} 必须是对象")
        fields = {}
        for name in ("provider", "credential", "model", "base_url"):
            item = value.get(name)
            if not isinstance(item, str) or not item.strip():
                raise ModelConfigError(f"{location}.{name} 不能为空")
            fields[name] = item.strip()
        parsed_url = urlsplit(fields["base_url"])
        is_local_http = (
            parsed_url.scheme == "http"
            and parsed_url.hostname in {"127.0.0.1", "localhost", "::1"}
        )
        if parsed_url.scheme != "https" and not is_local_http:
            raise ModelConfigError(
                f"{location}.base_url 必须使用 HTTPS；本机地址可使用 HTTP"
            )
        params = value.get("params") or {}
        if not isinstance(params, Mapping):
            raise ModelConfigError(f"{location}.params 必须是对象")
        return cls(
            **fields,
            params=MappingProxyType(dict(params)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "credential": self.credential,
            "model": self.model,
            "base_url": self.base_url,
            "params": dict(self.params),
        }


class ModelRegistry:
    """Resolved model profile with validated targets and fallback chains."""

    def __init__(
        self,
        *,
        version: int,
        profile_name: str,
        targets: Mapping[str, ModelTarget],
        fallback_targets: Mapping[str, tuple[ModelTarget, ...]],
        credentials: Mapping[str, CredentialDefinition],
        source_path: Path,
    ) -> None:
        self.version = version
        self.profile_name = profile_name
        self._targets = dict(targets)
        self._fallback_targets = dict(fallback_targets)
        self._credentials = dict(credentials)
        self.source_path = source_path

    @classmethod
    def from_file(
        cls,
        path: str | os.PathLike[str],
        profile: str | None = None,
    ) -> "ModelRegistry":
        source_path = Path(path)
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ModelConfigError(f"模型配置不存在: {source_path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ModelConfigError(f"模型配置无法读取: {source_path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ModelConfigError("模型配置根节点必须是对象")

        version = payload.get("version")
        if not isinstance(version, int) or version < 2:
            raise ModelConfigError("模型配置 version 必须为 2 或更高")

        raw_credentials = payload.get("credentials")
        if not isinstance(raw_credentials, dict) or not raw_credentials:
            raise ModelConfigError("模型配置缺少 credentials")
        credentials = {
            str(credential_id): CredentialDefinition.from_mapping(
                str(credential_id),
                value,
            )
            for credential_id, value in raw_credentials.items()
        }
        env_names = [item.env for item in credentials.values()]
        if len(env_names) != len(set(env_names)):
            raise ModelConfigError("credentials 中的 env 不能重复")

        profiles = payload.get("profiles")
        if not isinstance(profiles, dict) or not profiles:
            raise ModelConfigError("模型配置缺少 profiles")
        profile_name = str(
            profile
            or payload.get("active_profile")
            or ""
        ).strip()
        if not profile_name or profile_name not in profiles:
            raise ModelConfigError(f"模型配置档不存在: {profile_name or '<empty>'}")
        selected = profiles[profile_name]
        if not isinstance(selected, dict):
            raise ModelConfigError(f"profiles.{profile_name} 必须是对象")

        targets: dict[str, ModelTarget] = {}
        fallbacks: dict[str, tuple[ModelTarget, ...]] = {}
        for operation in OPERATIONS:
            location = f"profiles.{profile_name}.{operation}"
            raw_target = selected.get(operation)
            if raw_target is None:
                raise ModelConfigError(f"模型配置缺少 {location}")
            target = ModelTarget.from_mapping(raw_target, location=location)
            cls._validate_target(
                target,
                operation=operation,
                location=location,
                credentials=credentials,
            )
            targets[operation] = target

            raw_fallbacks = raw_target.get("fallbacks") or []
            if not isinstance(raw_fallbacks, list):
                raise ModelConfigError(f"{location}.fallbacks 必须是数组")
            operation_fallbacks = []
            for index, item in enumerate(raw_fallbacks):
                fallback_location = f"{location}.fallbacks[{index}]"
                fallback = ModelTarget.from_mapping(
                    item,
                    location=fallback_location,
                )
                cls._validate_target(
                    fallback,
                    operation=operation,
                    location=fallback_location,
                    credentials=credentials,
                )
                operation_fallbacks.append(fallback)
            fallbacks[operation] = tuple(operation_fallbacks)

        return cls(
            version=version,
            profile_name=profile_name,
            targets=targets,
            fallback_targets=fallbacks,
            credentials=credentials,
            source_path=source_path,
        )

    @staticmethod
    def _validate_target(
        target: ModelTarget,
        *,
        operation: str,
        location: str,
        credentials: Mapping[str, CredentialDefinition],
    ) -> None:
        allowed = _PROVIDERS_BY_OPERATION[operation]
        if target.provider not in allowed:
            raise ModelConfigError(
                f"{location}.provider 不支持 {target.provider!r}"
            )
        if target.credential not in credentials:
            raise ModelConfigError(
                f"{location}.credential 未定义: {target.credential}"
            )

    def target(self, operation: str) -> ModelTarget:
        try:
            return self._targets[operation]
        except KeyError as exc:
            raise ModelConfigError(f"不支持的模型操作: {operation}") from exc

    def fallbacks(self, operation: str) -> tuple[ModelTarget, ...]:
        if operation not in self._targets:
            raise ModelConfigError(f"不支持的模型操作: {operation}")
        return self._fallback_targets.get(operation, ())

    def routes(self, operation: str) -> tuple[ModelTarget, ...]:
        """Return primary and fallback targets in execution order."""
        return (self.target(operation), *self.fallbacks(operation))

    def credential(self, credential_id: str) -> CredentialDefinition:
        try:
            return self._credentials[credential_id]
        except KeyError as exc:
            raise ModelConfigError(
                f"未定义凭据: {credential_id}"
            ) from exc

    def credentials(self) -> tuple[CredentialDefinition, ...]:
        return tuple(self._credentials.values())

    def signature(self) -> str:
        payload = {
            "version": self.version,
            "profile": self.profile_name,
            "targets": {
                operation: self.target(operation).as_dict()
                for operation in OPERATIONS
            },
            "fallbacks": {
                operation: [
                    target.as_dict() for target in self.fallbacks(operation)
                ]
                for operation in OPERATIONS
            },
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]

    def as_config(self) -> dict[str, str]:
        """Export values used to construct the configured Providers."""
        text = self.target("text")
        vision = self.target("vision")
        image = self.target("image")
        config = {
            "MODEL_PROFILE": self.profile_name,
            "TEXT_PROVIDER": text.provider,
            "VISION_PROVIDER": vision.provider,
            "IMAGE_PROVIDER": image.provider,
        }

        if text.provider == "deepseek":
            config.update({
                "DEEPSEEK_BASE_URL": text.base_url,
                "DEEPSEEK_TEXT_MODEL": text.model,
            })
            text_fallbacks = self.fallbacks("text")
            if text_fallbacks:
                config["DEEPSEEK_TEXT_FALLBACK_MODEL"] = (
                    text_fallbacks[0].model
                )
        elif text.provider == "agnes":
            config.update({
                "AGNES_TEXT_BASE_URL": text.base_url,
                "AGNES_TEXT_MODEL": text.model,
            })

        if vision.provider == "agnes":
            config.update({
                "AGNES_VISION_BASE_URL": vision.base_url,
                "AGNES_VISION_MODEL": vision.model,
            })
            config.setdefault("AGNES_TEXT_BASE_URL", vision.base_url)
            config.setdefault("AGNES_TEXT_MODEL", vision.model)

        if image.provider == "agnes":
            config.update({
                "AGNES_IMAGE_BASE_URL": image.base_url,
                "AGNES_BASE_URL": image.base_url,
                "AGNES_IMAGE_MODEL": image.model,
            })
        elif image.provider == "gpt":
            config.update({
                "GPT_IMAGE_BASE_URL": image.base_url,
                "GPT_IMAGE_MODEL": image.model,
            })

        for fallback in self.fallbacks("image"):
            if (
                fallback.provider == "agnes"
                and "AGNES_IMAGE_FALLBACK_MODEL" not in config
            ):
                config["AGNES_IMAGE_FALLBACK_MODEL"] = fallback.model
            elif fallback.provider == "gpt":
                config.setdefault("GPT_IMAGE_BASE_URL", fallback.base_url)
                config.setdefault("GPT_IMAGE_MODEL", fallback.model)
        return config


_registry: ModelRegistry | None = None
_registry_key: tuple[str, str | None] | None = None
_registry_lock = threading.Lock()


def _configured_path() -> Path:
    return DEFAULT_MODEL_CONFIG_PATH


def list_model_profiles(
    path: str | os.PathLike[str] | None = None,
) -> tuple[str, ...]:
    """List configured profile names without resolving a specific profile."""
    source_path = Path(path) if path is not None else _configured_path()
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ModelConfigError(f"模型配置不存在: {source_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelConfigError(
            f"模型配置无法读取: {source_path}: {exc}"
        ) from exc
    profiles = payload.get("profiles") if isinstance(payload, dict) else None
    if not isinstance(profiles, dict) or not profiles:
        raise ModelConfigError("模型配置缺少 profiles")
    return tuple(str(name) for name in profiles)


def get_model_registry(
    profile: str | None = None,
    path: str | os.PathLike[str] | None = None,
) -> ModelRegistry:
    """Return the cached active registry."""
    global _registry, _registry_key
    resolved_path = Path(path) if path is not None else _configured_path()
    selected_profile = profile
    cache_key = (str(resolved_path.resolve()), selected_profile)
    with _registry_lock:
        if _registry is None or _registry_key != cache_key:
            _registry = ModelRegistry.from_file(
                resolved_path,
                profile=selected_profile,
            )
            _registry_key = cache_key
        return _registry


def reload_model_registry() -> ModelRegistry:
    """Discard the cached registry and load it again."""
    global _registry, _registry_key
    with _registry_lock:
        _registry = None
        _registry_key = None
    return get_model_registry()


def model_signature() -> str:
    return get_model_registry().signature()
