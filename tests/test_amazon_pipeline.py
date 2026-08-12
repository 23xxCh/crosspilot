from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from amazon_processor import delivery, pipeline
from amazon_processor.images.risk import (
    normalize_image_assessment,
    normalize_main_text_assessment,
)
from amazon_processor.review import exporter, translation


class DeterministicProvider:
    def assess_image(
        self,
        _url,
        *,
        confirmation=False,
        policy="general",
        retries=3,
    ):
        del confirmation, retries
        normalizer = (
            normalize_main_text_assessment
            if policy == "main_text_free"
            else normalize_image_assessment
        )
        return normalizer({
            "status": "safe",
            "reasons": [],
            "placement": "none",
            "detected_text": [],
            "confidence": 1.0,
            "evidence": "deterministic test",
        })

    def call_text(self, prompt, *, max_tokens=4096, **_kwargs):
        del max_tokens
        if "BULLET POINTS" in prompt:
            return json.dumps({
                "subtitle": "Stainless steel, easy installation",
                "bullets": [
                    "Durable material supports reliable everyday use",
                    "Practical dimensions fit the intended application",
                    "Simple design supports straightforward installation",
                    "Compatible construction suits common replacement needs",
                    "Package includes the complete product shown",
                ],
                "keywords": (
                    "replacement product, durable accessory, mounting kit, "
                    "easy installation, practical hardware, everyday use, "
                    "repair part, universal item, product kit, useful accessory"
                ),
            })
        if "必须只返回一个 JSON 对象" in prompt:
            return json.dumps({
                "title": "通用测试产品",
                "subtitle": "不锈钢材质，安装简单",
                "description": "适用于日常使用的通用测试产品。",
                "bullets": ["耐用材质", "尺寸实用", "安装简单", "兼容常见用途", "包装完整"],
                "keywords": "替换产品，耐用配件，安装套件",
            }, ensure_ascii=False)
        if "optimize Amazon product titles" in prompt:
            return "Generic Stainless Steel Mounting Kit"
        if "Relevant source block:" in prompt:
            return json.dumps({
                "summary": (
                    "Stainless steel mounting kit for straightforward "
                    "installation."
                ),
                "details": [
                    {"label": "Material", "value": "Stainless steel"},
                ],
            })
        return "Durable stainless steel product for straightforward installation."

    def metrics_snapshot(self):
        return {"requests": 4, "errors": 0}


