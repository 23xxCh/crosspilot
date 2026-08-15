import json
from unittest.mock import Mock

import pytest
import requests


def test_congestion_gate_opens_and_recovers_with_one_half_open_probe():
    from amazon_processor.providers.support import CongestionGate

    now = [100.0]
    gate = CongestionGate(
        threshold=2,
        cooldown_s=60,
        clock=lambda: now[0],
    )

    assert gate.try_acquire() is True
    gate.record_503()
    assert gate.try_acquire() is True
    gate.record_503()
    assert gate.try_acquire() is False

    now[0] = 161.0
    assert gate.try_acquire() is True
    assert gate.try_acquire() is False

    gate.record_success()
    assert gate.try_acquire() is True


def test_congestion_policy_uses_short_bounded_retry_delay():
    from amazon_processor.providers.support import CongestionPolicy

    policy = CongestionPolicy(
        retry_limit=1,
        backoff_min_s=3,
        backoff_max_s=8,
    )

    assert policy.retry_delay(
        retry_after=None,
        random_fn=lambda low, high: (low + high) / 2,
    ) == 5.5
    assert policy.retry_delay(
        retry_after="120",
        random_fn=lambda _low, _high: 4,
    ) == 8


def test_agnes_image_503_retries_once_without_long_blocking():
    from amazon_processor.providers.agnes import AgnesProvider
    from amazon_processor.providers.support import (
        CongestionPolicy,
        ProviderUnavailableError,
    )

    unavailable = Mock(
        ok=False,
        status_code=503,
        text="queue full",
        headers={},
    )
    provider = AgnesProvider(
        "test-key",
        congestion_policy=CongestionPolicy(
            retry_limit=1,
            backoff_min_s=3,
            backoff_max_s=8,
            circuit_threshold=3,
            circuit_cooldown_s=120,
        ),
        sleep_fn=lambda seconds: sleeps.append(seconds),
        random_fn=lambda _low, _high: 5,
    )
    sleeps = []
    provider._acquire_image = lambda: None
    provider._session = Mock()
    provider._session.post.side_effect = [unavailable, unavailable]

    with pytest.raises(ProviderUnavailableError):
        provider.call_image_gen("https://img.example/source.jpg", retries=5)

    assert provider._session.post.call_count == 2
    assert sleeps == [5]


def test_agnes_image_request_timeout_is_bounded():
    from amazon_processor.providers.agnes import AgnesProvider

    response = Mock(ok=True, status_code=200, text="ok", headers={})
    response.json.return_value = {
        "data": [{"url": "https://generated.example/result.png"}],
    }
    provider = AgnesProvider("test-key")
    provider._acquire_image = lambda: None
    provider._session = Mock()
    provider._session.post.return_value = response

    assert provider.call_image_gen(
        "https://img.example/source.jpg",
        retries=1,
    ) == "https://generated.example/result.png"
    assert provider._session.post.call_args.kwargs["timeout"] == 90


def test_agnes_manual_image_edit_uses_prompt_override_and_ratio():
    from amazon_processor.providers.agnes import AgnesProvider

    response = Mock(ok=True, status_code=200, text="ok", headers={})
    response.json.return_value = {
        "data": [{"url": "https://generated.example/manual.png"}],
    }
    provider = AgnesProvider(
        "test-key",
        image_model="agnes-image-2.1-flash",
    )
    provider._acquire_image = lambda: None
    provider._session = Mock()
    provider._session.post.return_value = response

    result = provider.call_image_gen(
        "data:image/png;base64,AAAA",
        size="1K",
        retries=1,
        prompt_override="Translate every Chinese label into English.",
        ratio="4:3",
    )

    assert result == "https://generated.example/manual.png"
    payload = provider._session.post.call_args.kwargs["json"]
    assert payload == {
        "model": "agnes-image-2.1-flash",
        "prompt": "Translate every Chinese label into English.",
        "size": "1K",
        "ratio": "4:3",
        "extra_body": {
            "image": ["data:image/png;base64,AAAA"],
            "response_format": "url",
        },
    }


