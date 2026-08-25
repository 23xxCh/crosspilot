"""Structured image-risk schema, parsing, validation, and assessment."""
from __future__ import annotations
import json
from pathlib import Path
import re
from typing import Any
IMAGE_RISK_SCHEMA_VERSION = 3
IMAGE_RISK_POLICY_VERSION = 'structured_image_text_translate_v2'
MAIN_TEXT_POLICY_VERSION = 'main_text_zero_text_v3'
MAIN_IMAGE_QUALITIES = {
    'preferred',
    'acceptable',
    'fallback',
    'unknown',
}
CONFIRMED_QUARANTINE_PATH = Path(__file__).resolve().parents[2] / 'config' / 'amazon_image_quarantine_ids.json'
RISK_REASONS = {
    'brand_logo',
    'seller_watermark',
    'person',
    'non_product_text',
    'non_english_product_text',
    'visible_text',
    'unclassified_risk',
}
RISK_PLACEMENTS = {'none', 'overlay', 'product_surface', 'packaging', 'background', 'unknown'}
_REASON_ALIASES = {
    'brand': 'brand_logo',
    'brand_name': 'brand_logo',
    'logo': 'brand_logo',
    'watermark': 'seller_watermark',
    'seller': 'seller_watermark',
    'human': 'person',
    'people': 'person',
    'text': 'visible_text',
    'nonproduct_text': 'non_product_text',
    'promo_text': 'non_product_text',
    'promotional_text': 'non_product_text',
    'foreign_text': 'non_english_product_text',
    'non_english_text': 'non_english_product_text',
    'translate_text': 'non_english_product_text',
    'any_text': 'visible_text',
}
_PLACEMENT_ALIASES = {'product': 'product_surface', 'on_product': 'product_surface', 'surface': 'product_surface', 'label': 'product_surface', 'box': 'packaging', 'package': 'packaging', 'scene': 'background'}
EDIT_REMOVE_REASONS = {
    'brand_logo',
    'seller_watermark',
    'person',
    'non_product_text',
    'unclassified_risk',
}
EDIT_TRANSLATE_REASONS = {'non_english_product_text'}

def unknown_image_assessment(evidence: str='image assessment unavailable') -> dict[str, Any]:
    return {'schema_version': IMAGE_RISK_SCHEMA_VERSION, 'status': 'unknown', 'reasons': [], 'placement': 'unknown', 'detected_text': [], 'confidence': 0.0, 'evidence': str(evidence or 'image assessment unavailable')[:500]}

def _clean_reason(value: object) -> str:
    reason = re.sub('[^a-z0-9_]+', '_', str(value or '').lower()).strip('_')
    reason = _REASON_ALIASES.get(reason, reason)
    return reason if reason in RISK_REASONS else ''

def _clean_placement(value: object) -> str:
    placement = re.sub('[^a-z0-9_]+', '_', str(value or '').lower()).strip('_')
    placement = _PLACEMENT_ALIASES.get(placement, placement)
    return placement if placement in RISK_PLACEMENTS else 'unknown'

