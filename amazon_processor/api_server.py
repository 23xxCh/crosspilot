"""Small authenticated HTTP job API in front of the unattended worker.

The API only validates and durably queues collection-table JSON.  It never
runs the paid processing pipeline inside an HTTP request; the existing worker
owns processing, retries, validation, and immutable delivery snapshots.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .config.credentials import DEFAULT_ENV_PATH, read_env_values
from .markets import normalize_market_code
from .schema import (
    AMAZON_JSON_INPUT_FIELDS,
    AMAZON_JSON_LEGACY_INPUT_FIELDS,
    validate_columnar_payload,
)
from . import server_worker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = server_worker.DEFAULT_INBOX
CLIENT_KEY_ENV = "AMAZON_PROCESSOR_API_KEY"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MAX_BODY_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_ROWS = 10_000
JOB_ID_RE = re.compile(r"^[0-9a-f]{64}$")
JOB_STATUS_LABELS = {
    "queued": "排队",
    "running": "处理中",
    "retry_wait": "等待自动重试",
    "delivery_retry": "正在整理结果",
    "blocked": "需要处理",
    "failed": "处理失败",
    "invalid_input": "输入不合格",
    "pending_review": "等待人工审核",
    "published": "已完成",
    "published_with_warnings": "已完成（有隔离商品）",
}


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
            )

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


class JobAPIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        service: JobAPIService,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        worker_max_age_seconds: float = 120.0,
    ) -> None:
        self.service = service
        self.max_body_bytes = max(1, int(max_body_bytes))
        self.worker_max_age_seconds = max(1.0, float(worker_max_age_seconds))
        super().__init__(address, JobAPIRequestHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(30)
        return connection, address


class JobAPIRequestHandler(BaseHTTPRequestHandler):
    server: JobAPIServer
    server_version = "AmazonProcessorAPI/1.0"
    sys_version = ""

    def log_message(self, message: str, *args: object) -> None:
        print(
            f"[API] {self.client_address[0]} " + (message % args),
            flush=True,
        )

    def _headers(self, *, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")

    def _write_json(
        self,
        status: int,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self._headers(
            content_type="application/json; charset=utf-8",
            length=len(body),
        )
        self.end_headers()
        self.wfile.write(body)

    def _write_error(self, error: APIRequestError) -> None:
        headers = {}
        if error.retry_after:
            headers["Retry-After"] = str(error.retry_after)
        if error.status == HTTPStatus.UNAUTHORIZED:
            headers["WWW-Authenticate"] = "X-API-Key"
        self._write_json(
            error.status,
            {"error": {"code": error.code, "message": error.message}},
            headers=headers,
        )

    def _authenticate(self) -> bool:
        if self.server.service.authenticated(self.headers.get("X-API-Key", "")):
            return True
        self._write_error(APIRequestError(
            HTTPStatus.UNAUTHORIZED,
            "unauthorized",
            "缺少或无效的 X-API-Key",
        ))
        return False

    def _check_rate(self, *, write: bool) -> bool:
        try:
            remaining, reset = self.server.service.check_rate(write=write)
        except APIRequestError as exc:
            self._write_error(exc)
            return False
        self._rate_headers = {
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset),
        }
        return True

    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise APIRequestError(
                HTTPStatus.LENGTH_REQUIRED,
                "content_length_required",
                "请求必须包含 Content-Length",
            )
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise APIRequestError(
                HTTPStatus.BAD_REQUEST,
                "invalid_content_length",
                "Content-Length 无效",
            ) from exc
        if length <= 0:
            raise APIRequestError(
                HTTPStatus.BAD_REQUEST,
                "empty_body",
                "请求体不能为空",
            )
        if length > self.server.max_body_bytes:
            raise APIRequestError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "payload_too_large",
                "采集表超过接口允许的大小",
            )
        body = self.rfile.read(length)
        if len(body) != length:
            raise APIRequestError(
                HTTPStatus.BAD_REQUEST,
                "incomplete_body",
                "请求体未完整传输",
            )
        return body

    def _serve_file(self, path: Path) -> None:
        content_type = (
            "application/json; charset=utf-8"
            if path.suffix.lower() == ".json"
            else "text/html; charset=utf-8"
        )
        self.send_response(HTTPStatus.OK)
        encoded_name = quote(path.name)
        self.send_header(
            "Content-Disposition",
            f"attachment; filename*=UTF-8''{encoded_name}",
        )
        self._headers(content_type=content_type, length=path.stat().st_size)
        self.end_headers()
        with path.open("rb") as stream:
            shutil.copyfileobj(stream, self.wfile, length=1024 * 1024)

    def _handle_exception(self, exc: Exception) -> None:
        if isinstance(exc, APIRequestError):
            self._write_error(exc)
            return
        print(f"[API] internal_error: {type(exc).__name__}: {exc}", flush=True)
        self._write_error(APIRequestError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "internal_error",
            "接口内部错误",
        ))

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path != "/api/v1/jobs":
            self._write_error(APIRequestError(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "接口不存在",
            ))
            return
        if not self._authenticate() or not self._check_rate(write=True):
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            self._write_error(APIRequestError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "unsupported_media_type",
                "提交采集表时 Content-Type 必须为 application/json",
            ))
            return
        try:
            job, created = self.server.service.submit(self._read_body())
            status = HTTPStatus.CREATED if created else HTTPStatus.OK
            headers = {"Location": job["links"]["self"], **self._rate_headers}
            self._write_json(status, {"data": job}, headers=headers)
        except Exception as exc:
            self._handle_exception(exc)

    def do_GET(self) -> None:
        if not self._authenticate() or not self._check_rate(write=False):
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/api/v1/health":
            health = self.server.service.health(
                self.server.worker_max_age_seconds
            )
            status = (
                HTTPStatus.OK
                if health["worker"].get("healthy")
                else HTTPStatus.SERVICE_UNAVAILABLE
            )
            self._write_json(
                status,
                {"data": health},
                headers=self._rate_headers,
            )
            return
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) not in {4, 5} or parts[:3] != ["api", "v1", "jobs"]:
            self._write_error(APIRequestError(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "接口不存在",
            ))
            return
        job_id = parts[3]
        try:
            if len(parts) == 4:
                self._write_json(
                    HTTPStatus.OK,
                    {"data": self.server.service.public_job(job_id)},
                    headers=self._rate_headers,
                )
                return
            if parts[4] in {"result", "review"}:
                self._serve_file(
                    self.server.service.artifact_path(job_id, parts[4])
                )
                return
            raise APIRequestError(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "接口不存在",
            )
        except Exception as exc:
            self._handle_exception(exc)

    def do_OPTIONS(self) -> None:
        self._write_error(APIRequestError(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "method_not_allowed",
            "接口不开放浏览器跨域调用",
        ))


def create_job_api_server(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    input_dir: Path = DEFAULT_INPUT_DIR,
    api_key: str | None = None,
    jobs_root: Path | None = None,
    deliveries_root: Path | None = None,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    worker_max_age_seconds: float = 120.0,
    worker_health_func: Callable[[float], dict] | None = None,
    submit_limit_per_minute: int = 10,
    read_limit_per_minute: int = 120,
) -> JobAPIServer:
    service = JobAPIService(
        api_key=api_key if api_key is not None else load_client_api_key(),
        input_dir=input_dir,
        jobs_root=jobs_root,
        deliveries_root=deliveries_root,
        worker_health_func=worker_health_func,
        submit_limit_per_minute=submit_limit_per_minute,
        read_limit_per_minute=read_limit_per_minute,
    )
    return JobAPIServer(
        (host, int(port)),
        service=service,
        max_body_bytes=max_body_bytes,
        worker_max_age_seconds=worker_max_age_seconds,
    )


def serve_job_api(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    input_dir: Path = DEFAULT_INPUT_DIR,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    worker_max_age_seconds: float = 120.0,
) -> None:
    server = create_job_api_server(
        host=host,
        port=port,
        input_dir=input_dir,
        max_body_bytes=max_body_bytes,
        worker_max_age_seconds=worker_max_age_seconds,
    )
    address, bound_port = server.server_address[:2]
    print(f"Amazon 任务 API: http://{address}:{bound_port}/api/v1", flush=True)
    if address not in {"127.0.0.1", "::1", "localhost"}:
        print(
            "[WARN] 当前监听非本机地址，请配合 Windows 防火墙、VPN 或 HTTPS 反向代理",
            flush=True,
        )
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def api_health_check(
    *,
    url: str = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/api/v1/health",
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Check API liveness; a degraded Worker still proves the API is alive."""
    key = _validate_client_api_key(load_client_api_key())
    request = Request(url, headers={"X-API-Key": key})
    try:
        with urlopen(request, timeout=max(0.5, timeout_seconds)) as response:
            body = response.read()
    except HTTPError as exc:
        if exc.code != HTTPStatus.SERVICE_UNAVAILABLE:
            raise
        body = exc.read()
    except URLError as exc:
        raise ConnectionError("Amazon 任务 API 无法连接") from exc
    payload = json.loads(body.decode("utf-8"))
    api = (payload.get("data") or {}).get("api") or {}
    if api.get("healthy") is not True:
        raise ConnectionError("Amazon 任务 API 健康响应无效")
    return payload["data"]


