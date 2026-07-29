"""Bounded concurrency helpers for provider-heavy pipeline stages."""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterable


def env_int(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    """Read an integer environment override with conservative clamping."""
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def configured_concurrency(kind: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    """Resolve CROSSPILOT_<KIND>_CONCURRENCY with safe bounds."""
    key = f'CROSSPILOT_{kind.upper()}_CONCURRENCY'
    configured_default = default
    if key not in os.environ:
        try:
            from crosspilot.config import load_config

            configured_default = int(
                load_config().get(
                    f'{kind.upper()}_CONCURRENCY',
                    str(default),
                )
            )
        except (ImportError, TypeError, ValueError):
            configured_default = default
    return env_int(
        key,
        configured_default,
        minimum=minimum,
        maximum=maximum,
    )


def env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def configured_rpm(
    kind: str,
    default: float,
    minimum: float = 1.0,
    maximum: float | None = None,
) -> float:
    """Resolve CROSSPILOT_<KIND>_RPM with safe bounds."""
    key = f'CROSSPILOT_{kind.upper()}_RPM'
    max_bound = maximum if maximum is not None else 1_000_000.0
    return env_float(key, default, minimum=minimum, maximum=max_bound)


def adaptive_map(
    items: Iterable[Any],
    worker: Callable[[Any], Any],
    *,
    operation: str,
    initial_workers: int,
    min_workers: int = 1,
    max_workers: int | None = None,
    is_success: Callable[[Any], bool] | None = None,
    on_result: Callable[[Any, Any], None] | None = None,
    terminal_exceptions: tuple[type[BaseException], ...] = (),
    backoff_s: float = 0.0,
    max_backoff_s: float | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> tuple[list[tuple[Any, Any]], dict[str, Any]]:
    """Run work in batches and reduce concurrency when a batch fails too often."""
    pending = list(items)
    if not pending:
        return [], {
            'operation': operation,
            'items': 0,
            'initial_workers': 0,
            'final_workers': 0,
            'min_workers': min_workers,
            'reductions': 0,
            'recoveries': 0,
            'failures': 0,
            'backoff_total_s': 0.0,
            'events': [],
        }

    maximum = max(1, max_workers if max_workers is not None else initial_workers)
    effective_min = max(1, min(min_workers, maximum, len(pending)))
    workers = max(effective_min, min(initial_workers, maximum, len(pending)))
    initial = workers
    failure_threshold = env_float(
        'CROSSPILOT_ADAPTIVE_FAILURE_RATE',
        0.25,
        minimum=0.05,
        maximum=1.0,
    )
    recovery_batches_needed = env_int(
        'CROSSPILOT_ADAPTIVE_RECOVERY_BATCHES',
        3,
        minimum=1,
        maximum=20,
    )
    success_check = is_success or (lambda result: result is not None and result != '')
    results: list[tuple[Any, Any]] = []
    events = []
    total_failures = 0
    reductions = 0
    recoveries = 0
    backoff_total_s = 0.0
    clean_batches = 0
    batch_index = 0
    cursor = 0
    sleeper = sleep_fn or time.sleep
    backoff_s = max(0.0, float(backoff_s or 0.0))
    max_backoff = (
        max(backoff_s, float(max_backoff_s))
        if max_backoff_s is not None else backoff_s
    )

    while cursor < len(pending):
        batch_index += 1
        batch = pending[cursor:cursor + workers]
        cursor += len(batch)
        batch_failures = 0

        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(batch)))) as pool:
            futures = {pool.submit(worker, item): item for item in batch}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    result = future.result()
                except terminal_exceptions:
                    for pending_future in futures:
                        pending_future.cancel()
                    raise
                except Exception as exc:
                    result = exc
                if not success_check(result):
                    batch_failures += 1
                    total_failures += 1
                results.append((item, result))
                if on_result:
                    on_result(item, result)

        failure_rate = batch_failures / max(len(batch), 1)
        if batch_failures and failure_rate >= failure_threshold and workers > min_workers:
            next_workers = max(min_workers, workers // 2)
            if next_workers < workers:
                wait_s = 0.0
                if backoff_s > 0 and cursor < len(pending):
                    wait_s = min(max_backoff, backoff_s * (2 ** reductions))
                events.append({
                    'batch': batch_index,
                    'from': workers,
                    'to': next_workers,
                    'reason': 'failure_rate',
                    'failure_rate': round(failure_rate, 3),
                    'backoff_s': round(wait_s, 3),
                })
                workers = next_workers
                reductions += 1
                if wait_s > 0:
                    sleeper(wait_s)
                    backoff_total_s += wait_s
            clean_batches = 0
        elif batch_failures == 0:
            clean_batches += 1
            if clean_batches >= recovery_batches_needed and workers < initial:
                next_workers = min(initial, max(workers + 1, workers * 2))
                if next_workers > workers:
                    events.append({
                        'batch': batch_index,
                        'from': workers,
                        'to': next_workers,
                        'reason': 'recovery',
                    })
                    workers = next_workers
                    recoveries += 1
                clean_batches = 0
        else:
            clean_batches = 0

    return results, {
        'operation': operation,
        'items': len(pending),
        'initial_workers': initial,
        'final_workers': workers,
        'min_workers': min_workers,
        'reductions': reductions,
        'recoveries': recoveries,
        'failures': total_failures,
        'backoff_total_s': round(backoff_total_s, 3),
        'events': events,
    }
