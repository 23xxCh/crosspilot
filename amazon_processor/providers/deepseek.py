"""Official DeepSeek text and structured vision provider."""
from __future__ import annotations

import base64
from io import BytesIO
import time
from typing import Any, Callable, Optional

from PIL import Image
import requests
from requests.adapters import HTTPAdapter

from ..config.models import get_model_registry
from ..config.prompts import get_prompt_registry
from ..images.risk import (
    assessment_status,
    parse_image_assessment_batch_response,
    parse_image_assessment_response,
    parse_main_text_assessment_batch_response,
    parse_main_text_assessment_response,
)
from .support import (
    ModelProvider,
    ProviderAuthError,
    ProviderError,
    ProviderQuotaError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    classify_http_error,
)


_IMAGE_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}


class DeepSeekProvider(ModelProvider):
    """DeepSeek Chat Completions client for text and image understanding."""

    _DEFAULTS = get_model_registry().as_config()
    BASE_URL = _DEFAULTS["DEEPSEEK_BASE_URL"]
    MODEL = _DEFAULTS["DEEPSEEK_TEXT_MODEL"]
    FALLBACK_MODEL = _DEFAULTS.get("DEEPSEEK_TEXT_FALLBACK_MODEL", "")
    VISION_MODEL = _DEFAULTS.get(
        "DEEPSEEK_VISION_MODEL",
        "deepseek-v4-flash-vision-exp",
    )
    MAX_IMAGE_BYTES = 20 * 1024 * 1024

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        model: str | None = None,
        fallback_model: str | None = None,
        vision_model: str | None = None,
        vision_base_url: str | None = None,
    ) -> None:
        self.BASE_URL = str(base_url or self.BASE_URL).rstrip("/")
        self.VISION_BASE_URL = str(
            vision_base_url or base_url or self.BASE_URL
        ).rstrip("/")
        self.MODEL = str(model or self.MODEL).strip()
        self.FALLBACK_MODEL = str(
            fallback_model
            if fallback_model is not None
            else self.FALLBACK_MODEL
        ).strip()
        self.VISION_MODEL = str(vision_model or self.VISION_MODEL).strip()
        prompts = get_prompt_registry()
        self.SYSTEM_PROMPT = prompts.get("system.product_listing_optimizer")
        self.RISK_PROMPT = prompts.get("images.risk_assessment")
        self.RISK_BATCH_PROMPT = prompts.get("images.risk_assessment_batch")
        self.CONFIRM_PROMPT = prompts.get("images.risk_confirmation")
        self.MAIN_PROMPT = prompts.get("images.main_text_free_review")
        self.MAIN_BATCH_PROMPT = prompts.get(
            "images.main_text_free_review_batch"
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

    @staticmethod
    def _request_error(exc: Exception, *, operation: str) -> ProviderError:
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

    @staticmethod
    def _message_text(response: requests.Response) -> str:
        try:
            message = response.json()["choices"][0]["message"]
        except (KeyError, IndexError, TypeError, ValueError):
            return ""
        return str(
            message.get("content")
            or message.get("reasoning_content")
            or ""
        ).strip()

    def _post_chat(
        self,
        *,
        base_url: str,
        payload: dict[str, Any],
        operation: str,
        retries: int,
        timeout_s: float,
    ) -> str:
        retries = max(1, int(retries or 1))
        last_error: ProviderError | None = None
        for attempt in range(retries):
            try:
                response = self._session.post(
                    f"{base_url}/v1/chat/completions",
                    json=payload,
                    timeout=timeout_s,
                )
            except (requests.Timeout, requests.RequestException) as exc:
                last_error = self._request_error(exc, operation=operation)
                status_code = None
            else:
                status_code = response.status_code
                if response.ok:
                    content = self._message_text(response)
                    if content:
                        self._record_attempt(
                            operation,
                            "deepseek",
                            status_code,
                            True,
                            retry=attempt > 0,
                        )
                        return content
                    last_error = ProviderResponseError(
                        "DeepSeek 成功响应缺少内容",
                        provider="deepseek",
                        operation=operation,
                        status_code=status_code,
                    )
                else:
                    last_error = classify_http_error(
                        "deepseek",
                        operation,
                        status_code,
                        response.text,
                    )
            self._record_attempt(
                operation,
                "deepseek",
                status_code,
                False,
                retry=attempt > 0,
                error=last_error,
            )
            if isinstance(last_error, (ProviderAuthError, ProviderQuotaError)):
                raise last_error
            if not last_error.retryable or attempt == retries - 1:
                break
            delay = (
                10 * (attempt + 1)
                if status_code == 429
                else 2 * (attempt + 1)
            )
            time.sleep(delay)
        if last_error is not None:
            raise last_error
        raise ProviderResponseError(
            "DeepSeek 请求没有返回结果",
            provider="deepseek",
            operation=operation,
        )

    def call_text(
        self,
        prompt: str,
        max_tokens: int = 2048,
        retries: int = 3,
    ) -> Optional[str]:
        """Call the configured text model and optional DeepSeek fallback."""
        models = [self.MODEL]
        if self.FALLBACK_MODEL and self.FALLBACK_MODEL != self.MODEL:
            models.append(self.FALLBACK_MODEL)
        last_error: ProviderError | None = None
        for model in models:
            try:
                return self._post_chat(
                    base_url=self.BASE_URL,
                    payload={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": self.SYSTEM_PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens": max_tokens,
                        "thinking": {"type": "disabled"},
                    },
                    operation="text",
                    retries=retries,
                    timeout_s=60,
                )
            except (ProviderAuthError, ProviderQuotaError):
                raise
            except ProviderError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        return None

    @classmethod
    def _download_image_data_url(cls, image_url: str) -> str:
        try:
            response = requests.get(
                str(image_url),
                timeout=30,
                headers={"User-Agent": "AmazonProcessor/1.0"},
            )
            response.raise_for_status()
        except requests.Timeout as exc:
            raise ProviderTimeoutError(
                "审图源图片下载超时",
                provider="deepseek",
                operation="vision",
            ) from exc
        except requests.RequestException as exc:
            raise ProviderUnavailableError(
                "审图源图片下载失败",
                provider="deepseek",
                operation="vision",
            ) from exc
        content = response.content
        if not content or len(content) > cls.MAX_IMAGE_BYTES:
            raise ProviderResponseError(
                "审图源图片为空或超过 20 MiB",
                provider="deepseek",
                operation="vision",
            )
        try:
            with Image.open(BytesIO(content)) as image:
                image.verify()
                mime = _IMAGE_MIME.get(str(image.format or "").upper())
        except Exception as exc:
            raise ProviderResponseError(
                "审图源文件不是可解码图片",
                provider="deepseek",
                operation="vision",
            ) from exc
        if not mime:
            raise ProviderResponseError(
                "DeepSeek 不支持该图片格式",
                provider="deepseek",
                operation="vision",
            )
        encoded = base64.b64encode(content).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    @staticmethod
    def _vision_content(
        prompt: str,
        data_urls: list[str],
    ) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for index, data_url in enumerate(data_urls, start=1):
            content.extend([
                {"type": "text", "text": f"IMAGE_INDEX={index}"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": data_url,
                        "detail": "original",
                    },
                },
            ])
        return content

    def _assess(
        self,
        image_urls: list[str],
        *,
        prompt: str,
        parser: Callable[[object], Any],
        retries: int,
    ) -> Any:
        data_urls = [self._download_image_data_url(url) for url in image_urls]
        raw = self._post_chat(
            base_url=self.VISION_BASE_URL,
            payload={
                "model": self.VISION_MODEL,
                "messages": [{
                    "role": "user",
                    "content": self._vision_content(prompt, data_urls),
                }],
                "max_tokens": max(800, 500 * len(image_urls)),
                "thinking": {"type": "disabled"},
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
            operation="vision",
            retries=retries,
            timeout_s=90 if len(image_urls) > 1 else 60,
        )
        parsed = parser(raw)
        if parsed is None:
            raise ProviderResponseError(
                "DeepSeek 审图响应不符合结构化 JSON 契约",
                provider="deepseek",
                operation="vision",
            )
        return parsed

    def assess_image(
        self,
        image_url: str,
        *,
        confirmation: bool = False,
        policy: str = "general",
    ) -> Optional[dict[str, Any]]:
        if policy == "main_text_free":
            prompt = self.MAIN_PROMPT
            parser = parse_main_text_assessment_response
        else:
            prompt = self.CONFIRM_PROMPT if confirmation else self.RISK_PROMPT
            parser = parse_image_assessment_response
        return self._assess(
            [image_url],
            prompt=prompt,
            parser=parser,
            retries=3,
        )

    def assess_images(
        self,
        image_urls: list[str],
        *,
        policy: str = "general",
    ) -> list[dict[str, Any]]:
        if not image_urls:
            return []
        if policy == "main_text_free":
            prompt = self.MAIN_BATCH_PROMPT
            batch_parser = parse_main_text_assessment_batch_response
        else:
            prompt = self.RISK_BATCH_PROMPT
            batch_parser = parse_image_assessment_batch_response
        return self._assess(
            list(image_urls),
            prompt=prompt,
            parser=lambda raw: batch_parser(
                raw,
                expected_count=len(image_urls),
            ),
            retries=2,
        )

    def call_vision(self, image_url: str) -> Optional[bool]:
        assessment = self.assess_image(image_url)
        status = assessment_status(assessment)
        if status == "unknown":
            return None
        return status == "risk"
