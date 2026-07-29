from amazon_processor.text.titles import normalize_amazon_title
from amazon_processor.quality import (
    normalize_bullets_for_row,
    normalize_keywords_for_row,
    split_keywords,
)


def test_brand_title_uses_generic_for_format_and_75_char_limit() -> None:
    result = normalize_amazon_title(
        "Toyota Camry Windshield Washer Spray Nozzle Replacement Kit 2 Pack"
    )

    assert result.startswith("Generic ")
    assert " for Toyota Camry" in result
    assert len(result) <= 75
    assert normalize_amazon_title(result) == result


def test_listing_rules_produce_five_bullets_and_ten_keywords() -> None:
    row = {
        "id": "p1",
        "title": "Generic Stainless Steel Mounting Kit for Workshop Equipment",
        "desc": (
            "Durable stainless steel mounting kit with adjustable brackets, "
            "corrosion resistance, secure fasteners and simple installation."
        ),
        "bullets": [
            "Stainless steel construction resists corrosion",
            "Adjustable brackets support flexible mounting",
            "Secure fasteners keep equipment firmly positioned",
            "Simple installation with common workshop tools",
            "Complete mounting kit for workshop equipment",
        ],
        "keywords": "",
    }
    normalize_bullets_for_row(row)
    normalize_keywords_for_row(row)

    assert len(row["bullets"]) == 5
    assert all(row["bullets"])
    assert len(split_keywords(row["keywords"])) == 10
