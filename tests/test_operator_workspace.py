from __future__ import annotations

import json
from pathlib import Path

from amazon_processor import operator_workspace


def _state(**overrides):
    value = {
        "source_name": "跨境电商自动化采集表.json",
        "sha256": "a" * 64,
        "status": "published_with_warnings",
        "submitted_at": "2026-08-14T10:20:30+08:00",
        "updated_at": "2026-08-14T10:30:30+08:00",
        "row_count": 300,
        "progress_current": 245,
        "progress_total": 245,
        "queue_position": 0,
        "isolated_product_ids": ["p1", "p2"],
        "blocker_reason": "",
        "operator_delivery_path": "",
    }
    value.update(overrides)
    return value


def test_ensure_workspace_exposes_exactly_three_operator_entries(tmp_path) -> None:
    paths = operator_workspace.ensure_workspace(tmp_path / "Amazon日常操作")

    assert paths.inbox.is_dir()
    assert paths.results.is_dir()
    assert paths.status.is_file()
    assert sorted(path.name for path in paths.root.iterdir()) == [
        "1_把采集表放这里",
        "2_到这里取结果",
        "3_查看处理状态.html",
    ]


def test_publish_success_creates_friendly_package_and_latest_alias(tmp_path) -> None:
    artifact = tmp_path / "formal"
    artifact.mkdir()
    payload = {
        "商品id": ["p1", "p2"],
        "有问题的产品id": ["bad1"],
    }
    (artifact / "跨境电商自动化回填表.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    (artifact / "终审包.html").write_text("<html>review</html>", encoding="utf-8")
    (artifact / "异常商品.json").write_text("[]", encoding="utf-8")
    root = tmp_path / "Amazon日常操作"

    target = operator_workspace.publish_success(
        _state(),
        artifact,
        root=root,
    )
    repeated = operator_workspace.publish_success(
        _state(),
        artifact,
        root=root,
    )

    assert repeated == target
    assert (target / "1_回填表.json").is_file()
    assert (target / "2_人工检查.html").is_file()
    assert (target / "3_处理摘要.txt").is_file()
    assert (target / "4_异常商品.json").is_file()
    assert "输入商品：300 个" in (target / "3_处理摘要.txt").read_text(
        encoding="utf-8"
    )
    assert "成功交付：2 个" in (target / "3_处理摘要.txt").read_text(
        encoding="utf-8"
    )
    latest = root / "2_到这里取结果" / "最新回填表.json"
    assert latest.read_bytes() == (target / "1_回填表.json").read_bytes()


def test_publish_attention_hides_raw_technical_error(tmp_path) -> None:
    root = tmp_path / "Amazon日常操作"
    target = operator_workspace.publish_attention(
        _state(
            status="blocked",
            blocker_reason="ProviderAuthError API_KEY=secret 401",
        ),
        root=root,
    )

    explanation = (target / "处理说明.txt").read_text(encoding="utf-8")
    assert "需要管理员处理" in explanation
    assert "API_KEY" not in explanation
    assert "secret" not in explanation


def test_status_page_is_static_auto_refreshing_and_operator_friendly(tmp_path) -> None:
    states = [
        _state(
            status="running",
            source_name="<测试商品>.json",
            progress_current=30,
            progress_total=100,
            queue_position=1,
        ),
        _state(
            sha256="b" * 64,
            source_name="已完成采集表.json",
            status="published",
            operator_delivery_path=str(
                tmp_path / "Amazon日常操作" / "2_到这里取结果" / "已完成" / "done"
            ),
        ),
    ]
    overview = operator_workspace.summarize_jobs(states)

    html = operator_workspace.render_status_page(overview, healthy=True)

    assert 'http-equiv="refresh" content="10"' in html
    assert "正在处理" in html
    assert "30/100" in html
    assert "下一步" in html
    assert "&lt;测试商品&gt;.json" in html
    assert "打开结果" in html
    assert "Worker" not in html
    assert "Provider" not in html
    assert "API_KEY" not in html
    assert "aaaaaaaa" not in html


def test_status_page_tells_operator_to_wait_during_maintenance() -> None:
    html = operator_workspace.render_status_page(
        operator_workspace.summarize_jobs([_state(status="published")]),
        healthy=False,
    )

    assert "暂时不要投放新的采集表" in html


def test_write_status_page_replaces_previous_page_atomically(tmp_path) -> None:
    paths = operator_workspace.ensure_workspace(tmp_path / "Amazon日常操作")
    operator_workspace.write_status_page(
        operator_workspace.summarize_jobs([_state(status="queued")]),
        healthy=True,
        path=paths.status,
    )

    html = paths.status.read_text(encoding="utf-8")
    assert "等待处理" in html
    assert not list(paths.root.glob("3_查看处理状态.html.*.tmp"))


def test_bootstrap_latest_result_reuses_existing_formal_output(tmp_path) -> None:
    formal = tmp_path / "formal"
    formal.mkdir()
    source = formal / operator_workspace.SOURCE_REFILL_NAME
    source.write_text('{"商品id": ["1"]}', encoding="utf-8")

    target = operator_workspace.bootstrap_latest_result(
        formal,
        root=tmp_path / "Amazon日常操作",
    )

    assert target is not None
    assert target.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
