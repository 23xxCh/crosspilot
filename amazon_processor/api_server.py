"""Small authenticated HTTP job API in front of the unattended worker.

The API only validates and durably queues collection-table JSON.  It never
runs the paid processing pipeline inside an HTTP request; the existing worker
owns processing, retries, validation, and immutable delivery snapshots.
"""
from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .api_jobs import (
    APIRequestError as APIRequestError,
    CLIENT_KEY_ENV as CLIENT_KEY_ENV,
    DEFAULT_INPUT_DIR as DEFAULT_INPUT_DIR,
    DEFAULT_MAX_ROWS as DEFAULT_MAX_ROWS,
    JOB_ID_RE as JOB_ID_RE,
    JobAPIService as JobAPIService,
    SlidingWindowLimiter as SlidingWindowLimiter,
    _utc_now as _utc_now,
    _validate_client_api_key as _validate_client_api_key,
    load_client_api_key as load_client_api_key,
)
from .system_status import (
    JOB_STATUS_LABELS as JOB_STATUS_LABELS,
    format_system_overview as format_system_overview,
    system_overview as system_overview,
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MAX_BODY_BYTES = 20 * 1024 * 1024

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
