from __future__ import annotations

import json

from amazon_processor.config.prompts import get_prompt_registry
from amazon_processor.markets import get_market, market_codes
from amazon_processor.policy import (
    PROHIBITED_LISTING_TERMS_RE,
    enforce_prohibited_listing_terms,
)
from amazon_processor.text.descriptions import format_description
from amazon_processor.text.locale import (
    localization_violations,
    market_prompt_values,
    normalize_localized_title,
)
from amazon_processor.text.localization import (
    LocalizationCache,
    _all_violations,
    _apply_candidate,
    _fact_violations,
    ensure_localized_rows,
)


def _localized_row(site: str = "DE") -> dict:
    return {
        "id": "item-de",
        "site": site,
        "_source_title": "Toyota Camry 2018 12V Switch Cover",
        "_source_desc": (
            "Protective switch cover for Toyota Camry 2018. Voltage: 12V. "
            "Material: ABS plastic. Package includes one switch cover."
        ),
        "title": "Generic Schalterabdeckung für Toyota Camry 2018 12V",
        "subtitle": "Robuster ABS-Kunststoff, einfache Montage",
        "desc": (
            "Diese Schalterabdeckung schützt den Startknopf im Fahrzeug und "
            "eignet sich für den täglichen Gebrauch.\n\n"
            "Material: ABS-Kunststoff\n"
            "Spezifikationen: 12V\n"
            "Lieferumfang: 1 Schalterabdeckung"
        ),
        "bullets": [
            "Robuster ABS-Kunststoff für den täglichen Einsatz im Fahrzeug",
            "Passgenaue Form für eine saubere und sichere Montage",
            "Schützt den Startknopf vor Kratzern und täglicher Abnutzung",
            "Die 12V-Ausführung bewahrt alle angegebenen technischen Daten",
            "Der Lieferumfang enthält eine Schalterabdeckung",
        ],
        "keywords": (
            "Schalterabdeckung Auto, Startknopf Abdeckung, ABS Abdeckung, "
            "Fahrzeug Schalter Schutz, 12V Zubehör, Startknopf Schutz, "
            "Innenraum Zubehör, Knopf Abdeckung, Auto Schalter Blende, "
            "Schalter Schutzkappe"
        ),
    }


def test_market_mapping_matches_nine_supported_sites() -> None:
    assert market_codes() == (
        "US", "UK", "CA", "MX", "ES", "BR", "DE", "FR", "IT"
    )
    assert get_market("mx").locale == "es-MX"
    assert get_market("BR").language_code == "pt"
    assert get_market("DE").compatibility_connector == "für"


def test_prompts_accept_marketplace_variables() -> None:
    row = {"site": "FR"}
    prompt = get_prompt_registry().render(
        "amazon.title_optimize",
        title="Toyota Camry Switch Cover",
        **market_prompt_values(row),
    )

    assert "France" not in prompt
    assert "法国" in prompt
    assert "fr-FR" in prompt
    assert "pour" in prompt


def test_locale_title_and_description_labels_are_deterministic() -> None:
    title = normalize_localized_title(
        "Generic Abdeckung for Toyota Camry 2018",
        "DE",
    )
    description, _ = format_description(
        "Robuste Abdeckung für den täglichen Gebrauch.",
        [("Size", "10 × 5 cm"), ("Package Includes", "1 Abdeckung")],
        site="DE",
    )

    assert title == "Generic Abdeckung für Toyota Camry 2018"
    assert "Größe: 10 × 5 cm" in description
    assert "Lieferumfang: 1 Abdeckung" in description


def test_language_validation_rejects_english_copy_for_germany() -> None:
    row = _localized_row()
    row["desc"] = (
        "This protective switch cover is made from durable ABS plastic and "
        "supports simple installation for everyday vehicle use. " * 3
    )
    row["bullets"] = [
        "Durable ABS plastic construction for everyday vehicle use"
    ] * 5

    assert any(
        violation.startswith("language:en->de")
        for violation in localization_violations(row)
    )


def test_release_validation_rejects_long_keywords_special_subtitle_and_oem() -> None:
    row = _localized_row("US")
    row["subtitle"] = "Universal 4-12 inch fit"
    row["bullets"][0] = "OEM construction for daily vehicle use"
    row["keywords"] = ", ".join(
        f"extended keyword phrase number {index}"
        for index in range(10)
    )

    violations = localization_violations(row)

    assert "subtitle_characters" in violations
    assert "keywords_length" in violations
    assert "listing_brand_or_oem" in violations


