"""Thread-safe congestion policy and circuit state for one provider model."""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping


def _number(
    values: Mapping[str, object],
    key: str,
    default: float,
) -> float:
    try:
        return float(values.get(key, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class CongestionPolicy:
    """Fast retry and circuit settings for transient 503 responses."""

    retry_limit: int = 1
    backoff_min_s: float = 3.0
    backoff_max_s: float = 8.0
    circuit_threshold: int = 3
    circuit_cooldown_s: float = 120.0

    def __post_init__(self) -> None:
        retry_limit = max(0, min(int(self.retry_limit), 5))
        minimum = max(0.0, float(self.backoff_min_s))
        maximum = max(minimum, float(self.backoff_max_s))
        threshold = max(1, int(self.circuit_threshold))
        cooldown = max(1.0, float(self.circuit_cooldown_s))
        object.__setattr__(self, "retry_limit", retry_limit)
        object.__setattr__(self, "backoff_min_s", minimum)
        object.__setattr__(self, "backoff_max_s", maximum)
        object.__setattr__(self, "circuit_threshold", threshold)
        object.__setattr__(self, "circuit_cooldown_s", cooldown)

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, object] | None,
    ) -> "CongestionPolicy":
        source = values or {}
        return cls(
            retry_limit=int(
                _number(source, "agnes_503_retry_limit", 1)
            ),
            backoff_min_s=_number(
                source,
                "agnes_503_backoff_min_s",
                3,
            ),
            backoff_max_s=_number(
                source,
                "agnes_503_backoff_max_s",
                8,
            ),
            circuit_threshold=int(
                _number(
                    source,
                    "agnes_503_circuit_threshold",
                    3,
                )
            ),
            circuit_cooldown_s=_number(
                source,
                "agnes_503_circuit_cooldown_s",
                120,
            ),
        )

    def retry_delay(
        self,
        *,
        retry_after: str | None,
        random_fn: Callable[[float, float], float] = random.uniform,
    ) -> float:
        """Return a short delay, respecting but capping Retry-After."""
        if retry_after:
            try:
                requested = max(0.0, float(retry_after))
            except (TypeError, ValueError):
                requested = -1.0
            if requested >= 0:
                return min(self.backoff_max_s, requested)
        return max(
            self.backoff_min_s,
            min(
                self.backoff_max_s,
                float(
                    random_fn(
                        self.backoff_min_s,
                        self.backoff_max_s,
                    )
                ),
            ),
        )


class CongestionGate:
    """Closed/open/half-open circuit for one operation and model."""

    def __init__(
        self,
        *,
        threshold: int,
        cooldown_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold = max(1, int(threshold))
        self.cooldown_s = max(1.0, float(cooldown_s))
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_until = 0.0
        self._half_open_probe = False

    def try_acquire(self) -> bool:
        """Allow normal traffic or exactly one probe after cooldown."""
        with self._lock:
            now = self._clock()
            if self._opened_until > now:
                return False
            if self._opened_until:
                if self._half_open_probe:
                    return False
                self._half_open_probe = True
                return True
            return True

    def record_503(self) -> None:
        with self._lock:
            self._failures += 1
            if (
                self._half_open_probe
                or self._failures >= self.threshold
            ):
                self._opened_until = self._clock() + self.cooldown_s
            self._half_open_probe = False

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_until = 0.0
            self._half_open_probe = False

    def record_probe_failure(self) -> None:
        """Keep a failed half-open probe from leaving the gate stuck."""
        with self._lock:
            if self._half_open_probe:
                self._opened_until = self._clock() + self.cooldown_s
            self._half_open_probe = False

    def is_open(self) -> bool:
        with self._lock:
            return self._opened_until > self._clock()

    def snapshot(self) -> dict[str, float | int | bool]:
        with self._lock:
            return {
                "failures": self._failures,
                "opened_until": self._opened_until,
                "half_open_probe": self._half_open_probe,
            }
