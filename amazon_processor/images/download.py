"""Bounded public-network image downloads for untrusted collection URLs."""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlsplit

from PIL import Image
import requests


_IMAGE_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}
_REDIRECT_STATUS = {301, 302, 303, 307, 308}


class ImageDownloadError(ValueError):
    """Safe image-download failure with a stable machine code."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = str(code)
        self.retryable = bool(retryable)


@dataclass(frozen=True)
class DownloadedImage:
    content: bytes
    mime_type: str
    width: int
    height: int
    final_url: str


def _public_addresses(url: str) -> None:
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ImageDownloadError("invalid_url", "图片 URL 必须是有效的 HTTP(S) 地址")
    if parsed.username is not None or parsed.password is not None:
        raise ImageDownloadError("unsafe_target", "图片 URL 不允许包含登录凭据")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ImageDownloadError("invalid_url", "图片 URL 端口无效") from exc
    try:
        literal = ipaddress.ip_address(parsed.hostname)
        addresses = [literal]
    except ValueError:
        try:
            records = socket.getaddrinfo(
                parsed.hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise ImageDownloadError(
                "dns_failed",
                "图片域名解析失败",
                retryable=True,
            ) from exc
        addresses = []
        for record in records:
            try:
                addresses.append(ipaddress.ip_address(record[4][0]))
            except (IndexError, ValueError):
                continue
    if not addresses:
        raise ImageDownloadError(
            "dns_failed",
            "图片域名没有可用地址",
            retryable=True,
        )
    if any(not address.is_global for address in addresses):
        raise ImageDownloadError(
            "unsafe_target",
            "图片 URL 必须指向公网地址，禁止访问本机或内网",
        )


def download_public_image(
    url: str,
    *,
    timeout_s: float = 30,
    max_bytes: int = 20 * 1024 * 1024,
    max_pixels: int = 64_000_000,
    max_redirects: int = 3,
    session: Any | None = None,
) -> DownloadedImage:
    """Download and decode one public image with strict resource bounds."""
    current_url = str(url or "").strip()
    owned_session = session is None
    client = session or requests.Session()
    client.trust_env = False
    try:
        for redirect_index in range(max(0, int(max_redirects)) + 1):
            _public_addresses(current_url)
            try:
                response = client.get(
                    current_url,
                    timeout=max(1.0, float(timeout_s)),
                    stream=True,
                    allow_redirects=False,
                    headers={"User-Agent": "AmazonProcessor/1.0"},
                )
            except requests.Timeout as exc:
                raise ImageDownloadError(
                    "download_timeout",
                    "图片下载超时",
                    retryable=True,
                ) from exc
            except requests.RequestException as exc:
                raise ImageDownloadError(
                    "download_failed",
                    "图片下载失败",
                    retryable=True,
                ) from exc
            try:
                if response.status_code in _REDIRECT_STATUS:
                    location = str(response.headers.get("location") or "").strip()
                    if not location:
                        raise ImageDownloadError(
                            "invalid_redirect",
                            "图片重定向缺少目标地址",
                        )
                    if redirect_index >= max_redirects:
                        raise ImageDownloadError(
                            "too_many_redirects",
                            "图片重定向次数过多",
                        )
                    current_url = urljoin(current_url, location)
                    continue
                try:
                    response.raise_for_status()
                except requests.RequestException as exc:
                    raise ImageDownloadError(
                        "download_failed",
                        "图片下载返回非成功状态",
                        retryable=response.status_code >= 500,
                    ) from exc
                content_type = str(
                    response.headers.get("content-type") or ""
                ).lower()
                if content_type and "image/" not in content_type:
                    raise ImageDownloadError(
                        "non_image_content_type",
                        "图片 URL 返回的不是图片内容",
                    )
                content_length = str(
                    response.headers.get("content-length") or ""
                ).strip()
                if content_length.isdigit() and int(content_length) > max_bytes:
                    raise ImageDownloadError(
                        "image_too_large",
                        "图片超过允许大小",
                    )
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise ImageDownloadError(
                            "image_too_large",
                            "图片超过允许大小",
                        )
                    chunks.append(chunk)
                if not chunks:
                    raise ImageDownloadError("empty_image", "图片内容为空")
                content = b"".join(chunks)
            finally:
                response.close()
            try:
                with Image.open(BytesIO(content)) as image:
                    width, height = image.size
                    image_format = str(image.format or "").upper()
                    image.verify()
            except Exception as exc:
                raise ImageDownloadError(
                    "image_decode_failed",
                    "文件不是可解码图片",
                ) from exc
            mime_type = _IMAGE_MIME.get(image_format)
            if not mime_type:
                raise ImageDownloadError(
                    "unsupported_image_format",
                    "图片格式不受支持",
                )
            if width < 1 or height < 1 or width * height > max_pixels:
                raise ImageDownloadError(
                    "invalid_dimensions",
                    "图片像素尺寸无效或过大",
                )
            return DownloadedImage(
                content=content,
                mime_type=mime_type,
                width=width,
                height=height,
                final_url=current_url,
            )
    finally:
        if owned_session:
            client.close()
    raise ImageDownloadError("download_failed", "图片下载没有返回结果")


__all__ = [
    "DownloadedImage",
    "ImageDownloadError",
    "download_public_image",
]
