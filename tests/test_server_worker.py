from __future__ import annotations

import json

from amazon_processor import server_worker


def test_iter_input_files_ignores_outputs_and_temp_files(tmp_path) -> None:
    (tmp_path / "跨境电商自动化采集表.json").write_text("{}", encoding="utf-8")
    (tmp_path / "跨境电商自动化回填表.json").write_text("{}", encoding="utf-8")
    (tmp_path / "审核决定.json").write_text("{}", encoding="utf-8")
    (tmp_path / "~$partial.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("", encoding="utf-8")

    assert [
        item.name for item in server_worker.iter_input_files(tmp_path)
    ] == ["跨境电商自动化采集表.json"]


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

    first = server_worker.process_one(source, stable_seconds=0)
    second = server_worker.process_one(source, stable_seconds=0)

    assert first is not None and first.status == "published"
    assert second is not None and second.status == "published"
    assert len(calls) == 1
    saved = json.loads(next(jobs.glob("*.json")).read_text(encoding="utf-8"))
    assert saved["sha256"] == server_worker.sha256_file(source)
    assert saved["output_path"] == "E:/output.json"


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