def test_public_pipeline_produces_refill_and_review(
    tmp_path,
    monkeypatch,
) -> None:
    fake = DeterministicProvider()
    source = tmp_path / "采集表.json"
    source.write_text(json.dumps({
        "商品id": ["p1"],
        "产品标题": ["Stainless Steel Mounting Kit"],
        "产品描述": ["Steel mounting kit for easy installation."],
        "产品图片链接": [[
            "https://img.example/main.jpg",
            "https://img.example/attachment.jpg",
        ]],
        "变种图片链接": [[]],
    }, ensure_ascii=False), encoding="utf-8")

    output_root = tmp_path / "02_处理结果"
    runtime_root = tmp_path / ".runtime"
    monkeypatch.setattr(pipeline, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(pipeline, "reload_provider", lambda: None)
    monkeypatch.setattr(pipeline, "get_provider", lambda: fake)
    monkeypatch.setattr(translation, "get_provider", lambda: fake)
    monkeypatch.setattr(delivery, "OUTPUT_ROOT", output_root)
    monkeypatch.setattr(delivery, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(delivery, "LATEST_DIR", output_root / "最新")
    monkeypatch.setattr(delivery, "ARCHIVE_DIR", output_root / "归档")

    def fake_download(payload, output_dir, **_kwargs):
        image_dir = Path(output_dir) / "图片"
        image_dir.mkdir(exist_ok=True)
        urls = {
            url
            for values in payload["产品图片链接"]
            for url in values
        }
        return {
            url: {"ok": True, "path": "图片/test.jpg"}
            for url in urls
        }, []

    monkeypatch.setattr(exporter, "download_all_images", fake_download)

    result = pipeline.process_json(source)

    payload = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert payload["商品id"] == ["p1"]
    assert len(payload["产品标题"][0]) <= 75
    assert payload["副标题"] == ["easy installation"]
    assert all(payload[f"Bullet Point{index}"][0] for index in range(1, 6))
    assert len(payload["关键词信息"][0].split(",")) == 10
    assert payload["产品描述"][0] == (
        "Stainless steel mounting kit for straightforward installation.\n\n"
        "Material: Stainless steel"
    )
    assert result.review_path.is_file()
    assert result.review_data_path.is_file()


def test_pipeline_rejects_missing_product_descriptions_before_api(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "采集表.json"
    source.write_text(json.dumps({
        "商品id": ["valid", "empty", "template"],
        "产品标题": [
            "Stainless Steel Mounting Kit",
            "Aluminum Door Lock Pins",
            "Tire Valve Stem Caps",
        ],
        "产品描述": [
            "Material: stainless steel. Easy mounting installation.",
            "",
            (
                "Store CategoriesStore Categories Product Description "
                "About UsAs a seller, Positive Feedback is important. "
                "Please contact us before Negative Feedback."
            ),
        ],
        "产品图片链接": [
            [
                "https://img.example/valid.jpg",
                "https://img.example/valid-attachment.jpg",
            ],
            ["https://img.example/empty.jpg"],
            ["https://img.example/template.jpg"],
        ],
        "变种图片链接": [[], [], []],
    }, ensure_ascii=False), encoding="utf-8")
    seen: dict[str, object] = {}
    provider = DeterministicProvider()
    monkeypatch.setattr(pipeline, "reload_provider", lambda: None)
    monkeypatch.setattr(pipeline, "get_provider", lambda: provider)
    monkeypatch.setattr(pipeline, "RUNTIME_ROOT", tmp_path / ".runtime")

    def image_gate(rows, *_args, runtime_metrics=None, **_kwargs):
        seen["image_ids"] = [row["id"] for row in rows]
        runtime_metrics["quarantined_products"] = [{
            "product_id": "image-risk-only",
        }]
        return rows

    def identity(rows, **_kwargs):
        return rows

    def listing(rows, **_kwargs):
        seen["text_ids"] = [row["id"] for row in rows]
        for row in rows:
            row["subtitle"] = "Stainless steel material, easy installation"
            row["bullets"] = [f"Valid product detail {index}" for index in range(5)]
            row["keywords"] = (
                "stainless steel, mounting kit, steel hardware, "
                "easy installation, replacement mount, durable fitting, "
                "mounting accessory, steel product, repair hardware, "
                "installation kit"
            )
        return rows

    def fake_deliver(context, *, problem_product_ids):
        seen["delivered_ids"] = [row["id"] for row in context.data]
        seen["problem_ids"] = list(problem_product_ids)
        seen["rejected"] = context.runtime_metrics[
            "description_rejected_products"
        ]
        return SimpleNamespace(
            status="delivered",
            output_path=tmp_path / "formal.json",
            review_path=tmp_path / "review.html",
        )

    monkeypatch.setattr(
        pipeline,
        "run_structured_image_safety_gate",
        image_gate,
    )
    monkeypatch.setattr(pipeline, "optimize_titles", identity)
    monkeypatch.setattr(pipeline, "clean_descriptions", identity)
    monkeypatch.setattr(pipeline, "generate_bullets_keywords", listing)
    monkeypatch.setattr(pipeline, "deliver", fake_deliver)

    result = pipeline.process_json(source)

    assert result.status == "delivered"
    assert seen["image_ids"] == ["valid"]
    assert seen["text_ids"] == ["valid"]
    assert seen["delivered_ids"] == ["valid"]
    assert seen["problem_ids"] == [
        "empty",
        "template",
    ]
    assert [
        item["code"] for item in seen["rejected"]
    ] == [
        "missing_source_description",
        "missing_product_description_content",
    ]


def test_pipeline_keeps_product_with_only_main_image(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "采集表.json"
    source.write_text(json.dumps({
        "商品id": ["kept", "no-attachment"],
        "产品标题": ["Kept Product", "Risk Attachment Product"],
        "产品描述": [
            "Kept product made from steel and includes mounting hardware.",
            (
                "Risk attachment product made from aluminum and includes "
                "one replacement part."
            ),
        ],
        "产品图片链接": [
            [
                "https://img.example/kept-main.jpg",
                "https://img.example/kept-attachment.jpg",
            ],
            [
                "https://img.example/risk-main.jpg",
                "https://img.example/risk-attachment.jpg",
            ],
        ],
        "变种图片链接": [[], []],
    }, ensure_ascii=False), encoding="utf-8")
    seen: dict[str, object] = {}
    provider = DeterministicProvider()
    monkeypatch.setattr(pipeline, "reload_provider", lambda: None)
    monkeypatch.setattr(pipeline, "get_provider", lambda: provider)
    monkeypatch.setattr(pipeline, "RUNTIME_ROOT", tmp_path / ".runtime")

    def image_gate(rows, *_args, **_kwargs):
        for row in rows:
            if row["id"] == "no-attachment":
                row["extra_imgs"] = []
        return rows

    def identity(rows, **_kwargs):
        seen["text_ids"] = [row["id"] for row in rows]
        return rows

    def fake_deliver(context, *, problem_product_ids):
        seen["delivered_ids"] = [row["id"] for row in context.data]
        seen["problem_ids"] = list(problem_product_ids)
        seen["attachment_rejected"] = context.runtime_metrics[
            "attachment_rejected_products"
        ]
        return SimpleNamespace(
            status="delivered",
            output_path=tmp_path / "formal.json",
            review_path=tmp_path / "review.html",
        )

    monkeypatch.setattr(
        pipeline,
        "run_structured_image_safety_gate",
        image_gate,
    )
    monkeypatch.setattr(pipeline, "optimize_titles", identity)
    monkeypatch.setattr(pipeline, "clean_descriptions", identity)
    monkeypatch.setattr(pipeline, "generate_bullets_keywords", identity)
    monkeypatch.setattr(
        pipeline,
        "ensure_localized_rows",
        lambda rows, **_kwargs: rows,
    )
    monkeypatch.setattr(pipeline, "deliver", fake_deliver)

    result = pipeline.process_json(source)

    assert result.status == "delivered"
    assert seen["text_ids"] == ["kept", "no-attachment"]
    assert seen["delivered_ids"] == ["kept", "no-attachment"]
    assert seen["problem_ids"] == []
    assert seen["attachment_rejected"] == []
