from __future__ import annotations

import json

import pytest

from amazon_processor.images.risk import (
    normalize_image_assessment,
    normalize_main_text_assessment,
    parse_image_assessment_batch_response,
    parse_main_text_assessment_response,
)
from amazon_processor.images.cache import (
    current_cache_versions,
    load_cache,
)
from amazon_processor.images import gate as amazon_image_safety
from amazon_processor.images import gate as remediation
from amazon_processor.images.gate import cached_generation


def assessment(
    status: str,
    *,
    reasons: list[str] | None = None,
    detected_text: list[str] | None = None,
    placement: str = "none",
    evidence: str = "test evidence",
) -> dict:
    return normalize_image_assessment({
        "status": status,
        "reasons": reasons or [],
        "placement": placement,
        "detected_text": detected_text or [],
        "confidence": 0.95,
        "evidence": evidence,
    })

def text_assessment(
    status: str,
    *,
    detected_text: list[str] | None = None,
    placement: str = "none",
    evidence: str = "zero-text test evidence",
    main_image_quality: str = "preferred",
) -> dict:
    return normalize_main_text_assessment({
        "status": status,
        "reasons": ["visible_text"] if status == "risk" else [],
        "placement": placement,
        "detected_text": detected_text or [],
        "confidence": 0.95,
        "evidence": evidence,
        "main_image_quality": main_image_quality,
    })

def test_main_text_parser_marks_functional_text_as_risk() -> None:
    result = parse_main_text_assessment_response(json.dumps({
        "status": "risk",
        "reasons": ["visible_text"],
        "placement": "product_surface",
        "detected_text": ["A8C"],
        "confidence": 0.8,
        "evidence": "Faint glyphs are visible.",
    }))

    assert result["status"] == "risk"
    assert result["detected_text"] == ["A8C"]
    assert result["policy_version"] == "main_text_zero_text_v3"


def test_main_text_parser_preserves_main_image_quality() -> None:
    result = parse_main_text_assessment_response(json.dumps({
        "status": "safe",
        "reasons": [],
        "placement": "none",
        "detected_text": [],
        "confidence": 0.96,
        "evidence": "Single product isolated on a clean white background.",
        "main_image_quality": "preferred",
    }))

    assert result["status"] == "safe"
    assert result["main_image_quality"] == "preferred"


def test_batch_parser_preserves_image_index_order() -> None:
    result = parse_image_assessment_batch_response(
        json.dumps({"results": [
            {
                "index": 2,
                "status": "risk",
                "reasons": ["brand_logo"],
                "placement": "product_surface",
                "confidence": 0.9,
                "evidence": "Logo is visible.",
            },
            {
                "index": 1,
                "status": "safe",
                "reasons": [],
                "placement": "none",
                "confidence": 0.9,
                "evidence": "No risk is visible.",
            },
        ]}),
        expected_count=2,
    )

    assert [item["status"] for item in result] == ["safe", "risk"]


class StructuredProvider:
    def __init__(
        self,
        assessments: dict[str, dict],
        *,
        confirmations: dict[str, dict] | None = None,
        generated: dict[str, str] | None = None,
        text_assessments: dict[str, dict] | None = None,
    ):
        self.assessments = assessments
        self.confirmations = confirmations or {}
        self.generated = generated or {}
        self.text_assessments = (
            text_assessments
            if text_assessments is not None
            else {
                url: text_assessment("safe")
                for url in assessments
            }
        )
        self.assess_calls: list[tuple[str, bool]] = []
        self.text_assess_calls: list[str] = []
        self.gen_calls: list[tuple[str, bool, int]] = []
        self.gen_contexts: list[str] = []
        self.gen_reference_free: list[bool] = []

    def assess_image(
        self,
        url: str,
        *,
        confirmation: bool = False,
        policy: str = "general",
        retries: int = 3,
    ) -> dict | None:
        del retries
        if policy == "main_text_free":
            self.text_assess_calls.append(url)
            return self.text_assessments.get(url)
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
        reference_free: bool = False,
    ) -> str:
        self.gen_contexts.append(context)
        self.gen_reference_free.append(reference_free)
        self.gen_calls.append((url, is_variant, route_offset))
        return self.generated.get(url, "")


