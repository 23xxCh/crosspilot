"""DeepSeek text provider."""
from __future__ import annotations

import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

from ..config.models import get_model_registry
from ..config.prompts import get_prompt_registry

from .support import ModelProvider
from .support import (
    ProviderAuthError,
    ProviderError,
    ProviderQuotaError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    classify_http_error,
)


class DeepSeekProvider(ModelProvider):
    """DeepSeek-compatible chat completion client."""

    _DEFAULTS = get_model_registry().as_config()
    BASE_URL = _DEFAULTS["DEEPSEEK_BASE_URL"]
    MODEL = _DEFAULTS["DEEPSEEK_TEXT_MODEL"]
    FALLBACK_MODEL = _DEFAULTS.get(
        "DEEPSEEK_TEXT_FALLBACK_MODEL",
        "",
    )

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        model: str | None = None,
        fallback_model: str | None = None,
    ) -> None:
        self.BASE_URL = str(base_url or self.BASE_URL).rstrip("/")
        self.MODEL = str(model or self.MODEL).strip()
        self.FALLBACK_MODEL = str(
            fallback_model
            if fallback_model is not None
            else self.FALLBACK_MODEL
        ).strip()
        self.SYSTEM_PROMPT = get_prompt_registry().get(
            "system.product_listing_optimizer"
        )
        self._session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=100,
            pool_maxsize=100,
            max_retries=0,
        )
        self._session.mount("https://", adapter)
        self._session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    def _request_error(
        self,
        exc: Exception,
        *,
        operation: str,
    ) -> ProviderError:
        if isinstance(exc, requests.Timeout):
            return ProviderTimeoutError(
                "DeepSeek 请求超时",
                provider="deepseek",
                operation=operation,
            )
        return ProviderUnavailableError(
            "DeepSeek 网络请求失败",
            provider="deepseek",
            operation=operation,
        )

    def call_text(
        self,
        prompt: str,
        max_tokens: int = 2048,
        retries: int = 3,
    ) -> Optional[str]:
        """Call primary then fallback model, with typed terminal errors."""
        models = [self.MODEL]
        if self.FALLBACK_MODEL and self.FALLBACK_MODEL != self.MODEL:
            models.append(self.FALLBACK_MODEL)
        retries = max(1, int(retries or 1))
        attempt_number = 0
        last_error: ProviderError | None = None

        for model in models:
            for attempt in range(retries):
                is_retry = attempt_number > 0
                attempt_number += 1
                try:
                    response = self._session.post(
                        f"{self.BASE_URL}/v1/chat/completions",
                        json={
                            "model": model,
                            "messages": [
                                {
                                    "role": "system",
                                    "content": self.SYSTEM_PROMPT,
                                },
                                {"role": "user", "content": prompt},
                            ],
                            "max_tokens": max_tokens,
                            "thinking": {"type": "disabled"},
                        },
                        timeout=60,
                    )
                except (requests.Timeout, requests.RequestException) as exc:
                    last_error = self._request_error(
                        exc,
                        operation="text",
                    )
                    self._record_attempt(
                        "text",
                        "deepseek",
                        None,
                        False,
                        retry=is_retry,
                        error=last_error,
                    )
                else:
                    if response.ok:
                        try:
                            message = (
                                response.json()
                                .get("choices", [{}])[0]
                                .get("message", {})
                            )
                            content = (
                                message.get("content", "")
                                or message.get("reasoning_content", "")
                            )
                        except (AttributeError, IndexError, TypeError, ValueError):
                            content = ""
                        if content:
                            self._record_attempt(
                                "text",
                                "deepseek",
                                response.status_code,
                                True,
                                retry=is_retry,
                            )
                            return content
                        last_error = ProviderResponseError(
                            "DeepSeek 成功响应缺少文本内容",
                            provider="deepseek",
                            operation="text",
                            status_code=response.status_code,
                        )
                        self._record_attempt(
                            "text",
                            "deepseek",
                            response.status_code,
                            False,
                            retry=is_retry,
                            error=last_error,
                        )
                    else:
                        last_error = classify_http_error(
                            "deepseek",
                            "text",
                            response.status_code,
                            response.text,
                        )
                        self._record_attempt(
                            "text",
                            "deepseek",
                            response.status_code,
                            False,
                            retry=is_retry,
                            error=last_error,
                        )
                        if isinstance(
                            last_error,
                            (ProviderAuthError, ProviderQuotaError),
                        ):
                            raise last_error

                if attempt < retries - 1:
                    if getattr(last_error, "status_code", None) == 429:
                        time.sleep(10 * (attempt + 1))
                    else:
                        time.sleep(2)

        if last_error is not None:
            raise last_error
        return None

    def call_vision(self, image_url: str) -> Optional[bool]:
        return None

    def call_image_gen(
        self,
        image_url: str,
        size: str = "1024x1024",
        is_variant: bool = False,
        context: str = "",
        route_offset: int = 0,
        reference_free: bool = False,
    ) -> Optional[str]:
        del reference_free
        return None
