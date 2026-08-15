"""Durable job submission and artifact service behind the HTTP adapter.

This module validates Amazon collection-table JSON, persists idempotent job
state, and locates immutable delivery artifacts. It does not open sockets or
run the paid processing pipeline.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from http import HTTPStatus
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any, Callable

from .config.credentials import DEFAULT_ENV_PATH, read_env_values
from .markets import normalize_market_code
from .schema import (
    AMAZON_JSON_INPUT_FIELDS,
    AMAZON_JSON_LEGACY_INPUT_FIELDS,
    validate_columnar_payload,
)
from . import server_worker


DEFAULT_INPUT_DIR = server_worker.DEFAULT_INBOX
CLIENT_KEY_ENV = "AMAZON_PROCESSOR_API_KEY"
DEFAULT_MAX_ROWS = 10_000
JOB_ID_RE = re.compile(r"^[0-9a-f]{64}$")


class APIRequestError(Exception):
    """Expected request failure safe to return to an API caller."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message
        self.retry_after = retry_after


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def load_client_api_key() -> str:
    """Resolve the partner-facing key without exposing provider credentials."""
    system_value = str(os.environ.get(CLIENT_KEY_ENV) or "").strip()
    if system_value:
        return system_value
    return str(read_env_values(DEFAULT_ENV_PATH).get(CLIENT_KEY_ENV) or "").strip()


def _validate_client_api_key(value: str) -> str:
    key = str(value or "").strip()
    if len(key) < 24:
        raise ValueError(
            f"{CLIENT_KEY_ENV} 未配置或长度不足 24 字符；"
            "请在项目 .env 中设置独立的调用密钥"
        )
    return key


class SlidingWindowLimiter:
    """In-memory per-key limiter; durable job idempotency is handled on disk."""

    def __init__(self) -> None:
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def consume(
        self,
        key: str,
        scope: str,
        *,
        limit: int,
        window_seconds: int = 60,
    ) -> tuple[bool, int, int]:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            events = self._events[(key, scope)]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                retry_after = max(1, int(window_seconds - (now - events[0])) + 1)
                return False, 0, retry_after
            events.append(now)
            return True, max(0, limit - len(events)), window_seconds