def run_gate(
    rows: list[dict],
    provider: StructuredProvider,
    tmp_path,
    monkeypatch,
    *,
    mode: str = "generate_replacements",
):
    monkeypatch.setattr(
        remediation,
        "validate_image_url",
        lambda _url: (True, ""),
    )
    monkeypatch.setattr(
        amazon_image_safety,
        "_image_processing_mode",
        lambda: mode,
    )
    metrics = {}
    result = amazon_image_safety.run_structured_image_safety_gate(
        rows,
        str(tmp_path / "cache.json"),
        runtime_metrics=metrics,
        provider_getter=lambda: provider,
    )
    return result, metrics


def test_select_existing_promotes_first_clean_attachment_without_generation(
    tmp_path,
    monkeypatch,
) -> None:
    main = "https://img.example/original-main.jpg"
    text_attachment = "https://img.example/dimension.jpg"
    clean_attachment = "https://img.example/clean.jpg"
    risk_attachment = "https://img.example/logo.jpg"
    unknown_attachment = "https://img.example/unknown.jpg"
    safe_variant = "https://img.example/variant-safe.jpg"
    risk_variant = "https://img.example/variant-risk.jpg"
    provider = StructuredProvider(
        {
            main: assessment("safe"),
            text_attachment: assessment("safe"),
            clean_attachment: assessment("safe"),
            risk_attachment: assessment(
                "risk",
                reasons=["brand_logo"],
                placement="overlay",
            ),
            unknown_attachment: assessment("unknown"),
            safe_variant: assessment("safe"),
            risk_variant: assessment(
                "risk",
                reasons=["seller_watermark"],
                placement="overlay",
            ),
        },
        text_assessments={
            main: text_assessment("risk", detected_text=["START"]),
            text_attachment: text_assessment(
                "risk",
                detected_text=["10 CM"],
            ),
            clean_attachment: text_assessment("safe"),
        },
    )
    rows = [{
        "id": "select-existing",
        "title": "Product",
        "main_img": main,
        "extra_imgs": [
            text_attachment,
            clean_attachment,
            risk_attachment,
            unknown_attachment,
        ],
        "var_img": safe_variant,
        "var_imgs": [safe_variant, risk_variant],
    }]

    result, metrics = run_gate(
        rows,
        provider,
        tmp_path,
        monkeypatch,
        mode="select_existing",
    )

    assert result[0]["main_img"] == clean_attachment
    assert result[0]["extra_imgs"] == [main, text_attachment]
    assert result[0]["var_imgs"] == [safe_variant]
    assert provider.text_assess_calls == [
        main,
        text_attachment,
        clean_attachment,
    ]
    assert provider.gen_calls == []
    safety = metrics["image_safety_gate"]
    assert safety["processing_mode"] == "select_existing"
    assert safety["main_reselected"] == 1
    assert safety["pending_products"] == 0
    assert safety["attachment_deleted"] == 2
    assert safety["variant_deleted"] == 1
    main_record = next(
        item
        for item in result[0]["_image_assessments"]
        if item["role"] == "main" and item["url"] == clean_attachment
    )
    assert main_record["original_role"] == "attachment"
    assert main_record["main_eligible"] is True


def test_select_existing_prefers_white_background_product_over_earlier_collage(
    tmp_path,
    monkeypatch,
) -> None:
    collage = "https://img.example/collage.jpg"
    white_background = "https://img.example/white-background.jpg"
    provider = StructuredProvider(
        {
            collage: assessment("safe"),
            white_background: assessment("safe"),
        },
        text_assessments={
            collage: text_assessment(
                "safe",
                main_image_quality="fallback",
                evidence="Multiple installation and lifestyle panels.",
            ),
            white_background: text_assessment(
                "safe",
                main_image_quality="preferred",
                evidence="Single product isolated on a clean white background.",
            ),
        },
    )
    rows = [{
        "id": "prefer-white-background",
        "title": "Product",
        "main_img": collage,
        "extra_imgs": [white_background],
        "var_img": "",
        "var_imgs": [],
    }]

    result, metrics = run_gate(
        rows,
        provider,
        tmp_path,
        monkeypatch,
        mode="select_existing",
    )

    assert result[0]["main_img"] == white_background
    assert result[0]["extra_imgs"] == [collage]
    assert provider.text_assess_calls == [collage, white_background]
    assert metrics["image_safety_gate"]["preferred_main_selected"] == 1
    assert provider.gen_calls == []


