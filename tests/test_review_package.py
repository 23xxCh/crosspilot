"""Amazon final-review package Module tests."""
from __future__ import annotations

import json

from amazon_processor.review import exporter
from amazon_processor.review import exporter as review_package
from amazon_processor.review.translation import (
    _source_row,
    _valid_translation,
)


def _payload() -> dict:
    return {
        "商品id": ["released-1"],
        "产品标题": ["Product"],
        "副标题": ["Compact fit, easy installation"],
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
        "subtitle": "紧凑适配，易安装",
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
            "subtitle": "Blocked highlight",
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
        audit_by_product={
            "released-1": [{
                "role": "main",
                "url": "https://img/main.jpg",
                "source": "generated",
                "source_url": "https://img/original-main.jpg",
                "assessment": {
                    "status": "safe",
                    "risk_categories": [],
                    "evidence": "No general risk.",
                },
                "text_assessment": {
                    "status": "safe",
                    "placement": "none",
                    "detected_text": [],
                    "evidence": "No visible text.",
                },
                "source_text_assessment": {
                    "status": "risk",
                    "placement": "product_surface",
                    "detected_text": ["12V"],
                    "evidence": "12V was visible.",
                },
                "source_image_action": "edit_translate",
                "source_detected_text": ["12V"],
                "generation_route_offset": 1,
                "candidates_reviewed": 2,
            }],
        },
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
    main = review_data["products"][0]["images"][0]
    assert main["text_assessment"]["status"] == "safe"
    assert main["source_text_assessment"]["detected_text"] == ["12V"]
    assert main["source_image_action"] == "edit_translate"
    html = (output_dir / "终审包.html").read_text(encoding="utf-8")
    assert "处理动作：edit_translate" in html
    assert "编辑前文字：12V" in html
    assert "拖动主图/附图可换位" in html
    assert 'data-action="reorder_images"' not in html
    assert "action:'reorder_images'" in html
    assert 'draggable="true"' in html
    assert "container.addEventListener('dragenter'" in html
    assert "document.querySelectorAll('.image[data-sortable=\"product\"]')" in html
    assert 'draggable="false"' in html
    assert 'id="export-refill"' in html
    assert "function buildReviewedRefill()" in html
    assert "跨境电商自动化回填表.json" in html
    assert '"商品id":["released-1"]' in html


def test_review_translation_preserves_description_paragraphs() -> None:
    payload = _payload()
    payload["产品描述"] = [
        "Car armrest pad for center consoles.\n\n"
        "Material: Artificial leather\n"
        "Size: 29 × 19 × 1 cm\n"
        "Package Includes: 1 armrest pad"
    ]
    source = _source_row(payload, 0)
    translated = {
        "title": "汽车扶手垫",
        "subtitle": "人造革材质，通用适配",
        "description": (
            "适用于中央扶手箱的汽车扶手垫。\n\n"
            "材质：人造革\n"
            "尺寸：29 × 19 × 1 cm\n"
            "包装清单：1 个扶手垫"
        ),
        "bullets": ["一", "二", "三", "四", "五"],
        "keywords": "扶手垫，中央扶手箱",
    }

    assert "\n\n" in source["description"]
    assert _valid_translation(source, translated) is True
    translated["description"] = translated["description"].replace("\n", " ")
    assert _valid_translation(source, translated) is False


def test_review_translation_accepts_known_missing_source_fields() -> None:
    payload = _payload()
    payload["产品描述"] = [""]
    payload["副标题"] = [""]
    for number in range(1, 6):
        payload[f"Bullet Point{number}"] = [""]
    payload["关键词信息"] = [""]
    source = _source_row(payload, 0)

    assert _valid_translation(source, {
        "title": "中文产品",
        "subtitle": "",
        "description": "",
        "bullets": ["", "", "", "", ""],
        "keywords": "",
    }) is True
