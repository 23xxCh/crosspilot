"""Direct Prompt loading, rendering, and cache signatures."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import string
import threading

from .models import model_signature


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROMPT_ROOT = PROJECT_ROOT / "config" / "prompts"
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
}


class PromptError(ValueError):
    """Prompt lookup or rendering failed."""


class PromptRegistry:
    """A small read-only Interface over editable Prompt text files."""

    def __init__(self, root: Path = PROMPT_ROOT) -> None:
        self.root = root
        self._cache: dict[str, tuple[int, int, str]] = {}
        self._lock = threading.RLock()

    def _path(self, prompt_id: str) -> Path:
        relative = PROMPT_FILES.get(prompt_id)
        if relative is None:
            raise PromptError(f"Prompt 未注册: {prompt_id}")
        return self.root / relative

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
            self._cache[prompt_id] = (
                stat.st_mtime_ns,
                stat.st_size,
                content,
            )
            return content

    def variables(self, prompt_id: str) -> tuple[str, ...]:
        names = []
        try:
            parsed = string.Formatter().parse(self.get(prompt_id))
            for _literal, field, _format, _conversion in parsed:
                if field and field not in names:
                    names.append(field)
        except ValueError as exc:
            raise PromptError(f"Prompt 模板语法错误: {prompt_id}") from exc
        return tuple(names)

    def render(self, prompt_id: str, **values: object) -> str:
        missing = [
            field for field in self.variables(prompt_id)
            if field not in values
        ]
        if missing:
            raise PromptError(
                f"{prompt_id} 缺少模板变量: {', '.join(missing)}"
            )
        try:
            return self.get(prompt_id).format_map(values)
        except (KeyError, ValueError, IndexError) as exc:
            raise PromptError(f"{prompt_id} 渲染失败: {exc}") from exc

    def signature(self, *prompt_ids: str) -> str:
        selected = prompt_ids or tuple(sorted(PROMPT_FILES))
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
    "PROMPT_FILES",
    "PromptError",
    "PromptRegistry",
    "build_runtime_signature",
    "get_prompt_registry",
    "reload_prompt_registry",
]
