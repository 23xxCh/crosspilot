from __future__ import annotations

from dataclasses import asdict
from http.client import HTTPConnection
from http import HTTPStatus
import json
from pathlib import Path
import threading

import pytest

from amazon_processor import api_server, server_worker


API_KEY = "test_" + "a" * 32


@pytest.fixture(autouse=True)
def _isolate_operator_delivery(tmp_path, monkeypatch) -> None:
    def fake_operator_success(_state, *, artifact_dir):
        target = tmp_path / "operator-result"
        target.mkdir(exist_ok=True)
        return target

    monkeypatch.setattr(
        server_worker,
        "_publish_operator_success",
        fake_operator_success,
    )
    monkeypatch.setattr(
        server_worker,
        "_publish_operator_attention",
        lambda _state: tmp_path / "operator-attention",
    )
    monkeypatch.setattr(
        server_worker,
        "refresh_operator_status",
        lambda **_kwargs: None,
    )


def _payload() -> dict:
    return {
        "商品id": ["10001"],
        "产品站点": ["US"],
        "产品标题": ["Universal car storage hook"],
        "产品描述": ["Plastic storage hook for vehicle interiors."],
        "产品图片链接": [["https://example.com/main.jpg"]],
        "变种图片链接": [[]],
    }


def _body(payload: dict | None = None) -> bytes:
    return json.dumps(payload or _payload(), ensure_ascii=False).encode("utf-8")


def _service(tmp_path: Path, **kwargs) -> api_server.JobAPIService:
    return api_server.JobAPIService(
        api_key=API_KEY,
        input_dir=tmp_path / "input",
        jobs_root=tmp_path / "jobs",
        deliveries_root=tmp_path / "deliveries",
        worker_health_func=lambda _age: {
            "healthy": True,
            "status": "idle",
        },
        **kwargs,
    )


def test_submit_is_durable_and_idempotent(tmp_path) -> None:
    service = _service(tmp_path)

    first, created = service.submit(_body())
    second, duplicated = service.submit(_body())

    assert created is True
    assert duplicated is False
    assert first == second
    assert first["status"] == "queued"
    assert first["stage"] == "queued"
    assert first["queue_position"] == 1
    assert first["progress"] == {"completed": 0, "total": 0}
    assert first["isolated_count"] == 0
    assert first["row_count"] == 1
    assert len(first["id"]) == 64
    queued = tmp_path / "input" / f"API_{first['id']}.json"
    assert queued.read_bytes() == _body()
    state = json.loads(
        (tmp_path / "jobs" / f"{first['id']}.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "queued"
    assert state["submitted_at"]
    assert state["row_count"] == 1


def test_duplicate_submission_restores_missing_queued_file(tmp_path) -> None:
    service = _service(tmp_path)
    first, _created = service.submit(_body())
    queued = tmp_path / "input" / f"API_{first['id']}.json"
    queued.unlink()

    second, created = service.submit(_body())

    assert created is False
    assert second["id"] == first["id"]
    assert queued.read_bytes() == _body()


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        (b"{", HTTPStatus.BAD_REQUEST, "invalid_json"),
        (
            json.dumps({**_payload(), "副标题": [""]}).encode("utf-8"),
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "invalid_contract",
        ),
        (
            json.dumps({**_payload(), "产品站点": ["XX"]}).encode("utf-8"),
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "invalid_contract",
        ),
    ],
)
def test_submission_validation_is_safe(tmp_path, body, status, code) -> None:
    service = _service(tmp_path)

    with pytest.raises(api_server.APIRequestError) as caught:
        service.submit(body)

    assert caught.value.status == status
    assert caught.value.code == code
    assert not list((tmp_path / "input").glob("*.json"))


