from __future__ import annotations

import json

from amazon_processor.images.risk import normalize_image_assessment
from amazon_processor.images import gate as amazon_image_safety
from amazon_processor.images import gate as remediation


def assessment(
    status: str,
    *,
    reasons: list[str] | None = None,
    placement: str = "none",
    evidence: str = "test evidence",
) -> dict:
    return normalize_image_assessment({
        "status": status,
        "reasons": reasons or [],
        "placement": placement,
        "detected_text": [],
        "confidence": 0.95,
        "evidence": evidence,
    })


class StructuredProvider:
    def __init__(
        self,
        assessments: dict[str, dict],
        *,
        confirmations: dict[str, dict] | None = None,
        generated: dict[str, str] | None = None,
    ):
        self.assessments = assessments
        self.confirmations = confirmations or {}
        self.generated = generated or {}
        self.assess_calls: list[tuple[str, bool]] = []
        self.gen_calls: list[tuple[str, bool, int]] = []

    def assess_image(
        self,
        url: str,
        *,
        confirmation: bool = False,
        retries: int = 3,
    ) -> dict | None:
        del retries
        self.assess_calls.append((url, confirmation))
        if confirmation:
            return self.confirmations.get(url)
        return self.assessments.get(url)

    def call_image_gen(
        self,
        url: str,
        *,
        is_variant: bool = False,
        context: str = "",
        route_offset: int = 0,
    ) -> str:
        del context
        self.gen_calls.append((url, is_variant, route_offset))
        return self.generated.get(url, "")


def run_gate(
    rows: list[dict],
    provider: StructuredProvider,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        remediation,
        "validate_image_url",
        lambda _url: (True, ""),
    )
    metrics = {}
    result = amazon_image_safety.run_structured_image_safety_gate(
        rows,
        str(tmp_path / "cache.json"),
        runtime_metrics=metrics,
        provider_getter=lambda: provider,
    )
    return result, metrics


def test_routes_each_role_and_rechecks_generated_variant(
    tmp_path,
    monkeypatch,
) -> None:
    main = "https://img.example/main.jpg"
    extra_safe = "https://img.example/safe-extra.jpg"
    extra_risk = "https://img.example/risk-extra.jpg"
    extra_unknown = "https://img.example/unknown-extra.jpg"
    variant = "https://img.example/risk-variant.jpg"
    generated = "https://generated.example/variant.png"
    provider = StructuredProvider(
        {
            main: assessment("safe"),
            extra_safe: assessment("safe"),
            extra_risk: assessment(
                "risk",
                reasons=["seller_watermark"],
                placement="overlay",
            ),
            extra_unknown: assessment("unknown", placement="unknown"),
            variant: assessment(
                "risk",
                reasons=["brand_logo"],
                placement="overlay",
            ),
            generated: assessment("safe"),
        },
        generated={variant: generated},
    )
    rows = [{
        "id": "p1",
        "title": "Product",
        "main_img": main,
        "var_img": variant,
        "var_imgs": [variant],
        "extra_imgs": [extra_safe, extra_risk, extra_unknown],
    }]

    result, metrics = run_gate(rows, provider, tmp_path, monkeypatch)

    assert len(result) == 1
    assert result[0]["main_img"] == main
    assert result[0]["var_imgs"] == [generated]
    assert result[0]["extra_imgs"] == [extra_safe]
    assert (generated, False) in provider.assess_calls
    assert metrics["image_safety_gate"]["attachment_deleted"] == 2
    assert metrics["image_safety_gate"]["quarantined_products"] == 0


def test_unknown_main_is_quarantined(tmp_path, monkeypatch) -> None:
    main = "https://img.example/unknown-main.jpg"
    provider = StructuredProvider({
        main: assessment("unknown", placement="unknown"),
    })
    rows = [{
        "id": "p2",
        "title": "Unknown product",
        "main_img": main,
        "var_img": "",
        "var_imgs": [],
        "extra_imgs": [],
    }]

    result, metrics = run_gate(rows, provider, tmp_path, monkeypatch)

    assert result == []
    quarantine = metrics["quarantined_products"]
    assert quarantine[0]["product_id"] == "p2"
    assert quarantine[0]["reasons"][0]["code"] == "unknown_main_image"


def test_legacy_boolean_provider_cannot_release_amazon_image(
    tmp_path,
    monkeypatch,
) -> None:
    main = "https://img.example/legacy-main.jpg"

    class LegacyBooleanProvider:
        def __init__(self):
            self.legacy_calls = 0

        def call_vision(self, _url: str) -> bool:
            self.legacy_calls += 1
            return False

    provider = LegacyBooleanProvider()
    rows = [{
        "id": "legacy-provider",
        "title": "Product",
        "main_img": main,
        "var_img": "",
        "var_imgs": [],
        "extra_imgs": [],
    }]

    result, metrics = run_gate(rows, provider, tmp_path, monkeypatch)

    assert result == []
    assert provider.legacy_calls == 0
    reason = metrics["quarantined_products"][0]["reasons"][0]
    assert reason["code"] == "unknown_main_image"


