from __future__ import annotations

import json

from amazon_processor.quality import (
    normalize_bullets_for_row,
    normalize_keywords_for_row,
)
from amazon_processor.text.descriptions import (
    clean_descriptions,
    enforce_description_safety,
    format_description,
    parse_structured_description,
    partition_product_description_rows,
    select_relevant_description_source,
)
from amazon_processor.text.listing import generate_bullets_keywords


def test_structured_description_renders_stable_paragraphs() -> None:
    parsed = parse_structured_description(json.dumps({
        "summary": (
            "Universal car armrest pad made from durable artificial leather."
        ),
        "details": [
            {"label": "Material", "value": "Artificial leather"},
            {"label": "Size", "value": "29 × 19 × 1 cm"},
            {"label": "Color", "value": "Black, Red, White"},
            {
                "label": "Compatibility",
                "value": "Universal fit for most vehicles",
            },
            {"label": "Package Includes", "value": "1 armrest pad"},
            {"label": "Quantity", "value": ""},
        ],
    }))

    assert parsed is not None
    description, compacted = format_description(*parsed)

    assert description == (
        "Universal car armrest pad made from durable artificial leather.\n\n"
        "Material: Artificial leather\n"
        "Size: 29 × 19 × 1 cm\n"
        "Color: Black, Red, White\n"
        "Compatibility: Universal fit for most vehicles\n"
        "Package Includes: 1 armrest pad"
    )
    assert compacted is False
    assert len(description) <= 500


def test_description_formatter_compacts_to_500_characters() -> None:
    details = [
        (label, f"{label} " + "supported specification " * 12)
        for label in (
            "Material",
            "Size",
            "Color",
            "Compatibility",
            "Quantity",
            "Specifications",
            "Features",
            "Package Includes",
        )
    ]

    description, compacted = format_description(
        "Useful product summary " * 20,
        details,
    )

    assert compacted is True
    assert len(description) <= 500
    assert "\n\n" in description
    assert not any(line.endswith(":") for line in description.splitlines())


def test_source_selector_discards_priced_cross_sell_catalog() -> None:
    source = (
        "Credit Card Knife Folding Wallet Gift 6.50 USD "
        "Beige PU Leather Seat Cushion 45.90 USD "
        "Welcome to My Store. Features: Universal car armrest pad protects "
        "the center console. Material: artificial leather. "
        "Size: 29x19x1cm. Package includes: 1 armrest pad. "
        "Payment Policy: We accept PayPal."
    )

    selected = select_relevant_description_source(
        "Generic Car Armrest Pad for Center Console",
        source,
    )

    assert "armrest pad" in selected.lower()
    assert "credit card knife" not in selected.lower()
    assert "seat cushion" not in selected.lower()
    assert "paypal" not in selected.lower()
    assert "usd" not in selected.lower()


class _RetryProvider:
    def __init__(self) -> None:
        self.calls = 0

    def call_text(self, _prompt, *, max_tokens=3000):
        del max_tokens
        self.calls += 1
        if self.calls == 1:
            return json.dumps({
                "summary": "Credit Card Knife folding wallet tool.",
                "details": [],
            })
        return json.dumps({
            "summary": "Universal car armrest pad protects the center console.",
            "details": [
                {"label": "Material", "value": "Artificial leather"},
                {"label": "Size", "value": "29x19x1cm"},
                {"label": "Package Includes", "value": "1 armrest pad"},
            ],
        })


def test_description_cleaner_retries_contaminated_model_result() -> None:
    provider = _RetryProvider()
    rows = [{
        "id": "p1",
        "title": "Generic Car Armrest Pad for Center Console",
        "desc": (
            "Credit Card Knife 6.50 USD Features: Universal car armrest pad "
            "protects the center console. Material: artificial leather. "
            "Size: 29x19x1cm. Package includes: 1 armrest pad."
        ),
    }]

    clean_descriptions(rows, provider_getter=lambda: provider)

    assert provider.calls == 2
    assert rows[0]["desc"].startswith("Universal car armrest pad")
    assert "\n\nMaterial:" in rows[0]["desc"]
    assert "Credit Card Knife" not in rows[0]["desc"]
    assert len(rows[0]["desc"]) <= 500


def test_final_description_gate_retains_row_and_replaces_dirty_text() -> None:
    rows = [{
        "id": "p1",
        "title": "Generic Car Armrest Pad",
        "desc": "Credit Card Knife Folding Wallet",
        "_description_source_block": (
            "Material: artificial leather. Size: 29x19x1cm."
        ),
    }]

    retained = enforce_description_safety(rows)

    assert retained is rows
    assert retained[0]["desc"].startswith("Generic Car Armrest Pad.")
    assert "Credit Card Knife" not in retained[0]["desc"]


def test_description_partition_removes_only_rows_without_product_content() -> None:
    rows = [
        {
            "id": "valid",
            "title": "Generic Car Armrest Pad",
            "desc": (
                "Features: Car armrest pad for center consoles. "
                "Material: artificial leather."
            ),
        },
        {
            "id": "empty",
            "title": "Generic Door Lock Pins",
            "desc": " ",
        },
        {
            "id": "template",
            "title": "Generic Tire Valve Caps",
            "desc": (
                "Store CategoriesStore Categories Product Description "
                "About UsAs a seller, Positive Feedback is important. "
                "Please contact us before Negative Feedback."
            ),
        },
    ]

    retained, rejected = partition_product_description_rows(rows)

    assert [row["id"] for row in retained] == ["valid"]
    assert [item["product_id"] for item in rejected] == [
        "empty",
        "template",
    ]
    assert [item["code"] for item in rejected] == [
        "missing_source_description",
        "missing_product_description_content",
    ]


def test_listing_normalization_removes_unrelated_cross_sell_content() -> None:
    row = {
        "title": "Generic Car Armrest Pad",
        "desc": (
            "Car armrest pad for a center console.\n\n"
            "Material: Artificial leather\n"
            "Package Includes: 1 armrest pad"
        ),
        "bullets": [
            "Credit Card Knife folding wallet tool",
            "Artificial leather construction supports daily use",
            "Center console pad protects the armrest surface",
            "Package includes one car armrest pad",
            "Armrest cover is easy to position",
        ],
        "keywords": (
            "credit card knife, armrest pad, center console cover, "
            "artificial leather, car armrest, console pad, armrest cover, "
            "vehicle armrest, protective pad, leather console cover"
        ),
    }

    normalize_bullets_for_row(row)
    normalize_keywords_for_row(row)

    assert row["bullets"][0] == ""
    assert "credit card knife" not in row["keywords"]
    assert len(row["keywords"].split(",")) == 10


def test_missing_source_description_does_not_generate_listing_facts() -> None:
    rows = [{
        "id": "missing",
        "title": "Generic Aluminum Door Lock Pins 2 Pack",
        "desc": "",
        "bullets": ["Invented fact"] * 5,
        "keywords": "invented keyword",
    }]

    generate_bullets_keywords(
        rows,
        provider_getter=lambda: (_ for _ in ()).throw(
            AssertionError("provider must not be called")
        ),
    )

    assert rows[0]["bullets"] == [""] * 5
    assert rows[0]["keywords"] == ""
    assert rows[0]["subtitle"] == ""
