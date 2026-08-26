from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from amazon_processor import delivery
from amazon_processor.config.models import ModelRegistry
from amazon_processor.config.prompts import PromptRegistry
from amazon_processor.images.risk import (
    normalize_main_text_assessment,
)
from amazon_processor.log import PipelineMetrics
from amazon_processor.runtime import RunContext


def _context(tmp_path: Path) -> RunContext:
    return RunContext(
        source_path=tmp_path / "input.json",
        request_id="run-test",
        provider=Mock(),
        metrics=PipelineMetrics(),
        data=[{"id": "p1"}],
    )


def test_run_context_transforms_rows_and_records_degrade(tmp_path) -> None:
    context = _context(tmp_path)

    def optimize(rows, progress=None):
        rows[0]["_quality_issues"] = [{"code": "title_ai_fallback"}]
        progress(1, 1)
        return rows

    assert context.transform("标题优化", optimize) is context.data
    stage = context.metrics.to_dict()["stages"]["标题优化"]
    assert stage["items"] == 1
    assert stage["success"] == 0


def test_delivery_publishes_one_fixed_artifact_set(
    tmp_path,
    monkeypatch,
) -> None:
    output_root = tmp_path / "02_处理结果"
    runtime_root = tmp_path / ".runtime"
    monkeypatch.setattr(delivery, "OUTPUT_ROOT", output_root)
    monkeypatch.setattr(delivery, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(delivery, "LATEST_DIR", output_root / "最新")
    monkeypatch.setattr(delivery, "ARCHIVE_DIR", output_root / "归档")

    def fake_review(input_path, output_dir, **_kwargs):
        output = Path(output_dir)
        (output / "终审包.html").write_text("review", encoding="utf-8")
        (output / "审核数据.json").write_text(
            json.dumps({
                "summary": {
                    "run_metrics": _kwargs.get("run_metrics") or {},
                },
            }),
            encoding="utf-8",
        )
        (output / "图片").mkdir()
        return {"products": 1}

    monkeypatch.setattr(delivery, "export_review", fake_review)
    context = _context(tmp_path)
    context.data = [{
        "id": "p1",
        "site": "US",
        "title": "Generic Product",
        "subtitle": "Useful material, simple installation",
        "desc": "Useful product description",
        "main_img": "https://img/main.jpg",
        "extra_imgs": [],
        "var_imgs": [],
        "bullets": [f"Useful detail {index}" for index in range(5)],
        "keywords": (
            "product, item, part, material, installation, use, "
            "accessory, hardware, kit, universal"
        ),
            "_image_assessments": [{
                "role": "main",
                "url": "https://img/main.jpg",
                "assessment": {"status": "safe"},
                "text_assessment": normalize_main_text_assessment({
                    "status": "safe",
                    "reasons": [],
                    "placement": "none",
                    "detected_text": [],
                    "confidence": 1.0,
                    "evidence": "No visible text.",
                }),
            }],
    }]
    context.runtime_metrics["image_safety_gate"] = {"reviewed": 1}
    context.runtime_metrics["marketplaces"] = {
        "input_by_site": {"US": 1},
        "completed_by_site": {"US": 1},
    }
    context.runtime_metrics["image_deduplication"] = {
        "references": 1,
        "unique_urls": 1,
    }

    result = delivery.deliver(context, problem_product_ids=[])

    assert result.published is True
    assert result.pending_product_ids == ()
    assert result.output_path == output_root / "最新" / delivery.REFILL_NAME
    payload = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert tuple(payload) == (
        "商品id",
        "产品站点",
        "产品标题",
        "副标题",
        "产品描述",
        "产品图片链接",
        "变种图片链接",
        "Bullet Point1",
        "Bullet Point2",
        "Bullet Point3",
        "Bullet Point4",
        "Bullet Point5",
        "关键词信息",
        "有问题的产品id",
    )
    assert result.review_path.is_file()
    assert result.review_data_path.is_file()
    status = json.loads(
        (result.review_path.parent / delivery.STATUS_NAME).read_text(
            encoding="utf-8",
        )
    )
    assert status["published"] is True
    assert status["status"] == "published"
    assert status["counts"] == {
        "input_rows": 1,
        "processed_rows": 1,
        "released_rows": 1,
        "pending_rows": 0,
        "problem_product_ids": 0,
    }
    review_data = json.loads(result.review_data_path.read_text(encoding="utf-8"))
    run_metrics = review_data.get("summary", {}).get("run_metrics", {})
    assert run_metrics.get("marketplaces", {}).get("completed_by_site") == {
        "US": 1,
    }
    assert run_metrics.get("image_deduplication", {}).get("unique_urls") == 1


def test_delivery_writes_pending_review_without_overwriting_latest(
    tmp_path,
    monkeypatch,
) -> None:
    output_root = tmp_path / "02_处理结果"
    latest = output_root / "最新"
    latest.mkdir(parents=True)
    formal = latest / delivery.REFILL_NAME
    formal.write_text("old-formal", encoding="utf-8")
    monkeypatch.setattr(delivery, "OUTPUT_ROOT", output_root)
    monkeypatch.setattr(delivery, "RUNTIME_ROOT", tmp_path / ".runtime")
    monkeypatch.setattr(delivery, "LATEST_DIR", latest)
    monkeypatch.setattr(delivery, "ARCHIVE_DIR", output_root / "归档")

    def fake_review(input_path, output_dir, **kwargs):
        output = Path(output_dir)
        (output / delivery.REVIEW_NAME).write_text("pending", encoding="utf-8")
        (output / delivery.REVIEW_DATA_NAME).write_text("{}", encoding="utf-8")
        assert [item["product_id"] for item in kwargs["quarantine_products"]] == ["p1"]
        return {"products": 1}

    monkeypatch.setattr(delivery, "export_review", fake_review)
    context = _context(tmp_path)
    context.data = [{
        "id": "p1",
        "site": "US",
        "title": "Generic Product",
        "subtitle": "Useful material, simple installation",
        "desc": "Useful product description",
        "main_img": "https://img/main.jpg",
        "extra_imgs": [],
        "var_imgs": [],
        "bullets": [f"Useful detail {index}" for index in range(5)],
        "keywords": "product, item, part, material, installation, use, accessory, hardware, kit, universal",
        "_main_selection_pending": True,
        "_image_assessments": [{
            "role": "main",
            "url": "https://img/main.jpg",
            "source": "source",
            "assessment": {"status": "risk"},
            "main_eligible": False,
        }],
    }]
    context.runtime_metrics["image_safety_gate"] = {
        "processing_mode": "select_existing",
        "pending_products": 1,
    }
    context.runtime_metrics["pending_main_products"] = [{
        "product_id": "p1",
        "site": "US",
        "title": "Generic Product",
        "reason": "missing_clean_main",
        "images": context.data[0]["_image_assessments"],
    }]

    result = delivery.deliver(context, problem_product_ids=[])

    assert result.published is False
    assert result.output_path is None
    assert result.pending_product_ids == ("p1",)
    assert result.review_path.is_file()
    assert result.review_data_path.is_file()
    assert result.review_path.parent.parent == output_root / "待人工审核"
    assert (result.review_path.parent / "待定商品.json").is_file()
    status = json.loads(
        (result.review_path.parent / delivery.STATUS_NAME).read_text(
            encoding="utf-8",
        )
    )
    assert status["published"] is False
    assert status["status"] == "pending_review"
    assert status["counts"]["released_rows"] == 0
    assert status["counts"]["pending_rows"] == 1
    assert status["pending_product_ids"] == ["p1"]
    assert formal.read_text(encoding="utf-8") == "old-formal"


def test_unattended_delivery_isolates_bad_row_and_publishes_good_rows(
    tmp_path,
    monkeypatch,
) -> None:
    output_root = tmp_path / "02_处理结果"
    monkeypatch.setattr(delivery, "OUTPUT_ROOT", output_root)
    monkeypatch.setattr(delivery, "RUNTIME_ROOT", tmp_path / ".runtime")
    monkeypatch.setattr(delivery, "LATEST_DIR", output_root / "最新")
    monkeypatch.setattr(delivery, "ARCHIVE_DIR", output_root / "归档")
    captured = {}

    def fake_review(input_path, output_dir, **kwargs):
        output = Path(output_dir)
        (output / delivery.REVIEW_NAME).write_text("review", encoding="utf-8")
        (output / delivery.REVIEW_DATA_NAME).write_text("{}", encoding="utf-8")
        (output / "图片").mkdir()
        captured["quarantine"] = kwargs.get("quarantine_products") or []
        return {"products": 2}

    monkeypatch.setattr(delivery, "export_review", fake_review)
    good = {
        "id": "good",
        "site": "US",
        "title": "Generic Stainless Steel Door Hinge Repair Kit",
        "subtitle": "Stainless steel, direct installation, hinge repair",
        "desc": (
            "Stainless steel door hinge repair kit for worn door hinge "
            "mounting points."
        ),
        "main_img": "https://img/good.jpg",
        "extra_imgs": [],
        "var_imgs": [],
        "bullets": [
            "Stainless steel door hinge repair plate for worn mounting points",
            "Door hinge repair kit aligns with existing hinge screw holes",
            "Steel door hinge bracket reinforces damaged mounting locations",
            "Door hinge repair plate installs with common hand tools",
            "Door hinge repair package includes one steel reinforcement plate",
        ],
        "keywords": (
            "door hinge repair, hinge reinforcement plate, steel hinge bracket, "
            "door repair hardware, hinge mounting plate, cabinet hinge repair, "
            "furniture repair plate, hinge screw support, metal hinge kit, "
            "replacement hinge bracket"
        ),
        "_image_assessments": [{
            "role": "main",
            "url": "https://img/good.jpg",
            "assessment": {"status": "safe"},
            "text_assessment": normalize_main_text_assessment({
                "status": "safe",
                "reasons": [],
                "placement": "none",
                "detected_text": [],
                "confidence": 1.0,
                "evidence": "No visible text.",
            }),
        }],
    }
    bad = {
        **good,
        "id": "bad",
        "main_img": "https://img/bad.jpg",
        "subtitle": "",
        "_main_selection_pending": True,
        "_image_assessments": [],
    }
    context = _context(tmp_path)
    context.data = [good, bad]
    context.runtime_metrics["unattended"] = True
    context.runtime_metrics["marketplaces"] = {"input_by_site": {"US": 2}}

    result = delivery.deliver(context, problem_product_ids=[])

    assert result.published is True
    assert result.pending_product_ids == ("bad",)
    assert result.isolated_product_ids == ("bad",)
    assert result.exception_path is not None
    exceptions = json.loads(result.exception_path.read_text(encoding="utf-8"))
    assert [item["product_id"] for item in exceptions["items"]] == ["bad"]
    payload = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert payload["商品id"] == ["good"]
    assert payload["有问题的产品id"] == []
    assert [item["product_id"] for item in captured["quarantine"]] == ["bad"]
    status = json.loads(
        (result.output_path.parent / delivery.STATUS_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert status["status"] == "published_with_warnings"
    assert status["pending_product_ids"] == ["bad"]
    assert status["isolated_product_ids"] == ["bad"]


def test_publish_replaces_files_when_latest_directory_is_open(
    tmp_path,
    monkeypatch,
) -> None:
    output_root = tmp_path / "02_处理结果"
    latest = output_root / "最新"
    runtime_root = tmp_path / ".runtime"
    archive = output_root / "归档"
    latest.mkdir(parents=True)
    (latest / "跨境电商自动化回填表.json").write_text(
        "old",
        encoding="utf-8",
    )
    (latest / delivery.EXCEPTIONS_NAME).write_text(
        "stale",
        encoding="utf-8",
    )
    staging = runtime_root / "staging" / "new"
    staging.mkdir(parents=True)
    (staging / "跨境电商自动化回填表.json").write_text(
        "new",
        encoding="utf-8",
    )
    (staging / "图片").mkdir()
    (staging / "图片" / "one.jpg").write_bytes(b"image")
    monkeypatch.setattr(delivery, "LATEST_DIR", latest)
    monkeypatch.setattr(delivery, "ARCHIVE_DIR", archive)
    monkeypatch.setattr(delivery, "RUNTIME_ROOT", runtime_root)
    original_replace = delivery.os.replace

    def locked_directory_replace(source, target):
        if Path(source) == latest:
            raise PermissionError("latest directory is open")
        return original_replace(source, target)

    monkeypatch.setattr(delivery.os, "replace", locked_directory_replace)

    delivery._publish(staging)

    assert (latest / "跨境电商自动化回填表.json").read_text(
        encoding="utf-8"
    ) == "new"
    assert (latest / "图片" / "one.jpg").read_bytes() == b"image"
    assert not (latest / delivery.EXCEPTIONS_NAME).exists()
    assert not staging.exists()


def test_publish_open_latest_rolls_back_when_one_file_replace_fails(
    tmp_path,
    monkeypatch,
) -> None:
    output_root = tmp_path / "02_处理结果"
    latest = output_root / "最新"
    runtime_root = tmp_path / ".runtime"
    archive = output_root / "归档"
    latest.mkdir(parents=True)
    formal = latest / "跨境电商自动化回填表.json"
    review = latest / delivery.REVIEW_NAME
    formal.write_text("old-formal", encoding="utf-8")
    review.write_text("old-review", encoding="utf-8")
    staging = runtime_root / "staging" / "new"
    staging.mkdir(parents=True)
    (staging / formal.name).write_text("new-formal", encoding="utf-8")
    (staging / review.name).write_text("new-review", encoding="utf-8")
    monkeypatch.setattr(delivery, "LATEST_DIR", latest)
    monkeypatch.setattr(delivery, "ARCHIVE_DIR", archive)
    monkeypatch.setattr(delivery, "RUNTIME_ROOT", runtime_root)
    original_replace = delivery.os.replace
    failed_once = False
    latest_replacements = 0

    def fail_second_target_once(source, target):
        nonlocal failed_once, latest_replacements
        if Path(source) == latest:
            raise PermissionError("latest directory is open")
        if Path(target).parent == latest:
            latest_replacements += 1
            if latest_replacements == 2 and not failed_once:
                failed_once = True
                raise PermissionError("review file is open")
        return original_replace(source, target)

    monkeypatch.setattr(delivery.os, "replace", fail_second_target_once)

    with pytest.raises(PermissionError, match="review file is open"):
        delivery._publish(staging)

    assert formal.read_text(encoding="utf-8") == "old-formal"
    assert review.read_text(encoding="utf-8") == "old-review"


def test_prompt_and_model_edits_change_cache_signatures(tmp_path) -> None:
    prompt_path = tmp_path / "prompts" / "amazon" / "title_optimize.txt"
    prompt_path.parent.mkdir(parents=True)
    prompt_path.write_text("Optimize {title}", encoding="utf-8")
    (prompt_path.parents[1] / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "prompts": [{
                "id": "amazon.title_optimize",
                "label": "Title",
                "category": "Amazon",
                "path": "amazon/title_optimize.txt",
                "variables": ["title"],
                "used_by": "test",
            }],
        }),
        encoding="utf-8",
    )
    prompts = PromptRegistry(prompt_path.parents[1])
    before_prompt = prompts.signature("amazon.title_optimize")
    prompt_path.write_text("Optimize for traffic {title}", encoding="utf-8")
    after_prompt = prompts.signature("amazon.title_optimize")

    settings = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "config"
            / "settings.json"
        ).read_text(encoding="utf-8")
    )
    first_path = tmp_path / "settings-first.json"
    second_path = tmp_path / "settings-second.json"
    first_path.write_text(json.dumps(settings), encoding="utf-8")
    settings["profiles"]["production"]["text"]["model"] = "another-model"
    second_path.write_text(json.dumps(settings), encoding="utf-8")

    assert before_prompt != after_prompt
    assert (
        ModelRegistry.from_file(first_path).signature()
        != ModelRegistry.from_file(second_path).signature()
    )
