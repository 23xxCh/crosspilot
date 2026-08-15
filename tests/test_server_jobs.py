from __future__ import annotations

from pathlib import Path

from amazon_processor import server_jobs


def test_begin_attempt_creates_deterministic_runtime_paths(tmp_path: Path) -> None:
    source = tmp_path / "已接收" / "采集表.json"
    source.parent.mkdir()
    source.write_text("{}", encoding="utf-8")
    file_hash = "a" * 64

    state = server_jobs.begin_attempt(
        source,
        file_hash,
        previous=None,
        logs_root=tmp_path / "logs",
        outcomes_root=tmp_path / "outcomes",
        started_at="2026-08-15T12:00:00+00:00",
    )

    assert state.status == "running"
    assert state.stage == "processing"
    assert state.attempt == 1
    assert state.source_name == source.name
    assert state.source_path == str(source.resolve())
    assert Path(state.log_path) == tmp_path / "logs" / f"{file_hash}_01.log"
    assert Path(state.outcome_path) == (
        tmp_path
        / "outcomes"
        / file_hash
        / "attempt_01"
        / "任务结果.json"
    )


def test_begin_attempt_preserves_intake_metadata_and_increments_attempt(
    tmp_path: Path,
) -> None:
    source = tmp_path / "archived.json"
    source.write_text("{}", encoding="utf-8")
    previous = server_jobs.JobState(
        source_path=str(source),
        source_name="原始采集表.json",
        sha256="b" * 64,
        status="retry_wait",
        submitted_at="2026-08-15T10:00:00+00:00",
        accepted_at="2026-08-15T10:01:00+00:00",
        row_count=25,
        attempt=2,
        progress_current=9,
        progress_total=25,
    )

    state = server_jobs.begin_attempt(
        source,
        previous.sha256,
        previous=previous,
        logs_root=tmp_path / "logs",
        outcomes_root=tmp_path / "outcomes",
        started_at="2026-08-15T12:00:00+00:00",
    )

    assert state.attempt == 3
    assert state.source_name == "原始采集表.json"
    assert state.submitted_at == previous.submitted_at
    assert state.accepted_at == previous.accepted_at
    assert state.row_count == 25
    assert state.progress_current == 0
    assert state.progress_total == 0
    assert state.started_at == "2026-08-15T12:00:00+00:00"
