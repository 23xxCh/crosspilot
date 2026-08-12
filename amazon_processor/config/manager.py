"""Loopback-only HTTP shell for the local configuration manager."""
from __future__ import annotations

from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

from .control import (
    ConfigurationConflict,
    ConfigurationError,
    public_state,
    restore_backup,
    save_payload,
    test_model_routes,
    validate_payload,
)
from .locking import ProcessBusyError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
HTML_PATH = PROJECT_ROOT / "config" / "manager.html"
MAX_BODY_BYTES = 5 * 1024 * 1024
PAGE_GONE_TIMEOUT_S = 120


class ConfigHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, ConfigRequestHandler)
        self.session_token = secrets.token_urlsafe(32)
        self.bootstrap_used = False
        self.last_seen = time.monotonic()
        host, port = self.server_address
        self.origin = f"http://{host}:{port}"

    @property
    def bootstrap_url(self) -> str:
        return f"{self.origin}/?token={self.session_token}"


class ConfigRequestHandler(BaseHTTPRequestHandler):
    server: ConfigHTTPServer

    def log_message(self, _format: str, *_args) -> None:
        """Do not log session URLs, request bodies, or credential metadata."""

    def _security_headers(self, *, html: bool = False) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if html:
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'none'",
            )

    def _cookie_token(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get("config_session")
        return morsel.value if morsel else ""

    def _host_is_valid(self) -> bool:
        expected = urlsplit(self.server.origin).netloc
        return self.headers.get("Host", "") == expected

    def _authenticated(self) -> bool:
        return (
            self._host_is_valid()
            and secrets.compare_digest(
                self._cookie_token(),
                self.server.session_token,
            )
        )

    def _write_json(self, status: int, payload: object) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, message: str) -> None:
        self._write_json(status, {"ok": False, "error": message})

    def _read_json(self) -> dict:
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ConfigurationError("请求长度不合法") from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise ConfigurationError("请求内容过大")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigurationError("请求 JSON 无法解析") from exc
        if not isinstance(payload, dict):
            raise ConfigurationError("请求 JSON 根节点必须是对象")
        return payload

    def _bootstrap(self, token: str) -> None:
        if (
            self.server.bootstrap_used
            or not token
            or not secrets.compare_digest(token, self.server.session_token)
        ):
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        self.server.bootstrap_used = True
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            "config_session="
            + self.server.session_token
            + "; Path=/; HttpOnly; SameSite=Strict",
        )
        self._security_headers()
        self.end_headers()

    def do_GET(self) -> None:
        self.server.last_seen = time.monotonic()
        parsed = urlsplit(self.path)
        if parsed.path == "/" and parse_qs(parsed.query).get("token"):
            self._bootstrap(parse_qs(parsed.query)["token"][0])
            return
        if not self._authenticated():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if parsed.path == "/":
            try:
                data = HTML_PATH.read_bytes()
            except OSError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self._security_headers(html=True)
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/api/state":
            try:
                self._write_json(HTTPStatus.OK, {"ok": True, **public_state()})
            except Exception as exc:
                self._handle_exception(exc)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _post_is_allowed(self) -> bool:
        return (
            self._authenticated()
            and self.headers.get("Origin", "") == self.server.origin
        )

    def do_POST(self) -> None:
        self.server.last_seen = time.monotonic()
        if not self._post_is_allowed():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        path = urlsplit(self.path).path
        try:
            if path == "/api/heartbeat":
                self._write_json(HTTPStatus.OK, {"ok": True})
                return
            if path == "/api/shutdown":
                self._write_json(HTTPStatus.OK, {"ok": True})
                threading.Thread(
                    target=self.server.shutdown,
                    daemon=True,
                ).start()
                return
            if path == "/api/test-routes":
                self._write_json(
                    HTTPStatus.OK,
                    {"ok": True, **test_model_routes()},
                )
                return
            payload = self._read_json()
            if path == "/api/validate":
                result = validate_payload(payload)
            elif path == "/api/save":
                result = save_payload(payload)
            elif path == "/api/restore":
                result = restore_backup(
                    str(payload.get("name") or ""),
                    str(payload.get("revision") or ""),
                )
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._write_json(HTTPStatus.OK, {"ok": True, **result})
        except Exception as exc:
            self._handle_exception(exc)

    def _handle_exception(self, exc: Exception) -> None:
        if isinstance(exc, ConfigurationConflict):
            self._error(HTTPStatus.CONFLICT, str(exc))
        elif isinstance(exc, ProcessBusyError):
            self._error(HTTPStatus.LOCKED, str(exc))
        elif isinstance(exc, ConfigurationError):
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        else:
            self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"配置操作失败: {type(exc).__name__}",
            )


def create_server() -> ConfigHTTPServer:
    if not HTML_PATH.is_file():
        raise FileNotFoundError(f"配置管理页面不存在: {HTML_PATH}")
    return ConfigHTTPServer(("127.0.0.1", 0))


def _watch_for_closed_page(server: ConfigHTTPServer) -> None:
    while True:
        time.sleep(15)
        if time.monotonic() - server.last_seen > PAGE_GONE_TIMEOUT_S:
            server.shutdown()
            return


def serve_config_manager(*, open_browser: bool = True) -> None:
    server = create_server()
    watcher = threading.Thread(
        target=_watch_for_closed_page,
        args=(server,),
        daemon=True,
    )
    watcher.start()
    if open_browser:
        webbrowser.open(server.bootstrap_url)
    print(f"配置管理中心: {server.origin}", flush=True)
    print("关闭页面后服务会自动退出；也可按 Ctrl+C 结束。", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


__all__ = [
    "ConfigHTTPServer",
    "create_server",
    "serve_config_manager",
]