def test_agnes_reference_free_generation_omits_input_image():
    from amazon_processor.providers.agnes import AgnesProvider

    response = Mock(ok=True, status_code=200, text="ok", headers={})
    response.json.return_value = {
        "data": [{"url": "https://generated.example/catalog.png"}],
    }
    provider = AgnesProvider("test-key")
    provider._acquire_image = lambda: None
    provider._session = Mock()
    provider._session.post.return_value = response

    result = provider.call_image_gen(
        "https://img.example/wrong-scene.jpg",
        retries=1,
        context="SOLD ITEM TITLE: Generic bumper hooks, 2 pieces.",
        reference_free=True,
    )

    assert result == "https://generated.example/catalog.png"
    payload = provider._session.post.call_args.kwargs["json"]
    assert payload["extra_body"] == {"response_format": "url"}
    assert provider._session.post.call_args.kwargs["timeout"] == 180
    assert "Do not copy or recreate" in payload["prompt"]
    assert "reference scene" in payload["prompt"]
    assert "Generic bumper hooks" in payload["prompt"]


def test_agnes_image_generation_keeps_long_repair_context():
    from amazon_processor.providers.agnes import AgnesProvider

    response = Mock(ok=True, status_code=200, text="ok", headers={})
    response.json.return_value = {
        "data": [{"url": "https://generated.example/catalog.png"}],
    }
    provider = AgnesProvider("test-key")
    provider._acquire_image = lambda: None
    provider._session = Mock()
    provider._session.post.return_value = response
    context = "LOCAL REPAIR RULE: " + ("paint over logos. " * 35)
    context += "VEHICLE EMBLEM RULE: match local material."

    result = provider.call_image_gen(
        "https://img.example/source.jpg",
        retries=1,
        context=context,
    )

    assert result == "https://generated.example/catalog.png"
    payload = provider._session.post.call_args.kwargs["json"]
    assert "LOCAL REPAIR RULE" in payload["prompt"]
    assert "VEHICLE EMBLEM RULE" in payload["prompt"]


def test_agnes_open_circuit_skips_http_until_cooldown():
    from amazon_processor.providers.agnes import AgnesProvider
    from amazon_processor.providers.support import (
        CongestionPolicy,
        ProviderUnavailableError,
    )

    now = [100.0]
    unavailable = Mock(
        ok=False,
        status_code=503,
        text="overloaded",
        headers={},
    )
    ok = Mock(ok=True, status_code=200, text="ok", headers={})
    ok.json.return_value = {
        "data": [{"url": "https://generated.example/result.png"}],
    }
    provider = AgnesProvider(
        "test-key",
        congestion_policy=CongestionPolicy(
            retry_limit=1,
            backoff_min_s=0,
            backoff_max_s=0,
            circuit_threshold=1,
            circuit_cooldown_s=60,
        ),
        clock=lambda: now[0],
        sleep_fn=lambda _seconds: None,
    )
    provider._acquire_image = lambda: None
    provider._session = Mock()
    provider._session.post.side_effect = [unavailable, ok]

    with pytest.raises(ProviderUnavailableError):
        provider.call_image_gen("https://img.example/first.jpg")
    with pytest.raises(ProviderUnavailableError):
        provider.call_image_gen("https://img.example/second.jpg")
    assert provider._session.post.call_count == 1

    now[0] = 161.0
    assert provider.call_image_gen(
        "https://img.example/probe.jpg"
    ) == "https://generated.example/result.png"
    assert provider._session.post.call_count == 2


def test_composite_moves_to_fallback_after_fast_primary_503():
    from amazon_processor.providers.composite import CompositeProvider

    unavailable = Mock(
        ok=False,
        status_code=503,
        text="queue full",
        headers={},
    )
    provider = CompositeProvider({
        "text_provider": "deepseek",
        "vision_provider": "agnes",
        "image_gen_provider": "agnes",
        "deepseek_key": "test-deepseek",
        "agnes_key": "test-agnes",
        "agnes_image_fallback_model": "agnes-image-fallback",
        "agnes_503_retry_limit": "1",
        "agnes_503_backoff_min_s": "0",
        "agnes_503_backoff_max_s": "0",
    })
    primary = provider._providers["image_gen"]
    primary._acquire_image = lambda: None
    primary._session = Mock()
    primary._session.post.side_effect = [unavailable, unavailable]
    fallback = Mock()
    fallback.call_image_gen.return_value = (
        "https://generated.example/fallback.png"
    )
    provider._image_gen_fallbacks = [fallback]

    result = provider.call_image_gen(
        "https://img.example/source.jpg",
    )

    assert result == "https://generated.example/fallback.png"
    assert primary._session.post.call_count == 2
    fallback.call_image_gen.assert_called_once()


