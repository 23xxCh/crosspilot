from unittest.mock import Mock

import pytest
import requests


def test_http_error_classification_is_structured():
    from scripts.providers.errors import (
        ProviderAuthError,
        ProviderQuotaError,
        ProviderRateLimitError,
        ProviderResponseError,
        ProviderUnavailableError,
        classify_http_error,
    )

    assert isinstance(
        classify_http_error("agnes", "image_gen", 401, "unauthorized"),
        ProviderAuthError,
    )
    assert isinstance(
        classify_http_error("agnes", "image_gen", 402, "balance"),
        ProviderQuotaError,
    )
    assert isinstance(
        classify_http_error("agnes", "image_gen", 429, "rate limited"),
        ProviderRateLimitError,
    )
    assert isinstance(
        classify_http_error("agnes", "image_gen", 503, "queue full"),
        ProviderUnavailableError,
    )
    assert isinstance(
        classify_http_error("agnes", "image_gen", 422, "invalid payload"),
        ProviderResponseError,
    )


def test_provider_error_public_context_is_redacted():
    from scripts.providers.errors import ProviderRateLimitError

    error = ProviderRateLimitError(
        "请求过多",
        provider="agnes",
        operation="image_gen",
        status_code=429,
        response_excerpt="rate limited",
    )

    assert error.retryable is True
    assert error.to_dict() == {
        "type": "ProviderRateLimitError",
        "provider": "agnes",
        "operation": "image_gen",
        "status_code": 429,
        "retryable": True,
        "detail": "请求过多",
        "response_excerpt": "rate limited",
    }

    secret_error = ProviderRateLimitError(
        "请求过多",
        response_excerpt="Bearer cpk-secret-value sk-other-secret",
    )
    serialized = str(secret_error.to_dict())
    assert "secret-value" not in serialized
    assert "other-secret" not in serialized


def test_deepseek_timeout_raises_typed_error():
    from scripts.model_provider import (
        DeepSeekProvider,
        ProviderTimeoutError,
    )

    provider = DeepSeekProvider("secret-key")
    provider._session = Mock()
    provider._session.post.side_effect = requests.Timeout("timed out")

    with pytest.raises(ProviderTimeoutError) as raised:
        provider.call_text("hello", retries=1)

    assert raised.value.provider == "deepseek"
    assert raised.value.operation == "text"
    assert "secret-key" not in str(raised.value)


def test_provider_does_not_mask_programming_errors():
    from scripts.model_provider import DeepSeekProvider

    provider = DeepSeekProvider("secret-key")
    provider._session = Mock()
    provider._session.post.side_effect = RuntimeError("programming bug")

    with pytest.raises(RuntimeError, match="programming bug"):
        provider.call_text("hello", retries=1)


def test_deepseek_invalid_success_payload_raises_response_error():
    from scripts.model_provider import (
        DeepSeekProvider,
        ProviderResponseError,
    )

    response = Mock(ok=True, status_code=200, text="ok")
    response.json.return_value = {"choices": []}
    provider = DeepSeekProvider("secret-key")
    provider._session = Mock()
    provider._session.post.return_value = response

    with pytest.raises(ProviderResponseError):
        provider.call_text("hello", retries=1)


def test_composite_image_route_falls_back_on_typed_provider_error():
    from scripts.model_provider import (
        CompositeProvider,
        ProviderUnavailableError,
    )

    provider = CompositeProvider({
        "text_provider": "deepseek",
        "vision_provider": "agnes",
        "image_gen_provider": "agnes",
        "deepseek_key": "test-deepseek",
        "agnes_key": "test-agnes",
    })
    primary = Mock()
    primary.call_image_gen.side_effect = ProviderUnavailableError(
        "upstream unavailable",
        provider="agnes",
        operation="image_gen",
        status_code=503,
    )
    fallback = Mock()
    fallback.call_image_gen.return_value = "https://generated.example/image.png"
    provider._providers["image_gen"] = primary
    provider._image_gen_fallbacks = [fallback]

    assert provider.call_image_gen("https://source.example/image.png") == (
        "https://generated.example/image.png"
    )


def test_legacy_facade_reexports_provider_modules():
    import scripts.model_provider as facade
    from scripts.providers.agnes import AgnesProvider
    from scripts.providers.composite import CompositeProvider
    from scripts.providers.deepseek import DeepSeekProvider

    assert facade.AgnesProvider is AgnesProvider
    assert facade.DeepSeekProvider is DeepSeekProvider
    assert facade.CompositeProvider is CompositeProvider
