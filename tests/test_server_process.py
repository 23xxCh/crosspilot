from __future__ import annotations

import json
from pathlib import Path

from amazon_processor import server_process, server_worker


def test_worker_keeps_failure_classifier_compatibility() -> None:
    assert server_worker._classify_failure is server_process.classify_failure
    assert (
        server_worker._classify_outcome_failure
        is server_process.classify_outcome_failure
    )
    assert server_worker._read_outcome is server_process.read_outcome


def test_failure_classification_and_log_tail_are_safe() -> None:
    assert server_process.classify_failure("HTTP 503", 1, 3) == (
        "retry_wait",
        "临时服务异常，等待下一轮断点续跑",
        "transient",
    )
    assert server_process.classify_failure("API key 401", 1, 3)[0] == "blocked"
    assert server_process.classify_failure("JSONDecodeError", 1, 3)[0] == (
        "invalid_input"
    )
    assert "secret-value" not in server_process.error_tail(
        "failed bearer secret-value"
    )


def test_structured_outcome_requires_supported_version(tmp_path: Path) -> None:
    outcome = tmp_path / "outcome.json"
    outcome.write_text(
        json.dumps({"version": 1, "status": "published"}),
        encoding="utf-8",
    )
    assert server_process.read_outcome(outcome) == {
        "version": 1,
        "status": "published",
    }

    outcome.write_text('{"version": 2}', encoding="utf-8")
    assert server_process.read_outcome(outcome) is None
    outcome.write_text("{broken", encoding="utf-8")
    assert server_process.read_outcome(outcome) is None
