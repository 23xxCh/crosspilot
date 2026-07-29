"""Amazon final-review package Module tests."""
from __future__ import annotations

import json

from amazon_processor.review import exporter
from amazon_processor.review import exporter as review_package


def _payload() -> dict:
    return {
        "商品id": ["released-1"],
        "产品标题": ["Product"],
        "产品描述": ["Description"],
        "产品图片链接": [[
            "https://img/main.jpg",
            "https://img/extra.jpg",
        ]],
        "变种图片链接": [[]],
        "Bullet Point1": ["One"],
        "Bullet Point2": ["Two"],
        "Bullet Point3": ["Three"],
        "Bullet Point4": ["Four"],
        "Bullet Point5": ["Five"],
        "关键词信息": ["one, two"],
        "有问题的产品id": [],
    }


def test_export_review_composes_package_and_quarantine(
    tmp_path,
    monkeypatch,
) -> None:
    input_path = tmp_path / "formal.json"
    input_path.write_text(
        json.dumps(_payload(), ensure_ascii=False),
        encoding="utf-8",
    )
    output_dir = tmp_path / "review"
    translation = {
        "title": "中文产品",
        "description": "中文描述",
        "bullets": ["一", "二", "三", "四", "五"],
        "keywords": "关键词一，关键词二",
    }

    monkeypatch.setattr(exporter, "reload_provider", lambda: None)
    monkeypatch.setattr(
        exporter,
        "translate_payload",
        lambda *_args, **_kwargs: ([translation], [], {"calls": 1}),
    )

    def fake_download(
        _payload_value,
        _output_dir,
        *,
        extra_urls,
        **_kwargs,
    ):
        urls = {
            "https://img/main.jpg",
            "https://img/extra.jpg",
            *[url for url in extra_urls if url],
        }
        return {
            url: {
                "ok": True,
                "path": "图片/" + url.rsplit("/", 1)[-1],
            }
            for url in urls
        }, []

    monkeypatch.setattr(
        exporter,
        "download_all_images",
        fake_download,
    )
    quarantine = [{
        "product_id": "quarantined-1",
        "source_row": {
            "title": "Blocked product",
            "description": "Blocked description",
            "bullets": ["1", "2", "3", "4", "5"],
            "keywords": "blocked",
        },
        "images": [{
            "role": "main",
            "url": "https://img/quarantined.jpg",
            "assessment": {
                "status": "risk",
                "risk_categories": ["brand_logo"],
                "evidence": "Visible logo",
            },
        }],
        "reasons": [{"code": "intrinsic_brand_product"}],
    }]

    summary = review_package.export_review(
        input_path,
        output_dir,
        quarantine_products=quarantine,
        run_id="module-test",
    )

    assert summary["released_products"] == 1
    assert summary["quarantined_products"] == 1
    assert summary["products"] == 2
    assert summary["image_occurrences"] == 3
    assert summary["provider_metrics"] == {"calls": 1}
    for filename in ("审核数据.json", "终审包.html"):
        assert (output_dir / filename).is_file()
    review_data = json.loads(
        (output_dir / "审核数据.json").read_text(
            encoding="utf-8",
        )
    )
    assert review_data["run_id"] == "module-test"
    assert review_data["products"][1]["quarantined"] is True
