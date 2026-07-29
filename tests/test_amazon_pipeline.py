from __future__ import annotations

import json
from pathlib import Path

from amazon_processor import delivery, pipeline
from amazon_processor.images.risk import normalize_image_assessment
from amazon_processor.review import exporter, translation


class DeterministicProvider:
    def assess_image(self, _url, *, confirmation=False, retries=3):
        del confirmation, retries
        return normalize_image_assessment({
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
                "description": "适用于日常使用的通用测试产品。",
                "bullets": ["耐用材质", "尺寸实用", "安装简单", "兼容常见用途", "包装完整"],
                "keywords": "替换产品，耐用配件，安装套件",
            }, ensure_ascii=False)
        if "optimize Amazon product titles" in prompt:
            return "Generic Stainless Steel Mounting Kit"
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
        "产品图片链接": [["https://img.example/main.jpg"]],
        "变种图片链接": [[]],
    }, ensure_ascii=False), encoding="utf-8")

    output_root = tmp_path / "输出"
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
    assert all(payload[f"Bullet Point{index}"][0] for index in range(1, 6))
    assert len(payload["关键词信息"][0].split(",")) == 10
    assert result.review_path.is_file()
    assert result.review_data_path.is_file()
