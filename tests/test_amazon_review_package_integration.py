from __future__ import annotations

import pytest

from amazon_processor.delivery import (
    _assert_formal_images_are_safe,
)

def test_formal_image_acceptance_rejects_missing_safe_record() -> None:
    rows = [{
        "id": "p1",
        "main_img": "https://img/main.jpg",
        "extra_imgs": [],
        "var_imgs": [],
        "_image_assessments": [],
    }]

    with pytest.raises(ValueError, match="未获 safe 记录"):
        _assert_formal_images_are_safe(
            rows,
            {"image_safety_gate": {"reviewed": 1}},
        )


def test_formal_image_acceptance_allows_only_safe_records() -> None:
    rows = [{
        "id": "p1",
        "main_img": "https://img/main.jpg",
        "extra_imgs": ["https://img/extra.jpg"],
        "var_imgs": ["https://img/variant.jpg"],
        "_image_assessments": [
            {
                "url": "https://img/main.jpg",
                "role": "main",
                "assessment": {"status": "safe"},
            },
            {
                "url": "https://img/extra.jpg",
                "role": "attachment",
                "assessment": {"status": "safe"},
            },
            {
                "url": "https://img/variant.jpg",
                "role": "variant",
                "assessment": {"status": "safe"},
            },
        ],
    }]

    _assert_formal_images_are_safe(
        rows,
        {"image_safety_gate": {"reviewed": 3}},
    )


def test_formal_image_acceptance_allows_main_without_text_gate() -> None:
    rows = [{
        "id": "p1",
        "main_img": "https://img/main.jpg",
        "extra_imgs": [],
        "var_imgs": [],
        "_image_assessments": [{
            "url": "https://img/main.jpg",
            "role": "main",
            "assessment": {"status": "safe"},
        }],
    }]

    _assert_formal_images_are_safe(
        rows,
        {"image_safety_gate": {"reviewed": 1}},
    )
