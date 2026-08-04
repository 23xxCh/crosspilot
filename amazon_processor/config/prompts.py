"""Direct Prompt loading, rendering, and cache signatures."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import string
import threading

from .models import model_signature


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROMPT_ROOT = PROJECT_ROOT / "config" / "prompts"
PROMPT_MANIFEST = PROMPT_ROOT / "manifest.json"


class PromptError(ValueError):
    """Prompt lookup or rendering failed."""


@dataclass(frozen=True)
class PromptSpec:
    prompt_id: str
    label: str
    category: str
    path: str
    variables: tuple[str, ...]
    used_by: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.prompt_id,
            "label": self.label,
            "category": self.category,
            "path": self.path,
            "variables": list(self.variables),
            "used_by": self.used_by,
        }


class PromptRegistry:
    """A small read-only Interface over editable Prompt text files."""

    def __init__(
        self,
        root: Path = PROMPT_ROOT,
        manifest_path: Path | None = None,
    ) -> None:
        self.root = Path(root)
        self.manifest_path = (
            Path(manifest_path)
            if manifest_path is not None
            else self.root / "manifest.json"
        )
        self._cache: dict[str, tuple[int, int, str]] = {}
        self._manifest_cache: tuple[int, int, dict[str, PromptSpec]] | None = None
        self._lock = threading.RLock()

    def _load_specs(self) -> dict[str, PromptSpec]:
        try:
            stat = self.manifest_path.stat()
        except OSError as exc:
            raise PromptError(
                f"Prompt manifest 不存在: {self.manifest_path}"
            ) from exc
        with self._lock:
            if (
                self._manifest_cache
                and self._manifest_cache[:2]
                == (stat.st_mtime_ns, stat.st_size)
            ):
                return dict(self._manifest_cache[2])
            try:
                payload = json.loads(
                    self.manifest_path.read_text(encoding="utf-8-sig")
                )
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise PromptError(f"Prompt manifest 无法读取: {exc}") from exc
            rows = payload.get("prompts") if isinstance(payload, dict) else None
            version = payload.get("version") if isinstance(payload, dict) else None
            if version != 1 or not isinstance(rows, list):
                raise PromptError("Prompt manifest 必须是 version 1")
            specs: dict[str, PromptSpec] = {}
            for index, row in enumerate(rows):
                location = f"prompts[{index}]"
                if not isinstance(row, dict):
                    raise PromptError(f"{location} 必须是对象")
                values = {}
                for name in ("id", "label", "category", "path", "used_by"):
                    value = row.get(name)
                    if not isinstance(value, str) or not value.strip():
                        raise PromptError(f"{location}.{name} 不能为空")
                    values[name] = value.strip()
                prompt_id = values.pop("id")
                if prompt_id in specs:
                    raise PromptError(f"Prompt ID 重复: {prompt_id}")
                variables = row.get("variables")
                if (
                    not isinstance(variables, list)
                    or any(
                        not isinstance(item, str) or not item.strip()
                        for item in variables
                    )
                ):
                    raise PromptError(f"{location}.variables 必须是字符串数组")
                normalized_variables = tuple(item.strip() for item in variables)
                if len(normalized_variables) != len(set(normalized_variables)):
                    raise PromptError(f"{location}.variables 不能重复")
                relative = Path(values["path"])
                full_path = (self.root / relative).resolve()
                try:
                    full_path.relative_to(self.root.resolve())
                except ValueError as exc:
                    raise PromptError(
                        f"{location}.path 不能离开 Prompt 目录"
                    ) from exc
                if full_path.suffix.lower() != ".txt":
                    raise PromptError(f"{location}.path 必须指向 .txt 文件")
                specs[prompt_id] = PromptSpec(
                    prompt_id=prompt_id,
                    variables=normalized_variables,
                    **values,
                )
            if not specs:
                raise PromptError("Prompt manifest 不能为空")
            self._manifest_cache = (
                stat.st_mtime_ns,
                stat.st_size,
                specs,
            )
            return dict(specs)

    def specs(self) -> tuple[PromptSpec, ...]:
        return tuple(self._load_specs().values())

    def spec(self, prompt_id: str) -> PromptSpec:
        try:
            return self._load_specs()[prompt_id]
        except KeyError as exc:
            raise PromptError(f"Prompt 未注册: {prompt_id}") from exc

    def _path(self, prompt_id: str) -> Path:
        return self.root / self.spec(prompt_id).path

    @staticmethod
    def _template_variables(content: str, prompt_id: str) -> tuple[str, ...]:
        names = []
        try:
            parsed = string.Formatter().parse(content)
            for _literal, field, _format, _conversion in parsed:
                if field and field not in names:
                    names.append(field)
        except ValueError as exc:
            raise PromptError(f"Prompt 模板语法错误: {prompt_id}") from exc
        return tuple(names)

    def get(self, prompt_id: str) -> str:
        path = self._path(prompt_id)
        try:
            stat = path.stat()
        except OSError as exc:
            raise PromptError(f"Prompt 文件不存在: {path}") from exc
        with self._lock:
            cached = self._cache.get(prompt_id)
            if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
                return cached[2]
            try:
                content = path.read_text(encoding="utf-8-sig").strip()
            except (OSError, UnicodeError) as exc:
                raise PromptError(f"Prompt 文件无法读取: {path}") from exc
            if not content:
                raise PromptError(f"Prompt 内容为空: {prompt_id}")
            expected = self.spec(prompt_id).variables
            if expected:
                actual = self._template_variables(content, prompt_id)
                missing = [name for name in expected if name not in actual]
                unknown = [name for name in actual if name not in expected]
                if missing or unknown:
                    details = []
                    if missing:
                        details.append(f"缺少变量 {', '.join(missing)}")
                    if unknown:
                        details.append(f"未知变量 {', '.join(unknown)}")
                    raise PromptError(
                        f"{prompt_id} 模板变量不匹配: {'；'.join(details)}"
                    )
            self._cache[prompt_id] = (
                stat.st_mtime_ns,
                stat.st_size,
                content,
            )
            return content

    def variables(self, prompt_id: str) -> tuple[str, ...]:
        self.get(prompt_id)
        return self.spec(prompt_id).variables

    def render(self, prompt_id: str, **values: object) -> str:
        variables = self.variables(prompt_id)
        missing = [field for field in variables if field not in values]
        if missing:
            raise PromptError(
                f"{prompt_id} 缺少模板变量: {', '.join(missing)}"
            )
        content = self.get(prompt_id)
        if not variables:
            return content
        try:
            return content.format_map(values)
        except (KeyError, ValueError, IndexError) as exc:
            raise PromptError(f"{prompt_id} 渲染失败: {exc}") from exc

    def signature(self, *prompt_ids: str) -> str:
        selected = prompt_ids or tuple(
            sorted(spec.prompt_id for spec in self.specs())
        )
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

    def reload(self) -> None:
        with self._lock:
            self._cache.clear()
            self._manifest_cache = None


_registry = PromptRegistry()


def get_prompt_registry() -> PromptRegistry:
    return _registry


def reload_prompt_registry() -> PromptRegistry:
    _registry.reload()
    return _registry


def build_runtime_signature(
    policy_version: str,
    *prompt_ids: str,
) -> str:
    """Preserve cache compatibility by hashing the same effective inputs."""
    from .env import load_config

    effective_models = {
        key: value
        for key, value in load_config().items()
        if (
            key == "MODEL_PROFILE"
            or key == "PROMPT_PROFILE"
            or key.endswith("_PROVIDER")
            or key.endswith("_MODEL")
            or key.endswith("_BASE_URL")
        )
    }
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


__all__ = [
    "PROMPT_MANIFEST",
    "PromptError",
    "PromptRegistry",
    "PromptSpec",
    "build_runtime_signature",
    "get_prompt_registry",
    "reload_prompt_registry",
]
