"""Provider abstract interface and redacted attempt instrumentation."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from crosspilot.image_risk import assessment_from_legacy


class ModelProvider(ABC):
    """Common provider operations used by CompositeProvider."""

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
    ):
        hook = getattr(self, '_attempt_hook', None)
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
            # Observability must never change the provider result.
            return

    def _record_circuit_skip(self, operation, provider) -> None:
        hook = getattr(self, '_circuit_hook', None)
        if not hook:
            return
        try:
            hook(operation=operation, provider=provider)
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
    ) -> Optional[dict[str, Any]]:
        """Return a structured image-risk result when supported."""
        del confirmation
        return assessment_from_legacy(self.call_vision(image_url))

    @abstractmethod
    def call_image_gen(
        self,
        image_url: str,
        size: str = "1024x1024",
        is_variant: bool = False,
        context: str = "",
        route_offset: int = 0,
    ) -> Optional[str]:
        raise NotImplementedError
