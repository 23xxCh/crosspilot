"""Small operation router for official DeepSeek text and vision models."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from .deepseek import DeepSeekProvider
from .support import (
    ModelProvider,
    ProviderAuthError,
    ProviderError,
    ProviderQuotaError,
)


class CompositeProvider(ModelProvider):
    """Route text and vision operations with bounded configured fallbacks."""

    def __init__(self, config: dict[str, Any]) -> None:
        routes = config.get("routes")
        if not isinstance(routes, dict):
            raise ValueError("模型配置缺少 routes")
        self._routes = {
            operation: [
                self._build_route_provider(operation, item)
                for item in self._require_routes(routes, operation)
            ]
            for operation in ("text", "vision")
        }
        self._providers = {
            operation: providers[0]
            for operation, providers in self._routes.items()
        }
        self._text_fallbacks = self._routes["text"][1:]
        self._vision_fallbacks = self._routes["vision"][1:]
        self._metrics_lock = threading.Lock()
        self._metrics: dict[str, Any] = {
            "api_calls": 0,
            "api_errors": 0,
            "latency_s": 0.0,
            "http_attempts": 0,
            "http_errors": 0,
            "http_retries": 0,
            "http_status": {},
            "circuit_open": 0,
            "fallback_attempts": 0,
            "fallback_successes": 0,
            "fallback_failures": 0,
            "fallback_routes": {},
            "by_operation": {},
        }
        for provider in {
            id(item): item
            for items in self._routes.values()
            for item in items
        }.values():
            provider.set_attempt_hook(self._record_attempt)

    @staticmethod
    def _require_routes(routes: dict[str, Any], operation: str) -> list[dict]:
        values = routes.get(operation)
        if not isinstance(values, list) or not values:
            raise ValueError(f"模型线路缺少 {operation}")
        return values

    @staticmethod
    def _build_route_provider(
        operation: str,
        route: dict[str, Any],
    ) -> ModelProvider:
        provider_name = str(route.get("provider") or "").strip().lower()
        if provider_name != "deepseek":
            raise ValueError(f"{operation} 只支持官方 DeepSeek")
        api_key = str(route.get("api_key") or "")
        credential = str(route.get("credential") or "")
        if not api_key:
            raise ValueError(
                f"{operation} 线路凭据未配置: {credential or '<empty>'}"
            )
        base_url = str(route.get("base_url") or "")
        model = str(route.get("model") or "")
        if operation == "text":
            return DeepSeekProvider(
                api_key,
                base_url=base_url,
                model=model,
                fallback_model="",
            )
        return DeepSeekProvider(
            api_key,
            base_url=base_url,
            vision_base_url=base_url,
            vision_model=model,
        )

    def _record_attempt(
        self,
        *,
        operation: str,
        provider: str,
        status_code: int | None,
        ok: bool,
        retry: bool,
        error: str | None,
        rate_wait_s: float = 0.0,
    ) -> None:
        del provider, rate_wait_s
        with self._metrics_lock:
            self._metrics["http_attempts"] += 1
            if retry:
                self._metrics["http_retries"] += 1
            if not ok:
                self._metrics["http_errors"] += 1
            if status_code is not None:
                key = str(status_code)
                status = self._metrics["http_status"]
                status[key] = int(status.get(key, 0)) + 1
            entry = self._metrics["by_operation"].setdefault(
                operation,
                {"calls": 0, "errors": 0, "retries": 0},
            )
            entry["calls"] += 1
            entry["errors"] += int(not ok)
            entry["retries"] += int(retry)
            if error:
                entry["last_error"] = error

    def _call_routes(
        self,
        operation: str,
        method_name: str,
        *args: object,
        **kwargs: object,
    ) -> Any:
        started_at = time.monotonic()
        last_error: ProviderError | None = None
        for index, provider in enumerate(self._routes[operation]):
            if index:
                with self._metrics_lock:
                    self._metrics["fallback_attempts"] += 1
            method: Callable[..., Any] = getattr(provider, method_name)
            try:
                result = method(*args, **kwargs)
                if result is None:
                    continue
            except (ProviderAuthError, ProviderQuotaError):
                raise
            except ProviderError as exc:
                last_error = exc
                continue
            if index:
                with self._metrics_lock:
                    self._metrics["fallback_successes"] += 1
            with self._metrics_lock:
                self._metrics["api_calls"] += 1
                self._metrics["latency_s"] += time.monotonic() - started_at
            return result
        with self._metrics_lock:
            self._metrics["api_calls"] += 1
            self._metrics["api_errors"] += 1
            self._metrics["latency_s"] += time.monotonic() - started_at
            if len(self._routes[operation]) > 1:
                self._metrics["fallback_failures"] += 1
        if last_error is not None:
            raise last_error
        return None

    def call_text(
        self,
        prompt: str,
        max_tokens: int = 2048,
    ) -> Optional[str]:
        return self._call_routes(
            "text",
            "call_text",
            prompt,
            max_tokens=max_tokens,
        )

    def call_vision(self, image_url: str) -> Optional[bool]:
        return self._call_routes("vision", "call_vision", image_url)

    def assess_image(
        self,
        image_url: str,
        *,
        confirmation: bool = False,
        policy: str = "general",
    ) -> Optional[dict[str, Any]]:
        return self._call_routes(
            "vision",
            "assess_image",
            image_url,
            confirmation=confirmation,
            policy=policy,
        )

    def assess_images(
        self,
        image_urls: list[str],
        *,
        policy: str = "general",
    ) -> list[dict[str, Any]]:
        result = self._call_routes(
            "vision",
            "assess_images",
            image_urls,
            policy=policy,
        )
        return list(result or [])

    def metrics_snapshot(self) -> dict[str, Any]:
        with self._metrics_lock:
            return {
                key: dict(value) if isinstance(value, dict) else value
                for key, value in self._metrics.items()
            }