def normalize_image_assessment(value: object) -> dict[str, Any] | None:
    """Validate and normalize one structured model assessment."""
    if not isinstance(value, dict):
        return None
    status = str(value.get('status') or '').strip().lower()
    if status not in {'safe', 'risk', 'unknown'}:
        return None
    raw_reasons = value.get('reasons') or []
    if isinstance(raw_reasons, str):
        raw_reasons = [raw_reasons]
    if not isinstance(raw_reasons, list):
        return None
    reasons = list(dict.fromkeys((reason for reason in (_clean_reason(item) for item in raw_reasons) if reason)))
    if status == 'risk' and (not reasons):
        reasons = ['unclassified_risk']
    if status == 'safe':
        reasons = []
    raw_text = value.get('detected_text') or []
    if isinstance(raw_text, str):
        raw_text = [raw_text]
    if not isinstance(raw_text, list):
        raw_text = []
    detected_text = [str(item).strip()[:120] for item in raw_text[:10] if str(item or '').strip()]
    try:
        confidence = float(value.get('confidence', 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = round(max(0.0, min(1.0, confidence)), 3)
    placement = 'none' if status == 'safe' else _clean_placement(value.get('placement'))
    evidence = str(value.get('evidence') or '').strip()[:500]
    if not evidence:
        evidence = 'No prohibited visual content detected' if status == 'safe' else 'No assessment evidence returned'
    return {'schema_version': IMAGE_RISK_SCHEMA_VERSION, 'policy_version': IMAGE_RISK_POLICY_VERSION, 'status': status, 'reasons': reasons, 'risk_categories': list(reasons), 'placement': placement, 'detected_text': detected_text, 'confidence': confidence, 'evidence': evidence}

def normalize_main_text_assessment(value: object) -> dict[str, Any] | None:
    """Normalize strict main-image zero-text assessment."""
    normalized = normalize_image_assessment(value)
    if normalized is None:
        return None
    if normalized["status"] == "risk":
        normalized["reasons"] = ["visible_text"]
        normalized["risk_categories"] = ["visible_text"]
    quality = re.sub(
        '[^a-z0-9_]+',
        '_',
        str(value.get('main_image_quality') or '').lower(),
    ).strip('_')
    normalized['main_image_quality'] = (
        quality if quality in MAIN_IMAGE_QUALITIES else 'unknown'
    )
    normalized["policy_version"] = MAIN_TEXT_POLICY_VERSION
    return normalized

def unknown_main_text_assessment(
    evidence: str = "main-image text assessment unavailable",
) -> dict[str, Any]:
    result = unknown_image_assessment(evidence)
    result["policy_version"] = MAIN_TEXT_POLICY_VERSION
    result["main_image_quality"] = "unknown"
    return result

def _parse_assessment_response(
    raw: object,
    normalizer,
) -> dict[str, Any] | None:
    text = str(raw or '').strip()
    if text.startswith('```'):
        text = re.sub('^```(?:json)?\\s*', '', text, flags=re.IGNORECASE)
        text = re.sub('\\s*```$', '', text)
    candidates = [text]
    start = text.find('{')
    end = text.rfind('}')
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        normalized = normalizer(value)
        if normalized is not None:
            return normalized
    return None

def parse_image_assessment_response(raw: object) -> dict[str, Any] | None:
    """Extract the first valid general-risk assessment JSON object."""
    return _parse_assessment_response(raw, normalize_image_assessment)

def parse_main_text_assessment_response(raw: object) -> dict[str, Any] | None:
    """Extract one strict main-image zero-text assessment."""
    return _parse_assessment_response(raw, normalize_main_text_assessment)


def _parse_assessment_batch_response(
    raw: object,
    normalizer,
    *,
    expected_count: int,
) -> list[dict[str, Any]] | None:
    """Extract an ordered assessment list from one multi-image response."""
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            envelope = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        values = envelope.get("results") if isinstance(envelope, dict) else None
        if not isinstance(values, list) or len(values) != expected_count:
            continue
        indexed: dict[int, dict[str, Any]] = {}
        for value in values:
            if not isinstance(value, dict):
                break
            try:
                index = int(value.get("index"))
            except (TypeError, ValueError):
                break
            normalized = normalizer(value)
            if normalized is None or index in indexed:
                break
            indexed[index] = normalized
        else:
            expected = list(range(1, expected_count + 1))
            if sorted(indexed) == expected:
                return [indexed[index] for index in expected]
    return None


def parse_image_assessment_batch_response(
    raw: object,
    *,
    expected_count: int,
) -> list[dict[str, Any]] | None:
    return _parse_assessment_batch_response(
        raw,
        normalize_image_assessment,
        expected_count=expected_count,
    )


def parse_main_text_assessment_batch_response(
    raw: object,
    *,
    expected_count: int,
) -> list[dict[str, Any]] | None:
    return _parse_assessment_batch_response(
        raw,
        normalize_main_text_assessment,
        expected_count=expected_count,
    )

def assessment_from_legacy(value: object) -> dict[str, Any] | None:
    """Compatibility adapter for callers still returning bool/None."""
    if value is None:
        return None
    if value is False:
        normalized = normalize_image_assessment({'status': 'safe', 'reasons': [], 'placement': 'none', 'confidence': 0.5, 'evidence': 'Legacy boolean image review returned NO'})
        normalized['schema_version'] = 1
        normalized['policy_version'] = 'legacy_boolean_compat'
        return normalized
    if value is True:
        normalized = normalize_image_assessment({'status': 'risk', 'reasons': ['unclassified_risk'], 'placement': 'unknown', 'confidence': 0.5, 'evidence': 'Legacy boolean image review returned YES'})
        normalized['schema_version'] = 1
        normalized['policy_version'] = 'legacy_boolean_compat'
        return normalized
    return normalize_image_assessment(value)

def assessment_status(value: object) -> str:
    normalized = normalize_image_assessment(value)
    return normalized['status'] if normalized else 'unknown'

def image_action(value: object) -> str:
    """Return the business action for one image assessment."""
    normalized = normalize_image_assessment(value)
    if normalized is None:
        return 'block_publish'
    if normalized['status'] == 'safe':
        return 'keep'
    if normalized['status'] == 'unknown':
        if isinstance(value, dict) and value.get('operational_failure'):
            return 'block_publish'
        return 'keep_review'
    reasons = set(normalized['reasons'])
    if reasons & EDIT_TRANSLATE_REASONS:
        return 'edit_translate'
    if reasons & EDIT_REMOVE_REASONS:
        return 'edit_remove'
    return 'keep_review'

def image_requires_edit(value: object) -> bool:
    return image_action(value) in {'edit_remove', 'edit_translate'}


def attachment_should_delete(value: object) -> bool:
    """Delete every attachment that the structured review marks as risk."""
    normalized = normalize_image_assessment(value)
    return bool(
        normalized
        and normalized['status'] == 'risk'
    )

def assessment_is_safe(value: object) -> bool:
    return assessment_status(value) == 'safe'

def assessment_is_risk(value: object) -> bool:
    return assessment_status(value) == 'risk'

def assessment_is_intrinsic_brand(value: object) -> bool:
    normalized = normalize_image_assessment(value)
    return bool(normalized and normalized['status'] == 'risk' and ('brand_logo' in normalized['reasons']) and (normalized['placement'] in {'product_surface', 'packaging'}))

def load_confirmed_image_quarantine() -> dict[str, dict[str, str]]:
    """Load human-confirmed product IDs that must never be model-released."""
    try:
        value = json.loads(CONFIRMED_QUARANTINE_PATH.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    products = value.get('products') if isinstance(value, dict) else {}
    if not isinstance(products, dict):
        return {}
    return {str(product_id): {'reason': str((item or {}).get('reason') or ''), 'source': str((item or {}).get('source') or '')} for product_id, item in products.items() if isinstance(item, dict)}
from io import BytesIO
import requests
from ..log import log as _log
from ..providers.support import (
    ProviderAuthError,
    ProviderQuotaError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
ROLE_PRIORITY = {'main': 0, 'variant': 1, 'attachment': 2}

def validate_image_url(url: str, *, timeout_s: float=30, max_bytes: int=25 * 1024 * 1024) -> tuple[bool, str]:
    """Download and decode an image URL before it may enter formal output."""
    url = str(url or '').strip()
    if not url.startswith(('http://', 'https://')):
        return (False, 'invalid_url')
    try:
        response = requests.get(url, timeout=max(1.0, float(timeout_s)), stream=True, headers={'User-Agent': 'AmazonProcessor/1.0'})
        response.raise_for_status()
        content_type = str(response.headers.get('content-type') or '').lower()
        if content_type and 'image/' not in content_type:
            return (False, 'non_image_content_type')
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                return (False, 'image_too_large')
            chunks.append(chunk)
        if not chunks:
            return (False, 'empty_image')
        from PIL import Image
        with Image.open(BytesIO(b''.join(chunks))) as image:
            image.verify()
            if image.width < 1 or image.height < 1:
                return (False, 'invalid_dimensions')
        return (True, '')
    except ImportError:
        return (False, 'image_decoder_unavailable')
    except requests.Timeout:
        return (False, 'download_timeout')
    except requests.RequestException:
        return (False, 'download_failed')
    except Exception:
        return (False, 'image_decode_failed')

def safe_assess(
    provider: object,
    url: str,
    *,
    confirmation: bool = False,
    policy: str = "general",
) -> dict[str, Any]:
    """Run structured assessment and convert malformed/errors to unknown."""
    try:
        if policy == "main_text_free":
            value = provider.assess_image(
                url,
                confirmation=False,
                policy=policy,
            )
        else:
            value = provider.assess_image(url, confirmation=confirmation)
    except (ProviderAuthError, ProviderQuotaError):
        raise
    except Exception as exc:
        _log.warn('结构化图审异常', error=str(exc)[:100])
        if policy == "main_text_free":
            result = unknown_main_text_assessment(type(exc).__name__)
        else:
            result = unknown_image_assessment(type(exc).__name__)
        result["operational_failure"] = True
        return result
    normalizer = (
        normalize_main_text_assessment
        if policy == "main_text_free"
        else normalize_image_assessment
    )
    normalized = normalizer(value)
    if normalized is None:
        if policy == "main_text_free":
            result = unknown_main_text_assessment(
                'provider returned no valid main-image text assessment'
            )
        else:
            result = unknown_image_assessment(
                'provider returned no valid structured assessment'
            )
        result["operational_failure"] = True
        return result
    return normalized


def safe_assess_batch(
    provider: object,
    urls: list[str],
    *,
    policy: str = "general",
) -> list[dict[str, Any]]:
    """Assess a small image batch, falling back to local single-item calls."""
    if not urls:
        return []
    method = getattr(provider, "assess_images", None)
    if not callable(method):
        return [safe_assess(provider, url, policy=policy) for url in urls]
    try:
        values = method(urls, policy=policy)
    except (ProviderAuthError, ProviderQuotaError):
        raise
    except (ProviderTimeoutError, ProviderUnavailableError) as exc:
        _log.warn("批量结构化图审暂不可用", error=str(exc)[:100])
        results = []
        for _url in urls:
            unknown = (
                unknown_main_text_assessment(type(exc).__name__)
                if policy == "main_text_free"
                else unknown_image_assessment(type(exc).__name__)
            )
            unknown["operational_failure"] = True
            results.append(unknown)
        return results
    except Exception as exc:
        _log.warn("批量结构化图审异常", error=str(exc)[:100])
        if len(urls) > 1:
            return [
                safe_assess_batch(provider, [url], policy=policy)[0]
                for url in urls
            ]
        unknown = (
            unknown_main_text_assessment(type(exc).__name__)
            if policy == "main_text_free"
            else unknown_image_assessment(type(exc).__name__)
        )
        unknown["operational_failure"] = True
        return [unknown]
    if not isinstance(values, list) or len(values) != len(urls):
        values = [None] * len(urls)
    normalizer = (
        normalize_main_text_assessment
        if policy == "main_text_free"
        else normalize_image_assessment
    )
    results = []
    for value in values:
        normalized = normalizer(value)
        if normalized is None:
            normalized = (
                unknown_main_text_assessment("invalid batch assessment")
                if policy == "main_text_free"
                else unknown_image_assessment("invalid batch assessment")
            )
            normalized["operational_failure"] = True
        results.append(normalized)
    return results

def row_image_roles(row: dict[str, Any]) -> list[tuple[str, str, int]]:
    """Enumerate a product's source images with stable role positions."""
    values: list[tuple[str, str, int]] = []
    main = str(row.get('main_img') or '').strip()
    if main:
        values.append((main, 'main', 0))
    values.extend(((str(url).strip(), 'variant', index) for index, url in enumerate(row.get('var_imgs') or []) if str(url or '').strip()))
    values.extend(((str(url).strip(), 'attachment', index) for index, url in enumerate(row.get('extra_imgs') or []) if str(url or '').strip()))
    return values

def assessment_record(*, url: str, role: str, assessment: dict[str, Any], source: str, source_url: str | None=None, text_assessment: dict[str, Any] | None=None) -> dict[str, Any]:
    """Build the review-package record for one source or generated image."""
    record = {'url': url, 'role': role, 'source': source, 'source_url': source_url or '', 'assessment': dict(assessment), 'image_action': image_action(assessment), 'detected_text': list(assessment.get('detected_text') or []), 'decision': '', 'evidence': assessment.get('evidence', '')}
    if text_assessment is not None:
        record['text_assessment'] = dict(text_assessment)
        record['detected_text'] = list(text_assessment.get('detected_text') or [])
        record['text_evidence'] = str(text_assessment.get('evidence') or '')
    return record
