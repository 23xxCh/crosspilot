from __future__ import annotations

from copy import deepcopy

from crosspilot.image_risk import normalize_image_assessment
from scripts import audit_amazon_image_safety


def value(status, reasons=None, placement="none"):
    return normalize_image_assessment({
        "status": status,
        "reasons": reasons or [],
        "placement": placement,
        "detected_text": [],
        "confidence": 0.95,
        "evidence": "test",
    })


class Provider:
    def __init__(self, values, confirmations=None):
        self.values = values
        self.confirmations = confirmations or {}

    def assess_image(self, url, *, confirmation=False):
        return (
            self.confirmations[url]
            if confirmation else self.values[url]
        )

    def metrics_snapshot(self):
        return {"api_calls": 3}


def test_readonly_audit_does_not_mutate_payload_and_routes_findings(
    tmp_path,
    monkeypatch,
) -> None:
    main = "https://img/main.jpg"
    extra = "https://img/extra.jpg"
    variant = "https://img/variant.jpg"
    payload = {
        "商品id": ["p1"],
        "产品标题": ["Product"],
        "产品描述": ["Description"],
        "产品图片链接": [[main, extra]],
        "变种图片链接": [[variant]],
    }
    original = deepcopy(payload)
    provider = Provider({
        main: value("safe"),
        extra: value(
            "risk",
            ["seller_watermark"],
            "overlay",
        ),
        variant: value("unknown", placement="unknown"),
    })
    monkeypatch.setattr(
        audit_amazon_image_safety,
        "get_provider",
        lambda: provider,
    )

    report = audit_amazon_image_safety.audit_payload(
        payload,
        cache_path=tmp_path / "audit-cache.json",
    )

    assert payload == original
    assert report["summary"]["products_with_findings"] == 1
    actions = {
        item["suggested_action"]
        for item in report["products"][0]["findings"]
    }
    assert actions == {"delete_attachment", "quarantine_product"}
