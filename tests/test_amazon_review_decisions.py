from __future__ import annotations

import json
import hashlib
from types import SimpleNamespace

import pytest

from amazon_processor.review import decisions as review_decisions
from amazon_processor.review.decisions import apply_decisions


def payload() -> dict:
    return {
        "商品id": ["p1", "p2"],
        "产品站点": ["US", "DE"],
        "产品标题": ["One", "Two"],
        "副标题": ["First highlight", "Second highlight"],
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
    assert list(tmp_path.glob("formal.backup_*.json"))


def test_delete_product_removes_review_data_and_recalculates_summary(
    tmp_path,
) -> None:
    formal = tmp_path / "formal.json"
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    decision_path = tmp_path / "decisions.json"
    write_json(formal, payload())
    write_json(decision_path, decisions({
        "product_id": "p2",
        "action": "delete_product",
        "role": "product",
    }))
    write_json(review_dir / "审核数据.json", {
        "version": 2,
        "run_id": "test",
        "summary": {
            "products": 2,
            "released_products": 2,
            "quarantined_products": 0,
            "image_occurrences": 2,
            "unique_images": 2,
            "downloaded_unique_images": 2,
        },
        "products": [
            {
                "row": 1,
                "product_id": "p1",
                "title": "One",
                "subtitle": "First highlight",
                "description": "One",
                "bullets": [],
                "keywords": "",
                "images": [{
                    "role": "主图",
                    "role_key": "main",
                    "url": "https://img/main1.jpg",
                    "local_path": "图片/main1.jpg",
                    "download_ok": True,
                    "source": "source",
                    "assessment": {"status": "safe"},
                }],
            },
            {
                "row": 2,
                "product_id": "p2",
                "title": "Two",
                "subtitle": "Second highlight",
                "description": "Two",
                "bullets": [],
                "keywords": "",
                "images": [{
                    "role": "主图",
                    "role_key": "main",
                    "url": "https://img/main2.jpg",
                    "local_path": "图片/main2.jpg",
                    "download_ok": True,
                    "source": "source",
                    "assessment": {"status": "safe"},
                }],
            },
        ],
        "images": {
            "https://img/main1.jpg": {"ok": True},
            "https://img/main2.jpg": {"ok": True},
        },
    })

    apply_decisions(
        formal,
        decision_path,
        review_package=review_dir,
    )

    review = json.loads(
        (review_dir / "审核数据.json").read_text(encoding="utf-8")
    )
    updated = json.loads(formal.read_text(encoding="utf-8"))
    assert updated["有问题的产品id"] == ["p2"]
    assert [item["product_id"] for item in review["products"]] == ["p1"]
    assert review["products"][0]["row"] == 1
    assert set(review["images"]) == {"https://img/main1.jpg"}
    assert review["summary"]["products"] == 1
    assert review["summary"]["released_products"] == 1
    assert review["summary"]["image_occurrences"] == 1
    assert review["summary"]["unique_images"] == 1
    assert review["summary"]["downloaded_unique_images"] == 1


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


def test_reorder_main_and_attachments_updates_formal_and_review_package(
    tmp_path,
) -> None:
    formal = tmp_path / "formal.json"
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    decision_path = tmp_path / "decisions.json"
    source = payload()
    source["产品图片链接"][0].append("https://img/extra2.jpg")
    write_json(formal, source)
    write_json(decision_path, decisions(
        {
            "product_id": "p1",
            "action": "reorder_images",
            "role": "product_images",
            "image_urls": [
                "https://img/extra2.jpg",
                "https://img/main1.jpg",
                "https://img/extra1.jpg",
            ],
        },
        {
            "product_id": "p1",
            "action": "delete_image",
            "role": "attachment",
            "image_url": "https://img/main1.jpg",
        },
    ))
    write_json(review_dir / "审核数据.json", {
        "version": 2,
        "run_id": "reorder-test",
        "summary": {
            "products": 1,
            "released_products": 1,
            "quarantined_products": 0,
            "image_occurrences": 3,
            "unique_images": 3,
            "downloaded_unique_images": 3,
        },
        "products": [{
            "row": 1,
            "product_id": "p1",
            "title": "One",
            "subtitle": "",
            "description": "One",
            "bullets": [],
            "keywords": "",
            "images": [
                {
                    "role": "主图",
                    "role_key": "main",
                    "position": 0,
                    "url": "https://img/main1.jpg",
                    "local_path": "图片/main1.jpg",
                    "download_ok": True,
                    "source": "source",
                    "assessment": {"status": "safe"},
                },
                {
                    "role": "附图 1",
                    "role_key": "attachment",
                    "position": 1,
                    "url": "https://img/extra1.jpg",
                    "local_path": "图片/extra1.jpg",
                    "download_ok": True,
                    "source": "source",
                    "assessment": {"status": "safe"},
                },
                {
                    "role": "附图 2",
                    "role_key": "attachment",
                    "position": 2,
                    "url": "https://img/extra2.jpg",
                    "local_path": "图片/extra2.jpg",
                    "download_ok": True,
                    "source": "source",
                    "assessment": {"status": "safe"},
                },
            ],
        }],
        "images": {
            "https://img/main1.jpg": {"ok": True},
            "https://img/extra1.jpg": {"ok": True},
            "https://img/extra2.jpg": {"ok": True},
        },
    })

    apply_decisions(
        formal,
        decision_path,
        review_package=review_dir,
    )

    updated = json.loads(formal.read_text(encoding="utf-8"))
    assert updated["产品图片链接"][0] == [
        "https://img/extra2.jpg",
        "https://img/extra1.jpg",
    ]
    review = json.loads(
        (review_dir / "审核数据.json").read_text(encoding="utf-8")
    )
    images = review["products"][0]["images"]
    assert [item["url"] for item in images] == updated["产品图片链接"][0]
    assert [item["role"] for item in images] == ["主图", "附图 1"]
    assert set(review["images"]) == {
        "https://img/extra1.jpg",
        "https://img/extra2.jpg",
    }
    assert review["summary"]["image_occurrences"] == 2


def test_reorder_images_rejects_missing_or_new_url(tmp_path) -> None:
    formal = tmp_path / "formal.json"
    decision_path = tmp_path / "decisions.json"
    write_json(formal, payload())
    write_json(decision_path, decisions({
        "product_id": "p1",
        "action": "reorder_images",
        "role": "product_images",
        "image_urls": [
            "https://img/extra1.jpg",
            "https://img/not-in-product.jpg",
        ],
    }))

    with pytest.raises(ValueError, match="不能新增、遗漏或重复图片"):
        apply_decisions(formal, decision_path)


def test_select_existing_reorder_rejects_ineligible_new_main(tmp_path) -> None:
    formal = tmp_path / "formal.json"
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    decision_path = tmp_path / "decisions.json"
    write_json(formal, payload())
    write_json(review_dir / "审核数据.json", {
        "summary": {
            "run_metrics": {
                "image_safety_gate": {
                    "processing_mode": "select_existing",
                },
            },
        },
        "products": [{
            "product_id": "p1",
            "images": [
                {"url": "https://img/main1.jpg", "main_eligible": True},
                {"url": "https://img/extra1.jpg", "main_eligible": False},
            ],
        }],
    })
    write_json(decision_path, decisions({
        "product_id": "p1",
        "action": "reorder_images",
        "role": "product_images",
        "image_urls": [
            "https://img/extra1.jpg",
            "https://img/main1.jpg",
        ],
    }))

    with pytest.raises(ValueError, match=r"没有 safe \+ text_free 主图资格"):
        apply_decisions(
            formal,
            decision_path,
            review_package=review_dir,
        )


def test_recheck_main_candidate_invalidates_both_reviews_and_reruns(
    tmp_path,
    monkeypatch,
) -> None:
    from amazon_processor import pipeline

    source = tmp_path / "input.json"
    source.write_text("{}", encoding="utf-8")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    runtime_root = tmp_path / ".runtime"
    cache_path = runtime_root / "cache" / "pipeline" / f"{source_hash}.json"
    cache_path.parent.mkdir(parents=True)
    target = "https://img/candidate.jpg"
    untouched = "https://img/other.jpg"
    write_json(cache_path, {
        "risk_assessments": {target: {"status": "safe"}, untouched: {"status": "safe"}},
        "main_text_assessments": {target: {"status": "risk"}, untouched: {"status": "safe"}},
    })
    decision_path = tmp_path / "decisions.json"
    write_json(decision_path, {
        "version": 1,
        "source": str(source),
        "decisions": [{
            "product_id": "p1",
            "action": "recheck_main_candidate",
            "role": "attachment",
            "image_url": target,
        }],
    })
    monkeypatch.setattr(pipeline, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(
        pipeline,
        "_process_json_unlocked",
        lambda path: SimpleNamespace(
            published=False,
            output_path=None,
            review_path=tmp_path / "pending" / "终审包.html",
            pending_product_ids=("p1",),
        ),
    )

    report = review_decisions.apply_latest_decisions(decision_path)

    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert target not in cache["risk_assessments"]
    assert target not in cache["main_text_assessments"]
    assert untouched in cache["risk_assessments"]
    assert untouched in cache["main_text_assessments"]
    assert report["status"] == "rechecked"
    assert report["published"] is False
    assert report["pending_product_ids"] == ["p1"]


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
        (
            tmp_path
            / ".runtime"
            / "cache"
            / "review_overrides.json"
        ).read_text(encoding="utf-8")
    )
    assert overrides["overrides"][0]["product_id"] == "quarantined-id"
    assert result["status"] == "applied"


def test_manual_main_regeneration_accepts_decodable_image_for_review(monkeypatch) -> None:
    class Provider:
        def __init__(self):
            self.policies = []

        def call_image_gen(self, *_args, **_kwargs):
            return "https://img/generated.jpg"

        def assess_image(self, _url, *, policy="general"):
            self.policies.append(policy)
            if policy == "main_text_free":
                return {"status": "risk"}
            return {"status": "safe"}

    provider = Provider()
    monkeypatch.setattr(
        review_decisions,
        "get_provider",
        lambda: provider,
    )
    monkeypatch.setattr(
        review_decisions,
        "validate_image_url",
        lambda _url: (True, ""),
    )

    generated, assessment = review_decisions._regenerate_safe_image(
        "https://img/source.jpg",
        role="main",
        routes=1,
    )

    assert generated == "https://img/generated.jpg"
    assert assessment["accepted_without_machine_review"] is True
    assert provider.policies == []
