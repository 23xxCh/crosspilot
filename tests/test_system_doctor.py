from __future__ import annotations

from amazon_processor import __main__ as cli
from amazon_processor import api_server, server_worker, system_doctor


def test_server_cli_defaults_to_operator_inbox() -> None:
    parser = cli.build_parser()

    worker = parser.parse_args(["worker"])
    api = parser.parse_args(["api"])

    assert worker.input_dir.endswith("Amazon日常操作\\1_把采集表放这里")
    assert api.input_dir == worker.input_dir


def test_system_doctor_is_offline_and_reports_ready_server(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(server_worker, "OPERATOR_ROOT", tmp_path / "Amazon日常操作")
    monkeypatch.setattr(server_worker, "FORMAL_LATEST_ROOT", tmp_path / "latest")
    monkeypatch.setattr(
        server_worker,
        "preflight",
        lambda _path: {
            "free_disk_gb": 88.5,
            "missing_operations": [],
            "blocker_reason": "",
        },
    )
    monkeypatch.setattr(
        server_worker,
        "worker_health",
        lambda _age: {"healthy": True, "status": "idle"},
    )
    monkeypatch.setattr(
        api_server,
        "api_health_check",
        lambda: {"api": {"healthy": True, "status": "ready"}},
    )
    monkeypatch.setattr(system_doctor, "_task_installed", lambda _name: True)
    monkeypatch.setattr(
        server_worker,
        "refresh_operator_status",
        lambda **_kwargs: None,
    )

    report = system_doctor.run_system_doctor(repair=False)

    assert report["healthy"] is True
    assert report["model_credentials_ready"] is True
    assert report["free_disk_gb"] == 88.5
    assert "通过" in system_doctor.format_report(report)


def test_system_doctor_starts_only_installed_unhealthy_services(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(server_worker, "OPERATOR_ROOT", tmp_path / "Amazon日常操作")
    monkeypatch.setattr(server_worker, "FORMAL_LATEST_ROOT", tmp_path / "latest")
    monkeypatch.setattr(
        server_worker,
        "preflight",
        lambda _path: {"free_disk_gb": 50, "missing_operations": []},
    )
    monkeypatch.setattr(
        server_worker,
        "worker_health",
        lambda _age: {"healthy": False, "status": "missing"},
    )
    monkeypatch.setattr(
        api_server,
        "api_health_check",
        lambda: (_ for _ in ()).throw(ConnectionError()),
    )
    monkeypatch.setattr(system_doctor, "_task_installed", lambda _name: True)
    started = []
    monkeypatch.setattr(
        system_doctor,
        "_start_task",
        lambda name: started.append(name) or True,
    )
    monkeypatch.setattr(
        server_worker,
        "refresh_operator_status",
        lambda **_kwargs: None,
    )

    report = system_doctor.run_system_doctor(repair=True)

    assert started == [
        "AmazonProcessor-Unattended",
        "AmazonProcessor-API",
    ]
    assert report["healthy"] is False
    assert report["started_tasks"] == started


def test_system_doctor_restarts_updated_services_only_when_idle(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(server_worker, "OPERATOR_ROOT", tmp_path / "Amazon日常操作")
    monkeypatch.setattr(server_worker, "FORMAL_LATEST_ROOT", tmp_path / "latest")
    monkeypatch.setattr(server_worker, "RUNTIME_ROOT", tmp_path / "runtime")
    server_worker.RUNTIME_ROOT.mkdir()
    marker = server_worker.RUNTIME_ROOT / system_doctor.RESTART_MARKER_NAME
    marker.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        server_worker,
        "preflight",
        lambda _path: {"free_disk_gb": 50, "missing_operations": []},
    )
    monkeypatch.setattr(
        server_worker,
        "worker_health",
        lambda _age: {
            "healthy": True,
            "status": "idle",
            "queue_depth": 0,
            "current_job": "",
        },
    )
    monkeypatch.setattr(
        api_server,
        "api_health_check",
        lambda: {"api": {"healthy": True, "status": "ready"}},
    )
    monkeypatch.setattr(system_doctor, "_task_installed", lambda _name: True)
    restarted = []
    monkeypatch.setattr(
        system_doctor,
        "_restart_task",
        lambda name: restarted.append(name) or True,
    )
    monkeypatch.setattr(
        server_worker,
        "refresh_operator_status",
        lambda **_kwargs: None,
    )

    report = system_doctor.run_system_doctor(repair=True)

    assert restarted == [
        "AmazonProcessor-Unattended",
        "AmazonProcessor-API",
    ]
    assert report["restart_required"] is False
    assert not marker.exists()