def test_localization_cache_restores_only_valid_current_rows(tmp_path) -> None:
    cache = LocalizationCache(tmp_path / "localization.json")
    row = _localized_row()
    assert localization_violations(row) == []
    cache.store(row)

    restored = {
        **row,
        "title": "stale",
        "subtitle": "",
        "desc": "stale",
        "bullets": [],
        "keywords": "",
    }
    assert cache.restore(restored) is True
    assert restored["title"] == row["title"]
    assert restored["_localization_cache_hit"] is True


def test_localization_cache_restores_successful_partial_stages(tmp_path) -> None:
    path = tmp_path / "localization.json"
    cache = LocalizationCache(path)
    row = _localized_row()
    assert cache.store_partial(row, ("title",)) is True
    assert cache.store_partial(row, ("desc",)) is True

    pending = {
        **row,
        "title": "",
        "desc": "",
        "subtitle": "",
        "bullets": [],
        "keywords": "",
    }
    restored = LocalizationCache(path)

    assert restored.restore(pending) is False
    assert pending["title"] == row["title"]
    assert pending["desc"] == row["desc"]
    assert set(pending["_localization_partial_fields"]) == {"title", "desc"}


def test_multilingual_prohibited_terms_are_removed() -> None:
    row = _localized_row()
    row["subtitle"] = "Für Kinder, Knopfzelle enthalten"
    row["desc"] += "\nKnopfzelle: 1 Stück"
    row["bullets"][0] = "Geeignet für Kinder und Mädchen"
    row["keywords"] = row["keywords"].replace(
        "Schalter Schutzkappe",
        "Knopfzelle",
    )

    enforce_prohibited_listing_terms([row])

    combined = " ".join([
        row["title"], row["subtitle"], row["desc"],
        *row["bullets"], row["keywords"],
    ])
    assert not PROHIBITED_LISTING_TERMS_RE.search(combined)


def test_fact_validation_uses_selected_product_block_and_quantity_semantics() -> None:
    row = _localized_row()
    row["_source_desc"] += (
        " Cross-sell item size 3cm 3.8cm 4cm 5cm."
    )
    row["_description_source_block"] = (
        "Protective switch cover for Toyota Camry 2018, 12V, 1pc."
    )

    violations = _fact_violations(row)

    assert "missing_fact:3cm" not in violations
    assert "missing_fact:1pc" not in violations


def test_repair_postprocessing_enforces_limits_counts_and_source_facts() -> None:
    row = _localized_row()
    row["_description_source_block"] = (
        "Protective switch cover, sizes 3cm 3.8cm 4cm 5cm, 12V."
    )
    candidate = _apply_candidate(row, {
        "title": row["title"],
        "subtitle": row["subtitle"],
        "description": (
            "Robuste Abdeckung für den täglichen Gebrauch.\n\n"
            "Material: ABS-Kunststoff. " * 20
        ),
        "bullets": ["Robuste Konstruktion"],
        "keywords": "Abdeckung",
    })

    assert len(candidate["desc"]) <= 500
    assert len(candidate["bullets"]) == 5
    assert len(candidate["keywords"].split(",")) == 10
    assert _all_violations(candidate) == []


class _GermanRepairProvider:
    def __init__(self) -> None:
        self.calls = 0

    def call_text(self, _prompt, max_tokens=4096):
        del max_tokens
        self.calls += 1
        row = _localized_row()
        return json.dumps({
            "title": row["title"],
            "subtitle": row["subtitle"],
            "description": row["desc"],
            "bullets": row["bullets"],
            "keywords": row["keywords"],
        }, ensure_ascii=False)


def test_final_repair_persists_successful_row(tmp_path) -> None:
    row = _localized_row()
    row["title"] = "English switch cover"
    row["subtitle"] = ""
    row["desc"] = "English product description"
    row["bullets"] = []
    row["keywords"] = ""
    provider = _GermanRepairProvider()
    cache = LocalizationCache(tmp_path / "localization.json")

    ensure_localized_rows(
        [row],
        cache=cache,
        provider_getter=lambda: provider,
        retry_delays=(0,),
    )

    assert provider.calls == 1
    assert localization_violations(row) == []
    assert cache.writes == 1
