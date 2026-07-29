from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from scripts.pipeline_log import PipelineMetrics
from scripts.pipelines import amazon_delivery
from scripts.pipelines import amazon_stages
from scripts.pipelines import amazon_text
from scripts.pipelines.amazon_runtime import AmazonRunContext


def _context(tmp_path, rows=None) -> AmazonRunContext:
    status = Mock()
    status.update = Mock()
    return AmazonRunContext(
        source_path=str(tmp_path / "input.json"),
        request_id="run-test",
        provider=Mock(),
        status=status,
        metrics=PipelineMetrics(),
        data=list(rows or []),
    )


def test_run_context_transforms_rows_and_records_degraded_stage(
    tmp_path,
) -> None:
    context = _context(tmp_path, [{"id": "p1"}])

    def optimize(rows, progress=None):
        rows[0]["_quality_issues"] = [{
            "code": "title_ai_fallback",
        }]
        progress(1, 1)
        return rows

    result = context.transform(
        "标题优化",
        optimize,
        unused_option=True,
    )

    assert result is context.data
    context.status.stage.assert_called_once_with("标题优化", 0, 1)
    context.status.update.assert_called_once_with(1, 1)
    stage = context.metrics.to_dict()["stages"]["标题优化"]
    assert stage["items"] == 1
    assert stage["success"] == 0


def test_run_context_marks_failed_stage_and_reraises(tmp_path) -> None:
    context = _context(tmp_path, [{"id": "p1"}])
    error = RuntimeError("stage failed")

    def fail(_rows, progress=None):
        del progress
        raise error

    with pytest.raises(RuntimeError, match="stage failed"):
        context.transform("描述清洗", fail)

    context.status.failed.assert_called_once_with("描述清洗", error)


def test_delivery_interface_writes_all_run_artifacts(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "input.json"
    source.write_text("{}", encoding="utf-8")
    output = tmp_path / "output.json"
    context = _context(tmp_path, [{
        "id": "p1",
        "title": "Generic Product",
        "desc": "Useful product description",
        "main_img": "https://img.example/main.jpg",
        "extra_imgs": [],
        "var_imgs": [],
        "bullets": [f"Useful product detail {index}" for index in range(5)],
        "keywords": (
            "product, useful item, replacement part, durable material, "
            "easy installation, daily use, accessory, hardware, kit, universal"
        ),
    }])
    context.source_path = str(source)
    context.runtime_metrics = {
        "quarantined_products": [],
        "image_remediation": {"reviewed": 1},
    }

    def write_output(rows, source_path, progress=None):
        del rows, source_path
        progress(1, 1)
        output.write_text("{}", encoding="utf-8")
        return str(output)

    monkeypatch.setattr(
        amazon_delivery,
        "_stage_write_output",
        write_output,
    )
    monkeypatch.setattr(
        amazon_delivery,
        "_validate_amazon_rows",
        lambda rows, extra_issues=None: {
            "passed": True,
            "issues": list(extra_issues or []),
        },
    )
    monkeypatch.setattr(
        amazon_delivery,
        "_attach_audit_to_validation",
        lambda validation, rows: validation,
    )
    monkeypatch.setattr(
        amazon_delivery,
        "_create_review_package",
        lambda *args, **kwargs: {"status": "created"},
    )

    result = amazon_delivery.deliver_amazon_output(context)

    assert result == str(output)
    quarantine = json.loads(
        (tmp_path / "output_隔离清单.json").read_text(
            encoding="utf-8",
        )
    )
    metrics = json.loads(
        (tmp_path / "output_metrics.json").read_text(
            encoding="utf-8",
        )
    )
    assert quarantine["run_id"] == "run-test"
    assert metrics["review_package"] == {"status": "created"}
    context.status.finish.assert_called_once()


def test_process_amazon_keeps_delivery_compatibility_adapter() -> None:
    from scripts import process_amazon

    assert (
        process_amazon._assert_formal_images_are_safe
        is amazon_delivery._assert_formal_images_are_safe
    )
    assert (
        process_amazon._review_root_for_output
        is amazon_delivery._review_root_for_output
    )


def test_private_runner_name_forwards_to_public_interface(
    monkeypatch,
) -> None:
    from scripts import process_amazon

    monkeypatch.setattr(
        process_amazon,
        "run_amazon_pipeline",
        lambda source_path: f"done:{source_path}",
    )

    assert process_amazon._main_impl("input.json") == "done:input.json"


def test_dirty_description_filter_only_removes_pure_store_template() -> None:
    rows = [
        {
            "id": "bad",
            "desc": "Welcome to my store. Visit our store.",
        },
        {
            "id": "good",
            "desc": (
                "Welcome to my store. Material: stainless steel. "
                "Package includes mounting hardware for installation."
            ),
        },
    ]

    retained, dirty_ids = amazon_text.remove_dirty_descriptions(
        rows
    )

    assert [row["id"] for row in retained] == ["good"]
    assert dirty_ids == ["bad"]
    assert rows[0]["_quality_issues"][0]["code"] == "dirty_description"


def test_amazon_stages_is_a_stable_text_compatibility_adapter() -> None:
    assert (
        amazon_stages._stage_optimize_titles
        is amazon_text.optimize_titles
    )
    assert (
        amazon_stages._stage_clean_descs
        is amazon_text.clean_descriptions
    )
    assert (
        amazon_stages._stage_generate_bullets_keywords
        is amazon_text.generate_bullets_keywords
    )
