from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from amazon_processor import server_health, server_jobs
from amazon_processor.config import credentials, models


def test_heartbeat_distinguishes_liveness_from_readiness(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    updated = "2026-08-15T12:00:00+00:00"

    server_health.write_health(
        runtime,
        "needs_attention",
        pid=123,
        updated_at=updated,
        blocker_reason="missing credential",
    )
    health = server_health.worker_health(
        runtime,
        max_age_seconds=120,
        now=datetime(2026, 8, 15, 12, 1, tzinfo=timezone.utc),
    )

    assert health["pid"] == 123
    assert health["healthy"] is True
    assert health["ready"] is False
    assert health["age_seconds"] == 60


def test_worker_health_reports_missing_and_invalid_heartbeat(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    assert server_health.worker_health(runtime)["status"] == "missing"
    runtime.mkdir()
    (runtime / "heartbeat.json").write_text("not-json", encoding="utf-8")

    health = server_health.worker_health(runtime)

    assert health["healthy"] is False
    assert health["status"] == "invalid"


def test_preflight_creates_runtime_directories_and_reports_missing_routes(
    tmp_path,
    monkeypatch,
) -> None:
    class Target:
        def __init__(self, provider: str, credential: str) -> None:
            self.provider = provider
            self.credential = credential

    class Registry:
        def routes(self, operation: str):
            return (Target("agnes", f"{operation}_credential"),)

        def credential(self, credential_id: str):
            return SimpleNamespace(
                credential_id=credential_id,
                label=credential_id,
                env=credential_id.upper(),
            )

    monkeypatch.setattr(models, "get_model_registry", lambda: Registry())
    monkeypatch.setattr(
        credentials.CredentialStore,
        "value",
        lambda _self, _credential_id: "",
    )
    monkeypatch.setattr(
        server_health.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=50 * 1024**3),
    )
    runtime = tmp_path / "runtime"
    directories = (
        tmp_path / "input",
        runtime,
        runtime / "jobs",
        runtime / "logs",
        tmp_path / "deliveries",
    )

    report = server_health.preflight(
        directories[0],
        project_root=tmp_path,
        runtime_root=runtime,
        jobs_root=directories[2],
        logs_root=directories[3],
        deliveries_root=directories[4],
        environ={},
    )

    assert report["missing_operations"] == ["text", "vision"]
    assert all(path.is_dir() for path in directories)
    assert not list(runtime.glob(".write_probe_*"))


def test_preflight_rejects_low_disk_after_cleaning_probe(
    tmp_path,
    monkeypatch,
) -> None:
    class Registry:
        def routes(self, _operation: str):
            return ()

        def credential(self, credential_id: str):
            raise KeyError(credential_id)

    monkeypatch.setattr(models, "get_model_registry", lambda: Registry())
    monkeypatch.setattr(
        server_health.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=0),
    )
    runtime = tmp_path / "runtime"

    with pytest.raises(OSError, match="磁盘剩余空间不足"):
        server_health.preflight(
            tmp_path / "input",
            project_root=tmp_path,
            runtime_root=runtime,
            jobs_root=runtime / "jobs",
            logs_root=runtime / "logs",
            deliveries_root=tmp_path / "deliveries",
            environ={},
        )

    assert not list(runtime.glob(".write_probe_*"))


def test_progress_from_log_uses_latest_complete_marker(tmp_path: Path) -> None:
    log = tmp_path / "run.log"
    log.write_text(
        "[images] 3 / 10\nnoise\n[text] 7/10\n",
        encoding="utf-8",
    )

    assert server_health.progress_from_log(log) == ("text", 7, 10)
    assert server_health.progress_from_log(tmp_path / "missing.log") == (
        "processing",
        0,
        0,
    )


def test_running_job_heartbeat_saves_only_changed_progress(
    tmp_path: Path,
) -> None:
    log = tmp_path / "run.log"
    log.write_text("[images] 2/5\n", encoding="utf-8")
    state = server_jobs.JobState(
        source_path=str(tmp_path / "input.json"),
        source_name="input.json",
        sha256="a" * 64,
        status="running",
        attempt=2,
        log_path=str(log),
    )
    saved: list[tuple[str, int, int]] = []
    refreshes: list[bool] = []
    health_calls: list[tuple[str, dict[str, object]]] = []
    heartbeat = server_health.RunningJobHeartbeat(
        state=state,
        log_path=log,
        save_state=lambda value: saved.append((
            value.stage,
            value.progress_current,
            value.progress_total,
        )),
        refresh_status=lambda: refreshes.append(True),
        write_health=lambda status, **details: health_calls.append(
            (status, details)
        ),
        telemetry=lambda: {"queue_depth": 3, "free_disk_gb": 42.5},
    )

    heartbeat()
    heartbeat()

    assert saved == [("images", 2, 5)]
    assert refreshes == [True]
    assert len(health_calls) == 2
    status, details = health_calls[-1]
    assert status == "running"
    assert details["current_job"] == state.sha256
    assert details["source_path"] == state.source_path
    assert details["attempt"] == 2
    assert details["stage"] == "images"
    assert details["progress_current"] == 2
    assert details["progress_total"] == 5
    assert details["queue_depth"] == 3
    assert details["free_disk_gb"] == 42.5
