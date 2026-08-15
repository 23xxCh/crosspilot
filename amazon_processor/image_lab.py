"""Local-only Agnes image-to-image workbench."""
from __future__ import annotations

import base64
import binascii
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import ipaddress
import json
from pathlib import Path
import secrets
import socket
import threading
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urljoin, urlsplit
import uuid
import webbrowser

from PIL import Image, UnidentifiedImageError
import requests

from .config.credentials import CredentialStore
from .config.models import get_model_registry
from .config.prompts import get_prompt_registry
from .providers import (
    AgnesProvider,
    CongestionPolicy,
    ProviderError,
    load_provider_config,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = PROJECT_ROOT / "config" / "image_lab.html"
OUTPUT_ROOT = PROJECT_ROOT / "02_处理结果" / "生图测试台"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_RESULT_BYTES = 30 * 1024 * 1024
MAX_BODY_BYTES = 15 * 1024 * 1024
PAGE_GONE_TIMEOUT_S = 30 * 60
ALLOWED_MODELS = (
    "agnes-image-2.1-flash",
    "agnes-image-2.0-flash",
)
ALLOWED_SIZES = ("1K", "2K")
ALLOWED_RATIOS = (
    "1:1",
    "3:4",
    "4:3",
    "16:9",
    "9:16",
    "2:3",
    "3:2",
    "21:9",
)
MARKETS = (
    {"site": "US", "label": "美国 · 英语", "language": "English (US)"},
    {"site": "UK", "label": "英国 · 英语", "language": "English (UK)"},
    {"site": "CA", "label": "加拿大 · 英语", "language": "English (Canada)"},
    {"site": "MX", "label": "墨西哥 · 西班牙语", "language": "Spanish (Mexico)"},
    {"site": "ES", "label": "西班牙 · 西班牙语", "language": "Spanish (Spain)"},
    {"site": "BR", "label": "巴西 · 葡萄牙语", "language": "Portuguese (Brazil)"},
    {"site": "DE", "label": "德国 · 德语", "language": "German (Germany)"},
    {"site": "FR", "label": "法国 · 法语", "language": "French (France)"},
    {"site": "IT", "label": "意大利 · 意大利语", "language": "Italian (Italy)"},
)
_RATIO_VALUES = {
    "1:1": 1.0,
    "3:4": 3 / 4,
    "4:3": 4 / 3,
    "16:9": 16 / 9,
    "9:16": 9 / 16,
    "2:3": 2 / 3,
    "3:2": 3 / 2,
    "21:9": 21 / 9,
}
_MIME_BY_FORMAT = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}
_PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


class ImageLabValidationError(ValueError):
    """A safe validation error that can be shown in the local UI."""


@dataclass(frozen=True)
class ImageInfo:
    data: bytes
    mime: str
    extension: str
    width: int
    height: int


@dataclass(frozen=True)
class GenerationRequest:
    source: ImageInfo
    source_kind: str
    source_name: str
    source_url: str
    model: str
    prompt: str
    target_language: str
    site: str
    size: str
    ratio: str
    fingerprint: str


def closest_ratio(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        raise ImageLabValidationError("图片宽高必须大于 0")
    actual = width / height
    return min(
        ALLOWED_RATIOS,
        key=lambda item: abs(_RATIO_VALUES[item] - actual),
    )


def inspect_image(data: bytes, *, max_bytes: int = MAX_IMAGE_BYTES) -> ImageInfo:
    if not data or len(data) > max_bytes:
        raise ImageLabValidationError(
            f"图片必须大于 0 且不超过 {max_bytes // (1024 * 1024)} MB"
        )
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image_format = str(image.format or "").upper()
            width, height = image.size
    except (OSError, UnidentifiedImageError) as exc:
        raise ImageLabValidationError("文件不是可解码的图片") from exc
    if image_format not in _MIME_BY_FORMAT:
        raise ImageLabValidationError("只支持 JPG、PNG、WebP 图片")
    if width <= 0 or height <= 0 or width * height > 80_000_000:
        raise ImageLabValidationError("图片分辨率不合法或过大")
    mime, extension = _MIME_BY_FORMAT[image_format]
    return ImageInfo(data, mime, extension, width, height)


def decode_data_uri(value: str) -> ImageInfo:
    header, separator, encoded = str(value or "").partition(",")
    if not separator or not header.startswith("data:image/") or ";base64" not in header:
        raise ImageLabValidationError("本地图片数据格式不正确")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageLabValidationError("本地图片 Base64 无法解析") from exc
    return inspect_image(raw)


def _assert_public_https_url(value: str) -> str:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise ImageLabValidationError("图片链接必须是无需登录的公共 HTTPS 地址")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)
        }
    except OSError as exc:
        raise ImageLabValidationError("图片链接域名无法解析") from exc
    if not addresses:
        raise ImageLabValidationError("图片链接域名没有可用地址")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        # Clash and similar Windows proxy clients commonly map every public
        # hostname into RFC 2544's 198.18.0.0/15 fake-IP range. Requests still
        # go through the proxy by hostname, so this range is safe to allow here.
        if not (ip.is_global or ip in _PROXY_FAKE_IP_NETWORK):
            raise ImageLabValidationError("图片链接不能指向本机或内网地址")
    return url