def test_composite_records_primary_congestion_circuit_skip():
    from amazon_processor.providers.composite import CompositeProvider

    unavailable = Mock(
        ok=False,
        status_code=503,
        text="queue full",
        headers={},
    )
    provider = CompositeProvider({
        "text_provider": "deepseek",
        "vision_provider": "agnes",
        "image_gen_provider": "agnes",
        "deepseek_key": "test-deepseek",
        "agnes_key": "test-agnes",
        "agnes_image_fallback_model": "agnes-image-fallback",
        "agnes_503_retry_limit": "0",
        "agnes_503_circuit_threshold": "1",
        "agnes_503_circuit_cooldown_s": "120",
    })
    primary = provider._providers["image_gen"]
    primary._acquire_image = lambda: None
    primary._session = Mock()
    primary._session.post.return_value = unavailable
    fallback = Mock()
    fallback.call_image_gen.return_value = (
        "https://generated.example/fallback.png"
    )
    provider._image_gen_fallbacks = [fallback]

    assert provider.call_image_gen("https://img.example/one.jpg")
    assert provider.call_image_gen("https://img.example/two.jpg")

    metrics = provider.metrics_snapshot()
    assert primary._session.post.call_count == 1
    assert metrics["circuit_open"] == 1
    assert (
        metrics["by_operation"]["image_gen"]["circuit_open"]
        == 1
    )


def test_composite_falls_back_immediately_after_primary_timeout():
    from amazon_processor.providers.composite import CompositeProvider

    provider = CompositeProvider({
        "text_provider": "deepseek",
        "vision_provider": "agnes",
        "image_gen_provider": "agnes",
        "deepseek_key": "test-deepseek",
        "agnes_key": "test-agnes",
        "agnes_image_fallback_model": "agnes-image-fallback",
    })
    primary = provider._providers["image_gen"]
    primary._acquire_image = lambda: None
    primary._sleep = lambda seconds: sleeps.append(seconds)
    primary._session = Mock()
    primary._session.post.side_effect = requests.Timeout()
    fallback = Mock()
    fallback.call_image_gen.return_value = (
        "https://generated.example/fallback.png"
    )
    provider._image_gen_fallbacks = [fallback]
    sleeps = []

    result = provider.call_image_gen(
        "https://img.example/source.jpg",
    )

    assert result == "https://generated.example/fallback.png"
    assert primary._session.post.call_count == 1
    assert sleeps == []


def test_agnes_vision_timeout_does_not_repeat_long_request():
    from amazon_processor.providers.agnes import AgnesProvider
    from amazon_processor.providers.support import ProviderTimeoutError

    provider = AgnesProvider(
        "test-key",
        sleep_fn=lambda seconds: sleeps.append(seconds),
    )
    provider._acquire_text = lambda: None
    provider._session = Mock()
    provider._session.post.side_effect = requests.Timeout()
    sleeps = []

    with pytest.raises(ProviderTimeoutError):
        provider.call_vision(
            "https://img.example/source.jpg",
            retries=3,
        )

    assert provider._session.post.call_count == 1
    assert sleeps == []


def test_agnes_assess_image_returns_structured_result():
    from amazon_processor.providers.agnes import AgnesProvider

    response = Mock(ok=True, status_code=200, text="ok", headers={})
    response.json.return_value = {
        "choices": [{
            "message": {
                "content": json.dumps({
                    "status": "risk",
                    "reasons": ["brand_logo"],
                    "placement": "product_surface",
                    "detected_text": ["TOYOTA"],
                    "confidence": 0.97,
                    "evidence": "A Toyota emblem appears on the product.",
                })
            }
        }]
    }
    provider = AgnesProvider("test-key")
    provider._acquire_text = lambda: None
    provider._session = Mock()
    provider._session.post.return_value = response

    result = provider.assess_image("https://img.example/source.jpg")

    assert result["status"] == "risk"
    assert result["reasons"] == ["brand_logo"]
    assert result["placement"] == "product_surface"
    assert result["detected_text"] == ["TOYOTA"]


