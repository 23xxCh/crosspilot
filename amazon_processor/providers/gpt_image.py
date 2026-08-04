"""GPT Image provider."""
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


class GPTImageProvider(ModelProvider):
    """GPT Image client using an OpenAI-compatible image endpoint."""

    _DEFAULTS = get_model_registry().as_config()
    BASE_URL = _DEFAULTS["GPT_IMAGE_BASE_URL"]
    IMAGE_MODEL = _DEFAULTS["GPT_IMAGE_MODEL"]
    _PROMPTS = get_prompt_registry()
    MAIN_PROMPT = _PROMPTS.get("images.main_product")
    VAR_PROMPT = _PROMPTS.get("images.variant")

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        image_model: str | None = None,
    ) -> None:
        self.BASE_URL = str(base_url or self.BASE_URL).rstrip("/")
        self.IMAGE_MODEL = str(image_model or self.IMAGE_MODEL).strip()
        prompts = get_prompt_registry()
        self.MAIN_PROMPT = prompts.get("images.main_product")
        self.MAIN_REFERENCE_FREE_PROMPT = prompts.get(
            "images.main_product_reference_free"
        )
        self.VAR_PROMPT = prompts.get("images.variant")
        self.LISTING_CONTEXT_PROMPT = prompts.get("images.listing_context")
        self._session = requests.Session()
        self._session.mount(
            "https://",
            HTTPAdapter(
                pool_connections=10,
                pool_maxsize=10,
                max_retries=0,
            ),
        )
        self._session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    def call_text(
        self,
        prompt: str,
        max_tokens: int = 2048,
    ) -> Optional[str]:
        return None

    def call_vision(self, image_url: str) -> Optional[bool]:
        return None

    def call_image_gen(
        self,
        image_url: str,
        size: str = "1024x1024",
        retries: int = 1,
        is_variant: bool = False,
        context: str = "",
        route_offset: int = 0,
        reference_free: bool = False,
    ) -> Optional[str]:
        del route_offset
        prompt = (
            self.MAIN_REFERENCE_FREE_PROMPT
            if reference_free and not is_variant
            else (self.VAR_PROMPT if is_variant else self.MAIN_PROMPT)
        )
        context_text = str(context or "").strip()[:1400]
        if context_text:
            prompt += "\n\n" + get_prompt_registry().render(
                "images.listing_context",
                context=context_text,
            )
        retries = max(1, int(retries or 1))
        last_error: ProviderError | None = None
        for attempt in range(retries):
            try:
                response = self._session.post(
                    f"{self.BASE_URL}/v1/images/generations",
                    json={
                        "model": self.IMAGE_MODEL,
                        "prompt": prompt,
                        "size": size,
                        **(
                            {}
                            if reference_free
                            else {"reference_images": [image_url]}
                        ),
                        "n": 1,
                    },
                    timeout=90,
                )
            except (requests.Timeout, requests.RequestException) as exc:
                if isinstance(exc, requests.Timeout):
                    last_error = ProviderTimeoutError(
                        "GPT Image 请求超时",
                        provider="gpt",
                        operation="image_gen",
                    )
                else:
                    last_error = ProviderUnavailableError(
                        "GPT Image 网络请求失败",
                        provider="gpt",
                        operation="image_gen",
                    )
                self._record_attempt(
                    "image_gen",
                    "gpt",
                    None,
                    False,
                    retry=attempt > 0,
                    error=last_error,
                )
            else:
                if response.ok:
                    try:
                        data = response.json().get("data", [])
                        url = data[0].get("url") if data else ""
                    except (AttributeError, IndexError, TypeError, ValueError):
                        url = ""
                    if url:
                        self._record_attempt(
                            "image_gen",
                            "gpt",
                            response.status_code,
                            True,
                            retry=attempt > 0,
                        )
                        return url
                    last_error = ProviderResponseError(
                        "GPT Image 成功响应缺少 URL",
                        provider="gpt",
                        operation="image_gen",
                        status_code=response.status_code,
                    )
                    self._record_attempt(
                        "image_gen",
                        "gpt",
                        response.status_code,
                        False,
                        retry=attempt > 0,
                        error=last_error,
                    )
                else:
                    last_error = classify_http_error(
                        "gpt",
                        "image_gen",
                        response.status_code,
                        response.text,
                    )
                    self._record_attempt(
                        "image_gen",
                        "gpt",
                        response.status_code,
                        False,
                        retry=attempt > 0,
                        error=last_error,
                    )
                    if isinstance(
                        last_error,
                        (ProviderAuthError, ProviderQuotaError),
                    ):
                        raise last_error
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
        if last_error is not None:
            raise last_error
        return None