def test_intrinsic_brand_product_requires_two_reviews_and_is_quarantined(
    tmp_path,
    monkeypatch,
) -> None:
    main = "https://img.example/toyota-emblem.jpg"
    high_risk = assessment(
        "risk",
        reasons=["brand_logo"],
        placement="product_surface",
        evidence="Toyota emblem is the sold product.",
    )
    provider = StructuredProvider(
        {main: high_risk},
        confirmations={main: high_risk},
    )
    rows = [{
        "id": "177983309011686411",
        "title": "Generic Emblem for Toyota",
        "main_img": main,
        "var_img": "",
        "var_imgs": [],
        "extra_imgs": [],
    }]

    result, metrics = run_gate(rows, provider, tmp_path, monkeypatch)

    assert result == []
    assert (main, True) in provider.assess_calls
    reason = metrics["quarantined_products"][0]["reasons"][0]
    assert reason["code"] == "intrinsic_brand_product"
    assert provider.gen_calls == []


def test_high_risk_confirmation_conflict_enters_manual_quarantine(
    tmp_path,
    monkeypatch,
) -> None:
    main = "https://img.example/brand-badge.jpg"
    provider = StructuredProvider(
        {
            main: assessment(
                "risk",
                reasons=["brand_logo"],
                placement="product_surface",
            )
        },
        confirmations={main: assessment("safe")},
    )
    rows = [{
        "id": "p3",
        "title": "Badge",
        "main_img": main,
        "var_img": "",
        "var_imgs": [],
        "extra_imgs": [],
    }]

    result, metrics = run_gate(rows, provider, tmp_path, monkeypatch)

    assert result == []
    reason = metrics["quarantined_products"][0]["reasons"][0]
    assert reason["code"] == "high_risk_confirmation_conflict"


def test_generated_main_still_risky_quarantines_product(
    tmp_path,
    monkeypatch,
) -> None:
    main = "https://img.example/watermark-main.jpg"
    generated = "https://generated.example/still-logo.png"
    provider = StructuredProvider(
        {
            main: assessment(
                "risk",
                reasons=["seller_watermark"],
                placement="overlay",
            ),
            generated: assessment(
                "risk",
                reasons=["brand_logo"],
                placement="overlay",
            ),
        },
        generated={main: generated},
    )
    rows = [{
        "id": "p4",
        "title": "Product",
        "main_img": main,
        "var_img": "",
        "var_imgs": [],
        "extra_imgs": [],
    }]

    result, metrics = run_gate(rows, provider, tmp_path, monkeypatch)

    assert result == []
    reason_codes = {
        item["code"]
        for item in metrics["quarantined_products"][0]["reasons"]
    }
    assert "main_image_remediation_failed" in reason_codes
    assert metrics["image_safety_gate"]["generated_reviewed"] >= 1
    assert (
        metrics["image_remediation"]["generated_candidates_reviewed"]
        == metrics["image_safety_gate"]["generated_reviewed"]
    )


def test_old_boolean_cache_is_invalidated_and_not_reused(
    tmp_path,
    monkeypatch,
) -> None:
    main = "https://img.example/main.jpg"
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({
        "review_prompt_version": "old-yes-no",
        "review_results": {main: False},
    }), encoding="utf-8")
    provider = StructuredProvider({main: assessment("safe")})
    rows = [{
        "id": "p5",
        "title": "Product",
        "main_img": main,
        "var_img": "",
        "var_imgs": [],
        "extra_imgs": [],
    }]
    monkeypatch.setattr(
        remediation,
        "validate_image_url",
        lambda _url: (True, ""),
    )

    result = amazon_image_safety.run_structured_image_safety_gate(
        rows,
        str(cache_path),
        provider_getter=lambda: provider,
    )

    assert len(result) == 1
    assert (main, False) in provider.assess_calls
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache["risk_assessments"][main]["status"] == "safe"


def test_human_false_positive_override_releases_exact_image_role(
    tmp_path,
    monkeypatch,
) -> None:
    main = "https://img.example/brand-like-shape.jpg"
    high_risk = assessment(
        "risk",
        reasons=["brand_logo"],
        placement="product_surface",
    )
    provider = StructuredProvider(
        {main: high_risk},
        confirmations={main: high_risk},
    )
    monkeypatch.setattr(
        remediation,
        "load_manual_overrides",
        lambda _cache_path: {("p6", "main", main): {
            "product_id": "p6",
            "action": "false_positive",
            "role": "main",
            "image_url": main,
            "note": "人工确认只是无品牌几何图形",
        }},
    )
    rows = [{
        "id": "p6",
        "title": "Generic shape",
        "main_img": main,
        "var_img": "",
        "var_imgs": [],
        "extra_imgs": [],
    }]

    result, metrics = run_gate(rows, provider, tmp_path, monkeypatch)

    assert len(result) == 1
    assert result[0]["main_img"] == main
    record = result[0]["_image_assessments"][0]
    assert record["assessment"]["status"] == "safe"
    assert record["assessment"]["manual_override"] is True
    assert metrics["image_safety_gate"]["manual_overrides_applied"] == 1
