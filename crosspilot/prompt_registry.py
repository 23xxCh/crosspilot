"""Central loader, renderer, and versioning for business prompts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import string
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from crosspilot.model_registry import model_signature


DEFAULT_PROMPT_ROOT = Path(__file__).with_name("prompts")
DEFAULT_DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
MAX_PROMPT_BYTES = 64 * 1024
PROFILE_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")
REVISION_PATTERN = re.compile(r"[0-9]+-[0-9a-f]{8}")
PROMPT_FILES = {
    "system.product_listing_optimizer": (
        "system/product_listing_optimizer.txt"
    ),
    "images.person_removal": "images/person_removal.txt",
    "images.review": "images/review.txt",
    "images.risk_assessment": "images/risk_assessment.txt",
    "images.risk_confirmation": "images/risk_confirmation.txt",
    "images.main_product": "images/main_product.txt",
    "images.variant": "images/variant.txt",
    "amazon.description_clean": "amazon/description_clean.txt",
    "amazon.title_optimize": "amazon/title_optimize.txt",
    "amazon.bullet_keywords": "amazon/bullet_keywords.txt",
    "translation.batch": "translation/batch.txt",
    "translation.batch_chinese": "translation/batch_chinese.txt",
    "translation.clean_batch": "translation/clean_batch.txt",
    "translation.title": "translation/title.txt",
    "translation.text": "translation/text.txt",
    "translation.chinese_text": "translation/chinese_text.txt",
    "ebay.description_clean": "ebay/description_clean.txt",
}


class PromptNotFoundError(KeyError):
    """A requested Prompt ID is not registered or its file is missing."""


class PromptRenderError(ValueError):
    """A Prompt cannot be rendered with the supplied variables."""


class PromptValidationError(ValueError):
    """A Prompt edit, profile, or revision is invalid."""


class PromptRegistry:
    """Load, override, version, and render prompts by stable IDs."""

    def __init__(
        self,
        root: str | Path = DEFAULT_PROMPT_ROOT,
        prompt_files: Mapping[str, str] = PROMPT_FILES,
        *,
        override_root: str | Path | None = None,
        history_root: str | Path | None = None,
        profile: str = "production",
    ) -> None:
        self.root = Path(root)
        self.prompt_files = dict(prompt_files)
        self.override_root = (
            Path(override_root) if override_root is not None else None
        )
        self.history_root = (
            Path(history_root) if history_root is not None else None
        )
        self.profile = self._validate_profile(profile)
        self._cache: dict[str, tuple[str, int, int, str]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _validate_profile(profile: str) -> str:
        value = str(profile or "").strip()
        if not PROFILE_PATTERN.fullmatch(value):
            raise PromptValidationError("Prompt 配置档名称格式无效")
        return value

    def configure(
        self,
        *,
        override_root: str | Path | None,
        history_root: str | Path | None,
        profile: str,
    ) -> None:
        """Update runtime storage without replacing this shared registry."""
        selected_profile = self._validate_profile(profile)
        selected_override = (
            Path(override_root) if override_root is not None else None
        )
        selected_history = (
            Path(history_root) if history_root is not None else None
        )
        with self._lock:
            changed = (
                self.override_root != selected_override
                or self.history_root != selected_history
                or self.profile != selected_profile
            )
            self.override_root = selected_override
            self.history_root = selected_history
            self.profile = selected_profile
            if changed:
                self._cache.clear()

    def reload(self) -> None:
        with self._lock:
            self._cache.clear()

    def set_profile(self, profile: str) -> None:
        selected = self._validate_profile(profile)
        with self._lock:
            if self.profile != selected:
                self.profile = selected
                self._cache.clear()

    def _relative_path(self, prompt_id: str) -> Path:
        relative_path = self.prompt_files.get(prompt_id)
        if relative_path is None:
            raise PromptNotFoundError(f"Prompt 未注册: {prompt_id}")
        return Path(relative_path)

    def _default_path(self, prompt_id: str) -> Path:
        return self.root / self._relative_path(prompt_id)

    def _override_path(
        self,
        prompt_id: str,
        profile: str | None = None,
    ) -> Path | None:
        if self.override_root is None:
            return None
        selected = self._validate_profile(profile or self.profile)
        return self.override_root / selected / self._relative_path(prompt_id)

    def _effective_path(
        self,
        prompt_id: str,
        profile: str | None = None,
    ) -> tuple[Path, str]:
        override_path = self._override_path(prompt_id, profile)
        if override_path is not None and override_path.is_file():
            return override_path, "override"
        return self._default_path(prompt_id), "default"

    @staticmethod
    def _read(path: Path, prompt_id: str) -> tuple[os.stat_result, str]:
        try:
            stat = path.stat()
            content = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise PromptNotFoundError(
                f"Prompt 文件不存在: {prompt_id} ({path})"
            ) from exc
        except OSError as exc:
            raise PromptNotFoundError(
                f"Prompt 文件无法读取: {prompt_id} ({path})"
            ) from exc
        if not content:
            raise PromptNotFoundError(f"Prompt 内容为空: {prompt_id}")
        return stat, content

    def get(
        self,
        prompt_id: str,
        *,
        profile: str | None = None,
    ) -> str:
        with self._lock:
            selected = self._validate_profile(profile or self.profile)
            path, _source = self._effective_path(prompt_id, selected)
            stat = path.stat() if path.exists() else None
            cache_key = f"{selected}:{prompt_id}"
            cached = self._cache.get(cache_key)
            if (
                stat is not None
                and cached
                and cached[0] == str(path)
                and cached[1] == stat.st_mtime_ns
                and cached[2] == stat.st_size
            ):
                return cached[3]
            stat, content = self._read(path, prompt_id)
            self._cache[cache_key] = (
                str(path),
                stat.st_mtime_ns,
                stat.st_size,
                content,
            )
            return content

    def source(
        self,
        prompt_id: str,
        *,
        profile: str | None = None,
    ) -> str:
        with self._lock:
            return self._effective_path(prompt_id, profile)[1]

    @staticmethod
    def _variables_from_content(
        content: str,
        *,
        prompt_id: str,
    ) -> tuple[str, ...]:
        fields: list[str] = []
        try:
            parsed = string.Formatter().parse(content)
            for _literal, field_name, _format_spec, _conversion in parsed:
                if field_name and field_name not in fields:
                    fields.append(field_name)
        except ValueError as exc:
            raise PromptValidationError(
                f"{prompt_id} 模板语法无效: {exc}"
            ) from exc
        return tuple(fields)

    def variables(self, prompt_id: str) -> tuple[str, ...]:
        return self._variables_from_content(
            self.get(prompt_id),
            prompt_id=prompt_id,
        )

    def validate(self, prompt_id: str, content: str) -> str:
        self._relative_path(prompt_id)
        if not isinstance(content, str):
            raise PromptValidationError(f"{prompt_id} 内容必须是文本")
        normalized = content.strip()
        if not normalized:
            raise PromptValidationError(f"{prompt_id} 内容不能为空")
        if len(normalized.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise PromptValidationError(
                f"{prompt_id} 内容不能超过 {MAX_PROMPT_BYTES // 1024} KiB"
            )
        proposed = set(
            self._variables_from_content(normalized, prompt_id=prompt_id)
        )
        _stat, default_content = self._read(
            self._default_path(prompt_id),
            prompt_id,
        )
        required = set(
            self._variables_from_content(
                default_content,
                prompt_id=prompt_id,
            )
        )
        if proposed != required:
            missing = sorted(required - proposed)
            extra = sorted(proposed - required)
            details = []
            if missing:
                details.append(f"缺少 {', '.join(missing)}")
            if extra:
                details.append(f"新增 {', '.join(extra)}")
            raise PromptValidationError(
                f"{prompt_id} 模板变量必须与默认值一致："
                + "；".join(details)
            )
        return normalized

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(
            f".{path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temp_path.write_text(content, encoding="utf-8")
            os.replace(temp_path, path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _history_directory(self, prompt_id: str, profile: str) -> Path:
        self._relative_path(prompt_id)
        if self.history_root is None:
            raise PromptValidationError("Prompt 历史目录未配置")
        safe_prompt_id = prompt_id.replace(".", "__")
        return self.history_root / profile / safe_prompt_id

    def _snapshot(
        self,
        prompt_id: str,
        content: str,
        *,
        profile: str,
        reason: str,
    ) -> dict[str, Any]:
        timestamp_ns = time.time_ns()
        signature = hashlib.sha256(
            content.encode("utf-8")
        ).hexdigest()[:16]
        revision_id = f"{timestamp_ns}-{signature[:8]}"
        timestamp = datetime.now(timezone.utc).isoformat()
        snapshot = {
            "revision_id": revision_id,
            "timestamp": timestamp,
            "prompt_id": prompt_id,
            "profile": profile,
            "signature": signature,
            "reason": str(reason or "edit")[:128],
            "content": content,
        }
        path = (
            self._history_directory(prompt_id, profile)
            / f"{revision_id}.json"
        )
        self._atomic_write(
            path,
            json.dumps(
                snapshot,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        return snapshot

    def save_override(
        self,
        prompt_id: str,
        content: str,
        *,
        profile: str | None = None,
        reason: str = "edit",
    ) -> dict[str, Any]:
        with self._lock:
            selected = self._validate_profile(profile or self.profile)
            normalized = self.validate(prompt_id, content)
            override_path = self._override_path(prompt_id, selected)
            if override_path is None:
                raise PromptValidationError("Prompt 覆盖目录未配置")
            current = self.get(prompt_id, profile=selected)
            if normalized == current:
                return self.metadata(prompt_id, profile=selected)
            self._snapshot(
                prompt_id,
                current,
                profile=selected,
                reason=reason,
            )
            self._atomic_write(override_path, normalized + "\n")
            self._cache.pop(f"{selected}:{prompt_id}", None)
            return self.metadata(prompt_id, profile=selected)

    def history(
        self,
        prompt_id: str,
        *,
        profile: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            selected = self._validate_profile(profile or self.profile)
            directory = self._history_directory(prompt_id, selected)
            if not directory.exists():
                return []
            revisions = []
            for path in sorted(
                directory.glob("*.json"),
                key=lambda item: item.name,
                reverse=True,
            ):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if (
                    isinstance(payload, dict)
                    and payload.get("prompt_id") == prompt_id
                    and payload.get("profile") == selected
                ):
                    revisions.append({
                        key: payload.get(key)
                        for key in (
                            "revision_id",
                            "timestamp",
                            "prompt_id",
                            "profile",
                            "signature",
                            "reason",
                        )
                    })
            return revisions

    def _load_revision(
        self,
        prompt_id: str,
        revision_id: str,
        *,
        profile: str,
    ) -> dict[str, Any]:
        if not REVISION_PATTERN.fullmatch(str(revision_id or "")):
            raise PromptValidationError("Prompt revision ID 格式无效")
        path = (
            self._history_directory(prompt_id, profile)
            / f"{revision_id}.json"
        )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise PromptValidationError(
                f"Prompt 历史版本不存在: {revision_id}"
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise PromptValidationError(
                f"Prompt 历史版本无法读取: {revision_id}"
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("prompt_id") != prompt_id
            or payload.get("profile") != profile
            or not isinstance(payload.get("content"), str)
        ):
            raise PromptValidationError("Prompt 历史版本内容无效")
        return payload

    def rollback(
        self,
        prompt_id: str,
        revision_id: str,
        *,
        profile: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            selected = self._validate_profile(profile or self.profile)
            payload = self._load_revision(
                prompt_id,
                revision_id,
                profile=selected,
            )
            return self.save_override(
                prompt_id,
                payload["content"],
                profile=selected,
                reason="rollback",
            )

    def reset_override(
        self,
        prompt_id: str,
        *,
        profile: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            selected = self._validate_profile(profile or self.profile)
            override_path = self._override_path(prompt_id, selected)
            if override_path is None:
                raise PromptValidationError("Prompt 覆盖目录未配置")
            if override_path.is_file():
                current = self.get(prompt_id, profile=selected)
                self._snapshot(
                    prompt_id,
                    current,
                    profile=selected,
                    reason="reset",
                )
                try:
                    override_path.unlink()
                except OSError as exc:
                    raise PromptValidationError(
                        f"Prompt 覆盖无法移除: {exc}"
                    ) from exc
                self._cache.pop(f"{selected}:{prompt_id}", None)
            return self.metadata(prompt_id, profile=selected)

    def metadata(
        self,
        prompt_id: str,
        *,
        profile: str | None = None,
        include_content: bool = False,
    ) -> dict[str, Any]:
        selected = self._validate_profile(profile or self.profile)
        content = self.get(prompt_id, profile=selected)
        result: dict[str, Any] = {
            "id": prompt_id,
            "profile": selected,
            "source": self.source(prompt_id, profile=selected),
            "signature": hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest()[:16],
            "variables": list(
                self._variables_from_content(
                    content,
                    prompt_id=prompt_id,
                )
            ),
        }
        if include_content:
            result["content"] = content
        return result

    def list_prompts(
        self,
        *,
        profile: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            self.metadata(prompt_id, profile=profile)
            for prompt_id in sorted(self.prompt_files)
        ]

    def render(self, prompt_id: str, **values: object) -> str:
        template = self.get(prompt_id)
        missing = [
            name for name in self.variables(prompt_id)
            if name not in values
        ]
        if missing:
            raise PromptRenderError(
                f"{prompt_id} 缺少模板变量: {', '.join(missing)}"
            )
        try:
            return template.format_map(values)
        except (KeyError, ValueError, IndexError) as exc:
            raise PromptRenderError(
                f"{prompt_id} 渲染失败: {exc}"
            ) from exc

    def signature(self, *prompt_ids: str) -> str:
        selected = prompt_ids or tuple(sorted(self.prompt_files))
        payload = {
            prompt_id: self.get(prompt_id)
            for prompt_id in selected
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]


_registry = PromptRegistry()


def _runtime_settings() -> tuple[Path, Path, str]:
    data_root = Path(
        os.environ.get("CROSSPILOT_DATA_DIR", str(DEFAULT_DATA_ROOT))
    )
    override_root = Path(
        os.environ.get(
            "CROSSPILOT_PROMPT_DIR",
            str(data_root / "prompts"),
        )
    )
    history_root = Path(
        os.environ.get(
            "CROSSPILOT_PROMPT_HISTORY_DIR",
            str(data_root / "prompt_history"),
        )
    )
    profile = os.environ.get("CROSSPILOT_PROMPT_PROFILE")
    if not profile:
        try:
            from crosspilot.config import load_config

            profile = load_config().get("PROMPT_PROFILE")
        except (ImportError, AttributeError):
            profile = None
    return override_root, history_root, profile or "production"


def get_prompt_registry() -> PromptRegistry:
    override_root, history_root, profile = _runtime_settings()
    _registry.configure(
        override_root=override_root,
        history_root=history_root,
        profile=profile,
    )
    return _registry


def reload_prompt_registry() -> PromptRegistry:
    registry = get_prompt_registry()
    registry.reload()
    return registry


def build_runtime_signature(
    policy_version: str,
    *prompt_ids: str,
) -> str:
    """Hash policy, Prompt content, and active model routing together."""
    effective_models = {}
    try:
        from crosspilot.config import load_config

        cfg = load_config()
        effective_models = {
            key: value
            for key, value in cfg.items()
            if (
                key == "MODEL_PROFILE"
                or key == "PROMPT_PROFILE"
                or key.endswith("_PROVIDER")
                or key.endswith("_MODEL")
                or key.endswith("_BASE_URL")
            )
        }
    except ImportError:
        pass
    payload = {
        "policy": str(policy_version),
        "model": model_signature(),
        "effective_models": effective_models,
        "prompts": get_prompt_registry().signature(*prompt_ids),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]
