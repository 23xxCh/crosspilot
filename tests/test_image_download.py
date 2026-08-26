from __future__ import annotations

from io import BytesIO
import socket

from PIL import Image
import pytest

from amazon_processor.images.download import (
    ImageDownloadError,
    download_public_image,
)


def _png_bytes() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (8, 8), "white").save(stream, format="PNG")
    return stream.getvalue()


class _Response:
    def __init__(
        self,
        *,
        status_code: int = 200,
        chunks: list[bytes] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._chunks = list(chunks or [])
        self.headers = dict(headers or {"content-type": "image/png"})

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(str(self.status_code))

    def iter_content(self, _size: int):
        yield from self._chunks

    def close(self) -> None:
        return None


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []
        self.trust_env = True

    def get(self, url: str, **_kwargs):
        self.calls.append(url)
        return self.responses.pop(0)

    def close(self) -> None:
        return None


def _public_dns(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


def test_private_image_target_is_rejected_before_request(monkeypatch) -> None:
    monkeypatch.setattr(
        "amazon_processor.images.download.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))
        ],
    )
    session = _Session([_Response(chunks=[_png_bytes()])])

    with pytest.raises(ImageDownloadError, match="公网") as exc_info:
        download_public_image(
            "http://localhost/internal.png",
            session=session,
        )

    assert exc_info.value.code == "unsafe_target"
    assert session.calls == []


def test_redirect_target_is_revalidated_before_following(monkeypatch) -> None:
    monkeypatch.setattr(
        "amazon_processor.images.download.socket.getaddrinfo",
        _public_dns,
    )
    session = _Session([
        _Response(
            status_code=302,
            chunks=[],
            headers={"location": "http://127.0.0.1/private.png"},
        )
    ])

    with pytest.raises(ImageDownloadError) as exc_info:
        download_public_image(
            "https://example.com/source.png",
            session=session,
        )

    assert exc_info.value.code == "unsafe_target"
    assert session.calls == ["https://example.com/source.png"]


def test_streaming_image_limit_stops_oversized_response(monkeypatch) -> None:
    monkeypatch.setattr(
        "amazon_processor.images.download.socket.getaddrinfo",
        _public_dns,
    )
    session = _Session([
        _Response(chunks=[b"a" * 8, b"b" * 8])
    ])

    with pytest.raises(ImageDownloadError) as exc_info:
        download_public_image(
            "https://example.com/large.png",
            max_bytes=10,
            session=session,
        )

    assert exc_info.value.code == "image_too_large"


def test_valid_public_image_is_decoded(monkeypatch) -> None:
    monkeypatch.setattr(
        "amazon_processor.images.download.socket.getaddrinfo",
        _public_dns,
    )
    content = _png_bytes()
    session = _Session([_Response(chunks=[content])])

    result = download_public_image(
        "https://example.com/product.png",
        session=session,
    )

    assert result.content == content
    assert result.mime_type == "image/png"
    assert result.width == 8
    assert result.height == 8
    assert session.trust_env is False
