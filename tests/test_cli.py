"""Unified CrossPilot command-line interface tests."""
from __future__ import annotations

import json

from crosspilot import cli


def test_parser_exposes_operational_commands() -> None:
    parser = cli.build_parser()

    for command in (
        "run",
        "audit",
        "review",
        "apply",
        "health",
        "config",
        "watch",
        "web",
    ):
        args = parser.parse_args([command, *(
            ["input.json"]
            if command in {"run", "audit"} else
            ["input.json", "output"]
            if command in {"review", "apply"} else
            []
        )])
        assert callable(args.func)


def test_bare_input_uses_run_without_mutating_sys_argv(
    monkeypatch,
) -> None:
    received = {}

    def fake_run(args):
        received["input"] = args.input
        return 7

    monkeypatch.setattr(cli, "cmd_run", fake_run)
    before = list(cli.sys.argv)

    result = cli.main(["delivery.json"])

    assert result == 7
    assert received == {"input": "delivery.json"}
    assert cli.sys.argv == before


def test_audit_command_maps_ids_and_prints_summary(
    monkeypatch,
    capsys,
) -> None:
    from scripts import audit_amazon_image_safety

    received = {}

    def fake_audit(input_path, **kwargs):
        received["input"] = input_path
        received.update(kwargs)
        return {
            "summary": {"products": 2},
            "report_path": "report.json",
            "package_summary": {"products": 2},
        }

    monkeypatch.setattr(
        audit_amazon_image_safety,
        "audit_file",
        fake_audit,
    )

    result = cli.main([
        "audit",
        "delivery.json",
        "--ids",
        "p2,p1",
        "--output-root",
        "review-root",
    ])

    assert result == 0
    assert received == {
        "input": "delivery.json",
        "output_root": "review-root",
        "cache_path": None,
        "product_ids": {"p1", "p2"},
        "create_package": True,
    }
    printed = json.loads(capsys.readouterr().out)
    assert printed["summary"]["products"] == 2


def test_review_command_uses_structured_audit_mapping(
    tmp_path,
    monkeypatch,
) -> None:
    from scripts import review_package

    audit_path = tmp_path / "audit.json"
    audit_path.write_text(json.dumps({
        "audit_by_product": {
            "p1": [{"role": "main", "url": "https://img/main.jpg"}],
        },
    }), encoding="utf-8")
    quarantine_path = tmp_path / "quarantine.json"
    quarantine_path.write_text(json.dumps({
        "products": [{"product_id": "p2"}],
    }), encoding="utf-8")
    received = {}

    def fake_export(input_path, output_dir, **kwargs):
        received["input"] = input_path
        received["output"] = output_dir
        received.update(kwargs)
        return {
            "translation_failures": [],
            "image_failures": [],
        }

    monkeypatch.setattr(
        review_package,
        "export_review",
        fake_export,
    )

    result = cli.main([
        "review",
        "delivery.json",
        "review-dir",
        "--audit-data",
        str(audit_path),
        "--quarantine-manifest",
        str(quarantine_path),
        "--run-id",
        "manual-review",
    ])

    assert result == 0
    assert received["audit_by_product"]["p1"][0]["role"] == "main"
    assert received["quarantine_products"] == [{"product_id": "p2"}]
    assert received["run_id"] == "manual-review"


def test_apply_command_delegates_dry_run(
    monkeypatch,
    capsys,
) -> None:
    from scripts import apply_amazon_review_decisions

    received = {}

    def fake_apply(formal, decisions, **kwargs):
        received.update({
            "formal": formal,
            "decisions": decisions,
            **kwargs,
        })
        return {"status": "dry_run"}

    monkeypatch.setattr(
        apply_amazon_review_decisions,
        "apply_decisions",
        fake_apply,
    )

    result = cli.main([
        "apply",
        "formal.json",
        "decisions.json",
        "--review-package",
        "review-dir",
        "--dry-run",
    ])

    assert result == 0
    assert received == {
        "formal": "formal.json",
        "decisions": "decisions.json",
        "review_package": "review-dir",
        "dry_run": True,
    }
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"


def test_legacy_script_main_is_thin_cli_adapter(monkeypatch) -> None:
    from scripts import audit_amazon_image_safety

    received = {}

    def fake_main(arguments):
        received["arguments"] = arguments
        return 3

    monkeypatch.setattr(cli, "main", fake_main)

    result = audit_amazon_image_safety.main([
        "delivery.json",
        "--no-package",
    ])

    assert result == 3
    assert received["arguments"] == [
        "audit",
        "delivery.json",
        "--no-package",
    ]
