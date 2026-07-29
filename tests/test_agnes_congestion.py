import json
from unittest.mock import Mock

import pytest
import requests


def test_congestion_gate_opens_and_recovers_with_one_half_open_probe():
    from scripts.providers.congestion import CongestionGate

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
    from scripts.providers.congestion import CongestionPolicy

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
    from scripts.providers.agnes import AgnesProvider
    from scripts.providers.congestion import CongestionPolicy
    from scripts.providers.errors import ProviderUnavailableError

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


def test_agnes_open_circuit_skips_http_until_cooldown():
    from scripts.providers.agnes import AgnesProvider
    from scripts.providers.congestion import CongestionPolicy
    from scripts.providers.errors import ProviderUnavailableError

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
    from scripts.providers.composite import CompositeProvider

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
    from scripts.providers.composite import CompositeProvider

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
    from scripts.providers.composite import CompositeProvider

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
    from scripts.providers.agnes import AgnesProvider
    from scripts.providers.errors import ProviderTimeoutError

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
    from scripts.providers.agnes import AgnesProvider

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


def test_composite_falls_back_immediately_after_primary_429():
    from scripts.providers.composite import CompositeProvider

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
