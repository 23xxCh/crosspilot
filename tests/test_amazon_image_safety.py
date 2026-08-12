from __future__ import annotations

import json
from pathlib import Path

from amazon_processor.images import gate
from amazon_processor.images.risk import (
    normalize_image_assessment,
    normalize_main_text_assessment,
    parse_image_assessment_batch_response,
    parse_main_text_assessment_response,
)


def assessment(status: str, *, reasons=None, evidence="test") -> dict:
    return normalize_image_assessment({
        "status": status,
        "reasons": reasons or [],
        "placement": "overlay" if reasons else "none",
        "detected_text": [],
        "confidence": 0.95,
        "evidence": evidence,
    })


def main_assessment(status: str, *, quality="preferred") -> dict:
    return normalize_main_text_assessment({
        "status": status,
        "reasons": ["visible_text"] if status == "risk" else [],
        "placement": "overlay" if status == "risk" else "none",
        "detected_text": ["TEXT"] if status == "risk" else [],
        "confidence": 0.95,
        "evidence": "single product on white background" if quality == "preferred" else "product image",
        "main_image_quality": quality,
    })


class Provider:
    def __init__(self, general: dict[str, dict], main: dict[str, dict]):
        self.general = general
        self.main = main
        self.general_calls: list[str] = []
        self.main_calls: list[str] = []
        self.gen_calls: list[str] = []

    def assess_image(self, url: str, *, confirmation=False, policy="general"):
        del confirmation
        if policy == "main_text_free":
            self.main_calls.append(url)
            return self.main.get(url)
        self.general_calls.append(url)
        return self.general.get(url)

    def call_image_gen(self, url: str, **_kwargs):
        self.gen_calls.append(url)
        raise AssertionError("formal pipeline must never call image generation")


def run_gate(rows: list[dict], provider: Provider, tmp_path: Path):
    metrics: dict = {}
    result = gate.run_structured_image_safety_gate(
        rows,
        str(tmp_path / "cache.json"),
        runtime_metrics=metrics,
        provider_getter=lambda: provider,
    )
    return result, metrics


def row(product_id="p1", *, main="main", extras=None, variants=None):
    return {
        "id": product_id,
        "title": "Product",
        "main_img": main,
        "extra_imgs": list(extras or []),
        "var_imgs": list(variants or []),
        "var_img": (variants or [""])[0],
    }


def test_main_parser_preserves_quality() -> None:
    value = parse_main_text_assessment_response(json.dumps({
        "status": "safe",
        "reasons": [],
        "placement": "none",
        "detected_text": [],
        "confidence": 0.9,
        "evidence": "white background",
        "main_image_quality": "preferred",
    }))
    assert value["status"] == "safe"
    assert value["main_image_quality"] == "preferred"


def test_batch_parser_preserves_order() -> None:
    values = parse_image_assessment_batch_response(
        json.dumps({"results": [
            {"index": 2, "status": "risk", "reasons": ["brand_logo"], "placement": "overlay", "confidence": 0.9, "evidence": "logo"},
            {"index": 1, "status": "safe", "reasons": [], "placement": "none", "confidence": 0.9, "evidence": "clean"},
        ]}),
        expected_count=2,
    )
    assert [value["status"] for value in values] == ["safe", "risk"]


def test_all_roles_are_reviewed_once_and_risk_unknown_are_deleted(tmp_path) -> None:
    urls = ["main", "safe-extra", "risk-extra", "unknown-extra", "safe-var", "risk-var"]
    provider = Provider(
        {
            "main": assessment("safe"),
            "safe-extra": assessment("safe"),
            "risk-extra": assessment("risk", reasons=["brand_logo"]),
            "unknown-extra": assessment("unknown"),
            "safe-var": assessment("safe"),
            "risk-var": assessment("risk", reasons=["seller_watermark"]),
        },
        {"main": main_assessment("safe"), "safe-extra": main_assessment("safe", quality="acceptable")},
    )
    result, metrics = run_gate([
        row(extras=["safe-extra", "risk-extra", "unknown-extra", "safe-extra"], variants=["safe-var", "risk-var"])
    ], provider, tmp_path)
    assert len(result) == 1
    assert result[0]["main_img"] == "main"
    assert result[0]["extra_imgs"] == ["safe-extra"]
    assert result[0]["var_imgs"] == ["safe-var"]
    assert set(provider.general_calls) == set(urls)
    assert len(provider.general_calls) == len(urls)
    assert provider.gen_calls == []
    assert metrics["image_safety_gate"]["generation_requests"] == 0


