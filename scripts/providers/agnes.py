"""Agnes text, vision, and image provider."""
from __future__ import annotations

import json
import re
import random
import threading
import time
from typing import Callable, Optional

import requests
from requests.adapters import HTTPAdapter

from crosspilot.model_registry import get_model_registry
from crosspilot.image_risk import parse_image_assessment_response
from crosspilot.prompt_registry import get_prompt_registry

from .base import ModelProvider
from .congestion import CongestionGate, CongestionPolicy
from .errors import (
    ProviderAuthError,
    ProviderError,
    ProviderQuotaError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    classify_http_error,
)


class AgnesProvider(ModelProvider):
    """Agnes OpenAI-compatible API client."""

    _DEFAULTS = get_model_registry().as_config()
    BASE_URL = _DEFAULTS["AGNES_BASE_URL"]
    TEXT_MODEL = _DEFAULTS["AGNES_TEXT_MODEL"]
    VISION_MODEL = _DEFAULTS.get("AGNES_VISION_MODEL", TEXT_MODEL)
    IMAGE_MODEL = _DEFAULTS["AGNES_IMAGE_MODEL"]

    _text_lock = threading.Lock()
    _text_last = [0.0]
    _text_interval = 60.0 / 1000

    _image_lock = threading.Lock()
    _image_last = [0.0]
    _image_interval = 60.0 / 100

    _PROMPTS = get_prompt_registry()
    REVIEW_PROMPT = _PROMPTS.get("images.review")
    RISK_ASSESSMENT_PROMPT = _PROMPTS.get("images.risk_assessment")
    RISK_CONFIRMATION_PROMPT = _PROMPTS.get("images.risk_confirmation")
    MAIN_IMAGE_PROMPT = _PROMPTS.get("images.main_product")
    VARIANT_IMAGE_PROMPT = _PROMPTS.get("images.variant")

    def __init__(
        self,
        api_key: str,
        *,
        image_model: str | None = None,
        base_url: str | None = None,
        text_model: str | None = None,
        vision_model: str | None = None,
        congestion_policy: CongestionPolicy | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        random_fn: Callable[[float, float], float] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.IMAGE_MODEL = (
            str(image_model or self.IMAGE_MODEL).strip()
            or self._DEFAULTS["AGNES_IMAGE_MODEL"]
        )
        self.BASE_URL = str(base_url or self.BASE_URL).rstrip("/")
        self.TEXT_MODEL = str(text_model or self.TEXT_MODEL).strip()
        self.VISION_MODEL = str(
            vision_model or text_model or self.VISION_MODEL
        ).strip()
        prompts = get_prompt_registry()
        self.REVIEW_PROMPT = prompts.get("images.review")
        self.RISK_ASSESSMENT_PROMPT = prompts.get(
            "images.risk_assessment"
        )
        self.RISK_CONFIRMATION_PROMPT = prompts.get(
            "images.risk_confirmation"
        )
        self.MAIN_IMAGE_PROMPT = prompts.get("images.main_product")
        self.VARIANT_IMAGE_PROMPT = prompts.get("images.variant")
        self._congestion_policy = (
            congestion_policy or CongestionPolicy()
        )
        self._sleep = sleep_fn or time.sleep
        self._random = random_fn or random.uniform
        effective_clock = clock or time.monotonic
        self._congestion_gates = {
            operation: CongestionGate(
                threshold=self._congestion_policy.circuit_threshold,
                cooldown_s=(
                    self._congestion_policy.circuit_cooldown_s
                ),
                clock=effective_clock,
            )
            for operation in ("vision", "image_gen")
        }
        self._session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=40,
            pool_maxsize=40,
            max_retries=0,
        )
        self._session.mount("https://", adapter)
        self._session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    def _acquire_text(self) -> None:
        with self._text_lock:
            wait = self._text_interval - (
                time.time() - self._text_last[0]
            )
            if wait > 0:
                time.sleep(wait)
            self._text_last[0] = time.time()

    def _acquire_image(self) -> None:
        with self._image_lock:
            wait = self._image_interval - (
                time.time() - self._image_last[0]
            )
            if wait > 0:
                time.sleep(wait)
            self._image_last[0] = time.time()

    @staticmethod
    def _request_error(
        exc: Exception,
        *,
        operation: str,
    ) -> ProviderError:
        if isinstance(exc, requests.Timeout):
            return ProviderTimeoutError(
                "Agnes 请求超时",
                provider="agnes",
                operation=operation,
            )
        return ProviderUnavailableError(
            "Agnes 网络请求失败",
            provider="agnes",
            operation=operation,
        )

    def _record_response_error(
        self,
        operation: str,
        response,
        *,
        retry: bool,
    ) -> ProviderError:
        error = classify_http_error(
            "agnes",
            operation,
            response.status_code,
            response.text,
        )
        self._record_attempt(
            operation,
            "agnes",
            response.status_code,
            False,
            retry=retry,
            error=error,
        )
        gate = self._congestion_gates.get(operation)
        if gate is not None:
            if response.status_code == 503:
                gate.record_503()
            else:
                gate.record_probe_failure()
        return error

    def _ensure_not_congested(self, operation: str) -> None:
        gate = self._congestion_gates.get(operation)
        if gate is not None and not gate.try_acquire():
            self._record_circuit_skip(operation, "agnes")
            raise ProviderUnavailableError(
                "Agnes 模型拥塞熔断中，立即尝试回退模型",
                provider="agnes",
                operation=operation,
                status_code=503,
            )

    def _record_congestion_success(self, operation: str) -> None:
        gate = self._congestion_gates.get(operation)
        if gate is not None:
            gate.record_success()

    def _record_congestion_request_failure(
        self,
        operation: str,
    ) -> None:
        gate = self._congestion_gates.get(operation)
        if gate is not None:
            gate.record_probe_failure()

    def _can_retry_503(
        self,
        operation: str,
        *,
        retries_used: int,
        attempt: int,
        total_attempts: int,
    ) -> bool:
        gate = self._congestion_gates.get(operation)
        return (
            retries_used < self._congestion_policy.retry_limit
            and attempt < total_attempts - 1
            and not (gate is not None and gate.is_open())
        )

    def _wait_for_503_retry(self, response) -> float:
        headers = getattr(response, "headers", {}) or {}
        delay = self._congestion_policy.retry_delay(
            retry_after=headers.get("Retry-After"),
            random_fn=self._random,
        )
        if delay > 0:
            self._sleep(delay)
        return delay

    def call_text(
        self,
        prompt: str,
        max_tokens: int = 2048,
        retries: int = 1,
    ) -> Optional[str]:
        retries = max(1, int(retries or 1))
        last_error: ProviderError | None = None
        for attempt in range(retries):
            self._acquire_text()
            try:
                response = self._session.post(
                    f"{self.BASE_URL}/v1/chat/completions",
                    json={
                        "model": self.TEXT_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": max_tokens,
                    },
                    timeout=30,
                )
            except (requests.Timeout, requests.RequestException) as exc:
                last_error = self._request_error(exc, operation="text")
                self._record_attempt(
                    "text",
                    "agnes",
                    None,
                    False,
                    retry=attempt > 0,
                    error=last_error,
                )
            else:
                if response.ok:
                    try:
                        content = (
                            response.json()
                            .get("choices", [{}])[0]
                            .get("message", {})
                            .get("content", "")
                        )
                    except (AttributeError, IndexError, TypeError, ValueError):
                        content = ""
                    if content:
                        self._record_attempt(
                            "text",
                            "agnes",
                            response.status_code,
                            True,
                            retry=attempt > 0,
                        )
                        return content
                    last_error = ProviderResponseError(
                        "Agnes 成功响应缺少文本内容",
                        provider="agnes",
                        operation="text",
                        status_code=response.status_code,
                    )
                    self._record_attempt(
                        "text",
                        "agnes",
                        response.status_code,
                        False,
                        retry=attempt > 0,
                        error=last_error,
                    )
                else:
                    last_error = self._record_response_error(
                        "text",
                        response,
                        retry=attempt > 0,
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

    def _call_vision_parsed(
        self,
        image_url: str,
        *,
        prompt: str,
        parser,
        invalid_message: str,
        max_tokens: int,
        retries: int,
    ):
        retries = max(1, int(retries or 1))
        last_error: ProviderError | None = None
        congestion_retries = 0
        for attempt in range(retries):
            self._ensure_not_congested("vision")
            self._acquire_text()
            response = None
            try:
                response = self._session.post(
                    f"{self.BASE_URL}/v1/chat/completions",
                    json={
                        "model": self.VISION_MODEL,
                        "messages": [{
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {"url": image_url},
                                },
                                {
                                    "type": "text",
                                    "text": prompt,
                                },
                            ],
                        }],
                        "temperature": 0,
                        "max_tokens": max_tokens,
                    },
                    timeout=60,
                )
            except (requests.Timeout, requests.RequestException) as exc:
                last_error = self._request_error(
                    exc,
                    operation="vision",
                )
                self._record_attempt(
                    "vision",
                    "agnes",
                    None,
                    False,
                    retry=attempt > 0,
                    error=last_error,
                )
                self._record_congestion_request_failure("vision")
            else:
                if response.ok:
                    self._record_congestion_success("vision")
                    try:
                        content = (
                            response.json()
                            .get("choices", [{}])[0]
                            .get("message", {})
                            .get("content", "")
                        )
                    except (AttributeError, IndexError, TypeError, ValueError):
                        content = ""
                    parsed = parser(content)
                    if parsed is not None:
                        self._record_attempt(
                            "vision",
                            "agnes",
                            response.status_code,
                            True,
                            retry=attempt > 0,
                        )
                        return parsed
                    last_error = ProviderResponseError(
                        invalid_message,
                        provider="agnes",
                        operation="vision",
                        status_code=response.status_code,
                    )
                    self._record_attempt(
                        "vision",
                        "agnes",
                        response.status_code,
                        False,
                        retry=attempt > 0,
                        error=last_error,
                    )
                else:
                    last_error = self._record_response_error(
                        "vision",
                        response,
                        retry=attempt > 0,
                    )
                    if isinstance(
                        last_error,
                        (ProviderAuthError, ProviderQuotaError),
                    ):
                        raise last_error
            if attempt < retries - 1:
                status_code = getattr(last_error, "status_code", None)
                if status_code is None:
                    # A network timeout already consumed the long wait.
                    # Return control to the caller/fallback immediately.
                    break
                if status_code == 503:
                    if not self._can_retry_503(
                        "vision",
                        retries_used=congestion_retries,
                        attempt=attempt,
                        total_attempts=retries,
                    ):
                        break
                    self._wait_for_503_retry(response)
                    congestion_retries += 1
                elif status_code == 429:
                    break
                else:
                    self._sleep(2 * (attempt + 1))
        if last_error is not None:
            raise last_error
        return None
    def call_vision(
        self,
        image_url: str,
        retries: int = 3,
    ) -> Optional[bool]:
        def parse_bool(content):
            answer = re.match(
                r"^\s*(YES|NO)\b",
                content or "",
                re.IGNORECASE,
            )
            return (
                answer.group(1).upper() == "YES"
                if answer else None
            )

        return self._call_vision_parsed(
            image_url,
            prompt=self.REVIEW_PROMPT,
            parser=parse_bool,
            invalid_message="Agnes 图审响应不是 YES/NO",
            max_tokens=10,
            retries=retries,
        )

    def assess_image(
        self,
        image_url: str,
        *,
        confirmation: bool = False,
        retries: int = 3,
    ) -> Optional[dict]:
        prompt = (
            self.RISK_CONFIRMATION_PROMPT
            if confirmation
            else self.RISK_ASSESSMENT_PROMPT
        )
        return self._call_vision_parsed(
            image_url,
            prompt=prompt,
            parser=parse_image_assessment_response,
            invalid_message="Agnes 图审响应不是结构化风险 JSON",
            max_tokens=400,
            retries=retries,
        )

    def call_image_gen(
        self,
        image_url: str,
        size: str = "1024x1024",
        retries: int = 5,
        is_variant: bool = False,
        context: str = "",
        route_offset: int = 0,
    ) -> Optional[str]:
        del route_offset
        prompt = (
            self.VARIANT_IMAGE_PROMPT
            if is_variant
            else self.MAIN_IMAGE_PROMPT
        )
        context_text = str(context or "").strip()[:500]
        if context_text:
            prompt += (
                "\n\nLISTING CONTEXT: "
                + context_text
                + "\nThe named product is the sold item. Treat any vehicle, "
                "wheel, fixture, or installation scene only as context; do "
                "not invent it as part of the product."
            )
        retries = max(1, int(retries or 1))
        last_error: ProviderError | None = None
        congestion_retries = 0
        for attempt in range(retries):
            self._ensure_not_congested("image_gen")
            self._acquire_image()
            try:
                response = self._session.post(
                    f"{self.BASE_URL}/v1/images/generations",
                    json={
                        "model": self.IMAGE_MODEL,
                        "prompt": prompt,
                        "size": size,
                        "extra_body": {
                            "image": [image_url],
                            "response_format": "url",
                        },
                    },
                    timeout=300,
                )
            except (requests.Timeout, requests.RequestException) as exc:
                last_error = self._request_error(
                    exc,
                    operation="image_gen",
                )
                self._record_attempt(
                    "image_gen",
                    "agnes",
                    None,
                    False,
                    retry=attempt > 0,
                    error=last_error,
                )
                self._record_congestion_request_failure("image_gen")
            else:
                if response.ok:
                    self._record_congestion_success("image_gen")
                    try:
                        data = response.json().get("data", [])
                        url = data[0].get("url") if data else ""
                    except (AttributeError, IndexError, TypeError, ValueError):
                        url = ""
                    if url:
                        self._record_attempt(
                            "image_gen",
                            "agnes",
                            response.status_code,
                            True,
                            retry=attempt > 0,
                        )
                        return url
                    last_error = ProviderResponseError(
                        "Agnes 生图响应缺少 URL",
                        provider="agnes",
                        operation="image_gen",
                        status_code=response.status_code,
                    )
                    self._record_attempt(
                        "image_gen",
                        "agnes",
                        response.status_code,
                        False,
                        retry=attempt > 0,
                        error=last_error,
                    )
                else:
                    last_error = self._record_response_error(
                        "image_gen",
                        response,
                        retry=attempt > 0,
                    )
                    if isinstance(
                        last_error,
                        (ProviderAuthError, ProviderQuotaError),
                    ):
                        raise last_error
            if attempt < retries - 1:
                status_code = getattr(last_error, "status_code", None)
                if status_code is None:
                    # Let CompositeProvider try the next image model.
                    break
                if status_code == 503:
                    if not self._can_retry_503(
                        "image_gen",
                        retries_used=congestion_retries,
                        attempt=attempt,
                        total_attempts=retries,
                    ):
                        break
                    wait = self._wait_for_503_retry(response)
                    congestion_retries += 1
                    print(
                        f"  [Agnes] 503 quick retry {wait:.1f}s "
                        f"({congestion_retries}/"
                        f"{self._congestion_policy.retry_limit})",
                        flush=True,
                    )
                elif status_code == 429:
                    break
                else:
                    self._sleep(5)
        if last_error is not None:
            raise last_error
        return None