def test_artifacts_come_from_immutable_delivery(tmp_path) -> None:
    service = _service(tmp_path)
    job, _created = service.submit(_body())
    state = service.get_state(job["id"])
    delivery = tmp_path / "deliveries" / "成功" / "job"
    delivery.mkdir(parents=True)
    result = delivery / "跨境电商自动化回填表.json"
    review = delivery / "终审包.html"
    result.write_text('{"ok": true}', encoding="utf-8")
    review.write_text("<html>review</html>", encoding="utf-8")
    state.status = "published"
    state.output_path = str(tmp_path / "latest" / result.name)
    state.delivery_path = str(delivery)
    server_worker._atomic_json(
        tmp_path / "jobs" / f"{state.sha256}.json",
        asdict(state),
    )

    assert service.artifact_path(job["id"], "result") == result
    assert service.artifact_path(job["id"], "review") == review


@pytest.fixture
def http_api(tmp_path):
    service = _service(tmp_path)
    server = api_server.JobAPIServer(("127.0.0.1", 0), service=service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(server, method, path, *, body=None, key=API_KEY, headers=None):
    connection = HTTPConnection(
        server.server_address[0],
        server.server_address[1],
        timeout=3,
    )
    request_headers = dict(headers or {})
    if key is not None:
        request_headers["X-API-Key"] = key
    connection.request(method, path, body=body, headers=request_headers)
    response = connection.getresponse()
    data = response.read()
    result = response.status, dict(response.headers), data
    connection.close()
    return result


def test_http_auth_submit_status_health_and_pending_result(http_api) -> None:
    status, _headers, body = _request(
        http_api,
        "GET",
        "/api/v1/health",
        key=None,
    )
    assert status == HTTPStatus.UNAUTHORIZED
    assert API_KEY.encode() not in body

    status, headers, body = _request(
        http_api,
        "POST",
        "/api/v1/jobs",
        body=_body(),
        headers={"Content-Type": "application/json"},
    )
    payload = json.loads(body)
    job_id = payload["data"]["id"]
    assert status == HTTPStatus.CREATED
    assert headers["Location"] == f"/api/v1/jobs/{job_id}"
    assert API_KEY.encode() not in body

    status, _headers, body = _request(
        http_api,
        "GET",
        f"/api/v1/jobs/{job_id}",
    )
    assert status == HTTPStatus.OK
    assert json.loads(body)["data"]["status"] == "queued"

    status, headers, body = _request(
        http_api,
        "GET",
        f"/api/v1/jobs/{job_id}/result",
    )
    assert status == HTTPStatus.ACCEPTED
    assert headers["Retry-After"] == "15"
    assert json.loads(body)["error"]["code"] == "result_not_ready"

    status, _headers, body = _request(
        http_api,
        "GET",
        "/api/v1/health",
    )
    assert status == HTTPStatus.OK
    assert json.loads(body)["data"]["api"]["healthy"] is True


def test_http_rejects_wrong_media_type_and_rate_limits(tmp_path) -> None:
    service = _service(tmp_path, submit_limit_per_minute=1)
    server = api_server.JobAPIServer(("127.0.0.1", 0), service=service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _headers, body = _request(
            server,
            "POST",
            "/api/v1/jobs",
            body=_body(),
            headers={"Content-Type": "text/plain"},
        )
        assert status == HTTPStatus.UNSUPPORTED_MEDIA_TYPE
        assert json.loads(body)["error"]["code"] == "unsupported_media_type"

        status, headers, body = _request(
            server,
            "POST",
            "/api/v1/jobs",
            body=_body(),
            headers={"Content-Type": "application/json"},
        )
        assert status == HTTPStatus.TOO_MANY_REQUESTS
        assert int(headers["Retry-After"]) >= 1
        assert json.loads(body)["error"]["code"] == "rate_limit_exceeded"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_health_reports_api_ready_when_worker_is_degraded(tmp_path) -> None:
    service = api_server.JobAPIService(
        api_key=API_KEY,
        input_dir=tmp_path / "input",
        jobs_root=tmp_path / "jobs",
        deliveries_root=tmp_path / "deliveries",
        worker_health_func=lambda _age: {
            "healthy": False,
            "status": "missing",
        },
    )

    health = service.health(120)

    assert health["api"] == {"healthy": True, "status": "ready"}
    assert health["worker"]["healthy"] is False


def test_api_health_check_accepts_degraded_worker(
    tmp_path,
    monkeypatch,
) -> None:
    service = api_server.JobAPIService(
        api_key=API_KEY,
        input_dir=tmp_path / "input",
        jobs_root=tmp_path / "jobs",
        deliveries_root=tmp_path / "deliveries",
        worker_health_func=lambda _age: {
            "healthy": False,
            "status": "missing",
        },
    )
    server = api_server.JobAPIServer(("127.0.0.1", 0), service=service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(api_server.CLIENT_KEY_ENV, API_KEY)
    try:
        health = api_server.api_health_check(
            url=(
                f"http://127.0.0.1:{server.server_address[1]}"
                "/api/v1/health"
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert health["api"]["healthy"] is True
    assert health["worker"]["healthy"] is False


def test_worker_preserves_api_submission_metadata(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    job, _created = service.submit(_body())
    source = tmp_path / "input" / f"API_{job['id']}.json"
    monkeypatch.setattr(server_worker, "JOBS_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(server_worker, "LOGS_ROOT", tmp_path / "logs")
    monkeypatch.setattr(server_worker, "is_file_stable", lambda *_args: True)
    monkeypatch.setattr(
        server_worker,
        "run_child",
        lambda *_args, **_kwargs: (0, "正式表已更新: E:/output.json"),
    )
    monkeypatch.setattr(
        server_worker,
        "validate_published_output",
        lambda _path: {"published": True, "pending_product_ids": []},
    )
    monkeypatch.setattr(
        server_worker,
        "snapshot_delivery",
        lambda *_args, **_kwargs: tmp_path / "delivery",
    )

    processed = server_worker.process_one(source, stable_seconds=0)

    assert processed is not None
    assert processed.status == "published"
    assert processed.submitted_at == job["submitted_at"]
    assert processed.row_count == 1


def test_system_overview_is_plain_and_counts_jobs(tmp_path) -> None:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    first = server_worker.JobState(
        source_path=str(tmp_path / "first.json"),
        sha256="1" * 64,
        status="published",
        submitted_at="2026-08-11T01:00:00+00:00",
        finished_at="2026-08-11T01:10:00+00:00",
        row_count=18,
        attempt=1,
    )
    second = server_worker.JobState(
        source_path=str(tmp_path / "second.json"),
        sha256="2" * 64,
        status="retry_wait",
        submitted_at="2026-08-11T02:00:00+00:00",
        row_count=40,
        attempt=2,
    )
    for state in (first, second):
        (jobs / f"{state.sha256}.json").write_text(
            json.dumps(asdict(state), ensure_ascii=False),
            encoding="utf-8",
        )

    overview = api_server.system_overview(
        jobs_root=jobs,
        worker_health_func=lambda _age: {
            "healthy": True,
            "status": "idle",
        },
        api_health_func=lambda: {
            "api": {"healthy": True, "status": "ready"}
        },
    )
    rendered = api_server.format_system_overview(overview)

    assert overview["healthy"] is True
    assert overview["counts"]["published"] == 1
    assert overview["counts"]["retry_wait"] == 1
    assert overview["latest"]["source_name"] == "second.json"
    assert "总体状态：运行正常" in rendered
    assert "等待自动重试：1" in rendered
    assert "API_KEY" not in rendered


def test_system_overview_gives_one_simple_recovery_action(tmp_path) -> None:
    overview = api_server.system_overview(
        jobs_root=tmp_path / "missing",
        worker_health_func=lambda _age: {
            "healthy": False,
            "status": "missing",
        },
        api_health_func=lambda: (_ for _ in ()).throw(ConnectionError()),
    )

    rendered = api_server.format_system_overview(overview)

    assert overview["healthy"] is False
    assert "自动处理：未启动或异常" in rendered
    assert "调用接口：未启动或异常" in rendered
    assert "双击 04_一键安装服务器.bat" in rendered