def test_select_existing_allows_product_with_only_clean_main(
    tmp_path,
    monkeypatch,
) -> None:
    main = "https://img.example/main.jpg"
    risk_attachment = "https://img.example/risk.jpg"
    provider = StructuredProvider(
        {
            main: assessment("safe"),
            risk_attachment: assessment(
                "risk",
                reasons=["brand_logo"],
                placement="overlay",
            ),
        },
        text_assessments={main: text_assessment("safe")},
    )
    rows = [{
        "id": "main-only",
        "title": "Product",
        "main_img": main,
        "extra_imgs": [risk_attachment],
        "var_img": "",
        "var_imgs": [],
    }]

    result, metrics = run_gate(
        rows,
        provider,
        tmp_path,
        monkeypatch,
        mode="select_existing",
    )

    assert result[0]["main_img"] == main
    assert result[0]["extra_imgs"] == []
    assert metrics["image_safety_gate"]["pending_products"] == 0
    assert provider.gen_calls == []


def test_select_existing_records_pending_when_no_clean_main(
    tmp_path,
    monkeypatch,
) -> None:
    main = "https://img.example/logo-main.jpg"
    text_attachment = "https://img.example/text.jpg"
    provider = StructuredProvider(
        {
            main: assessment(
                "risk",
                reasons=["brand_logo"],
                placement="product_surface",
            ),
            text_attachment: assessment("safe"),
        },
        text_assessments={
            text_attachment: text_assessment(
                "risk",
                detected_text=["SIZE"],
            ),
        },
    )
    rows = [{
        "id": "pending-main",
        "site": "US",
        "title": "Product",
        "main_img": main,
        "extra_imgs": [text_attachment],
        "var_img": "",
        "var_imgs": [],
    }]

    result, metrics = run_gate(
        rows,
        provider,
        tmp_path,
        monkeypatch,
        mode="select_existing",
    )

    assert result[0]["_main_selection_pending"] is True
    assert provider.gen_calls == []
    assert metrics["image_safety_gate"]["pending_products"] == 1
    pending = metrics["pending_main_products"][0]
    assert pending["product_id"] == "pending-main"
    assert {item["url"] for item in pending["images"]} == {
        main,
        text_attachment,
    }


def test_multisite_rows_review_each_unique_image_only_once(
    tmp_path,
    monkeypatch,
) -> None:
    sites = ["US", "UK", "CA", "MX", "ES", "BR", "DE", "FR", "IT"]
    product_urls = {
        physical_index: [
            f"https://img.example/product-{physical_index}-{image_index}.jpg"
            for image_index in range(12)
        ]
        for physical_index in range(2)
    }
    rows = []
    for physical_index, urls in product_urls.items():
        for site in sites:
            rows.append({
                "id": f"{physical_index}-{site}",
                "site": site,
                "title": f"Product {physical_index} {site}",
                "main_img": urls[0],
                "extra_imgs": urls[1:],
                "var_imgs": [],
            })

    unique_urls = {
        url
        for urls in product_urls.values()
        for url in urls
    }
    provider = StructuredProvider({
        url: assessment("safe")
        for url in unique_urls
    })

    result, _metrics = run_gate(rows, provider, tmp_path, monkeypatch)

    assert len(result) == 18
    assert sum(1 + len(row["extra_imgs"]) for row in rows) == 216
    reviewed_urls = [url for url, confirmation in provider.assess_calls if not confirmation]
    assert len(reviewed_urls) == 24
    assert set(reviewed_urls) == unique_urls
    assert len(provider.text_assess_calls) == 2
    assert set(provider.text_assess_calls) == {
        product_urls[0][0],
        product_urls[1][0],
    }
    assert provider.gen_calls == []


