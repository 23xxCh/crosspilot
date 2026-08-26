from __future__ import annotations

from io import BytesIO
import json
from unittest.mock import Mock

from PIL import Image
import pytest
import requests

from amazon_processor.images.download import DownloadedImage, ImageDownloadError
from amazon_processor.providers.deepseek import DeepSeekProvider
from amazon_processor.providers.support import (
    ProviderAuthError,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderUnavailableError,
    classify_http_error,
)


def image_bytes() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (8, 8), "white").save(stream, format="PNG")
    return stream.getvalue()


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        *,
        payload: dict | None = None,
        content: bytes = b"",
        text: str = "",
    ) -> None:
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload or {}
        self.content = content
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.HTTPError(str(self.status_code))


def chat_response(content: str = "", reasoning: str = "") -> FakeResponse:
    return FakeResponse(payload={
        "choices": [{
            "message": {
                "content": content,
                "reasoning_content": reasoning,
            }
        }]
    })


def safe_assessment(index: int | None = None) -> dict:
    value = {
        "status": "safe",
        "reasons": [],
        "placement": "none",
        "detected_text": [],
        "confidence": 0.99,
        "evidence": "Clean product image",
    }
    if index is not None:
        value["index"] = index
    return value


def patch_image_download(monkeypatch) -> None:
    monkeypatch.setattr(
        "amazon_processor.providers.deepseek.download_public_image",
        lambda *_args, **_kwargs: DownloadedImage(
            content=image_bytes(),
            mime_type="image/png",
            width=8,
            height=8,
            final_url="https://example.com/product.png",
        ),
    )


def test_http_error_classification_and_secret_redaction() -> None:
    assert isinstance(classify_http_error("deepseek", "vision", 401), ProviderAuthError)
    assert isinstance(classify_http_error("deepseek", "vision", 402), ProviderQuotaError)
    assert isinstance(classify_http_error("deepseek", "vision", 429), ProviderRateLimitError)
    assert isinstance(classify_http_error("deepseek", "vision", 503), ProviderUnavailableError)
    error = classify_http_error(
        "deepseek",
        "vision",
        500,
        "Bearer secret-value",
    )
    assert "secret-value" not in json.dumps(error.to_dict())


def test_single_vision_request_uses_data_uri_original_detail_and_json_output(
    monkeypatch,
) -> None:
    patch_image_download(monkeypatch)
    provider = DeepSeekProvider("secret-key")
    provider._session.post = Mock(return_value=chat_response(
        json.dumps(safe_assessment())
    ))
    result = provider.assess_image("https://example.com/product.png")
    assert result and result["status"] == "safe"
    request = provider._session.post.call_args.kwargs["json"]
    assert request["model"] == "deepseek-v4-flash-vision-exp"
    assert request["response_format"] == {"type": "json_object"}
    image_block = next(
        item
        for item in request["messages"][0]["content"]
        if item["type"] == "image_url"
    )
    assert image_block["image_url"]["url"].startswith("data:image/png;base64,")
    assert image_block["image_url"]["detail"] == "original"


def test_batch_vision_preserves_index_order(monkeypatch) -> None:
    patch_image_download(monkeypatch)
    provider = DeepSeekProvider("secret-key")
    response = {
        "results": [safe_assessment(2), {**safe_assessment(1), "evidence": "first"}]
    }
    provider._session.post = Mock(return_value=chat_response(json.dumps(response)))
    result = provider.assess_images([
        "https://example.com/1.png",
        "https://example.com/2.png",
    ])
    assert [item["evidence"] for item in result] == ["first", "Clean product image"]
    content = provider._session.post.call_args.kwargs["json"]["messages"][0]["content"]
    assert [item["text"] for item in content if item.get("text", "").startswith("IMAGE_INDEX=")] == [
        "IMAGE_INDEX=1",
        "IMAGE_INDEX=2",
    ]


def test_reasoning_content_is_accepted_for_structured_vision(monkeypatch) -> None:
    patch_image_download(monkeypatch)
    provider = DeepSeekProvider("secret-key")
    provider._session.post = Mock(return_value=chat_response(
        reasoning=json.dumps(safe_assessment())
    ))
    assert provider.assess_image("https://example.com/product.png")["status"] == "safe"


def test_transient_503_retries_but_auth_and_quota_stop(monkeypatch) -> None:
    patch_image_download(monkeypatch)
    monkeypatch.setattr("amazon_processor.providers.deepseek.time.sleep", lambda _value: None)
    provider = DeepSeekProvider("secret-key")
    provider._session.post = Mock(side_effect=[
        FakeResponse(503, text="busy"),
        chat_response(json.dumps(safe_assessment())),
    ])
    assert provider.assess_image("https://example.com/product.png")["status"] == "safe"
    assert provider._session.post.call_count == 2

    for status, error_type, body in [
        (401, ProviderAuthError, "invalid key"),
        (402, ProviderQuotaError, "insufficient balance"),
    ]:
        blocked = DeepSeekProvider("secret-key")
        blocked._session.post = Mock(return_value=FakeResponse(status, text=body))
        with pytest.raises(error_type):
            blocked.assess_image("https://example.com/product.png")
        assert blocked._session.post.call_count == 1


def test_invalid_image_and_malformed_json_are_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        "amazon_processor.providers.deepseek.download_public_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ImageDownloadError("image_decode_failed", "文件不是可解码图片")
        ),
    )
    provider = DeepSeekProvider("secret-key")
    with pytest.raises(ProviderResponseError, match="可解码"):
        provider.assess_image("https://example.com/product.png")

    patch_image_download(monkeypatch)
    provider._session.post = Mock(return_value=chat_response("not-json"))
    with pytest.raises(ProviderResponseError, match="结构化 JSON"):
        provider.assess_image("https://example.com/product.png")