def test_agnes_assess_image_reads_structured_reasoning_content():
    from amazon_processor.providers.agnes import AgnesProvider

    response = Mock(ok=True, status_code=200, text="ok", headers={})
    response.json.return_value = {
        "choices": [{
            "message": {
                "content": "",
                "reasoning_content": json.dumps({
                    "status": "risk",
                    "reasons": ["seller_watermark"],
                    "placement": "overlay",
                    "detected_text": ["SHOP"],
                    "confidence": 0.97,
                    "evidence": "A seller watermark is visible.",
                }),
            }
        }]
    }
    provider = AgnesProvider("test-key")
    provider._acquire_text = lambda: None
    provider._session = Mock()
    provider._session.post.return_value = response

    result = provider.assess_image("https://img.example/source.jpg")

    assert result["status"] == "risk"
    assert result["reasons"] == ["seller_watermark"]
    assert result["detected_text"] == ["SHOP"]

def test_agnes_main_text_policy_uses_strict_prompt():
    from amazon_processor.providers.agnes import AgnesProvider

    response = Mock(ok=True, status_code=200, text="ok", headers={})
    response.json.return_value = {
        "choices": [{
            "message": {
                "content": json.dumps({
                    "status": "risk",
                    "reasons": ["visible_text"],
                    "placement": "product_surface",
                    "detected_text": ["12V"],
                    "confidence": 0.99,
                    "evidence": "12V is printed on the product.",
                })
            }
        }]
    }
    provider = AgnesProvider("test-key")
    provider._acquire_text = lambda: None
    provider._session = Mock()
    provider._session.post.return_value = response

    result = provider.assess_image(
        "https://img.example/source.jpg",
        policy="main_text_free",
    )

    request_json = provider._session.post.call_args.kwargs["json"]
    assert "strict zero-text policy" in (
        request_json["messages"][0]["content"][1]["text"].lower()
    )
    assert result["status"] == "risk"
    assert result["reasons"] == ["visible_text"]
    assert result["policy_version"] == "main_text_zero_text_v3"


def test_agnes_batch_review_uploads_local_image_data(monkeypatch):
    from amazon_processor.providers.agnes import AgnesProvider

    response = Mock(ok=True, status_code=200, text="ok", headers={})
    response.json.return_value = {
        "choices": [{"message": {"content": "", "reasoning_content": json.dumps({
            "results": [
                {
                    "index": 1,
                    "status": "safe",
                    "reasons": [],
                    "placement": "none",
                    "confidence": 0.9,
                    "evidence": "No risk is visible.",
                },
                {
                    "index": 2,
                    "status": "risk",
                    "reasons": ["brand_logo"],
                    "placement": "packaging",
                    "confidence": 0.9,
                    "evidence": "A logo is visible.",
                },
            ]
        })}}]
    }
    provider = AgnesProvider("test-key", vision_model="agnes-2.0-flash")
    provider._acquire_text = lambda: None
    provider._session = Mock()
    provider._session.post.return_value = response
    monkeypatch.setattr(
        provider,
        "_download_image_data_url",
        lambda _url: "data:image/jpeg;base64,AA==",
    )

    result = provider.assess_images([
        "https://img.example/one.jpg",
        "https://img.example/two.jpg",
    ])

    request = provider._session.post.call_args.kwargs["json"]
    image_parts = [
        item for item in request["messages"][0]["content"]
        if item["type"] == "image_url"
    ]
    assert request["model"] == "agnes-2.0-flash"
    assert len(image_parts) == 2
    assert [item["status"] for item in result] == ["safe", "risk"]


def test_composite_falls_back_immediately_after_primary_429():
    from amazon_processor.providers.composite import CompositeProvider

    rate_limited = Mock(
        ok=False,
        status_code=429,
        text="rate limited",
        headers={},
    )
    provider = CompositeProvider({
        "text_provider": "deepseek",
        "vision_provider": "agnes",
        "image_gen_provider": "agnes",
        "deepseek_key": "test-deepseek",
        "agnes_key": "test-agnes",
        "agnes_image_fallback_model": "agnes-image-fallback",
    })
    primary = provider._providers["image_gen"]
    primary._acquire_image = lambda: None
    primary._sleep = lambda seconds: sleeps.append(seconds)
    primary._session = Mock()
    primary._session.post.return_value = rate_limited
    fallback = Mock()
    fallback.call_image_gen.return_value = (
        "https://generated.example/fallback.png"
    )
    provider._image_gen_fallbacks = [fallback]
    sleeps = []

    result = provider.call_image_gen(
        "https://img.example/source.jpg",
    )

    assert result == "https://generated.example/fallback.png"
    assert primary._session.post.call_count == 1
    assert sleeps == []