def test_deletes_branded_attachment_and_edits_variant(
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
    assert result[0]["extra_imgs"] == [
        extra_safe,
        extra_unknown,
    ]
    assert (generated, False) not in provider.assess_calls
    generated_record = next(
        item for item in result[0]["_image_assessments"]
        if item.get("url") == generated
    )
    assert generated_record["accepted_without_machine_review"] is True
    attachment_record = next(
        item for item in result[0]["_image_assessments"]
        if item.get("url") == extra_risk
    )
    assert attachment_record["role"] == "attachment"
    assert attachment_record["image_action"] == "delete_attachment"
    assert provider.text_assess_calls == [main]
    assert metrics["image_safety_gate"]["attachment_deleted"] == 1
    assert metrics["image_safety_gate"]["generated_attachment"] == 0
    assert metrics["image_safety_gate"]["quarantined_products"] == 0


def test_deletes_every_risk_attachment(
    tmp_path,
    monkeypatch,
) -> None:
    main = "https://img.example/main.jpg"
    chinese_spec = "https://img.example/chinese-spec.jpg"
    person = "https://img.example/person.jpg"
    provider = StructuredProvider({
        main: assessment("safe"),
        chinese_spec: assessment(
            "risk",
            reasons=["non_english_product_text"],
            detected_text=["尺寸"],
            placement="overlay",
        ),
        person: assessment("risk", reasons=["person"], placement="background"),
    })
    rows = [{
        "id": "p-attachment-keep",
        "title": "Product",
        "main_img": main,
        "var_img": "",
        "var_imgs": [],
        "extra_imgs": [chinese_spec, person],
    }]

    result, metrics = run_gate(rows, provider, tmp_path, monkeypatch)

    assert result[0]["extra_imgs"] == []
    assert provider.gen_calls == []
    assert metrics["image_safety_gate"]["attachment_deleted"] == 2
    attachment_actions = {
        item["url"]: item["image_action"]
        for item in result[0]["_image_assessments"]
        if item["role"] == "attachment"
    }
    assert attachment_actions == {
        chinese_spec: "delete_attachment",
        person: "delete_attachment",
    }


def test_unknown_main_is_remediated_then_quarantined_on_failure(
    tmp_path,
    monkeypatch,
) -> None:
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

    assert len(result) == 1
    assert result[0]["main_img"] == main
    assert result[0]["_image_assessments"][0]["image_action"] == "keep_review"
    assert metrics["image_safety_gate"]["quarantined_products"] == 0


def test_legacy_boolean_provider_is_retained_for_human_review(
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

    assert result[0]["main_img"] == main
    assert result[0]["_image_assessments"][0]["image_action"] == "keep_review"
    assert len(metrics["image_review_warnings"]) == 1
    assert provider.legacy_calls == 0


def test_intrinsic_brand_main_is_remediated_before_quarantine(
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
        "id": "intrinsic-main",
        "title": "Generic Emblem for Toyota",
        "main_img": main,
        "var_img": "",
        "var_imgs": [],
        "extra_imgs": [],
    }]

    with pytest.raises(RuntimeError, match="图片编辑失败"):
        run_gate(rows, provider, tmp_path, monkeypatch)
    assert (main, True) not in provider.assess_calls
    assert provider.gen_calls


def test_high_risk_main_confirmation_conflict_still_attempts_remediation(
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

    with pytest.raises(RuntimeError, match="图片编辑失败"):
        run_gate(rows, provider, tmp_path, monkeypatch)


def test_generated_main_is_accepted_for_human_review_without_recheck(
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

    assert result[0]["main_img"] == generated
    assert metrics["image_safety_gate"]["failed_main"] == 0
    generated_record = next(
        item for item in result[0]["_image_assessments"]
        if item.get("url") == generated
    )
    assert generated_record["accepted_without_machine_review"] is True
    assert metrics["image_safety_gate"]["generated_reviewed"] == 0
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

def test_main_text_cache_invalidates_without_dropping_general_review(
    tmp_path,
) -> None:
    main = "https://img.example/main.jpg"
    review_version, text_version, generation_version = (
        current_cache_versions()
    )
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({
        "risk_prompt_version": review_version,
        "main_text_prompt_version": "old-main-text-policy",
        "gen_prompt_version": generation_version,
        "risk_assessments": {main: assessment("safe")},
        "risk_confirmations": {},
        "main_text_assessments": {
            main: text_assessment("safe"),
        },
        "gen_results": {},
        "gen_meta": {},
        "gen_failures": {},
    }), encoding="utf-8")

    loaded = load_cache(
        str(cache_path),
        review_version,
        text_version,
        generation_version,
    )

    assert loaded["risk_assessments"][main]["status"] == "safe"
    assert loaded["main_text_assessments"] == {}


def test_operational_unknown_general_review_is_retried(tmp_path) -> None:
    main = "https://img.example/unknown-main.jpg"
    review_version, text_version, generation_version = (
        current_cache_versions()
    )
    unknown = assessment("unknown", placement="unknown")
    unknown["operational_failure"] = True
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({
        "risk_prompt_version": review_version,
        "main_text_prompt_version": text_version,
        "gen_prompt_version": generation_version,
        "risk_assessments": {main: unknown},
        "risk_confirmations": {main: unknown},
        "main_text_assessments": {},
        "gen_results": {},
        "gen_meta": {},
        "gen_failures": {},
    }), encoding="utf-8")

    loaded = load_cache(
        str(cache_path),
        review_version,
        text_version,
        generation_version,
    )

    assert loaded["risk_assessments"] == {}
    assert loaded["risk_confirmations"] == {}

def test_generated_main_cache_is_not_bound_to_text_prompt_version() -> None:
    main = "https://img.example/main.jpg"
    generated = "https://generated.example/main.png"
    cache = {
        "gen_results": {"main:" + main: generated},
        "gen_meta": {
            "main:" + main: {
                "prompt_version": "generation-v1",
                "main_text_prompt_version": "text-v1",
                "risk_assessment": assessment("safe"),
                "text_assessment": text_assessment("safe"),
            },
        },
    }

    assert cached_generation(
        cache,
        "generation-v1",
        "main",
        main,
        "text-v1",
    ) == generated
    assert cached_generation(
        cache,
        "generation-v1",
        "main",
        main,
        "text-v2",
    ) == generated


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

def test_manual_override_cannot_bypass_main_zero_text_rule(
    tmp_path,
    monkeypatch,
) -> None:
    main = "https://img.example/text-shape.jpg"
    generated = "https://generated.example/text-removed.png"
    provider = StructuredProvider(
        {main: assessment(
            "risk",
            reasons=["brand_logo"],
            detected_text=["TOYOTA"],
            placement="product_surface",
        ), generated: assessment("safe")},
        generated={main: generated},
        text_assessments={
            main: text_assessment(
                "risk",
                detected_text=["TOYOTA"],
                placement="product_surface",
            ),
        },
    )
    monkeypatch.setattr(
        remediation,
        "load_manual_overrides",
        lambda _cache_path: {("override-text", "main", main): {
            "product_id": "override-text",
            "action": "false_positive",
            "role": "main",
            "image_url": main,
        }},
    )
    rows = [{
        "id": "override-text",
        "title": "Product",
        "main_img": main,
        "var_img": "",
        "var_imgs": [],
        "extra_imgs": [],
    }]

    result, metrics = run_gate(rows, provider, tmp_path, monkeypatch)

    assert len(result) == 1
    assert result[0]["main_img"] == generated
    assert metrics["image_safety_gate"]["failed_main"] == 0


def test_visible_text_main_is_replaced_for_human_review(
    tmp_path,
    monkeypatch,
) -> None:
    main = "https://img.example/dimension-main.jpg"
    generated = "https://generated.example/no-text.png"
    provider = StructuredProvider(
        {
            main: assessment("safe", detected_text=["29 x 19 cm"]),
            generated: assessment("safe"),
        },
        generated={main: generated},
        text_assessments={
            main: text_assessment(
                "risk",
                detected_text=["29 x 19 cm"],
                placement="product_surface",
            ),
            generated: text_assessment("safe"),
        },
    )
    rows = [{
        "id": "text-main",
        "title": "Product",
        "main_img": main,
        "var_img": "",
        "var_imgs": [],
        "extra_imgs": [],
    }]

    result, metrics = run_gate(rows, provider, tmp_path, monkeypatch)

    assert result[0]["main_img"] == generated
    assert provider.text_assess_calls == [main]
    assert [item[0] for item in provider.gen_calls] == [main]
    assert metrics["image_safety_gate"]["generated_main"] == 1


def test_attachment_text_does_not_use_main_zero_text_policy(
    tmp_path,
    monkeypatch,
) -> None:
    main = "https://img.example/main.jpg"
    attachment = "https://img.example/spec-attachment.jpg"
    provider = StructuredProvider(
        {
            main: assessment("safe"),
            attachment: assessment("safe"),
        },
        text_assessments={main: text_assessment("safe")},
    )
    rows = [{
        "id": "attachment-text",
        "title": "Product",
        "main_img": main,
        "var_img": "",
        "var_imgs": [],
        "extra_imgs": [attachment],
    }]

    result, _metrics = run_gate(
        rows,
        provider,
        tmp_path,
        monkeypatch,
    )

    assert result[0]["extra_imgs"] == [attachment]
    assert provider.text_assess_calls == [main]
    assert attachment not in provider.text_assess_calls


def test_generated_main_with_text_is_left_for_human_review(
    tmp_path,
    monkeypatch,
) -> None:
    main = "https://img.example/text-main.jpg"
    generated = "https://generated.example/pseudo-text.png"
    provider = StructuredProvider(
        {
            main: assessment(
                "risk",
                reasons=["non_english_product_text"],
                detected_text=["尺寸"],
                placement="product_surface",
            ),
            generated: assessment("safe"),
        },
        generated={main: generated},
        text_assessments={
            main: text_assessment(
                "risk",
                detected_text=["ABC"],
                placement="overlay",
            ),
            generated: text_assessment(
                "risk",
                detected_text=["A8C"],
                placement="product_surface",
            ),
        },
    )
    rows = [{
        "id": "generated-text",
        "title": "Product",
        "main_img": main,
        "var_img": "",
        "var_imgs": [],
        "extra_imgs": [],
    }]

    result, metrics = run_gate(rows, provider, tmp_path, monkeypatch)

    assert result[0]["main_img"] == generated
    assert metrics["image_safety_gate"]["failed_main"] == 0
    assert [item[2] for item in provider.gen_calls] == [0]
    generated_record = result[0]["_image_assessments"][-1]
    assert generated_record["accepted_without_machine_review"] is True
    cache = json.loads(
        (tmp_path / "cache.json").read_text(encoding="utf-8")
    )
    attempts = cache["gen_meta"][f"main:{main}"]["attempts"]
    assert [item["route_offset"] for item in attempts] == [0]
    assert all(item["candidate_url"] == generated for item in attempts)
    assert "CATALOG ISOLATION" not in provider.gen_contexts[0]
    assert "UNBRANDED REBUILD" not in provider.gen_contexts[0]
    assert "MINIMAL GEOMETRY" not in provider.gen_contexts[0]
    assert provider.gen_reference_free == [False]


def test_generation_context_names_detected_text_and_risks(
    tmp_path,
    monkeypatch,
) -> None:
    main = "https://img.example/mazda-main.jpg"
    generated = "https://generated.example/clean-mazda.png"
    provider = StructuredProvider(
        {
            main: assessment(
                "risk",
                reasons=["brand_logo", "non_product_text"],
                detected_text=["MAZDA", "Produced by Spark"],
                placement="overlay",
                evidence="Mazda badge and producer credit are visible.",
            ),
            generated: assessment("safe"),
        },
        generated={main: generated},
        text_assessments={
            main: text_assessment(
                "risk",
                detected_text=["MAZDA", "Produced by Spark"],
                placement="product_surface",
            ),
            generated: text_assessment("safe"),
        },
    )
    rows = [{
        "id": "dynamic-removal-context",
        "title": "Generic Grille Trim for Mazda",
        "main_img": main,
        "var_img": "",
        "var_imgs": [],
        "extra_imgs": [],
    }]

    result, _metrics = run_gate(rows, provider, tmp_path, monkeypatch)

    assert result[0]["main_img"] == generated
    context = provider.gen_contexts[0]
    assert "EDIT TARGETS" in context
    assert "MAZDA" in context
    assert "Produced by Spark" in context
    assert "brand_logo" in context
    assert "Generic Grille Trim for Mazda" in context
    assert "MAIN IMAGE ZERO-TEXT RULE" in context
    assert "Do not translate or keep existing English text" in context
    assert "Preserve product geometry" in context


def test_generated_image_machine_review_is_skipped_in_human_review_mode(
    tmp_path,
    monkeypatch,
) -> None:
    main = "https://img.example/unknown-review-main.jpg"
    generated = "https://generated.example/unknown-then-safe.png"

    class RetryProvider(StructuredProvider):
        def __init__(self):
            super().__init__(
                    {
                        main: assessment(
                            "risk",
                            reasons=["non_english_product_text"],
                            detected_text=["尺寸"],
                            placement="product_surface",
                        ),
                    generated: assessment("safe"),
                },
                generated={main: generated},
                text_assessments={
                    main: text_assessment(
                        "risk",
                        detected_text=["SIZE 12"],
                        placement="overlay",
                    ),
                },
            )
            self.generated_text_reviews = 0

        def assess_image(
            self,
            url: str,
            *,
            confirmation: bool = False,
            policy: str = "general",
            retries: int = 3,
        ) -> dict | None:
            if url == generated and policy == "main_text_free":
                self.generated_text_reviews += 1
                if self.generated_text_reviews == 1:
                    return text_assessment(
                        "unknown",
                        evidence="ProviderResponseError",
                    )
                return text_assessment("safe")
            return super().assess_image(
                url,
                confirmation=confirmation,
                policy=policy,
                retries=retries,
            )

    monkeypatch.setattr(remediation.time, "sleep", lambda _seconds: None)
    provider = RetryProvider()
    rows = [{
        "id": "retry-unknown-review",
        "title": "Generic Product",
        "main_img": main,
        "var_img": "",
        "var_imgs": [],
        "extra_imgs": [],
    }]

    result, _metrics = run_gate(rows, provider, tmp_path, monkeypatch)

    assert result[0]["main_img"] == generated
    assert provider.generated_text_reviews == 0


def test_zero_candidate_network_failure_retries_after_stage_cooldown(
    tmp_path,
    monkeypatch,
) -> None:
    from amazon_processor.providers import ProviderUnavailableError

    main = "https://img.example/transient-main.jpg"
    generated = "https://generated.example/recovered.png"

    class RecoveringProvider(StructuredProvider):
        def __init__(self):
            super().__init__(
                {
                    main: assessment(
                        "risk",
                        reasons=["brand_logo"],
                        detected_text=["LOGO"],
                        placement="product_surface",
                    ),
                    generated: assessment("safe"),
                },
                text_assessments={
                    main: text_assessment(
                        "risk",
                        detected_text=["LOGO"],
                        placement="product_surface",
                    ),
                    generated: text_assessment("safe"),
                },
            )
            self.generation_attempts = 0

        def call_image_gen(self, *_args, **_kwargs):
            self.generation_attempts += 1
            if self.generation_attempts <= 7:
                raise ProviderUnavailableError(
                    "temporary upstream congestion",
                    provider="agnes",
                    operation="image_gen",
                    status_code=503,
                )
            return generated

    monkeypatch.setattr(remediation.time, "sleep", lambda _seconds: None)
    provider = RecoveringProvider()
    rows = [{
        "id": "transient-stage-retry",
        "title": "Generic Product",
        "main_img": main,
        "var_img": "",
        "var_imgs": [],
        "extra_imgs": [],
    }]

    result, metrics = run_gate(rows, provider, tmp_path, monkeypatch)

    assert result[0]["main_img"] == generated
    assert provider.generation_attempts == 8
    generation_stats = metrics["concurrency"]["amazon_safe_image_gen"]
    assert generation_stats["stage_retry_rounds"] == 3
    assert generation_stats["stage_retry_wait_total_s"] > 0


def test_generation_circuit_skip_aborts_without_product_failure(
    tmp_path,
    monkeypatch,
) -> None:
    from amazon_processor.providers import ProviderCircuitOpenError

    main = "https://img.example/circuit-main.jpg"

    class CircuitProvider(StructuredProvider):
        def call_image_gen(self, *_args, **_kwargs):
            raise ProviderCircuitOpenError(
                "cooling down",
                provider="agnes",
                operation="image_gen",
            )

    provider = CircuitProvider(
        {main: assessment("risk", reasons=["brand_logo"])},
        text_assessments={
            main: text_assessment(
                "risk",
                detected_text=["LOGO"],
                placement="product_surface",
            ),
        },
    )
    rows = [{
        "id": "circuit-product",
        "title": "Product",
        "main_img": main,
        "var_img": "",
        "var_imgs": [],
        "extra_imgs": [],
    }]

    monkeypatch.setattr(remediation.time, "sleep", lambda _seconds: None)
    with pytest.raises(ProviderCircuitOpenError):
        run_gate(rows, provider, tmp_path, monkeypatch)

    assert len(provider.gen_calls) == 0
    cache = json.loads(
        (tmp_path / "cache.json").read_text(encoding="utf-8")
    )
    assert cache["gen_failures"] == {}


def test_generation_resumes_after_temporary_circuit(
    tmp_path,
    monkeypatch,
) -> None:
    from amazon_processor.providers import ProviderCircuitOpenError

    main = "https://img.example/resumed-main.jpg"
    generated = "https://generated.example/resumed-main.png"

    class ResumeProvider(StructuredProvider):
        def __init__(self):
            super().__init__(
                {
                    main: assessment(
                        "risk",
                        reasons=["non_english_product_text"],
                        detected_text=["文字"],
                        placement="product_surface",
                    ),
                    generated: assessment("safe"),
                },
                text_assessments={
                    main: text_assessment(
                        "risk",
                        detected_text=["TEXT"],
                        placement="product_surface",
                    ),
                    generated: text_assessment("safe"),
                },
            )
            self.attempts = 0

        def call_image_gen(
            self,
            url,
            *,
            is_variant=False,
            context="",
            route_offset=0,
            reference_free=False,
        ):
            del context, reference_free
            self.gen_calls.append((url, is_variant, route_offset))
            self.attempts += 1
            if self.attempts == 1:
                raise ProviderCircuitOpenError(
                    "cooling down",
                    provider="agnes",
                    operation="image_gen",
                )
            return generated

    provider = ResumeProvider()
    rows = [{
        "id": "resumed-product",
        "title": "Product",
        "main_img": main,
        "var_img": "",
        "var_imgs": [],
        "extra_imgs": [],
    }]
    waits = []
    monkeypatch.setattr(remediation.time, "sleep", waits.append)

    result, metrics = run_gate(
        rows,
        provider,
        tmp_path,
        monkeypatch,
    )

    assert result[0]["main_img"] == generated
    assert waits
    stats = metrics["concurrency"]["amazon_safe_image_gen"]
    assert stats["circuit_resumes"] == 1


def test_limited_precise_routes_stop_before_gpt_quota_path(
    tmp_path,
    monkeypatch,
) -> None:
    from amazon_processor.providers import ProviderQuotaError

    main = "https://img.example/gpt-quota-main.jpg"

    class GPTQuotaProvider(StructuredProvider):
        def call_image_gen(
            self,
            url,
            *,
            is_variant=False,
            context="",
            route_offset=0,
            reference_free=False,
        ):
            del context, reference_free
            self.gen_calls.append((url, is_variant, route_offset))
            if route_offset < 2:
                raise RuntimeError("agnes unavailable")
            raise ProviderQuotaError(
                "balance unavailable",
                provider="gpt",
                operation="image_gen",
            )

    provider = GPTQuotaProvider(
        {main: assessment("risk", reasons=["brand_logo"])},
        text_assessments={
            main: text_assessment(
                "risk",
                detected_text=["TEXT"],
                placement="product_surface",
            ),
        },
    )
    rows = [{
        "id": "gpt-quota-product",
        "title": "Product",
        "main_img": main,
        "var_img": "",
        "var_imgs": [],
        "extra_imgs": [],
    }]

    with pytest.raises(RuntimeError, match="图片编辑失败"):
        run_gate(
            rows,
            provider,
            tmp_path,
            monkeypatch,
        )
    assert [item[2] for item in provider.gen_calls] == [0, 1]
    cache = json.loads(
        (tmp_path / "cache.json").read_text(encoding="utf-8")
    )
    failure = cache["gen_failures"][f"main:{main}"]
    assert failure["reason"] == "RuntimeError"
