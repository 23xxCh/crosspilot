from __future__ import annotations

from amazon_processor import quality as amazon_quality


def test_quality_interface_deduplicates_issue_and_records_audit() -> None:
    row = {"desc": "12V stainless lock with 2 keys"}

    amazon_quality.add_quality_issue(
        row,
        "description_fact_loss",
        "Description lost facts",
    )
    amazon_quality.add_quality_issue(
        row,
        "description_fact_loss",
        "Description lost facts",
    )

    assert row["_quality_issues"] == [{
        "code": "description_fact_loss",
        "message": "Description lost facts",
    }]
    assert row["_audit"][0]["reason"] == "description_fact_loss"
    assert amazon_quality.missing_factual_markers(
        "12V lock includes 2 keys",
        "Lock for trailer use",
    ) == ["12v", "2 key"]


def test_fact_markers_ignore_glued_scrape_fragments() -> None:
    text = (
        "new60 12vled with8-inchtouchscreen W212FitmentFit "
        "C1172014-2018Fit K5Features Fit6 Ix35For 2016Fit "
        "CR-V2023-2025 NX42022-2024 NX420222023 L2021-2025 10:00PM"
    )

    assert amazon_quality.extract_factual_markers(text) == []


def test_fact_markers_keep_real_year_measurement_and_part_number() -> None:
    text = "Fits 2007-2018, 6.5 inch speaker, OE 5183172AC, 7pin plug"

    assert amazon_quality.extract_factual_markers(text) == [
        "2007",
        "2018",
        "6.5 inch",
        "5183172ac",
        "7pin",
    ]


def test_fact_markers_match_model_numbers_case_insensitively() -> None:
    source = "Fits Ford F150 F250 F350, replacement part 7851884AA"
    candidate = "fits ford f150 f250 f350, replacement part 7851884aa"

    assert amazon_quality.missing_factual_markers(source, candidate) == []


def test_unexpected_brands_match_chinese_compatibility_aliases() -> None:
    assert amazon_quality.unexpected_brand_markers(
        "适用于特斯拉 Model 3 / Y",
        "Generic Schutz für Tesla Model 3 Y",
    ) == []
    assert amazon_quality.unexpected_brand_markers(
        "适用于讴歌 2022-2025",
        "Generic Accessory for Acura 2022-2025",
    ) == []


def test_quality_interface_accepts_complete_listing_row() -> None:
    row = {
        "title": "Generic Stainless Trailer Hitch Lock 2 Pack",
        "subtitle": "Corrosion resistant finish, receiver installation",
        "desc": (
            "Stainless steel trailer lock with corrosion resistance "
            "and simple receiver installation."
        ),
        "main_img": "https://img.example/main.jpg",
        "bullets": [
            "Stainless steel construction supports regular towing use",
            "Corrosion resistant finish suits outdoor receiver hardware",
            "Trailer hitch design secures compatible towing accessories",
            "Simple installation supports routine lock replacement",
            "Two pack configuration provides matching receiver hardware",
        ],
        "keywords": (
            "trailer hitch lock, stainless lock pin, towing receiver lock, "
            "corrosion resistant lock, trailer security hardware, "
            "receiver pin lock, towing accessory lock, hitch replacement pin, "
            "outdoor trailer lock, two pack lock"
        ),
    }

    result = amazon_quality.validate_amazon_rows([row])

    assert result == {
        "passed": True,
        "issues": [],
        "truncated": False,
    }


def test_quality_rejects_invalid_subtitle_shape() -> None:
    row = {
        "title": "Generic Product",
        "subtitle": "Best Seller! Free shipping.",
        "desc": "Generic product with useful product details.",
        "main_img": "https://img.example/main.jpg",
        "bullets": ["Useful product detail"] * 5,
        "keywords": "product one, product two",
    }

    result = amazon_quality.validate_amazon_rows([row])

    assert any("副标题" in issue for issue in result["issues"])