class JobAPIService:
    """Validate submissions and expose only sanitized worker state/artifacts."""

    def __init__(
        self,
        *,
        api_key: str,
        input_dir: Path = DEFAULT_INPUT_DIR,
        jobs_root: Path | None = None,
        deliveries_root: Path | None = None,
        max_rows: int = DEFAULT_MAX_ROWS,
        worker_health_func: Callable[[float], dict] | None = None,
        submit_limit_per_minute: int = 10,
        read_limit_per_minute: int = 120,
    ) -> None:
        self.api_key = _validate_client_api_key(api_key)
        self.input_dir = Path(input_dir).expanduser().resolve()
        self.jobs_root = Path(jobs_root or server_worker.JOBS_ROOT).resolve()
        self.deliveries_root = Path(
            deliveries_root or server_worker.DELIVERIES_ROOT
        ).resolve()
        self.max_rows = max(1, int(max_rows))
        self.worker_health_func = worker_health_func or server_worker.worker_health
        self.submit_limit_per_minute = max(1, int(submit_limit_per_minute))
        self.read_limit_per_minute = max(1, int(read_limit_per_minute))
        self.limiter = SlidingWindowLimiter()
        self._submit_lock = threading.Lock()
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_root.mkdir(parents=True, exist_ok=True)

    def authenticated(self, supplied: str) -> bool:
        value = str(supplied or "").strip()
        return bool(value) and secrets.compare_digest(value, self.api_key)

    def check_rate(self, *, write: bool) -> tuple[int, int]:
        scope = "submit" if write else "read"
        limit = (
            self.submit_limit_per_minute
            if write
            else self.read_limit_per_minute
        )
        allowed, remaining, reset = self.limiter.consume(
            self.api_key,
            scope,
            limit=limit,
        )
        if not allowed:
            raise APIRequestError(
                HTTPStatus.TOO_MANY_REQUESTS,
                "rate_limit_exceeded",
                "请求过于频繁，请稍后重试",
                retry_after=reset,
            )
        return remaining, reset

    def _state_path(self, job_id: str) -> Path:
        if not JOB_ID_RE.fullmatch(job_id):
            raise APIRequestError(
                HTTPStatus.NOT_FOUND,
                "job_not_found",
                "任务不存在",
            )
        return self.jobs_root / f"{job_id}.json"

    def _load_state(self, job_id: str) -> server_worker.JobState | None:
        path = self._state_path(job_id)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            defaults = asdict(server_worker.JobState("", "", ""))
            return server_worker.JobState(**{
                name: payload.get(name, default)
                for name, default in defaults.items()
            })
        except (OSError, TypeError, ValueError):
            raise APIRequestError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "job_state_unavailable",
                "任务状态暂时不可用",
                retry_after=15,
            ) from None

    def _save_state(self, state: server_worker.JobState) -> None:
        server_worker._atomic_json(  # package-internal shared atomic writer
            self.jobs_root / f"{state.sha256}.json",
            asdict(state),
        )
        if self.jobs_root == server_worker.JOBS_ROOT.resolve():
            server_worker.refresh_operator_status()

    def _validate_submission(self, body: bytes) -> tuple[dict, int]:
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise APIRequestError(
                HTTPStatus.BAD_REQUEST,
                "invalid_encoding",
                "采集表必须使用 UTF-8 编码",
            ) from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise APIRequestError(
                HTTPStatus.BAD_REQUEST,
                "invalid_json",
                f"采集表不是有效 JSON（第 {exc.lineno} 行）",
            ) from exc
        if not isinstance(payload, dict):
            raise APIRequestError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "invalid_contract",
                "采集表 JSON 顶层必须是对象",
            )
        legacy = "产品站点" not in payload
        expected = (
            AMAZON_JSON_LEGACY_INPUT_FIELDS
            if legacy
            else AMAZON_JSON_INPUT_FIELDS
        )
        if set(payload) != set(expected):
            missing = [name for name in expected if name not in payload]
            unknown = [name for name in payload if name not in expected]
            details = []
            if missing:
                details.append("缺少 " + ", ".join(missing))
            if unknown:
                details.append("多出 " + ", ".join(unknown))
            raise APIRequestError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "invalid_contract",
                "采集表字段不符合输入契约：" + "；".join(details),
            )
        try:
            row_count = validate_columnar_payload(
                payload,
                required_fields=expected,
                max_rows=self.max_rows,
            )
            if not legacy:
                for site in payload["产品站点"]:
                    normalize_market_code(site)
        except ValueError as exc:
            raise APIRequestError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "invalid_contract",
                str(exc),
            ) from exc
        return payload, row_count

    def submit(self, body: bytes) -> tuple[dict[str, Any], bool]:
        _payload, row_count = self._validate_submission(body)
        job_id = sha256(body).hexdigest()
        target = self.input_dir / f"API_{job_id}.json"
        with self._submit_lock:
            previous = self._load_state(job_id)
            if previous is not None:
                source = Path(previous.source_path)
                if (
                    previous.status
                    in {"queued", "running", "retry_wait", "blocked"}
                    and not source.is_file()
                ):
                    temporary = self.input_dir / (
                        f".API_{job_id}.{os.getpid()}.restore.uploading"
                    )
                    try:
                        with temporary.open("wb") as stream:
                            stream.write(body)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temporary, target)
                        previous.source_path = str(target)
                        self._save_state(previous)
                    finally:
                        temporary.unlink(missing_ok=True)
                return self.public_job(previous), False

            temporary = self.input_dir / (
                f".API_{job_id}.{os.getpid()}.{threading.get_ident()}.uploading"
            )
            state = server_worker.JobState(
                source_path=str(target),
                source_name=target.name,
                sha256=job_id,
                status="queued",
                submitted_at=_utc_now(),
                row_count=row_count,
                stage="queued",
            )
            try:
                with temporary.open("wb") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._save_state(state)
                os.replace(temporary, target)
            except Exception:
                temporary.unlink(missing_ok=True)
                (self.jobs_root / f"{job_id}.json").unlink(missing_ok=True)
                raise
        return self.public_job(state), True

    def get_state(self, job_id: str) -> server_worker.JobState:
        state = self._load_state(job_id)
        if state is None:
            raise APIRequestError(
                HTTPStatus.NOT_FOUND,
                "job_not_found",
                "任务不存在",
            )
        return state

    @staticmethod
    def _public_message(state: server_worker.JobState) -> str:
        messages = {
            "queued": "任务已进入处理队列",
            "running": "任务正在处理",
            "retry_wait": "临时服务异常，系统将自动续跑",
            "delivery_retry": "结果已生成，系统正在自动整理交付目录",
            "blocked": "模型鉴权或额度异常，等待服务器恢复",
            "failed": "任务自动重试后仍未完成",
            "invalid_input": "采集表格式或源数据不合格",
            "pending_review": "任务需要人工审核，正式表未覆盖",
            "published": "正式回填表已生成",
            "published_with_warnings": "正式回填表已生成，部分商品已自动隔离",
        }
        return messages.get(state.status, "任务状态已更新")

    def public_job(
        self,
        state_or_id: server_worker.JobState | str,
    ) -> dict[str, Any]:
        state = (
            self.get_state(state_or_id)
            if isinstance(state_or_id, str)
            else state_or_id
        )
        base = f"/api/v1/jobs/{state.sha256}"
        queue_position = state.queue_position
        if state.status in server_worker.ACTIVE_STATUSES:
            active = []
            for path in self.jobs_root.glob("*.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, TypeError, ValueError):
                    continue
                if str(payload.get("status") or "") in server_worker.ACTIVE_STATUSES:
                    active.append(payload)
            active.sort(key=lambda value: (
                str(value.get("submitted_at") or ""),
                str(value.get("sha256") or ""),
            ))
            queue_position = next(
                (
                    index
                    for index, value in enumerate(active, start=1)
                    if value.get("sha256") == state.sha256
                ),
                queue_position,
            )
        return {
            "id": state.sha256,
            "status": state.status,
            "message": self._public_message(state),
            "source_name": state.source_name or Path(state.source_path).name,
            "row_count": state.row_count,
            "attempt": state.attempt,
            "submitted_at": state.submitted_at,
            "started_at": state.started_at,
            "finished_at": state.finished_at,
            "next_retry_at": state.next_retry_at,
            "failure_kind": state.failure_kind,
            "stage": state.stage,
            "progress": {
                "completed": state.progress_current,
                "total": state.progress_total,
            },
            "queue_position": queue_position,
            "isolated_count": len(state.isolated_product_ids or []),
            "isolated_product_ids": list(state.isolated_product_ids or []),
            "blocker_reason": state.blocker_reason,
            "pending_product_ids": list(state.pending_product_ids or []),
            "links": {
                "self": base,
                "result": f"{base}/result",
                "review": f"{base}/review",
            },
        }

    def health(self, max_age_seconds: float) -> dict[str, Any]:
        try:
            worker = self.worker_health_func(max_age_seconds)
        except Exception:
            worker = {
                "healthy": False,
                "status": "unavailable",
                "message": "Worker 状态暂时不可用",
            }
        return {
            "api": {"healthy": True, "status": "ready"},
            "worker": worker,
        }

    @staticmethod
    def _inside(directory: Path, candidate: Path) -> bool:
        try:
            candidate.resolve().relative_to(directory.resolve())
            return True
        except ValueError:
            return False

    def artifact_path(self, job_id: str, kind: str) -> Path:
        state = self.get_state(job_id)
        if state.status in {
            "queued",
            "running",
            "retry_wait",
            "delivery_retry",
            "blocked",
        }:
            raise APIRequestError(
                HTTPStatus.ACCEPTED,
                "result_not_ready",
                "任务尚未产生可下载结果",
                retry_after=15,
            )
        if not state.delivery_path:
            raise APIRequestError(
                HTTPStatus.CONFLICT,
                "artifact_unavailable",
                "该任务没有可下载的正式交付物",
            )
        delivery = Path(state.delivery_path)
        if (
            not self._inside(self.deliveries_root, delivery)
            or not delivery.is_dir()
        ):
            raise APIRequestError(
                HTTPStatus.NOT_FOUND,
                "artifact_not_found",
                "任务交付文件已不存在",
            )

        candidates: list[Path] = []
        if kind == "result":
            if state.status not in {"published", "published_with_warnings"}:
                raise APIRequestError(
                    HTTPStatus.CONFLICT,
                    "formal_result_unavailable",
                    "该任务没有正式回填表",
                )
            if state.output_path:
                candidates.append(delivery / Path(state.output_path).name)
            candidates.append(delivery / "跨境电商自动化回填表.json")
        elif kind == "review":
            if state.review_path:
                candidates.append(delivery / Path(state.review_path).name)
            candidates.extend([
                delivery / "终审包.html",
                delivery / "管理员人工审核.html",
            ])
        else:
            raise APIRequestError(
                HTTPStatus.NOT_FOUND,
                "artifact_not_found",
                "交付物不存在",
            )
        for candidate in candidates:
            if self._inside(delivery, candidate) and candidate.is_file():
                return candidate
        raise APIRequestError(
            HTTPStatus.NOT_FOUND,
            "artifact_not_found",
            "交付包中没有找到请求的文件",
        )


__all__ = [
    "APIRequestError",
    "CLIENT_KEY_ENV",
    "DEFAULT_INPUT_DIR",
    "DEFAULT_MAX_ROWS",
    "JOB_ID_RE",
    "JobAPIService",
    "SlidingWindowLimiter",
    "load_client_api_key",
]