def download_public_image(
    url: str,
    *,
    max_bytes: int = MAX_IMAGE_BYTES,
    session: requests.Session | None = None,
) -> ImageInfo:
    current = _assert_public_https_url(url)
    client = session or requests.Session()
    for _redirect in range(4):
        try:
            response = client.get(
                current,
                stream=True,
                allow_redirects=False,
                timeout=(10, 60),
                headers={"User-Agent": "AmazonProcessor-ImageLab/1.0"},
            )
        except requests.Timeout as exc:
            raise ImageLabValidationError("图片链接下载超时") from exc
        except requests.RequestException as exc:
            raise ImageLabValidationError("图片链接下载失败") from exc
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise ImageLabValidationError("图片链接重定向缺少目标地址")
            current = _assert_public_https_url(urljoin(current, location))
            continue
        try:
            try:
                response.raise_for_status()
            except requests.RequestException as exc:
                raise ImageLabValidationError(
                    f"图片链接返回 HTTP {response.status_code}"
                ) from exc
            content_type = str(response.headers.get("Content-Type") or "")
            if content_type and not content_type.lower().startswith("image/"):
                raise ImageLabValidationError("链接返回的不是图片")
            chunks = []
            length = 0
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                length += len(chunk)
                if length > max_bytes:
                    raise ImageLabValidationError(
                        f"图片超过 {max_bytes // (1024 * 1024)} MB"
                    )
                chunks.append(chunk)
        finally:
            response.close()
        return inspect_image(b"".join(chunks), max_bytes=max_bytes)
    raise ImageLabValidationError("图片链接重定向次数过多")


def _data_uri(image: ImageInfo) -> str:
    encoded = base64.b64encode(image.data).decode("ascii")
    return f"data:{image.mime};base64,{encoded}"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ProviderError):
        return exc.to_dict()
    return {
        "type": type(exc).__name__,
        "status_code": None,
        "retryable": False,
        "detail": str(exc or type(exc).__name__)[:300],
    }


