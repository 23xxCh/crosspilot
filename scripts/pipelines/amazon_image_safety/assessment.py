"""Structured image assessment and source-image helpers."""
from __future__ import annotations

from io import BytesIO
from typing import Any

import requests

from crosspilot.image_risk import (
    normalize_image_assessment,
    unknown_image_assessment,
)
from ...model_provider import ProviderQuotaError
from ...pipeline_log import log as _log


ROLE_PRIORITY = {"main": 0, "variant": 1, "attachment": 2}


def validate_image_url(
    url: str,
    *,
    timeout_s: float = 30,
    max_bytes: int = 25 * 1024 * 1024,
) -> tuple[bool, str]:
    """Download and decode an image URL before it may enter formal output."""
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return False, "invalid_url"
    try:
        response = requests.get(
            url,
            timeout=max(1.0, float(timeout_s)),
            stream=True,
            headers={"User-Agent": "CrossPilot/1.0"},
        )
        response.raise_for_status()
        content_type = str(response.headers.get("content-type") or "").lower()
        if content_type and "image/" not in content_type:
            return False, "non_image_content_type"
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                return False, "image_too_large"
            chunks.append(chunk)
        if not chunks:
            return False, "empty_image"
        from PIL import Image

        with Image.open(BytesIO(b"".join(chunks))) as image:
            image.verify()
            if image.width < 1 or image.height < 1:
                return False, "invalid_dimensions"
        return True, ""
    except ImportError:
        return False, "image_decoder_unavailable"
    except requests.Timeout:
        return False, "download_timeout"
    except requests.RequestException:
        return False, "download_failed"
    except Exception:
        return False, "image_decode_failed"


def safe_assess(
    provider: object,
    url: str,
    *,
    confirmation: bool = False,
) -> dict[str, Any]:
    """Run structured assessment and convert malformed/errors to unknown."""
    try:
        value = provider.assess_image(url, confirmation=confirmation)
    except ProviderQuotaError:
        raise
    except Exception as exc:
        _log.warn("结构化图审异常", error=str(exc)[:100])
        return unknown_image_assessment(type(exc).__name__)
    normalized = normalize_image_assessment(value)
    if normalized is None:
        return unknown_image_assessment(
            "provider returned no valid structured assessment"
        )
    return normalized


def row_image_roles(row: dict[str, Any]) -> list[tuple[str, str, int]]:
    """Enumerate a product's source images with stable role positions."""
    values: list[tuple[str, str, int]] = []
    main = str(row.get("main_img") or "").strip()
    if main:
        values.append((main, "main", 0))
    values.extend(
        (str(url).strip(), "variant", index)
        for index, url in enumerate(row.get("var_imgs") or [])
        if str(url or "").strip()
    )
    values.extend(
        (str(url).strip(), "attachment", index)
        for index, url in enumerate(row.get("extra_imgs") or [])
        if str(url or "").strip()
    )
    return values


def assessment_record(
    *,
    url: str,
    role: str,
    assessment: dict[str, Any],
    source: str,
    source_url: str | None = None,
) -> dict[str, Any]:
    """Build the review-package record for one source or generated image."""
    return {
        "url": url,
        "role": role,
        "source": source,
        "source_url": source_url or "",
        "assessment": dict(assessment),
        "decision": "",
        "evidence": assessment.get("evidence", ""),
    }


__all__ = [
    "ROLE_PRIORITY",
    "assessment_record",
    "row_image_roles",
    "safe_assess",
    "validate_image_url",
]