def test_white_background_candidate_is_promoted_and_old_main_demoted(tmp_path) -> None:
    provider = Provider(
        {"old": assessment("safe"), "white": assessment("safe"), "extra": assessment("safe")},
        {
            "old": main_assessment("safe", quality="acceptable"),
            "white": main_assessment("safe", quality="preferred"),
            "extra": main_assessment("risk"),
        },
    )
    result, metrics = run_gate([row(main="old", extras=["white", "extra"])], provider, tmp_path)
    assert result[0]["main_img"] == "white"
    assert result[0]["extra_imgs"] == ["old", "extra"]
    assert metrics["image_safety_gate"]["main_reselected"] == 1


def test_no_eligible_main_removes_product_and_records_reason(tmp_path) -> None:
    provider = Provider(
        {"main": assessment("safe"), "extra": assessment("safe")},
        {"main": main_assessment("risk"), "extra": main_assessment("unknown")},
    )
    result, metrics = run_gate([row(extras=["extra"])], provider, tmp_path)
    assert result == []
    assert metrics["image_rejected_products"][0]["product_id"] == "p1"
    assert metrics["image_rejected_products"][0]["reason"] == "missing_eligible_main"


def test_only_main_left_removes_product_even_with_variant(tmp_path) -> None:
    provider = Provider(
        {"main": assessment("safe"), "bad": assessment("risk", reasons=["brand_logo"]), "variant": assessment("safe")},
        {"main": main_assessment("safe")},
    )
    result, metrics = run_gate([
        row(extras=["bad"], variants=["variant"])
    ], provider, tmp_path)
    assert result == []
    rejected = metrics["image_rejected_products"][0]
    assert rejected["reason"] == "missing_product_attachment"
    assert metrics["attachment_rejected_products"] == [rejected]


def test_one_main_and_one_attachment_is_deliverable(tmp_path) -> None:
    provider = Provider(
        {"main": assessment("safe"), "extra": assessment("safe")},
        {"main": main_assessment("safe"), "extra": main_assessment("risk")},
    )
    result, metrics = run_gate([row(extras=["extra"])], provider, tmp_path)
    assert len(result) == 1
    assert result[0]["extra_imgs"] == ["extra"]
    assert metrics["image_rejected_products"] == []


def test_shared_url_across_products_is_reviewed_once(tmp_path) -> None:
    provider = Provider(
        {"shared": assessment("safe"), "a": assessment("safe"), "b": assessment("safe")},
        {"shared": main_assessment("safe"), "a": main_assessment("risk"), "b": main_assessment("risk")},
    )
    result, _metrics = run_gate([
        row("p1", main="shared", extras=["a"]),
        row("p2", main="shared", extras=["b"]),
    ], provider, tmp_path)
    assert len(result) == 2
    assert provider.general_calls.count("shared") == 1
    assert provider.main_calls.count("shared") == 1


def test_manual_general_override_does_not_bypass_main_qualification(tmp_path, monkeypatch) -> None:
    provider = Provider(
        {"main": assessment("risk", reasons=["brand_logo"]), "clean": assessment("safe"), "extra": assessment("safe")},
        {"main": main_assessment("risk"), "clean": main_assessment("safe"), "extra": main_assessment("risk")},
    )
    monkeypatch.setattr(
        gate,
        "load_manual_overrides",
        lambda _path: {("p1", "main", "main"): {"decision": "false_positive"}},
    )
    result, _metrics = run_gate([row(extras=["clean", "extra"])], provider, tmp_path)
    assert result[0]["main_img"] == "clean"
    assert "main" in result[0]["extra_imgs"]


def test_cached_review_avoids_repeated_provider_calls(tmp_path) -> None:
    general = {"main": assessment("safe"), "extra": assessment("safe")}
    main = {"main": main_assessment("safe"), "extra": main_assessment("risk")}
    first = Provider(general, main)
    run_gate([row(extras=["extra"])], first, tmp_path)
    second = Provider(general, main)
    run_gate([row(extras=["extra"])], second, tmp_path)
    assert second.general_calls == []
    assert second.main_calls == []


def test_formal_gate_has_no_generation_entrypoint() -> None:
    source = Path(gate.__file__).read_text(encoding="utf-8")
    assert "call_image_gen" not in source
    assert "generate_replacements" not in source
    assert "regenerate_all_localized" not in source