class ImageLabService:
    """Validate requests, run one-at-a-time Agnes jobs, and persist results."""

    def __init__(
        self,
        *,
        output_root: Path = OUTPUT_ROOT,
        provider_builder: Callable[[str], AgnesProvider] | None = None,
        source_downloader: Callable[..., ImageInfo] = download_public_image,
        result_downloader: Callable[..., ImageInfo] = download_public_image,
    ) -> None:
        self.output_root = Path(output_root)
        self._provider_builder = provider_builder or self._build_provider
        self._source_downloader = source_downloader
        self._result_downloader = result_downloader
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="agnes-image-lab",
        )
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._inputs: dict[str, GenerationRequest] = {}
        self._active_fingerprints: dict[str, str] = {}
        self._registry = get_model_registry()
        self._agnes_target = next(
            (
                target
                for target in self._registry.routes("image")
                if target.provider == "agnes"
            ),
            None,
        )

    def _credential_value(self) -> str:
        if self._agnes_target is None:
            return ""
        return CredentialStore(self._registry).value(
            self._agnes_target.credential
        ).strip()

    def _build_provider(self, model: str) -> AgnesProvider:
        if self._agnes_target is None:
            raise ImageLabValidationError("配置中没有 Agnes 生图线路")
        api_key = self._credential_value()
        if not api_key:
            raise ImageLabValidationError(
                "Agnes API 密钥未配置，请先双击 00_常用入口\\03_配置与模型.bat"
            )
        return AgnesProvider(
            api_key,
            image_model=model,
            base_url=self._agnes_target.base_url,
            congestion_policy=CongestionPolicy.from_mapping(
                load_provider_config()
            ),
        )

    def public_state(self) -> dict[str, Any]:
        target = self._agnes_target
        return {
            "models": list(ALLOWED_MODELS),
            "markets": list(MARKETS),
            "sizes": list(ALLOWED_SIZES),
            "ratios": list(ALLOWED_RATIOS),
            "default_prompt_template": get_prompt_registry().get(
                "images.manual_edit"
            ),
            "endpoint": target.base_url if target else "未配置",
            "credential_configured": bool(self._credential_value()),
            "output_root": str(self.output_root.resolve()),
        }

    def _prepare_request(self, payload: dict[str, Any]) -> GenerationRequest:
        model = str(payload.get("model") or "").strip()
        if model not in ALLOWED_MODELS:
            raise ImageLabValidationError("不支持的 Agnes 生图模型")
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt or len(prompt) > 8_000:
            raise ImageLabValidationError("Prompt 不能为空且不能超过 8000 字符")
        size = str(payload.get("size") or "1K").strip()
        if size not in ALLOWED_SIZES:
            raise ImageLabValidationError("输出尺寸只支持 1K 或 2K")
        site = str(payload.get("site") or "US").strip().upper()
        market = next((item for item in MARKETS if item["site"] == site), None)
        if market is None:
            raise ImageLabValidationError("不支持的目标站点")
        target_language = str(
            payload.get("target_language") or market["language"]
        ).strip()
        if not target_language or len(target_language) > 80:
            raise ImageLabValidationError("目标语言不合法")

        source_kind = str(payload.get("source_kind") or "data")
        source_url = ""
        if source_kind == "data":
            source = decode_data_uri(str(payload.get("image_data") or ""))
        elif source_kind == "url":
            source_url = _assert_public_https_url(
                str(payload.get("image_url") or "")
            )
            source = self._source_downloader(source_url)
        else:
            raise ImageLabValidationError("图片来源类型无效")

        ratio = str(payload.get("ratio") or "auto").strip()
        if ratio == "auto":
            ratio = closest_ratio(source.width, source.height)
        if ratio not in ALLOWED_RATIOS:
            raise ImageLabValidationError("输出宽高比不受支持")
        fingerprint = sha256(
            b"\0".join([
                source.data,
                model.encode("utf-8"),
                prompt.encode("utf-8"),
                size.encode("ascii"),
                ratio.encode("ascii"),
            ])
        ).hexdigest()
        return GenerationRequest(
            source=source,
            source_kind=source_kind,
            source_name=str(payload.get("file_name") or "原图")[:160],
            source_url=source_url,
            model=model,
            prompt=prompt,
            target_language=target_language,
            site=site,
            size=size,
            ratio=ratio,
            fingerprint=fingerprint,
        )

    def start_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = self._prepare_request(payload)
        with self._lock:
            existing = self._active_fingerprints.get(request.fingerprint)
            if existing and self._jobs.get(existing, {}).get("status") in {
                "queued",
                "running",
            }:
                return {"job_id": existing, "deduplicated": True}
            job_id = uuid.uuid4().hex
            now = time.time()
            self._jobs[job_id] = {
                "job_id": job_id,
                "status": "queued",
                "model": request.model,
                "site": request.site,
                "target_language": request.target_language,
                "size": request.size,
                "ratio": request.ratio,
                "source": {
                    "kind": request.source_kind,
                    "name": request.source_name,
                    "width": request.source.width,
                    "height": request.source.height,
                    "bytes": len(request.source.data),
                },
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "elapsed_s": 0.0,
                "attempts": 0,
                "retries": 0,
                "http_status": None,
                "error": None,
                "result_path": "",
                "record_path": "",
                "provider_url": "",
            }
            self._inputs[job_id] = request
            self._active_fingerprints[request.fingerprint] = job_id
        self._executor.submit(self._run_job, job_id)
        return {"job_id": job_id, "deduplicated": False}

    def _record_attempt(self, job_id: str, **event: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["attempts"] += 1
            if event.get("retry"):
                job["retries"] += 1
            if event.get("status_code") is not None:
                job["http_status"] = event["status_code"]

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            request = self._inputs[job_id]
            job = self._jobs[job_id]
            job["status"] = "running"
            job["started_at"] = time.time()
        try:
            provider = self._provider_builder(request.model)
            provider.set_attempt_hook(
                lambda **event: self._record_attempt(job_id, **event)
            )
            generated_url = str(
                provider.call_image_gen(
                    _data_uri(request.source),
                    size=request.size,
                    retries=2,
                    prompt_override=request.prompt,
                    ratio=request.ratio,
                )
                or ""
            ).strip()
            if not generated_url:
                raise ImageLabValidationError("Agnes 成功响应中没有生成图片")
            with self._lock:
                self._jobs[job_id]["provider_url"] = generated_url
            result = self._result_downloader(
                generated_url,
                max_bytes=MAX_RESULT_BYTES,
            )
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_dir = self.output_root / f"{stamp}_{job_id[:6]}"
            output_dir.mkdir(parents=True, exist_ok=False)
            source_path = output_dir / f"原图{request.source.extension}"
            result_path = output_dir / f"生成图{result.extension}"
            source_path.write_bytes(request.source.data)
            result_path.write_bytes(result.data)
            record_path = output_dir / "生成记录.json"
            record = {
                "version": 1,
                "job_id": job_id,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "model": request.model,
                "endpoint": (
                    self._agnes_target.base_url if self._agnes_target else ""
                ),
                "site": request.site,
                "target_language": request.target_language,
                "size": request.size,
                "ratio": request.ratio,
                "prompt": request.prompt,
                "source": {
                    "kind": request.source_kind,
                    "name": request.source_name,
                    "url": request.source_url,
                    "file": source_path.name,
                    "width": request.source.width,
                    "height": request.source.height,
                },
                "result": {
                    "provider_url": generated_url,
                    "file": result_path.name,
                    "width": result.width,
                    "height": result.height,
                },
                "attempts": self._jobs[job_id]["attempts"],
                "retries": self._jobs[job_id]["retries"],
                "http_status": self._jobs[job_id]["http_status"],
            }
            _atomic_json(record_path, record)
            with self._lock:
                job = self._jobs[job_id]
                job.update({
                    "status": "succeeded",
                    "result_path": str(result_path.resolve()),
                    "record_path": str(record_path.resolve()),
                })
        except Exception as exc:
            with self._lock:
                job = self._jobs[job_id]
                job["status"] = "failed"
                job["error"] = _safe_error(exc)
                if job["http_status"] is None:
                    job["http_status"] = getattr(exc, "status_code", None)
        finally:
            finished = time.time()
            with self._lock:
                job = self._jobs[job_id]
                job["finished_at"] = finished
                started = job.get("started_at") or job["created_at"]
                job["elapsed_s"] = round(finished - started, 3)
                self._active_fingerprints.pop(request.fingerprint, None)
                self._inputs.pop(job_id, None)

    def public_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                job = dict(self._jobs[job_id])
            except KeyError as exc:
                raise ImageLabValidationError("生图任务不存在") from exc
        if job["status"] in {"queued", "running"}:
            started = job.get("started_at") or job["created_at"]
            job["elapsed_s"] = round(time.time() - started, 1)
        if job["status"] == "succeeded":
            job["image_url"] = f"/api/jobs/{job_id}/image"
            job["download_url"] = f"/api/jobs/{job_id}/download"
        return job

    def public_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            job_ids = sorted(
                self._jobs,
                key=lambda item: self._jobs[item]["created_at"],
                reverse=True,
            )
        return [self.public_job(job_id) for job_id in job_ids[:20]]

    def result_path(self, job_id: str) -> Path:
        job = self.public_job(job_id)
        if job["status"] != "succeeded" or not job["result_path"]:
            raise ImageLabValidationError("该任务还没有可用生成图")
        path = Path(job["result_path"])
        if not path.is_file():
            raise ImageLabValidationError("生成图文件不存在")
        return path

    def has_active_jobs(self) -> bool:
        with self._lock:
            return any(
                job["status"] in {"queued", "running"}
                for job in self._jobs.values()
            )

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)


class ImageLabHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        service: ImageLabService | None = None,
    ) -> None:
        self.service = service or ImageLabService()
        super().__init__(address, ImageLabRequestHandler)
        self.session_token = secrets.token_urlsafe(32)
        self.bootstrap_used = False
        self.last_seen = time.monotonic()
        host, port = self.server_address
        self.origin = f"http://{host}:{port}"

    @property
    def bootstrap_url(self) -> str:
        return f"{self.origin}/?token={self.session_token}"

    def server_close(self) -> None:
        self.service.close()
        super().server_close()


class ImageLabRequestHandler(BaseHTTPRequestHandler):
    server: ImageLabHTTPServer

    def log_message(self, _format: str, *_args: Any) -> None:
        """Do not log session URLs, prompts, image data, or credentials."""

    def _security_headers(self, *, html: bool = False) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if html:
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob: https:; connect-src 'self'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            )

    def _cookie_token(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get("image_lab_session")
        return morsel.value if morsel else ""

    def _authenticated(self) -> bool:
        expected = urlsplit(self.server.origin).netloc
        return (
            self.headers.get("Host", "") == expected
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
            "image_lab_session="
            + self.server.session_token
            + "; Path=/; HttpOnly; SameSite=Strict",
        )
        self._security_headers()
        self.end_headers()

    def _serve_result(self, job_id: str, *, download: bool) -> None:
        try:
            path = self.server.service.result_path(job_id)
            image = inspect_image(path.read_bytes(), max_bytes=MAX_RESULT_BYTES)
        except Exception as exc:
            self._handle_exception(exc)
            return
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", image.mime)
        self.send_header("Content-Length", str(len(data)))
        if download:
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="agnes-result{image.extension}"',
            )
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

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
            self._write_json(
                HTTPStatus.OK,
                {"ok": True, **self.server.service.public_state()},
            )
            return
        if parsed.path == "/api/jobs":
            self._write_json(
                HTTPStatus.OK,
                {"ok": True, "jobs": self.server.service.public_jobs()},
            )
            return
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) in {3, 4} and parts[:2] == ["api", "jobs"]:
            job_id = parts[2]
            if len(parts) == 4 and parts[3] in {"image", "download"}:
                self._serve_result(job_id, download=parts[3] == "download")
                return
            if len(parts) == 3:
                try:
                    job = self.server.service.public_job(job_id)
                except Exception as exc:
                    self._handle_exception(exc)
                    return
                self._write_json(HTTPStatus.OK, {"ok": True, "job": job})
                return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise ImageLabValidationError("请求长度不合法") from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise ImageLabValidationError("上传内容过大")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ImageLabValidationError("请求 JSON 无法解析") from exc
        if not isinstance(payload, dict):
            raise ImageLabValidationError("请求 JSON 根节点必须是对象")
        return payload

    def do_POST(self) -> None:
        self.server.last_seen = time.monotonic()
        if not (
            self._authenticated()
            and self.headers.get("Origin", "") == self.server.origin
        ):
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
            if path == "/api/generate":
                result = self.server.service.start_job(self._read_json())
                self._write_json(HTTPStatus.ACCEPTED, {"ok": True, **result})
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._handle_exception(exc)

    def _handle_exception(self, exc: Exception) -> None:
        if isinstance(exc, ImageLabValidationError):
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        else:
            self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"生图测试台操作失败: {type(exc).__name__}",
            )


