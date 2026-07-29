from __future__ import annotations

import json

from crosspilot.image_risk import (
    IMAGE_RISK_POLICY_VERSION,
    IMAGE_RISK_SCHEMA_VERSION,
    assessment_from_legacy,
    assessment_is_intrinsic_brand,
    parse_image_assessment_response,
)


def test_parse_safe_structured_assessment() -> None:
    result = parse_image_assessment_response(
        json.dumps(
            {
                "status": "safe",
                "reasons": [],
                "placement": "none",
                "detected_text": [],
                "confidence": 0.98,
                "evidence": "Only the unbranded product is visible.",
            }
        )
    )

    assert result["schema_version"] == IMAGE_RISK_SCHEMA_VERSION
    assert result["policy_version"] == IMAGE_RISK_POLICY_VERSION
    assert result["status"] == "safe"
    assert result["reasons"] == []
    assert result["confidence"] == 0.98


def test_parse_risk_aliases_and_detect_intrinsic_brand_product() -> None:
    result = parse_image_assessment_response(
        """
        ```json
        {
          "status": "RISK",
          "reasons": ["logo", "brand"],
          "placement": "on-product",
          "detected_text": ["TOYOTA"],
          "confidence": 0.95,
          "evidence": "Toyota emblem is the product."
        }
        ```
        """
    )

    assert result["status"] == "risk"
    assert result["reasons"] == ["brand_logo"]
    assert result["placement"] == "product_surface"
    assert result["detected_text"] == ["TOYOTA"]
    assert assessment_is_intrinsic_brand(result) is True


def test_invalid_or_incomplete_response_becomes_unknown() -> None:
    invalid_json = parse_image_assessment_response("not json")
    incomplete = parse_image_assessment_response('{"status":"safe"}')

    assert invalid_json is None
    assert incomplete is not None
    assert incomplete["status"] == "safe"


def test_legacy_assessment_is_compatible_but_not_current_cache_schema() -> None:
    safe = assessment_from_legacy(False)
    risk = assessment_from_legacy(True)
    unknown = assessment_from_legacy(None)

    assert safe["status"] == "safe"
    assert risk["status"] == "risk"
    assert risk["reasons"] == ["unclassified_risk"]
    assert unknown is None
    assert safe["schema_version"] == 1
    assert safe["policy_version"] == "legacy_boolean_compat"
