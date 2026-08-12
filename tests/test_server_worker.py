from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from amazon_processor import server_worker


_REAL_SNAPSHOT_DELIVERY = server_worker.snapshot_delivery


@pytest.fixture(autouse=True)
def _isolate_worker_runtime(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server_worker, "RUNTIME_ROOT", tmp_path / "server")
    monkeypatch.setattr(server_worker, "ACCEPTED_ROOT", tmp_path / "accepted")
    monkeypatch.setattr(server_worker, "OUTCOMES_ROOT", tmp_path / "outcomes")
    monkeypatch.setattr(
        server_worker,
        "RETENTION_STATE_PATH",
        tmp_path / "server" / "retention.json",
    )
    monkeypatch.setattr(
        server_worker,
        "DELIVERIES_ROOT",
        tmp_path / "deliveries",
    )

    def fake_snapshot(_state, *, category, artifact_dir=None):
        target = tmp_path / "deliveries" / category / "job"
        target.mkdir(parents=True, exist_ok=True)
        return target

    monkeypatch.setattr(server_worker, "snapshot_delivery", fake_snapshot)


def test_iter_input_files_ignores_outputs_and_temp_files(tmp_path) -> None:
    (tmp_path / "跨境电商自动化采集表.json").write_text("{}", encoding="utf-8")
    (tmp_path / "跨境电商自动化回填表.json").write_text("{}", encoding="utf-8")
    (tmp_path / "审核决定.json").write_text("{}", encoding="utf-8")
    (tmp_path / "~$partial.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("", encoding="utf-8")

    assert [
        item.name for item in server_worker.iter_input_files(tmp_path)
    ] == ["跨境电商自动化采集表.json"]


def test_iter_input_files_uses_received_time_not_filename(tmp_path) -> None:
    first = tmp_path / "z-first.json"
    second = tmp_path / "a-second.json"
    first.write_text("{}", encoding="utf-8")
    second.write_text("{}", encoding="utf-8")
    server_worker.os.utime(first, (100, 100))
    server_worker.os.utime(second, (200, 200))

    assert [path.name for path in server_worker.iter_input_files(tmp_path)] == [
        "z-first.json",
        "a-second.json",
    ]


def test_process_one_deduplicates_published_hash(tmp_path, monkeypatch) -> None:
    source = tmp_path / "input.json"
    source.write_text('{"商品id": []}', encoding="utf-8")
    jobs = tmp_path / "jobs"
    logs = tmp_path / "logs"
    monkeypatch.setattr(server_worker, "JOBS_ROOT", jobs)
    monkeypatch.setattr(server_worker, "LOGS_ROOT", logs)
    monkeypatch.setattr(server_worker, "is_file_stable", lambda *_args: True)
    calls = []

    def fake_run(_source, _log_path, **_kwargs):
        calls.append(1)
        return 0, "正式表已更新: E:/output.json"

    monkeypatch.setattr(server_worker, "run_child", fake_run)
    monkeypatch.setattr(
        server_worker,
        "validate_published_output",
        lambda _path: {"published": True, "pending_product_ids": []},
    )

    first = server_worker.process_one(source, stable_seconds=0)
    second = server_worker.process_one(source, stable_seconds=0)

    assert first is not None and first.status == "published"
    assert second is not None and second.status == "published"
    assert len(calls) == 1
    saved = json.loads(next(jobs.glob("*.json")).read_text(encoding="utf-8"))
    assert saved["sha256"] == server_worker.sha256_file(source)
    assert saved["output_path"] == "E:/output.json"
    assert saved["delivery_path"]


def test_process_one_blocks_auth_errors_without_retry(tmp_path, monkeypatch) -> None:
    source = tmp_path / "input.json"
    source.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(server_worker, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(server_worker, "LOGS_ROOT", tmp_path / "logs")
    monkeypatch.setattr(server_worker, "is_file_stable", lambda *_args: True)
    calls = []

    def fake_run(_source, _log_path, **_kwargs):
        calls.append(1)
        return 1, "ProviderAuthError: API 鉴权失败 (401)"

    monkeypatch.setattr(server_worker, "run_child", fake_run)
    first = server_worker.process_one(source, stable_seconds=0)
    second = server_worker.process_one(source, stable_seconds=0)

    assert first is not None and first.status == "blocked"
    assert second is not None and second.status == "blocked"
    assert len(calls) == 1


def test_process_one_retries_transient_failure_with_backoff_zero(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "input.json"
    source.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(server_worker, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(server_worker, "LOGS_ROOT", tmp_path / "logs")
    monkeypatch.setattr(server_worker, "is_file_stable", lambda *_args: True)
    outcomes = iter([
        (1, "ProviderUnavailableError: 503"),
        (0, "待人工审核包: E:/review"),
    ])
    monkeypatch.setattr(
        server_worker,
        "run_child",
        lambda *_args, **_kwargs: next(outcomes),
    )

    first = server_worker.process_one(
        source,
        stable_seconds=0,
        retry_base_seconds=0,
        max_retries=2,
    )
    second = server_worker.process_one(
        source,
        stable_seconds=0,
        retry_base_seconds=0,
        max_retries=2,
    )

    assert first is not None and first.status == "retry_wait"
    assert second is not None and second.status == "pending_review"
    assert second.attempt == 2


def test_process_one_does_not_retry_after_final_failure(tmp_path, monkeypatch) -> None:
    source = tmp_path / "input.json"
    source.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(server_worker, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(server_worker, "LOGS_ROOT", tmp_path / "logs")
    monkeypatch.setattr(server_worker, "is_file_stable", lambda *_args: True)
    calls = []

    def fake_run(_source, _log_path, **_kwargs):
        calls.append(1)
        return 1, "ProviderResponseError: invalid response"

    monkeypatch.setattr(server_worker, "run_child", fake_run)
    first = server_worker.process_one(
        source,
        stable_seconds=0,
        max_retries=1,
    )
    second = server_worker.process_one(
        source,
        stable_seconds=0,
        max_retries=1,
    )

    assert first is not None and first.status == "failed"
    assert second is not None and second.status == "failed"
    assert len(calls) == 1


def test_process_one_does_not_accept_exit_zero_without_result_marker(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "input.json"
    source.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(server_worker, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(server_worker, "LOGS_ROOT", tmp_path / "logs")
    monkeypatch.setattr(server_worker, "is_file_stable", lambda *_args: True)
    monkeypatch.setattr(
        server_worker,
        "run_child",
        lambda *_args, **_kwargs: (0, "处理结束但没有发布结果"),
    )

    state = server_worker.process_one(
        source,
        stable_seconds=0,
        max_retries=1,
    )

    assert state is not None
    assert state.status == "failed"
    assert state.output_path == ""


def test_transient_failures_can_retry_without_fixed_limit(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "input.json"
    source.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(server_worker, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(server_worker, "LOGS_ROOT", tmp_path / "logs")
    monkeypatch.setattr(server_worker, "is_file_stable", lambda *_args: True)
    monkeypatch.setattr(
        server_worker,
        "run_child",
        lambda *_args, **_kwargs: (1, "ProviderUnavailableError: HTTP 503"),
    )

    state = None
    for _ in range(5):
        state = server_worker.process_one(
            source,
            stable_seconds=0,
            max_retries=0,
            retry_base_seconds=0,
        )

    assert state is not None
    assert state.status == "retry_wait"
    assert state.attempt == 5
    assert state.failure_kind == "transient"


def test_validate_published_output_rejects_blank_required_fields(tmp_path) -> None:
    output = tmp_path / "跨境电商自动化回填表.json"
    payload = {
        "商品id": ["p1"],
        "产品站点": ["US"],
        "产品标题": ["Generic Product"],
        "副标题": [""],
        "产品描述": ["Useful product description"],
        "产品图片链接": [["https://img/main.jpg"]],
        "变种图片链接": [[]],
        "Bullet Point1": ["Useful detail one"],
        "Bullet Point2": ["Useful detail two"],
        "Bullet Point3": ["Useful detail three"],
        "Bullet Point4": ["Useful detail four"],
        "Bullet Point5": ["Useful detail five"],
        "关键词信息": ["one, two, three, four, five, six, seven, eight, nine, ten"],
        "有问题的产品id": [],
    }
    output.write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "运行状态.json").write_text(
        json.dumps({"published": True}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="副标题.*空值"):
        server_worker.validate_published_output(output)


def test_worker_health_reports_current_heartbeat(tmp_path) -> None:
    server_worker._write_health("idle", input_dir=str(tmp_path))

    health = server_worker.worker_health(max_age_seconds=120)

    assert health["healthy"] is True
    assert health["status"] == "idle"
    assert health["age_seconds"] <= 2


def test_worker_health_distinguishes_alive_from_ready(tmp_path) -> None:
    server_worker._write_health(
        "needs_attention",
        blocker_reason="缺少模型凭据",
    )

    health = server_worker.worker_health(max_age_seconds=120)

    assert health["healthy"] is True
    assert health["ready"] is False
    assert health["blocker_reason"] == "缺少模型凭据"


def test_snapshot_delivery_copies_only_selected_artifact_directory(
    tmp_path,
    monkeypatch,
) -> None:
    delivery_root = tmp_path / "deliveries"
    monkeypatch.setattr(server_worker, "DELIVERIES_ROOT", delivery_root)
    artifact = tmp_path / "latest"
    artifact.mkdir()
    (artifact / "跨境电商自动化回填表.json").write_text(
        "{}",
        encoding="utf-8",
    )
    source = tmp_path / "input.json"
    source.write_text("{}", encoding="utf-8")
    log = tmp_path / "job.log"
    log.write_text("done", encoding="utf-8")
    state = server_worker.JobState(
        source_path=str(source),
        sha256="a" * 64,
        status="published",
        log_path=str(log),
    )

    target = _REAL_SNAPSHOT_DELIVERY(
        state,
        category="成功",
        artifact_dir=artifact,
    )

    assert (target / "跨境电商自动化回填表.json").is_file()
    assert (target / "input.json").is_file()
    assert (target / "job.log").is_file()


def test_preflight_reports_missing_credentials_without_exit_loop(
    tmp_path,
    monkeypatch,
) -> None:
    from amazon_processor.config import credentials, env

    class Target:
        def __init__(self, provider, credential):
            self.provider = provider
            self.credential = credential

    class Registry:
        def routes(self, operation):
            return (Target("deepseek", f"{operation}_credential"),)

        def credential(self, credential_id):
            return type("Definition", (), {
                "credential_id": credential_id,
                "label": credential_id,
                "env": credential_id.upper(),
            })()

    monkeypatch.setattr(server_worker, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(server_worker, "LOGS_ROOT", tmp_path / "logs")
    monkeypatch.setattr(
        env,
        "load_config",
        lambda: {"IMAGE_PROCESSING_MODE": "select_existing"},
    )
    monkeypatch.setattr(
        "amazon_processor.config.models.get_model_registry",
        lambda: Registry(),
    )
    monkeypatch.setattr(
        credentials.CredentialStore,
        "value",
        lambda _self, _credential_id: "",
    )

    readiness = server_worker.preflight(tmp_path / "input")

    assert readiness["missing_operations"] == ["text", "vision"]
    assert "text, vision" in readiness["blocker_reason"]


def test_stalled_child_is_classified_as_transient_unlimited_retry() -> None:
    status, _message, kind = server_worker._classify_failure(
        "子进程长时间无日志进展，已终止并准备续跑",
        attempt=20,
        max_retries=0,
    )

    assert status == "retry_wait"
    assert kind == "transient"


def test_pending_review_with_unknown_images_is_retryable(tmp_path) -> None:
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

    assert server_worker._pending_review_is_retryable(review) is True


def test_stability_tracker_scans_one_thousand_files_without_sleep(
    tmp_path,
    monkeypatch,
) -> None:
    tracker = server_worker.StabilityTracker()
    files = []
    for index in range(1000):
        path = tmp_path / f"item_{index:04d}.json"
        path.write_text("{}", encoding="utf-8")
        files.append(path)
    monkeypatch.setattr(
        server_worker.time,
        "sleep",
        lambda _seconds: pytest.fail("非阻塞扫描不应逐文件 sleep"),
    )

    assert not any(
        tracker.ready(path, stable_seconds=5, now_monotonic=100)
        for path in files
    )
    assert all(
        tracker.ready(path, stable_seconds=5, now_monotonic=106)
        for path in files
    )


def test_accept_input_moves_to_month_archive_and_deduplicates(
    tmp_path,
    monkeypatch,
) -> None:
    inbox = tmp_path / "待处理"
    accepted = tmp_path / "已接收"
    inbox.mkdir()
    jobs = tmp_path / "jobs"
    monkeypatch.setattr(server_worker, "JOBS_ROOT", jobs)
    source = inbox / "采集表.json"
    source.write_text('{"商品id": []}', encoding="utf-8")
    expected_hash = server_worker.sha256_file(source)

    state = server_worker.accept_input(source, accepted_root=accepted)

    archived = Path(state.source_path)
    assert not source.exists()
    assert archived.is_file()
    assert archived.parent.parent == accepted
    assert server_worker.sha256_file(archived) == expected_hash
    duplicate = inbox / "同内容.json"
    duplicate.write_bytes(archived.read_bytes())
    same = server_worker.accept_input(duplicate, accepted_root=accepted)
    assert same.sha256 == state.sha256
    assert not duplicate.exists()
    assert len(list(accepted.rglob("*.json"))) == 1


def test_reconcile_running_job_resumes_immediately_after_process_loss(
    tmp_path,
    monkeypatch,
) -> None:
    jobs = tmp_path / "jobs"
    accepted = tmp_path / "accepted"
    jobs.mkdir()
    accepted.mkdir()
    monkeypatch.setattr(server_worker, "JOBS_ROOT", jobs)
    monkeypatch.setattr(server_worker, "ACCEPTED_ROOT", accepted)
    source = accepted / ("input_" + "a" * 12 + ".json")
    source.write_text("{}", encoding="utf-8")
    file_hash = server_worker.sha256_file(source)
    renamed = accepted / f"input_{file_hash[:12]}.json"
    source.rename(renamed)
    state = server_worker.JobState(
        source_path=str(renamed),
        source_name="input.json",
        sha256=file_hash,
        status="running",
        started_at="2026-08-11T00:00:00+00:00",
    )
    server_worker._save_state(state)
    monkeypatch.setattr(server_worker, "processor_is_running", lambda: False)
    now = datetime(2026, 8, 11, 1, tzinfo=timezone.utc)

    repaired = server_worker.reconcile_jobs(now=now)

    assert repaired[0].status == "retry_wait"
    assert repaired[0].stage == "recovering"
    assert repaired[0].next_retry_at == now.isoformat(timespec="seconds")


def test_reconcile_recovers_archived_file_without_state(tmp_path, monkeypatch) -> None:
    jobs = tmp_path / "jobs"
    accepted = tmp_path / "accepted" / "2026-08"
    accepted.mkdir(parents=True)
    monkeypatch.setattr(server_worker, "JOBS_ROOT", jobs)
    source = accepted / "orphan.json"
    source.write_text("{}", encoding="utf-8")

    repaired = server_worker.reconcile_jobs(accepted_root=accepted.parent)

    assert len(repaired) == 1
    assert repaired[0].status == "queued"
    assert repaired[0].stage == "recovering"
    assert Path(repaired[0].source_path) == source.resolve()


def test_structured_child_outcome_is_preferred_over_console_text(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "input.json"
    source.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(server_worker, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(server_worker, "LOGS_ROOT", tmp_path / "logs")
    monkeypatch.setattr(server_worker, "is_file_stable", lambda *_args: True)

    def fake_run(_source, _log_path, *, outcome_path, **_kwargs):
        server_worker._atomic_json(outcome_path, {
            "version": 1,
            "status": "published_with_warnings",
            "output_path": "E:/formal.json",
            "review_path": "E:/review.html",
            "exception_path": "E:/exceptions.json",
            "isolated_product_ids": ["bad-row"],
            "pending_product_ids": ["bad-row"],
        })
        return 0, "这段控制台文字不包含旧结果标记"

    monkeypatch.setattr(server_worker, "run_child", fake_run)
    monkeypatch.setattr(
        server_worker,
        "validate_published_output",
        lambda _path: {
            "published": True,
            "pending_product_ids": ["bad-row"],
            "isolated_product_ids": ["bad-row"],
        },
    )

    state = server_worker.process_one(source, stable_seconds=0)

    assert state is not None
    assert state.status == "published_with_warnings"
    assert state.isolated_product_ids == ["bad-row"]
    assert state.exception_path == "E:/exceptions.json"


def test_retry_schedule_matches_agreed_steps_without_jitter() -> None:
    assert [
        server_worker.retry_delay_seconds(attempt, jitter=False)
        for attempt in range(1, 6)
    ] == [30, 120, 300, 600, 600]


def test_retention_removes_old_logs_but_protects_active_input(
    tmp_path,
    monkeypatch,
) -> None:
    accepted = tmp_path / "accepted"
    logs = tmp_path / "logs"
    deliveries = tmp_path / "deliveries"
    jobs = tmp_path / "jobs"
    cache = tmp_path / "cache"
    for path in (accepted, logs, deliveries, jobs, cache):
        path.mkdir()
    monkeypatch.setattr(server_worker, "JOBS_ROOT", jobs)
    monkeypatch.setattr(server_worker, "LOGS_ROOT", logs)
    monkeypatch.setattr(
        server_worker,
        "RETENTION_STATE_PATH",
        tmp_path / "retention.json",
    )
    active = accepted / "active.json"
    old_log = logs / "old.log"
    active.write_text("{}", encoding="utf-8")
    old_log.write_text("old", encoding="utf-8")
    old_timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    for path in (active, old_log):
        server_worker.os.utime(path, (old_timestamp, old_timestamp))
    state = server_worker.JobState(
        source_path=str(active),
        sha256="f" * 64,
        status="queued",
    )
    server_worker._save_state(state)

    report = server_worker.run_retention(
        now=datetime(2026, 8, 11, tzinfo=timezone.utc),
        disk_free=lambda: 100.0,
        accepted_root=accepted,
        deliveries_root=deliveries,
        cache_root=cache,
    )

    assert active.is_file()
    assert not old_log.exists()
    assert report["logs_removed"] == 1


def test_corrupt_state_is_quarantined_instead_of_crashing(tmp_path, monkeypatch) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setattr(server_worker, "JOBS_ROOT", jobs)
    broken = jobs / ("a" * 64 + ".json")
    broken.write_text("{", encoding="utf-8")

    assert server_worker._load_state("a" * 64) is None
    assert not broken.exists()
    assert len(list(jobs.glob("*.corrupt_*"))) == 1


def test_low_disk_keeps_new_inbox_file_unaccepted(tmp_path, monkeypatch) -> None:
    inbox = tmp_path / "待处理"
    inbox.mkdir()
    source = inbox / "new.json"
    source.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(server_worker, "WORKER_LOCK", tmp_path / "worker.lock")
    monkeypatch.setattr(server_worker, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(server_worker, "LOGS_ROOT", tmp_path / "logs")
    monkeypatch.setattr(server_worker, "_disk_free_gb", lambda *_args: 9.0)
    monkeypatch.setattr(server_worker, "_maintenance_enabled", lambda: False)
    monkeypatch.setattr(server_worker, "reconcile_jobs", lambda **_kwargs: [])
    monkeypatch.setattr(server_worker, "run_retention", lambda **_kwargs: {})
    monkeypatch.setattr(
        server_worker,
        "preflight",
        lambda _path: {
            "input_dir": str(inbox),
            "image_processing_mode": "select_existing",
            "free_disk_gb": 9.0,
            "missing_operations": [],
            "blocker_reason": "",
        },
    )

    code = server_worker.run_worker(
        input_dir=inbox,
        stable_seconds=0,
        once=True,
    )

    assert code == 0
    assert source.is_file()
    assert not list((tmp_path / "jobs").glob("*.json"))
