"""Provider contracts and safe, structured operational failures."""
from __future__ import annotations

from abc import ABC, abstractmethod
import re
from typing import Any, Optional

_QUOTA_MARKERS = (
    "quota",
    "insufficient",
    "balance",
    "billing",
    "payment required",
    "exceeded",
    "credit",
    "out of credits",
    "额度",
    "余额",
    "欠费",
    "用尽",
    "不足",
)
_SECRET_PATTERN = re.compile(
    r"(?i)(bearer\s+|(?:sk|cpk)-)[A-Za-z0-9._:/+-]+"
)


def _redact_excerpt(value: str) -> str:
    return _SECRET_PATTERN.sub(
        lambda match: f"{match.group(1)}***",
        str(value or ""),
    )[:200]


class ProviderError(RuntimeError):
    """Base error containing only safe operational context."""

    retryable = False

    def __init__(
        self,
        detail: str,
        *,
        provider: str = "unknown",
        operation: str = "unknown",
        status_code: int | None = None,
        retryable: bool | None = None,
        response_excerpt: str = "",
    ) -> None:
        safe_detail = str(detail or type(self).__name__)[:300]
        super().__init__(safe_detail)
        self.detail = safe_detail
        self.provider = str(provider or "unknown")[:64]
        self.operation = str(operation or "unknown")[:64]
        self.status_code = status_code
        if retryable is not None:
            self.retryable = bool(retryable)
        self.response_excerpt = _redact_excerpt(response_excerpt)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "provider": self.provider,
            "operation": self.operation,
            "status_code": self.status_code,
            "retryable": self.retryable,
            "detail": self.detail,
            "response_excerpt": self.response_excerpt,
        }


class ProviderAuthError(ProviderError):
    """Credentials are missing, invalid, or unauthorized."""


class ProviderQuotaError(ProviderError):
    """Balance or quota prevents further useful retries."""


class ProviderRateLimitError(ProviderError):
    """The provider temporarily rejected request volume."""

    retryable = True


class ProviderTimeoutError(ProviderError):
    """The provider request exceeded its timeout."""

    retryable = True


class ProviderUnavailableError(ProviderError):
    """The provider or network is temporarily unavailable."""

    retryable = True


class ProviderCircuitOpenError(ProviderUnavailableError):
    """Compatibility name for a temporary logical circuit skip."""


class ProviderResponseError(ProviderError):
    """A successful response did not match the API contract."""


def contains_quota_marker(body: str = "") -> bool:
    text = str(body or "").lower()
    return any(marker in text for marker in _QUOTA_MARKERS)


def classify_http_error(
    provider: str,
    operation: str,
    status_code: int | None,
    body: str = "",
) -> ProviderError:
    """Convert one failed HTTP response into a stable error category."""
    common = {
        "provider": provider,
        "operation": operation,
        "status_code": status_code,
        "response_excerpt": _redact_excerpt(body),
    }
    if status_code == 402 or contains_quota_marker(body):
        return ProviderQuotaError("额度或余额不可用", **common)
    if status_code in (401, 403):
        return ProviderAuthError("API 鉴权失败", **common)
    if status_code == 429:
        return ProviderRateLimitError("API 请求过于频繁", **common)
    if status_code in (408, 504):
        return ProviderTimeoutError("API 请求超时", **common)
    if status_code is not None and status_code >= 500:
        return ProviderUnavailableError("上游服务暂时不可用", **common)
    return ProviderResponseError("API 返回非成功状态", **common)


def is_quota_error(status_code: int | None, body: str = "") -> bool:
    return status_code == 402 or contains_quota_marker(body)


class ModelProvider(ABC):
    """Text and vision operations used by the Amazon pipeline."""

    def set_attempt_hook(self, hook) -> None:
        self._attempt_hook = hook

    def set_circuit_hook(self, hook) -> None:
        self._circuit_hook = hook

    def _record_attempt(
        self,
        operation,
        provider,
        status_code=None,
        ok=False,
        retry=False,
        error=None,
        rate_wait_s=0.0,
    ) -> None:
        hook = getattr(self, "_attempt_hook", None)
        if not hook:
            return
        try:
            hook(
                operation=operation,
                provider=provider,
                status_code=status_code,
                ok=ok,
                retry=retry,
                error=type(error).__name__ if error else None,
                rate_wait_s=rate_wait_s,
            )
        except Exception:
            return

    @abstractmethod
    def call_text(
        self,
        prompt: str,
        max_tokens: int = 2048,
    ) -> Optional[str]:
        raise NotImplementedError

    @abstractmethod
    def call_vision(self, image_url: str) -> Optional[bool]:
        raise NotImplementedError

    def assess_image(
        self,
        image_url: str,
        *,
        confirmation: bool = False,
        policy: str = "general",
    ) -> Optional[dict[str, Any]]:
        from ..images.risk import assessment_from_legacy

        del confirmation
        if policy != "general":
            return None
        return assessment_from_legacy(self.call_vision(image_url))

    def assess_images(
        self,
        image_urls: list[str],
        *,
        policy: str = "general",
    ) -> list[dict[str, Any] | None]:
        return [
            self.assess_image(url, policy=policy)
            for url in image_urls
        ]
