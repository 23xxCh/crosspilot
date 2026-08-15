"""Deterministic Worker state transitions, isolated from file delivery."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import random
from typing import Protocol

from . import server_jobs, server_process


JobState = server_jobs.JobState
FailureResult = tuple[str, str, str]
FailureClassifier = Callable[[str, int, int], FailureResult]
ResultExtractor = Callable[[str], tuple[str, str, str]]
ErrorTail = Callable[[str], str]
RandomUniform = Callable[[float, float], float]


class OutcomeClassifier(Protocol):
    def __call__(
        self,
        outcome: dict,
        *,
        attempt: int,
        max_retries: int,
    ) -> FailureResult: ...


@dataclass(frozen=True)
class StateAction:
    """Local side effects the Worker should perform after a transition."""

    delivery_category: str | None = None
    needs_operator_attention: bool = False


@dataclass(frozen=True)
class WorkerControl:
    """One loop decision shared by intake, execution and status reporting."""

    accepts_inputs: bool
    runs_jobs: bool
    health_status: str
    blocker_reason: str
    operator_healthy: bool


def evaluate_worker_control(
    *,
    maintenance: bool,
    configuration_blocked: bool,
    configuration_blocker: str,
    free_disk_gb: float,
    current: JobState | None,
) -> WorkerControl:
    """Apply the Worker gate and status priority without performing I/O."""
    disk_blocked = free_disk_gb < 10.0
    accepts_inputs = not maintenance and not disk_blocked
    runs_jobs = accepts_inputs and not configuration_blocked
    health_status = (
        "maintenance"
        if maintenance
        else "needs_attention"
        if configuration_blocked
        else "blocked_disk"
        if disk_blocked
        else "needs_attention"
        if current and current.status == "blocked"
        else "paused_provider"
        if current and current.status == "retry_wait"
        else "delivering_result"
        if current and current.status == "delivery_retry"
        else "idle"
    )
    blocker_reason = (
        configuration_blocker
        if configuration_blocked
        else "磁盘剩余低于 10 GB，已停止接收新任务"
        if disk_blocked
        else current.blocker_reason if current else ""
    )
    return WorkerControl(
        accepts_inputs=accepts_inputs,
        runs_jobs=runs_jobs,
        health_status=health_status,
        blocker_reason=blocker_reason,
        operator_healthy=not configuration_blocked and not disk_blocked,
    )


def retry_delay_seconds(
    attempt: int,
    *,
    retry_base_seconds: float = 30.0,
    jitter: bool = True,
    random_uniform: RandomUniform = random.uniform,
) -> float:
    """Return the agreed 30s/2m/5m/10m retry schedule."""
    if retry_base_seconds <= 0:
        return 0.0
    schedule = (30.0, 120.0, 300.0)
    base = schedule[min(max(1, attempt), 4) - 1] if attempt <= 3 else 600.0
    if retry_base_seconds != 30.0:
        base *= retry_base_seconds / 30.0
    if not jitter:
        return base
    return max(0.0, base * random_uniform(0.85, 1.15))


def apply_success_outcome(state: JobState, outcome: dict) -> None:
    state.status = str(outcome.get("status") or "invalid_result")
    state.output_path = str(outcome.get("output_path") or "")
    state.review_path = str(outcome.get("review_path") or "")
    state.exception_path = str(outcome.get("exception_path") or "")
    state.pending_product_ids = [
        str(value) for value in outcome.get("pending_product_ids") or []
    ]
    state.isolated_product_ids = [
        str(value) for value in outcome.get("isolated_product_ids") or []
    ]


def apply_child_result(
    state: JobState,
    *,
    outcome: dict | None,
    exit_code: int,
    output: str,
    attempt: int,
    max_retries: int,
    classify_failure: FailureClassifier = server_process.classify_failure,
    classify_outcome_failure: OutcomeClassifier = (
        server_process.classify_outcome_failure
    ),
    extract_result: ResultExtractor = server_process.extract_result,
) -> None:
    """Apply structured child output first, then legacy console fallbacks."""
    if outcome and str(outcome.get("status")) != "failed":
        apply_success_outcome(state, outcome)
        return
    if exit_code == 0:
        state.status, state.output_path, state.review_path = extract_result(output)
        if state.status == "invalid_result":
            message = "PublishedArtifactValidationError: 子进程成功但没有结果标记"
            state.status, state.error, state.failure_kind = classify_failure(
                message,
                attempt,
                max_retries,
            )
        return
    if outcome:
        state.status, state.error, state.failure_kind = classify_outcome_failure(
            outcome,
            attempt=attempt,
            max_retries=max_retries,
        )
        return
    state.status, state.error, state.failure_kind = classify_failure(
        output,
        attempt,
        max_retries,
    )


def transition_pending_review(
    state: JobState,
    *,
    retryable: bool,
) -> StateAction:
    if retryable:
        state.status = "retry_wait"
        state.failure_kind = "transient"
        state.error = "待定商品仍含 operational unknown，将断点续审"
        state.stage = "waiting_provider"
        return StateAction()
    state.failure_kind = "row_quality"
    state.error = "全部商品均无法自动放行，正式表未覆盖"
    state.blocker_reason = state.error
    state.stage = "needs_review"
    return StateAction(
        delivery_category="待处理",
        needs_operator_attention=True,
    )


def schedule_operator_delivery_retry(
    state: JobState,
    exc: Exception,
    *,
    retry_base_seconds: float,
    now: datetime | None = None,
    retry_delay: Callable[..., float] = retry_delay_seconds,
) -> JobState:
    delay = retry_delay(
        max(1, state.attempt),
        retry_base_seconds=retry_base_seconds,
    )
    current = now or datetime.now(timezone.utc)
    state.status = "delivery_retry"
    state.stage = "delivering_result"
    state.failure_kind = "internal"
    state.next_retry_at = (
        current + timedelta(seconds=delay)
    ).isoformat(timespec="seconds")
    state.blocker_reason = "结果已生成，系统正在自动整理操作员交付目录"
    state.error = f"OperatorDeliveryError: {type(exc).__name__}: {exc}"
    return state


def complete_operator_delivery(state: JobState) -> JobState:
    state.status = (
        "published_with_warnings"
        if state.isolated_product_ids
        else "published"
    )
    state.stage = "completed"
    state.failure_kind = ""
    state.next_retry_at = ""
    state.blocker_reason = ""
    state.error = ""
    return state


def finalize_failure_state(
    state: JobState,
    *,
    output: str,
    attempt: int,
    retry_base_seconds: float,
    blocked_retry_hours: float,
    now: datetime | None = None,
    jitter: bool = True,
    retry_delay: Callable[..., float] = retry_delay_seconds,
    error_tail: ErrorTail = server_process.error_tail,
) -> StateAction:
    """Finalize retry timing and report the required local delivery action."""
    current = now or datetime.now(timezone.utc)
    if state.status == "retry_wait":
        delay = retry_delay(
            attempt,
            retry_base_seconds=retry_base_seconds,
            jitter=jitter,
        )
        state.next_retry_at = (
            current + timedelta(seconds=delay)
        ).isoformat(timespec="seconds")
        state.stage = (
            "waiting_provider"
            if state.failure_kind == "transient"
            else "recovering"
        )
        state.blocker_reason = state.error
        state.error = f"{state.error}\n{error_tail(output)}".strip()
        return StateAction()
    if state.status == "blocked":
        state.next_retry_at = (
            current + timedelta(hours=max(0.1, blocked_retry_hours))
        ).isoformat(timespec="seconds")
        state.stage = "needs_attention"
        state.blocker_reason = state.error
        state.error = f"{state.error}\n{error_tail(output)}".strip()
        return StateAction(
            delivery_category="阻塞",
            needs_operator_attention=True,
        )
    if state.status in {"failed", "invalid_input"}:
        state.stage = "stopped"
        state.blocker_reason = state.error
        state.error = f"{state.error}\n{error_tail(output)}".strip()
        return StateAction(
            delivery_category="阻塞",
            needs_operator_attention=True,
        )
    return StateAction()


__all__ = [
    "StateAction",
    "WorkerControl",
    "apply_child_result",
    "apply_success_outcome",
    "complete_operator_delivery",
    "evaluate_worker_control",
    "finalize_failure_state",
    "retry_delay_seconds",
    "schedule_operator_delivery_retry",
    "transition_pending_review",
]
