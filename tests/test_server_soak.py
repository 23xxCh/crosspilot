from __future__ import annotations

import json
from pathlib import Path

from amazon_processor import __main__ as cli
from amazon_processor.server_soak import run_soak


def test_soak_recovers_faults_and_keeps_provider_calls_at_zero(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "soak-report.json"

    report = run_soak(
        cycles=40,
        unique_job_limit=8,
        report_path=report_path,
    )

    assert report["passed"] is True
    assert report["provider_requests"] == 0
    assert report["unique_jobs"] == 8
    assert report["duplicate_submissions"] == 32
    assert report["interrupted_jobs_recovered"] > 0
    assert report["orphan_states_recovered"] == 1
    assert report["corrupt_states_quarantined"] == 1
    assert json.loads(report_path.read_text(encoding="utf-8")) == report


def test_soak_cli_writes_report(tmp_path: Path, capsys) -> None:
    report_path = tmp_path / "cli-report.json"

    exit_code = cli.main(
        [
            "soak",
            "--cycles",
            "10",
            "--report",
            str(report_path),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["passed"] is True
    assert report_path.is_file()
