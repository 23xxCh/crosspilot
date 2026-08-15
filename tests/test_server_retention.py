from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path

from amazon_processor import server_jobs, server_retention, server_worker


def _paths(tmp_path: Path) -> server_retention.RetentionPaths:
    paths = server_retention.RetentionPaths(
        project_root=tmp_path,
        accepted_root=tmp_path / "accepted",
        deliveries_root=tmp_path / "deliveries",
        jobs_root=tmp_path / "jobs",
        outcomes_root=tmp_path / "outcomes",
        logs_root=tmp_path / "logs",
        cache_root=tmp_path / "cache",
        report_path=tmp_path / "retention.json",
    )
    for root in (
        paths.accepted_root,
        paths.deliveries_root,
        paths.jobs_root,
        paths.outcomes_root,
        paths.logs_root,
        paths.cache_root,
    ):
        root.mkdir(parents=True)
    return paths


def test_worker_keeps_retention_compatibility() -> None:
    assert server_worker._remove_old_entries is server_retention.remove_old_entries


def test_retention_protects_active_artifacts_and_skips_cache_when_busy(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    active_source = paths.accepted_root / "active.json"
    active_outcome = paths.outcomes_root / "active.json"
    old_cache = paths.cache_root / "old.bin"
    for path in (active_source, active_outcome, old_cache):
        path.write_text("old", encoding="utf-8")
        old = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        os.utime(path, (old, old))
    state = server_jobs.JobState(
        source_path=str(active_source),
        sha256="a" * 64,
        status="running",
        outcome_path=str(active_outcome),
    )
    state_path = paths.jobs_root / f"{state.sha256}.json"
    state_path.write_text("{}", encoding="utf-8")
    old = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(state_path, (old, old))

    report = server_retention.run_retention(
        paths=paths,
        states=[state],
        active_statuses={"running"},
        processor_active=lambda: True,
        now=datetime(2026, 8, 15, tzinfo=timezone.utc),
        disk_free=lambda: 100.0,
    )

    assert active_source.is_file()
    assert active_outcome.is_file()
    assert state_path.is_file()
    assert old_cache.is_file()
    assert report["cache_removed"] == 0
