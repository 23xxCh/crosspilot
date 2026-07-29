"""Provider contracts, structured failures, retry, circuit, and congestion control."""
from __future__ import annotations
import re
from typing import Any
_QUOTA_MARKERS = ('quota', 'insufficient', 'balance', 'billing', 'payment required', 'exceeded', 'credit', 'out of credits', '额度', '余额', '欠费', '用尽', '不足')
_SECRET_PATTERN = re.compile('(?i)(bearer\\s+|(?:sk|cpk)-)[A-Za-z0-9._:/+-]+')

def _redact_excerpt(value: str) -> str:
    return _SECRET_PATTERN.sub(lambda match: f'{match.group(1)}***', str(value or ''))[:200]

class ProviderError(RuntimeError):
    """Base error containing only safe operational context."""
    retryable = False

    def __init__(self, detail: str, *, provider: str='unknown', operation: str='unknown', status_code: int | None=None, retryable: bool | None=None, response_excerpt: str='') -> None:
        safe_detail = str(detail or type(self).__name__)[:300]
        super().__init__(safe_detail)
        self.detail = safe_detail
        self.provider = str(provider or 'unknown')[:64]
        self.operation = str(operation or 'unknown')[:64]
        self.status_code = status_code
        if retryable is not None:
            self.retryable = bool(retryable)
        self.response_excerpt = _redact_excerpt(response_excerpt)

    def to_dict(self) -> dict[str, Any]:
        return {'type': type(self).__name__, 'provider': self.provider, 'operation': self.operation, 'status_code': self.status_code, 'retryable': self.retryable, 'detail': self.detail, 'response_excerpt': self.response_excerpt}

class ProviderAuthError(ProviderError):
    """Credentials are missing, invalid, or unauthorized."""

class ProviderQuotaError(ProviderError):
    """Balance or quota prevents further useful retries."""

class ProviderRateLimitError(ProviderError):
    """The provider temporarily rejected request volume."""
    retryable = True

class ProviderTimeoutError(ProviderError):
    """The provider request exceeded its timeout."""
    retryable = True

class ProviderUnavailableError(ProviderError):
    """The provider or queue is temporarily unavailable."""
    retryable = True

class ProviderResponseError(ProviderError):
    """The request or successful response did not match the API contract."""

def contains_quota_marker(body: str='') -> bool:
    text = str(body or '').lower()
    return any((marker in text for marker in _QUOTA_MARKERS))

def classify_http_error(provider: str, operation: str, status_code: int | None, body: str='') -> ProviderError:
    """Convert one failed HTTP response into a stable error category."""
    excerpt = _redact_excerpt(body)
    common = {'provider': provider, 'operation': operation, 'status_code': status_code, 'response_excerpt': excerpt}
    if status_code == 402 or contains_quota_marker(body):
        return ProviderQuotaError('额度或余额不可用', **common)
    if status_code in (401, 403):
        return ProviderAuthError('API 鉴权失败', **common)
    if status_code == 429:
        return ProviderRateLimitError('API 请求过于频繁', **common)
    if status_code in (408, 504):
        return ProviderTimeoutError('API 请求超时', **common)
    if status_code is not None and status_code >= 500:
        return ProviderUnavailableError('上游服务暂时不可用', **common)
    return ProviderResponseError('API 返回非成功状态', **common)

def is_quota_error(status_code: int | None, body: str='') -> bool:
    return status_code == 402 or contains_quota_marker(body)
import random
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping

def _number(values: Mapping[str, object], key: str, default: float) -> float:
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
        object.__setattr__(self, 'retry_limit', retry_limit)
        object.__setattr__(self, 'backoff_min_s', minimum)
        object.__setattr__(self, 'backoff_max_s', maximum)
        object.__setattr__(self, 'circuit_threshold', threshold)
        object.__setattr__(self, 'circuit_cooldown_s', cooldown)

    @classmethod
    def from_mapping(cls, values: Mapping[str, object] | None) -> 'CongestionPolicy':
        source = values or {}
        return cls(retry_limit=int(_number(source, 'agnes_503_retry_limit', 1)), backoff_min_s=_number(source, 'agnes_503_backoff_min_s', 3), backoff_max_s=_number(source, 'agnes_503_backoff_max_s', 8), circuit_threshold=int(_number(source, 'agnes_503_circuit_threshold', 3)), circuit_cooldown_s=_number(source, 'agnes_503_circuit_cooldown_s', 120))

    def retry_delay(self, *, retry_after: str | None, random_fn: Callable[[float, float], float]=random.uniform) -> float:
        """Return a short delay, respecting but capping Retry-After."""
        if retry_after:
            try:
                requested = max(0.0, float(retry_after))
            except (TypeError, ValueError):
                requested = -1.0
            if requested >= 0:
                return min(self.backoff_max_s, requested)
        return max(self.backoff_min_s, min(self.backoff_max_s, float(random_fn(self.backoff_min_s, self.backoff_max_s))))

class CongestionGate:
    """Closed/open/half-open circuit for one operation and model."""

    def __init__(self, *, threshold: int, cooldown_s: float, clock: Callable[[], float]=time.monotonic) -> None:
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
            if self._half_open_probe or self._failures >= self.threshold:
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
            return {'failures': self._failures, 'opened_until': self._opened_until, 'half_open_probe': self._half_open_probe}
from abc import ABC, abstractmethod
from typing import Any, Optional
from ..images.risk import assessment_from_legacy

class ModelProvider(ABC):
    """Common provider operations used by CompositeProvider."""

    def set_attempt_hook(self, hook) -> None:
        self._attempt_hook = hook

    def set_circuit_hook(self, hook) -> None:
        self._circuit_hook = hook

    def _record_attempt(self, operation, provider, status_code=None, ok=False, retry=False, error=None, rate_wait_s=0.0):
        hook = getattr(self, '_attempt_hook', None)
        if not hook:
            return
        try:
            hook(operation=operation, provider=provider, status_code=status_code, ok=ok, retry=retry, error=type(error).__name__ if error else None, rate_wait_s=rate_wait_s)
        except Exception:
            return

    def _record_circuit_skip(self, operation, provider) -> None:
        hook = getattr(self, '_circuit_hook', None)
        if not hook:
            return
        try:
            hook(operation=operation, provider=provider)
        except Exception:
            return

    @abstractmethod
    def call_text(self, prompt: str, max_tokens: int=2048) -> Optional[str]:
        raise NotImplementedError

    @abstractmethod
    def call_vision(self, image_url: str) -> Optional[bool]:
        raise NotImplementedError

    def assess_image(self, image_url: str, *, confirmation: bool=False) -> Optional[dict[str, Any]]:
        """Return a structured image-risk result when supported."""
        del confirmation
        return assessment_from_legacy(self.call_vision(image_url))

    @abstractmethod
    def call_image_gen(self, image_url: str, size: str='1024x1024', is_variant: bool=False, context: str='', route_offset: int=0) -> Optional[str]:
        raise NotImplementedError
