from __future__ import annotations

import base64
from http.cookiejar import CookieJar
from io import BytesIO
import json
from pathlib import Path
import threading
import time
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from PIL import Image
import pytest


def _png_bytes(width: int = 40, height: int = 30) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _data_uri(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _payload(data: bytes | None = None) -> dict:
    return {
        "source_kind": "data",
        "image_data": _data_uri(data or _png_bytes()),
        "file_name": "sample.png",
        "model": "agnes-image-2.1-flash",
        "site": "US",
        "target_language": "English (US)",
        "size": "1K",
        "ratio": "auto",
        "prompt": "Translate the product information into English.",
    }


def _wait_for_job(service, job_id: str, timeout: float = 3) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.public_job(job_id)
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    raise AssertionError("image lab job did not finish")


def test_closest_ratio_uses_supported_agnes_ratio() -> None:
    from amazon_processor.image_lab import closest_ratio

    assert closest_ratio(1600, 900) == "16:9"
    assert closest_ratio(900, 1600) == "9:16"
    assert closest_ratio(1000, 1000) == "1:1"


def test_data_uri_validation_accepts_supported_image_and_rejects_bad_data() -> None:
    from amazon_processor.image_lab import (
        ImageLabValidationError,
        decode_data_uri,
    )

    info = decode_data_uri(_data_uri(_png_bytes(80, 50)))
    assert (info.mime, info.width, info.height) == ("image/png", 80, 50)

    with pytest.raises(ImageLabValidationError):
        decode_data_uri("data:image/png;base64,not-base64")


def test_public_url_allows_windows_proxy_fake_ip_but_rejects_localhost(
    monkeypatch,
) -> None:
    from amazon_processor import image_lab

    monkeypatch.setattr(
        image_lab.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("198.18.1.5", 443))],
    )
    assert image_lab._assert_public_https_url(
        "https://images.example/product.png"
    ).startswith("https://")

    monkeypatch.setattr(
        image_lab.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("127.0.0.1", 443))],
    )
    with pytest.raises(image_lab.ImageLabValidationError):
        image_lab._assert_public_https_url(
            "https://localhost.example/product.png"
        )


def test_image_lab_job_deduplicates_active_request_and_persists_result(
    tmp_path: Path,
) -> None:
    from amazon_processor.image_lab import ImageLabService, inspect_image

    release = threading.Event()
    calls = []

    class FakeProvider:
        def set_attempt_hook(self, hook):
            self.hook = hook

        def call_image_gen(self, image_url, **kwargs):
            calls.append((image_url, kwargs))
            self.hook(
                operation="image_gen",
                provider="agnes",
                status_code=200,
                ok=True,
                retry=False,
                error=None,
                rate_wait_s=0,
            )
            release.wait(timeout=2)
            return "https://generated.example/result.png"

    result_info = inspect_image(_png_bytes(64, 64))
    service = ImageLabService(
        output_root=tmp_path,
        provider_builder=lambda _model: FakeProvider(),
        result_downloader=lambda *_args, **_kwargs: result_info,
    )
    try:
        first = service.start_job(_payload())
        second = service.start_job(_payload())
        assert second == {"job_id": first["job_id"], "deduplicated": True}

        release.set()
        job = _wait_for_job(service, first["job_id"])
        assert job["status"] == "succeeded"
        assert job["http_status"] == 200
        assert Path(job["result_path"]).is_file()
        record = json.loads(Path(job["record_path"]).read_text(encoding="utf-8"))
        assert record["model"] == "agnes-image-2.1-flash"
        assert record["ratio"] == "4:3"
        assert "api_key" not in json.dumps(record).lower()
        assert len(calls) == 1
        assert calls[0][0].startswith("data:image/png;base64,")
        assert calls[0][1]["prompt_override"].startswith("Translate")
        assert calls[0][1]["ratio"] == "4:3"
    finally:
        service.close()


