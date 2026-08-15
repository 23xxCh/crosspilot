import re

from amazon_processor.text.titles import normalize_amazon_title
from amazon_processor.quality import (
    normalize_bullets_for_row,
    normalize_keywords_for_row,
    split_keywords,
)
from amazon_processor.text.subtitles import (
    SUBTITLE_MAX_LENGTH,
    build_subtitle,
    normalize_subtitle_for_row,
)
from amazon_processor.policy import enforce_prohibited_listing_terms


def test_brand_title_uses_generic_for_format_and_subtitle_display_limit() -> None:
    result = normalize_amazon_title(
        "Toyota Camry Windshield Washer Spray Nozzle Replacement Kit 2 Pack"
    )

    assert result.startswith("Generic ")
    assert " for Toyota Camry" in result
    assert len(result) < 75
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


def test_prohibited_listing_terms_remove_button_battery_claims() -> None:
    row = {
        "id": "p1",
        "title": "Generic Mini Clock with Button Battery for Car",
        "subtitle": "Plated metal, black, built in battery",
        "desc": (
            "Generic Mini Clock for Car.\n\n"
            "Material: Plated metal\n"
            "Color: Black Battery: Built-in button battery "
            "Battery Life: About 1 year Waterproof: No\n"
            "Quantity: 1pc"
        ),
        "bullets": [
            "Plated metal construction with a black finish",
            "Compact profile for vehicle interiors",
            "Quartz movement supports reliable timekeeping",
            "Built-in button battery provides about 1 year of operation",
            "Package includes one mini clock",
        ],
        "keywords": (
            "car clock, button battery clock, mini clock, dashboard clock, "
            "vehicle clock, quartz clock, black clock, metal clock, "
            "small clock, auto clock"
        ),
    }

    enforce_prohibited_listing_terms([row])

    combined = " ".join([
        row["title"],
        row["subtitle"],
        row["desc"],
        *row["bullets"],
        row["keywords"],
    ]).lower()
    assert "button battery" not in combined
    assert "button batteries" not in combined
    assert "battery life" not in combined
    assert "1 year of operation" not in combined
    assert "built in battery" not in combined
    assert len([bullet for bullet in row["bullets"] if bullet]) == 5
    assert len(split_keywords(row["keywords"])) == 10


def test_prohibited_audience_words_are_removed_as_complete_words() -> None:
    row = {
        "title": "Generic Boys Girls Kids Storage Bag",
        "subtitle": "For boy and girl rooms",
        "desc": "Storage bag for boys, girls and kids. Boysenberry color option.",
        "bullets": [
            "Sized for boys and girls",
            "Simple storage design",
            "Reusable fabric construction",
            "Suitable for bedroom organization",
            "Package includes one storage bag",
        ],
        "keywords": (
            "boys bag, girls bag, kids storage, room bag, storage bag, "
            "fabric bag, reusable bag, bedroom bag, organizer bag, small bag"
        ),
    }

    enforce_prohibited_listing_terms([row])

    combined = " ".join([
        row["title"],
        row["subtitle"],
        row["desc"],
        *row["bullets"],
        row["keywords"],
    ])
    assert not re.search(r"(?i)\b(?:boy|boys|girl|girls|kids)\b", combined)
    assert "Boysenberry" in row["desc"]


def test_subtitle_rule_fills_short_phrase_from_product_details() -> None:
    row = {
        "title": "Generic Car Armrest Pad",
        "desc": (
            "Universal car armrest pad made from artificial leather.\n\n"
            "Material: Artificial leather\n"
            "Size: 29 x 19 x 1 cm\n"
            "Color: Black Red White\n"
            "Compatibility: Universal fit for most vehicles"
        ),
        "subtitle": "Free shipping, Best Seller!",
        "bullets": [],
        "keywords": "armrest cover, center console pad, easy installation",
    }

    normalize_subtitle_for_row(row)

    assert row["subtitle"]
    assert len(row["subtitle"]) <= SUBTITLE_MAX_LENGTH
    assert "Free shipping" not in row["subtitle"]
    assert "Best Seller" not in row["subtitle"]
    assert "." not in row["subtitle"]
    assert "," in row["subtitle"]


def test_title_at_display_limit_is_compacted_and_gets_subtitle() -> None:
    row = {
        "title": "A" * 75,
        "desc": "Material: Stainless steel",
        "subtitle": "Stainless steel material",
    }

    normalize_subtitle_for_row(row)

    assert len(row["title"]) < 75
    assert row["subtitle"] == "Stainless steel material"


def test_localized_subtitle_fills_from_description_when_model_returns_empty() -> None:
    row = {
        "site": "IT",
        "title": "Cornice in fibra di carbonio per pulsante start-stop auto",
        "subtitle": "",
        "desc": (
            "Cornice decorativa per il pulsante di avvio del motore.\n\n"
            "Materiale: lega metallica\n"
            "Colore: fibra di carbonio\n"
            "Diametro interno: 3 cm\n"
            "Diametro esterno: 3,8 cm\n"
            "Dimensioni: 4 cm x 5 cm"
        ),
        "bullets": [],
        "keywords": "cornice pulsante, protezione interni auto",
    }

    normalize_subtitle_for_row(row)

    assert row["subtitle"]
    assert len(row["subtitle"]) <= SUBTITLE_MAX_LENGTH
    assert "lega metallica" in row["subtitle"]
    assert "3,8 cm" in row["subtitle"]
    assert ":" not in row["subtitle"]


def test_build_subtitle_rejects_special_symbols_and_title_duplicates() -> None:
    subtitle = build_subtitle(
        "Generic Stainless Steel Mounting Kit",
        [
            "Stainless Steel Mounting Kit",
            "Tool-free install!",
            "Corrosion resistant finish",
            "Workshop use",
        ],
    )

    assert subtitle == "Tool free install, Corrosion resistant finish, Workshop use"
