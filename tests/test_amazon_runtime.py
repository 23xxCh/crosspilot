from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

from amazon_processor import delivery
from amazon_processor.config.models import ModelRegistry
from amazon_processor.config.prompts import PromptRegistry
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
    output_root = tmp_path / "输出"
    runtime_root = tmp_path / ".runtime"
    monkeypatch.setattr(delivery, "OUTPUT_ROOT", output_root)
    monkeypatch.setattr(delivery, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(delivery, "LATEST_DIR", output_root / "最新")
    monkeypatch.setattr(delivery, "ARCHIVE_DIR", output_root / "归档")

    def fake_review(input_path, output_dir, **_kwargs):
        output = Path(output_dir)
        (output / "终审包.html").write_text("review", encoding="utf-8")
        (output / "审核数据.json").write_text("{}", encoding="utf-8")
        (output / "图片").mkdir()
        return {"products": 1}

    monkeypatch.setattr(delivery, "export_review", fake_review)
    context = _context(tmp_path)
    context.data = [{
        "id": "p1",
        "title": "Generic Product",
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
        }],
    }]
    context.runtime_metrics["image_safety_gate"] = {"reviewed": 1}

    result = delivery.deliver(context, additional_problem_ids=[])

    assert result.output_path == output_root / "最新" / delivery.REFILL_NAME
    payload = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert tuple(payload) == (
        "商品id",
        "产品标题",
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


def test_prompt_and_model_edits_change_cache_signatures(tmp_path) -> None:
    prompt_path = tmp_path / "prompts" / "amazon" / "title_optimize.txt"
    prompt_path.parent.mkdir(parents=True)
    prompt_path.write_text("Optimize {title}", encoding="utf-8")
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