def create_server(
    *,
    service: ImageLabService | None = None,
) -> ImageLabHTTPServer:
    if not HTML_PATH.is_file():
        raise FileNotFoundError(f"生图测试台页面不存在: {HTML_PATH}")
    return ImageLabHTTPServer(("127.0.0.1", 0), service=service)


def _watch_for_closed_page(server: ImageLabHTTPServer) -> None:
    while True:
        time.sleep(15)
        if server.service.has_active_jobs():
            continue
        if time.monotonic() - server.last_seen > PAGE_GONE_TIMEOUT_S:
            server.shutdown()
            return


def serve_image_lab(*, open_browser: bool = True) -> None:
    server = create_server()
    watcher = threading.Thread(
        target=_watch_for_closed_page,
        args=(server,),
        daemon=True,
    )
    watcher.start()
    if open_browser:
        webbrowser.open(server.bootstrap_url)
    print(f"Agnes 生图测试台: {server.origin}", flush=True)
    print("关闭页面后空闲 30 分钟会自动退出；也可按 Ctrl+C 结束。", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


__all__ = [
    "ALLOWED_MODELS",
    "ImageLabHTTPServer",
    "ImageLabService",
    "ImageLabValidationError",
    "closest_ratio",
    "create_server",
    "decode_data_uri",
    "download_public_image",
    "inspect_image",
    "serve_image_lab",
]
