from __future__ import annotations

import json
from pathlib import Path

from amazon_processor.providers import ProviderAuthError, ProviderUnavailableError
from amazon_processor.runtime import RunResult
from amazon_processor.schema import AMAZON_JSON_OUTPUT_FIELDS
from scripts import process_amazon_json as runner


ROOT = Path(__file__).resolve().parents[1]


def output_payload() -> dict:
    payload = {field: [""] for field in AMAZON_JSON_OUTPUT_FIELDS}
    payload.update({
        "商品id": ["product-1"],
        "产品站点": ["US"],
        "产品标题": ["Generic Product"],
        "副标题": ["Durable material, easy installation"],
        "产品描述": ["Product description.\n\nMaterial: ABS"],
        "产品图片链接": [[
            "https://example.com/main.jpg",
            "https://example.com/extra.jpg",
        ]],
        "变种图片链接": [[]],
        "Bullet Point1": ["Feature one"],
        "Bullet Point2": ["Feature two"],
        "Bullet Point3": ["Feature three"],
        "Bullet Point4": ["Feature four"],
        "Bullet Point5": ["Feature five"],
        "关键词信息": ["one, two, three, four, five, six, seven, eight, nine, ten"],
        "有问题的产品id": [],
    })
    return payload


def fake_result(tmp_path: Path) -> RunResult:
    latest = tmp_path / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    output = latest / "跨境电商自动化回填表.json"
    output.write_text(json.dumps(output_payload(), ensure_ascii=False), encoding="utf-8")
    review = latest / "终审包.html"
    review.write_text("ok", encoding="utf-8")
    review_data = latest / "审核数据.json"
    review_data.write_text("{}", encoding="utf-8")
    (latest / "运行状态.json").write_text(
        json.dumps({
            "provider_metrics": {"api_calls": 4, "http_retries": 1},
            "image_safety": {"generation_requests": 0},
        }),
        encoding="utf-8",
    )
    return RunResult(
        output_path=output,
        review_path=review,
        review_data_path=review_data,
        archived_path=None,
        retained_products=1,
        quarantined_products=0,
        elapsed_s=1.0,
        published=True,
    )


def test_skill_metadata_and_installer_exist() -> None:
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    metadata = (ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
    installer = (ROOT / "scripts" / "install_skill.ps1").read_text(encoding="utf-8")
    assert "name: amazon-json-processor" in skill
    assert "allow_implicit_invocation: true" in metadata
    assert "ItemType Junction" in installer


def test_runner_publishes_only_after_contract_validation(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "input.json"
    source.write_text("{}", encoding="utf-8")
    before = runner.sha256_file(source)
    monkeypatch.setattr(runner, "AGENT_RUNS", tmp_path / "agent_runs")
    monkeypatch.setattr(runner, "process_json", lambda *_args, **_kwargs: fake_result(tmp_path))
    code, result_path = runner.run(source, attempts=1, retry_delay_s=0)
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert code == 0
    assert payload["published"] is True
    assert payload["validation"] == {"released_rows": 1, "problem_product_ids": 0}
    assert payload["request_stats"]["api_calls"] == 4
    assert payload["image_stats"]["generation_requests"] == 0
    assert runner.sha256_file(source) == before


def test_runner_retries_transient_failure_once(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "input.json"
    source.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(runner, "AGENT_RUNS", tmp_path / "agent_runs")
    calls = 0

    def process(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderUnavailableError(
                "temporary", provider="deepseek", operation="vision"
            )
        return fake_result(tmp_path)

    monkeypatch.setattr(runner, "process_json", process)
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)
    code, result_path = runner.run(source, attempts=2, retry_delay_s=30)
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert code == 0
    assert calls == 2
    assert payload["attempts"] == 2


def test_runner_does_not_retry_auth_failure(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "input.json"
    source.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(runner, "AGENT_RUNS", tmp_path / "agent_runs")
    calls = 0

    def blocked(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise ProviderAuthError(
            "invalid key", provider="deepseek", operation="vision"
        )

    monkeypatch.setattr(runner, "process_json", blocked)
    code, result_path = runner.run(source, attempts=3, retry_delay_s=0)
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert code == 1
    assert calls == 1
    assert payload["failure"]["type"] == "ProviderAuthError"


def test_verify_output_rejects_problem_overlap(tmp_path: Path) -> None:
    output = tmp_path / "output.json"
    payload = output_payload()
    payload["有问题的产品id"] = ["product-1"]
    output.write_text(json.dumps(payload), encoding="utf-8")
    try:
        runner.verify_output(output)
    except ValueError as exc:
        assert "product-1" in str(exc)
    else:
        raise AssertionError("problem overlap must fail")
