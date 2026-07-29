from __future__ import annotations

import json

import pytest

from scripts.apply_amazon_review_decisions import apply_decisions


def payload() -> dict:
    return {
        "商品id": ["p1", "p2"],
        "产品标题": ["One", "Two"],
        "产品描述": ["Description one", "Description two"],
        "产品图片链接": [
            ["https://img/main1.jpg", "https://img/extra1.jpg"],
            ["https://img/main2.jpg"],
        ],
        "变种图片链接": [
            ["https://img/variant1.jpg"],
            [],
        ],
        "Bullet Point1": ["1", "1"],
        "Bullet Point2": ["2", "2"],
        "Bullet Point3": ["3", "3"],
        "Bullet Point4": ["4", "4"],
        "Bullet Point5": ["5", "5"],
        "关键词信息": ["a,b", "c,d"],
        "有问题的产品id": [],
    }


def write_json(path, value) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def decisions(*items) -> dict:
    return {
        "version": 1,
        "decisions": list(items),
    }


def test_delete_attachment_and_product_with_backup(tmp_path) -> None:
    formal = tmp_path / "formal.json"
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    (review_dir / "marker.txt").write_text("review", encoding="utf-8")
    decision_path = tmp_path / "decisions.json"
    write_json(formal, payload())
    write_json(decision_path, decisions(
        {
            "product_id": "p1",
            "action": "delete_image",
            "role": "attachment",
            "image_url": "https://img/extra1.jpg",
        },
        {
            "product_id": "p2",
            "action": "delete_product",
            "role": "product",
        },
    ))

    result = apply_decisions(
        formal,
        decision_path,
        review_package=review_dir,
    )

    updated = json.loads(formal.read_text(encoding="utf-8"))
    assert updated["商品id"] == ["p1"]
    assert updated["产品图片链接"][0] == [
        "https://img/main1.jpg",
    ]
    assert updated["有问题的产品id"] == ["p2"]
    assert result["status"] == "applied"
    assert (tmp_path / "审核应用备份").exists()
    assert (
        list((tmp_path / "审核应用备份").iterdir())[0]
        / "终审包"
        / "marker.txt"
    ).exists()


def test_main_or_variant_cannot_be_deleted(tmp_path) -> None:
    formal = tmp_path / "formal.json"
    decision_path = tmp_path / "decisions.json"
    write_json(formal, payload())
    write_json(decision_path, decisions({
        "product_id": "p1",
        "action": "delete_image",
        "role": "main",
        "image_url": "https://img/main1.jpg",
    }))

    with pytest.raises(ValueError, match="主图/变种图不能直接删除"):
        apply_decisions(formal, decision_path)


def test_dry_run_does_not_mutate_or_create_backup(tmp_path) -> None:
    formal = tmp_path / "formal.json"
    decision_path = tmp_path / "decisions.json"
    original = payload()
    write_json(formal, original)
    write_json(decision_path, decisions({
        "product_id": "p2",
        "action": "delete_product",
        "role": "product",
    }))

    result = apply_decisions(
        formal,
        decision_path,
        dry_run=True,
    )

    assert result["status"] == "dry_run"
    assert json.loads(formal.read_text(encoding="utf-8")) == original
    assert not (tmp_path / "审核应用备份").exists()


def test_false_positive_for_quarantined_product_is_recorded(
    tmp_path,
) -> None:
    formal = tmp_path / "formal.json"
    decision_path = tmp_path / "decisions.json"
    write_json(formal, payload())
    write_json(decision_path, decisions({
        "product_id": "quarantined-id",
        "action": "false_positive",
        "role": "main",
        "image_url": "https://img/brand.jpg",
        "note": "人工确认是通用装饰图形",
    }))

    result = apply_decisions(formal, decision_path)

    overrides = json.loads(
        (tmp_path / "formal_图片人工覆盖.json").read_text(
            encoding="utf-8"
        )
    )
    assert overrides["overrides"][0]["product_id"] == "quarantined-id"
    assert result["status"] == "applied"