def test_image_lab_failure_keeps_safe_provider_error(tmp_path: Path) -> None:
    from amazon_processor.image_lab import ImageLabService
    from amazon_processor.providers import ProviderAuthError

    class FakeProvider:
        def set_attempt_hook(self, _hook):
            pass

        def call_image_gen(self, *_args, **_kwargs):
            raise ProviderAuthError(
                "API 鉴权失败",
                provider="agnes",
                operation="image_gen",
                status_code=401,
            )

    service = ImageLabService(
        output_root=tmp_path,
        provider_builder=lambda _model: FakeProvider(),
    )
    try:
        started = service.start_job(_payload())
        job = _wait_for_job(service, started["job_id"])
        assert job["status"] == "failed"
        assert job["http_status"] == 401
        assert job["error"]["type"] == "ProviderAuthError"
        assert not list(tmp_path.glob("*"))
    finally:
        service.close()


def test_image_lab_rejects_unknown_model_and_empty_prompt(tmp_path: Path) -> None:
    from amazon_processor.image_lab import (
        ImageLabService,
        ImageLabValidationError,
    )

    service = ImageLabService(output_root=tmp_path)
    try:
        invalid_model = _payload()
        invalid_model["model"] = "gpt-image"
        with pytest.raises(ImageLabValidationError):
            service.start_job(invalid_model)

        empty_prompt = _payload()
        empty_prompt["prompt"] = ""
        with pytest.raises(ImageLabValidationError):
            service.start_job(empty_prompt)
    finally:
        service.close()


def test_image_lab_http_server_requires_cookie_and_same_origin(
    tmp_path: Path,
) -> None:
    from amazon_processor.image_lab import ImageLabService, create_server

    service = ImageLabService(output_root=tmp_path)
    server = create_server(service=service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(HTTPError) as forbidden:
            build_opener().open(server.origin + "/api/state")
        assert forbidden.value.code == 403

        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        opener.open(server.bootstrap_url)
        state = json.loads(opener.open(server.origin + "/api/state").read())
        serialized = json.dumps(state, ensure_ascii=False)
        assert state["ok"] is True
        assert "agnes-image-2.1-flash" in state["models"]
        assert "AGNES_KEY" not in serialized
        assert "Bearer " not in serialized

        request = Request(
            server.origin + "/api/heartbeat",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "Origin": "https://attacker.example",
            },
            method="POST",
        )
        with pytest.raises(HTTPError) as csrf:
            opener.open(request)
        assert csrf.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_manual_image_prompt_is_registered() -> None:
    from amazon_processor.config.prompts import get_prompt_registry

    registry = get_prompt_registry()
    rendered = registry.render(
        "images.manual_edit",
        target_language="English (US)",
        exact_text_instruction="Use exact text: Example",
    )
    assert "English (US)" in rendered
    assert "Use exact text: Example" in rendered
    assert "automotive emblems" in rendered


def test_image_lab_page_contains_core_controls_without_external_assets() -> None:
    path = Path(__file__).resolve().parents[1] / "config" / "image_lab.html"
    html = path.read_text(encoding="utf-8")

    for element_id in (
        'id="file-input"',
        'id="image-url"',
        'id="model"',
        'id="market"',
        'id="exact-text"',
        'id="prompt"',
        'id="generate"',
        'id="download"',
    ):
        assert element_id in html
    assert "https://cdn" not in html
    assert "<script src=" not in html


def test_image_lab_page_refresh_does_not_stop_local_service() -> None:
    from amazon_processor.image_lab import PAGE_GONE_TIMEOUT_S

    path = Path(__file__).resolve().parents[1] / "config" / "image_lab.html"
    html = path.read_text(encoding="utf-8")

    assert "beforeunload" not in html
    assert "navigator.sendBeacon('/api/shutdown'" not in html
    assert '<option value="agnes-image-2.1-flash"' in html
    assert '<option value="agnes-image-2.0-flash"' in html
    assert '<option value="US"' in html
    assert PAGE_GONE_TIMEOUT_S == 30 * 60
