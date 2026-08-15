from __future__ import annotations

from datetime import datetime, timezone

from amazon_processor import server_jobs, server_state


def _state(**overrides: object) -> server_jobs.JobState:
    values: dict[str, object] = {
        "source_path": "E:/input.json",
        "sha256": "a" * 64,
        "status": "running",
        "attempt": 2,
    }
    values.update(overrides)
    return server_jobs.JobState(**values)


def test_apply_child_result_prefers_structured_outcome() -> None:
    state = _state()

    server_state.apply_child_result(
        state,
        outcome={
            "status": "published_with_warnings",
            "output_path": "E:/formal.json",
            "review_path": "E:/review.html",
            "exception_path": "E:/exceptions.json",
            "pending_product_ids": ["p1"],
            "isolated_product_ids": ["p1"],
        },
        exit_code=0,
        output="console text without markers",
        attempt=2,
        max_retries=3,
    )

    assert state.status == "published_with_warnings"
    assert state.output_path == "E:/formal.json"
    assert state.exception_path == "E:/exceptions.json"
    assert state.isolated_product_ids == ["p1"]


def test_apply_child_result_rejects_exit_zero_without_marker() -> None:
    state = _state()

    server_state.apply_child_result(
        state,
        outcome=None,
        exit_code=0,
        output="completed without result marker",
        attempt=1,
        max_retries=1,
    )

    assert state.status == "failed"
    assert state.output_path == ""
    assert "最大自动重试次数" in state.error


def test_pending_review_transition_distinguishes_retry_from_quality() -> None:
    retrying = _state(status="pending_review")
    manual = _state(status="pending_review")

    retry_action = server_state.transition_pending_review(
        retrying,
        retryable=True,
    )
    manual_action = server_state.transition_pending_review(
        manual,
        retryable=False,
    )

    assert retrying.status == "retry_wait"
    assert retrying.failure_kind == "transient"
    assert retry_action.delivery_category is None
    assert manual.status == "pending_review"
    assert manual.failure_kind == "row_quality"
    assert manual_action.delivery_category == "待处理"
    assert manual_action.needs_operator_attention is True


def test_finalize_failure_state_sets_retry_and_blocked_actions() -> None:
    now = datetime(2026, 8, 15, 12, tzinfo=timezone.utc)
    retrying = _state(
        status="retry_wait",
        failure_kind="transient",
        error="temporary",
    )
    blocked = _state(status="blocked", error="auth")

    retry_action = server_state.finalize_failure_state(
        retrying,
        output="HTTP 503",
        attempt=2,
        retry_base_seconds=30,
        blocked_retry_hours=6,
        now=now,
        jitter=False,
    )
    blocked_action = server_state.finalize_failure_state(
        blocked,
        output="401",
        attempt=2,
        retry_base_seconds=30,
        blocked_retry_hours=6,
        now=now,
        jitter=False,
    )

    assert retrying.next_retry_at == "2026-08-15T12:02:00+00:00"
    assert retrying.stage == "waiting_provider"
    assert retry_action.delivery_category is None
    assert blocked.next_retry_at == "2026-08-15T18:00:00+00:00"
    assert blocked.stage == "needs_attention"
    assert blocked_action.delivery_category == "阻塞"
    assert blocked_action.needs_operator_attention is True


def test_schedule_delivery_retry_and_complete_clear_each_other() -> None:
    now = datetime(2026, 8, 15, 12, tzinfo=timezone.utc)
    state = _state(isolated_product_ids=["p1"])

    server_state.schedule_operator_delivery_retry(
        state,
        PermissionError("busy"),
        retry_base_seconds=0,
        now=now,
    )
    assert state.status == "delivery_retry"
    assert state.stage == "delivering_result"

    server_state.complete_operator_delivery(state)
    assert state.status == "published_with_warnings"
    assert state.stage == "completed"
    assert state.error == ""


def test_worker_control_allows_intake_but_pauses_processing_for_config() -> None:
    control = server_state.evaluate_worker_control(
        maintenance=False,
        configuration_blocked=True,
        configuration_blocker="missing vision credential",
        free_disk_gb=50.0,
        current=_state(status="queued"),
    )

    assert control.accepts_inputs is True
    assert control.runs_jobs is False
    assert control.health_status == "needs_attention"
    assert control.blocker_reason == "missing vision credential"
    assert control.operator_healthy is False


def test_worker_control_applies_status_priority_and_disk_gate() -> None:
    maintenance = server_state.evaluate_worker_control(
        maintenance=True,
        configuration_blocked=False,
        configuration_blocker="",
        free_disk_gb=50.0,
        current=_state(status="retry_wait", blocker_reason="503"),
    )
    low_disk = server_state.evaluate_worker_control(
        maintenance=False,
        configuration_blocked=False,
        configuration_blocker="",
        free_disk_gb=9.9,
        current=_state(status="blocked", blocker_reason="quota"),
    )

    assert maintenance.health_status == "maintenance"
    assert maintenance.accepts_inputs is False
    assert maintenance.runs_jobs is False
    assert low_disk.health_status == "blocked_disk"
    assert low_disk.blocker_reason == "磁盘剩余低于 10 GB，已停止接收新任务"
    assert low_disk.operator_healthy is False


def test_worker_control_maps_current_job_state() -> None:
    expected = {
        "blocked": "needs_attention",
        "retry_wait": "paused_provider",
        "delivery_retry": "delivering_result",
        "queued": "idle",
    }

    for job_status, health_status in expected.items():
        control = server_state.evaluate_worker_control(
            maintenance=False,
            configuration_blocked=False,
            configuration_blocker="",
            free_disk_gb=20.0,
            current=_state(status=job_status, blocker_reason="reason"),
        )
        assert control.health_status == health_status
        assert control.blocker_reason == "reason"
        assert control.accepts_inputs is True
        assert control.runs_jobs is True
        assert control.operator_healthy is True
