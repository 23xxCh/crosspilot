"""Named API credentials backed by the local ignored .env file."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping

from .models import ModelRegistry


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


def read_env_values(path: Path = DEFAULT_ENV_PATH) -> dict[str, str]:
    """Read simple KEY=value pairs without logging or exposing their source text."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"API 密钥文件无法读取: {path}") from exc
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def masked_value(value: str) -> str:
    """Return a non-reversible display value."""
    text = str(value or "")
    if not text:
        return ""
    tail = text[-4:] if len(text) >= 4 else text
    return f"••••{tail}"


@dataclass(frozen=True)
class CredentialState:
    credential_id: str
    label: str
    env: str
    configured: bool
    source: str
    masked: str

    def as_public_dict(self) -> dict[str, str | bool]:
        return {
            "id": self.credential_id,
            "label": self.label,
            "env": self.env,
            "configured": self.configured,
            "source": self.source,
            "masked": self.masked,
        }


class CredentialStore:
    """Resolve route credentials without hard-coding provider names."""

    def __init__(
        self,
        registry: ModelRegistry,
        *,
        env_path: Path = DEFAULT_ENV_PATH,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.registry = registry
        self.env_path = Path(env_path)
        self.environ = os.environ if environ is None else environ
        self._file_values = read_env_values(self.env_path)

    def value(self, credential_id: str) -> str:
        definition = self.registry.credential(credential_id)
        system_value = str(self.environ.get(definition.env) or "")
        if system_value:
            return system_value
        return str(self._file_values.get(definition.env) or "")

    def values_by_env(self) -> dict[str, str]:
        return {
            definition.env: self.value(definition.credential_id)
            for definition in self.registry.credentials()
        }

    def state(self, credential_id: str) -> CredentialState:
        definition = self.registry.credential(credential_id)
        system_value = str(self.environ.get(definition.env) or "")
        file_value = str(self._file_values.get(definition.env) or "")
        value = system_value or file_value
        source = "system" if system_value else ("env_file" if file_value else "missing")
        return CredentialState(
            credential_id=definition.credential_id,
            label=definition.label,
            env=definition.env,
            configured=bool(value),
            source=source,
            masked=masked_value(value),
        )

    def public_states(self) -> list[dict[str, str | bool]]:
        return [
            self.state(definition.credential_id).as_public_dict()
            for definition in self.registry.credentials()
        ]

    def missing_routes(self) -> list[str]:
        missing: list[str] = []
        for operation in ("text", "vision", "image"):
            for index, target in enumerate(self.registry.routes(operation)):
                if not self.value(target.credential):
                    route = "主线路" if index == 0 else f"备用线路 {index}"
                    missing.append(
                        f"{operation} {route}（{target.credential}）"
                    )
        return missing


__all__ = [
    "CredentialState",
    "CredentialStore",
    "DEFAULT_ENV_PATH",
    "masked_value",
    "read_env_values",
]
