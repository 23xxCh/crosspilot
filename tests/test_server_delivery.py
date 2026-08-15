from __future__ import annotations

import json
from pathlib import Path

import pytest

from amazon_processor import server_delivery, server_jobs
from amazon_processor.schema import AMAZON_JSON_OUTPUT_FIELDS


def _valid_output_payload() -> dict[str, list]:
    return {
        "商品id": ["p1"],
        "产品站点": ["US"],
        "产品标题": ["Generic Product"],
        "副标题": ["Useful feature"],
        "产品描述": ["Useful product description"],
        "产品图片链接": [[
            "https://img/main.jpg",
            "https://img/detail.jpg",
        ]],
        "变种图片链接": [[]],
        "Bullet Point1": ["Useful detail one"],
        "Bullet Point2": ["Useful detail two"],
        "Bullet Point3": ["Useful detail three"],
        "Bullet Point4": ["Useful detail four"],
        "Bullet Point5": ["Useful detail five"],
        "关键词信息": [
            "one, two, three, four, five, six, seven, eight, nine, ten"
        ],
        "有问题的产品id": [],
    }


def _state(tmp_path: Path, **overrides: object) -> server_jobs.JobState:
    source = tmp_path / "input.json"
    source.write_text("{}", encoding="utf-8")
    values: dict[str, object] = {
        "source_path": str(source),
        "source_name": source.name,
        "sha256": "a" * 64,
        "status": "published",
        "submitted_at": "2026-08-15T10:00:00+00:00",
    }
    values.update(overrides)
    return server_jobs.JobState(**values)


def test_snapshot_delivery_is_immutable_and_updates_latest(tmp_path: Path) -> None:
    artifact = tmp_path / "formal"
    artifact.mkdir()
    refill_name = "跨境电商自动化回填表.json"
    (artifact / refill_name).write_text('{"商品id": []}', encoding="utf-8")
    log = tmp_path / "run.log"
    log.write_text("done", encoding="utf-8")
    state = _state(tmp_path, log_path=str(log))
    deliveries = tmp_path / "deliveries"

    target = server_delivery.snapshot_delivery(
        state,
        category="成功",
        deliveries_root=deliveries,
        refill_name=refill_name,
        artifact_dir=artifact,
    )

    assert (target / refill_name).read_text(encoding="utf-8") == '{"商品id": []}'
    assert (target / "input.json").is_file()
    assert (target / "run.log").is_file()
    assert (
        deliveries / "跨境电商自动化回填表_最新.json"
    ).read_text(encoding="utf-8") == '{"商品id": []}'


def test_write_delivery_state_uses_atomic_writer(tmp_path: Path) -> None:
    state = _state(tmp_path, delivery_path=str(tmp_path / "delivery"))

    server_delivery.write_delivery_state(state)

    payload = json.loads(
        (tmp_path / "delivery" / "任务状态.json").read_text(encoding="utf-8")
    )
    assert payload["sha256"] == state.sha256
    assert payload["status"] == "published"


def test_repair_operator_deliveries_repairs_success_and_attention(
    tmp_path: Path,
) -> None:
    formal = tmp_path / "latest"
    formal.mkdir()
    refill_name = "跨境电商自动化回填表.json"
    (formal / refill_name).write_text("{}", encoding="utf-8")
    success = _state(tmp_path, output_path=str(formal / refill_name))
    blocked = _state(
        tmp_path,
        sha256="b" * 64,
        status="blocked",
        source_name="blocked.json",
    )
    saved: list[str] = []

    repaired = server_delivery.repair_operator_deliveries(
        [success, blocked],
        formal_latest_root=formal,
        refill_name=refill_name,
        operator_root=tmp_path / "operator",
        save_state=lambda state: saved.append(state.sha256),
    )

    assert repaired == 2
    assert saved == [success.sha256, blocked.sha256]
    assert Path(success.operator_delivery_path).is_dir()
    assert Path(blocked.operator_delivery_path).is_dir()


def test_validate_published_output_returns_validated_rows(tmp_path: Path) -> None:
    output = tmp_path / "跨境电商自动化回填表.json"
    output.write_text(
        json.dumps(_valid_output_payload(), ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp_path / "运行状态.json").write_text(
        json.dumps({"published": True}, ensure_ascii=False),
        encoding="utf-8",
    )

    status = server_delivery.validate_published_output(
        output,
        status_name="运行状态.json",
    )

    assert status["published"] is True
    assert status["validated_rows"] == 1
    assert tuple(_valid_output_payload()) == AMAZON_JSON_OUTPUT_FIELDS


def test_validate_published_output_rejects_blank_subtitle(tmp_path: Path) -> None:
    output = tmp_path / "跨境电商自动化回填表.json"
    payload = _valid_output_payload()
    payload["副标题"] = [""]
    output.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp_path / "运行状态.json").write_text(
        json.dumps({"published": True}, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="副标题.*空值"):
        server_delivery.validate_published_output(
            output,
            status_name="运行状态.json",
        )


def test_pending_review_with_operational_unknown_is_retryable(
    tmp_path: Path,
) -> None:
    review = tmp_path / "终审包.html"
    review.write_text("review", encoding="utf-8")
    (tmp_path / "待定商品.json").write_text(
        json.dumps([{
            "product_id": "p1",
            "images": [{
                "assessment": {"status": "unknown"},
                "text_assessment": {"status": "safe"},
            }],
        }]),
        encoding="utf-8",
    )

    assert server_delivery.pending_review_is_retryable(review) is True


def test_pending_review_row_validation_failure_is_not_retryable(
    tmp_path: Path,
) -> None:
    review = tmp_path / "终审包.html"
    review.write_text("review", encoding="utf-8")
    (tmp_path / "待定商品.json").write_text(
        json.dumps([{
            "product_id": "p1",
            "reasons": [{"code": "formal_row_validation_failed"}],
            "images": [{"assessment": {"status": "unknown"}}],
        }]),
        encoding="utf-8",
    )

    assert server_delivery.pending_review_is_retryable(review) is False