def system_overview(
    *,
    jobs_root: Path | None = None,
    worker_health_func: Callable[[float], dict] | None = None,
    api_health_func: Callable[[], dict] | None = None,
) -> dict[str, Any]:
    """Return a small operator-facing summary without logs or secrets."""
    worker_check = worker_health_func or server_worker.worker_health
    api_check = api_health_func or api_health_check
    try:
        worker = worker_check(120.0)
    except Exception:
        worker = {"healthy": False, "status": "unavailable"}
    try:
        api = api_check().get("api") or {"healthy": False}
    except Exception:
        api = {"healthy": False, "status": "stopped"}

    root = Path(jobs_root or server_worker.JOBS_ROOT)
    counts = {status: 0 for status in JOB_STATUS_LABELS}
    latest: dict[str, Any] | None = None
    latest_key = ""
    if root.is_dir():
        for path in root.glob("*.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                continue
            status = str(state.get("status") or "")
            counts[status] = counts.get(status, 0) + 1
            key = str(
                state.get("finished_at")
                or state.get("started_at")
                or state.get("submitted_at")
                or ""
            )
            if key >= latest_key:
                latest_key = key
                latest = {
                    "status": status,
                    "source_name": str(state.get("source_name") or "")
                    or Path(str(state.get("source_path") or "")).name,
                    "row_count": int(state.get("row_count") or 0),
                    "attempt": int(state.get("attempt") or 0),
                    "stage": str(state.get("stage") or ""),
                    "progress_current": int(
                        state.get("progress_current") or 0
                    ),
                    "progress_total": int(state.get("progress_total") or 0),
                    "queue_position": int(state.get("queue_position") or 0),
                    "isolated_count": len(
                        state.get("isolated_product_ids") or []
                    ),
                    "blocker_reason": str(
                        state.get("blocker_reason") or ""
                    ),
                    "updated_at": key,
                }
    healthy = bool(
        worker.get("healthy")
        and worker.get("ready", True)
        and api.get("healthy")
    )
    return {
        "healthy": healthy,
        "worker": worker,
        "api": api,
        "counts": counts,
        "latest": latest,
        "input_dir": str(server_worker.DEFAULT_INBOX),
        "delivery_dir": str(
            server_worker.operator_workspace.paths_for(
                server_worker.OPERATOR_ROOT
            ).results
        ),
        "operator_status_path": str(
            server_worker.operator_workspace.paths_for(
                server_worker.OPERATOR_ROOT
            ).status
        ),
    }


def format_system_overview(overview: dict[str, Any]) -> str:
    """Render the status in plain Chinese for a double-click console."""
    worker_ok = bool((overview.get("worker") or {}).get("healthy"))
    worker_ready = bool((overview.get("worker") or {}).get("ready", True))
    api_ok = bool((overview.get("api") or {}).get("healthy"))
    lines = [
        "Amazon 自动处理系统",
        "=" * 36,
        f"总体状态：{'运行正常' if overview.get('healthy') else '需要检查'}",
        "自动处理："
        + (
            "正常"
            if worker_ok and worker_ready
            else "需要处理"
            if worker_ok
            else "未启动或异常"
        ),
        f"调用接口：{'正常' if api_ok else '未启动或异常'}",
        "",
        "任务统计：",
    ]
    counts = overview.get("counts") or {}
    visible = [
        "queued",
        "running",
        "retry_wait",
        "delivery_retry",
        "pending_review",
        "blocked",
        "invalid_input",
        "failed",
        "published",
        "published_with_warnings",
    ]
    for status in visible:
        count = int(counts.get(status) or 0)
        if count or status in {"queued", "running", "retry_wait"}:
            lines.append(f"  {JOB_STATUS_LABELS[status]}：{count}")
    latest = overview.get("latest")
    if isinstance(latest, dict):
        label = JOB_STATUS_LABELS.get(
            str(latest.get("status") or ""),
            "状态未知",
        )
        lines.extend([
            "",
            "最近任务：",
            f"  文件：{latest.get('source_name') or '未知'}",
            f"  商品：{int(latest.get('row_count') or 0)} 个",
            f"  状态：{label}",
        ])
        if latest.get("progress_total"):
            lines.append(
                "  进度："
                f"{int(latest.get('progress_current') or 0)}/"
                f"{int(latest.get('progress_total') or 0)}"
            )
        if latest.get("isolated_count"):
            lines.append(
                f"  隔离商品：{int(latest.get('isolated_count') or 0)} 个"
            )
        if latest.get("blocker_reason"):
            lines.append(f"  阻塞原因：{latest.get('blocker_reason')}")
    else:
        lines.extend(["", "最近任务：暂无"])
    if not overview.get("healthy"):
        lines.extend([
            "",
            "建议操作：打开 00_常用入口，双击 04_一键安装服务器.bat；",
            "如果已经安装，等待 1 分钟后再查看。",
        ])
    lines.extend([
        "",
        f"采集表入口：{overview.get('input_dir')}",
        f"结果目录：{overview.get('delivery_dir')}",
    ])
    return "\n".join(lines)


__all__ = [
    "APIRequestError",
    "CLIENT_KEY_ENV",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "JobAPIService",
    "api_health_check",
    "create_job_api_server",
    "format_system_overview",
    "load_client_api_key",
    "serve_job_api",
    "system_overview",
]
