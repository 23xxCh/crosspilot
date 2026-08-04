from unittest.mock import Mock, call

import pytest
import requests


def test_http_error_classification_is_structured():
    from amazon_processor.providers.support import (
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
    from amazon_processor.providers.support import ProviderRateLimitError

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
    from amazon_processor.providers import (
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
    from amazon_processor.providers import DeepSeekProvider

    provider = DeepSeekProvider("secret-key")
    provider._session = Mock()
    provider._session.post.side_effect = RuntimeError("programming bug")

    with pytest.raises(RuntimeError, match="programming bug"):
        provider.call_text("hello", retries=1)


def test_deepseek_invalid_success_payload_raises_response_error():
    from amazon_processor.providers import (
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
    from amazon_processor.providers import (
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


def test_composite_vision_timeout_uses_fallback():
    from amazon_processor.providers import (
        CompositeProvider,
        ProviderTimeoutError,
    )

    provider = CompositeProvider({
        "text_provider": "deepseek",
        "vision_provider": "agnes",
        "image_gen_provider": "agnes",
        "deepseek_key": "test-deepseek",
        "agnes_key": "test-agnes",
    })
    primary = Mock()
    primary.assess_image.side_effect = ProviderTimeoutError(
        "vision timed out",
        provider="agnes",
        operation="vision",
    )
    fallback = Mock()
    fallback.assess_image.return_value = {"status": "safe"}
    provider._providers["vision"] = primary
    provider._vision_fallbacks = [fallback]

    assert provider.assess_image(
        "https://source.example/image.png"
    ) == {"status": "safe"}
    fallback.assess_image.assert_called_once()


def test_composite_batch_vision_timeout_uses_fallback():
    from amazon_processor.providers import (
        CompositeProvider,
        ProviderTimeoutError,
    )

    provider = CompositeProvider({
        "text_provider": "deepseek",
        "vision_provider": "agnes",
        "image_gen_provider": "agnes",
        "deepseek_key": "test-deepseek",
        "agnes_key": "test-agnes",
    })
    primary = Mock()
    primary.assess_images.side_effect = ProviderTimeoutError(
        "vision timed out",
        provider="agnes",
        operation="vision",
    )
    fallback = Mock()
    fallback.assess_images.return_value = [{"status": "safe"}]
    provider._providers["vision"] = primary
    provider._vision_fallbacks = [fallback]

    assert provider.assess_images(
        ["https://source.example/image.png"]
    ) == [{"status": "safe"}]
    fallback.assess_images.assert_called_once()


def test_ollama_vision_batch_reviews_each_image_individually():
    from amazon_processor.providers.ollama_vision import (
        OllamaVisionProvider,
    )

    provider = OllamaVisionProvider(
        "local-only",
        base_url="http://127.0.0.1:11434",
        model="qwen3-vl:4b-instruct-q4_K_M",
    )
    provider.assess_image = Mock(side_effect=[
        {"status": "safe"},
        {"status": "risk"},
    ])

    assert provider.assess_images(
        ["https://example.test/1.jpg", "https://example.test/2.jpg"],
        policy="general",
    ) == [
        {"status": "safe"},
        {"status": "risk"},
    ]
    assert provider.assess_image.call_args_list == [
        call(
            "https://example.test/1.jpg",
            policy="general",
            retries=1,
        ),
        call(
            "https://example.test/2.jpg",
            policy="general",
            retries=1,
        ),
    ]


def test_explicit_image_route_offset_attempts_only_one_provider():
    from amazon_processor.providers import (
        CompositeProvider,
        ProviderUnavailableError,
    )

    provider = CompositeProvider({
        "text_provider": "deepseek",
        "vision_provider": "agnes",
        "image_gen_provider": "agnes",
        "deepseek_key": "test-deepseek",
        "agnes_key": "test-agnes",
        "circuit_failure_threshold": 1,
    })
    primary = Mock()
    primary.call_image_gen.side_effect = ProviderUnavailableError(
        "upstream unavailable",
        provider="agnes",
        operation="image_gen",
        status_code=503,
    )
    fallback = Mock()
    fallback.call_image_gen.return_value = (
        "https://generated.example/fallback.png"
    )
    provider._providers["image_gen"] = primary
    provider._image_gen_fallbacks = [fallback]

    with pytest.raises(ProviderUnavailableError):
        provider.call_image_gen(
            "https://source.example/image.png",
            route_offset=0,
        )
    fallback.call_image_gen.assert_not_called()
    assert provider._is_circuit_open("image_gen") is False

    assert provider.call_image_gen(
        "https://source.example/image.png",
        route_offset=1,
    ) == "https://generated.example/fallback.png"
    assert fallback.call_image_gen.call_count == 1


def test_response_format_error_does_not_open_composite_circuit():
    from amazon_processor.providers import (
        CompositeProvider,
        ProviderResponseError,
    )

    provider = CompositeProvider({
        "text_provider": "deepseek",
        "vision_provider": "agnes",
        "image_gen_provider": "agnes",
        "deepseek_key": "test-deepseek",
        "agnes_key": "test-agnes",
    })
    call = Mock(side_effect=[
        ProviderResponseError(
            "invalid structured response",
            provider="agnes",
            operation="vision",
        ),
        {"status": "safe"},
    ])

    with pytest.raises(ProviderResponseError):
        provider._call("vision", call)

    assert provider._is_circuit_open("vision") is False
    assert provider._call("vision", call) == {"status": "safe"}


def test_open_composite_circuit_raises_retryable_skip():
    from amazon_processor.providers import (
        CompositeProvider,
        ProviderCircuitOpenError,
    )

    provider = CompositeProvider({
        "text_provider": "deepseek",
        "vision_provider": "agnes",
        "image_gen_provider": "agnes",
        "deepseek_key": "test-deepseek",
        "agnes_key": "test-agnes",
        "circuit_failure_threshold": 1,
    })
    provider._record_circuit_result("image_gen", False)

    with pytest.raises(ProviderCircuitOpenError) as raised:
        provider._call("image_gen", Mock(return_value="unused"))

    assert raised.value.retryable is True


def test_gpt_image_generation_keeps_long_repair_context():
    from amazon_processor.providers.gpt_image import GPTImageProvider

    response = Mock(ok=True, status_code=200, text="ok")
    response.json.return_value = {
        "data": [{"url": "https://generated.example/gpt.png"}],
    }
    provider = GPTImageProvider("test-key")
    provider._session = Mock()
    provider._session.post.return_value = response
    context = "LOCAL REPAIR RULE: " + ("paint over logos. " * 35)
    context += "VEHICLE EMBLEM RULE: match local material."

    result = provider.call_image_gen(
        "https://img.example/source.jpg",
        retries=1,
        context=context,
    )

    assert result == "https://generated.example/gpt.png"
    payload = provider._session.post.call_args.kwargs["json"]
    assert "LOCAL REPAIR RULE" in payload["prompt"]
    assert "VEHICLE EMBLEM RULE" in payload["prompt"]


def test_provider_interface_exports_implementations():
    import amazon_processor.providers as facade
    from amazon_processor.providers.agnes import AgnesProvider
    from amazon_processor.providers.composite import CompositeProvider
    from amazon_processor.providers.deepseek import DeepSeekProvider

    assert facade.AgnesProvider is AgnesProvider
    assert facade.DeepSeekProvider is DeepSeekProvider
    assert facade.CompositeProvider is CompositeProvider
