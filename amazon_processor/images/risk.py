"""Structured image-risk schema, parsing, validation, and assessment."""
from __future__ import annotations
import json
from pathlib import Path
import re
from typing import Any
IMAGE_RISK_SCHEMA_VERSION = 2
IMAGE_RISK_POLICY_VERSION = 'structured_image_safety_v2'
CONFIRMED_QUARANTINE_PATH = Path(__file__).resolve().parents[2] / 'config' / 'amazon_image_quarantine_ids.json'
RISK_REASONS = {'brand_logo', 'seller_watermark', 'person', 'non_product_text', 'unclassified_risk'}
RISK_PLACEMENTS = {'none', 'overlay', 'product_surface', 'packaging', 'background', 'unknown'}
_REASON_ALIASES = {'brand': 'brand_logo', 'brand_name': 'brand_logo', 'logo': 'brand_logo', 'watermark': 'seller_watermark', 'seller': 'seller_watermark', 'human': 'person', 'people': 'person', 'text': 'non_product_text', 'nonproduct_text': 'non_product_text'}
_PLACEMENT_ALIASES = {'product': 'product_surface', 'on_product': 'product_surface', 'surface': 'product_surface', 'label': 'product_surface', 'box': 'packaging', 'package': 'packaging', 'scene': 'background'}

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

def parse_image_assessment_response(raw: object) -> dict[str, Any] | None:
    """Extract the first valid assessment JSON object from model output."""
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
        normalized = normalize_image_assessment(value)
        if normalized is not None:
            return normalized
    return None

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
from typing import Any
import requests
from ..log import log as _log
from ..providers.support import ProviderQuotaError
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

def safe_assess(provider: object, url: str, *, confirmation: bool=False) -> dict[str, Any]:
    """Run structured assessment and convert malformed/errors to unknown."""
    try:
        value = provider.assess_image(url, confirmation=confirmation)
    except ProviderQuotaError:
        raise
    except Exception as exc:
        _log.warn('结构化图审异常', error=str(exc)[:100])
        return unknown_image_assessment(type(exc).__name__)
    normalized = normalize_image_assessment(value)
    if normalized is None:
        return unknown_image_assessment('provider returned no valid structured assessment')
    return normalized

def row_image_roles(row: dict[str, Any]) -> list[tuple[str, str, int]]:
    """Enumerate a product's source images with stable role positions."""
    values: list[tuple[str, str, int]] = []
    main = str(row.get('main_img') or '').strip()
    if main:
        values.append((main, 'main', 0))
    values.extend(((str(url).strip(), 'variant', index) for index, url in enumerate(row.get('var_imgs') or []) if str(url or '').strip()))
    values.extend(((str(url).strip(), 'attachment', index) for index, url in enumerate(row.get('extra_imgs') or []) if str(url or '').strip()))
    return values

def assessment_record(*, url: str, role: str, assessment: dict[str, Any], source: str, source_url: str | None=None) -> dict[str, Any]:
    """Build the review-package record for one source or generated image."""
    return {'url': url, 'role': role, 'source': source, 'source_url': source_url or '', 'assessment': dict(assessment), 'decision': '', 'evidence': assessment.get('evidence', '')}
