from __future__ import annotations

from scripts.pipelines import amazon_constants
from scripts.pipelines import amazon_quality


def test_amazon_constants_is_a_stable_quality_adapter() -> None:
    assert (
        amazon_constants._add_audit
        is amazon_quality.add_audit
    )
    assert (
        amazon_constants._normalize_keywords_for_row
        is amazon_quality.normalize_keywords_for_row
    )
    assert (
        amazon_constants._validate_amazon_rows
        is amazon_quality.validate_amazon_rows
    )
    assert (
        amazon_constants.AMAZON_REVIEW_CONCURRENCY
        == amazon_quality.AMAZON_REVIEW_CONCURRENCY
    )


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


def test_quality_interface_accepts_complete_listing_row() -> None:
    row = {
        "title": "Generic Stainless Trailer Hitch Lock 2 Pack",
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
