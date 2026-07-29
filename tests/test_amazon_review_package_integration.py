from __future__ import annotations

import pytest

from scripts.pipelines.amazon_delivery import (
    _assert_formal_images_are_safe,
    _review_root_for_output,
    _write_latest_review_entry,
)


def test_review_root_uses_amazon_workspace_folder(tmp_path) -> None:
    root = tmp_path / "亚马逊表"
    output = root / "90_旧文件" / "回填表.json"

    result = _review_root_for_output(str(output))

    assert result == root / "检查图片文字"


def test_latest_review_entry_redirects_to_timestamp_package(
    tmp_path,
) -> None:
    review_root = tmp_path / "检查图片文字"
    run_dir = review_root / "运行_20260101_120000"
    run_dir.mkdir(parents=True)

    latest = _write_latest_review_entry(review_root, run_dir)

    content = latest.read_text(encoding="utf-8")
    assert "运行_20260101_120000/中文文案检查表.html" in content


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
