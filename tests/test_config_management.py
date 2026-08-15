from __future__ import annotations

from http.cookiejar import CookieJar
import json
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.request import (
    HTTPCookieProcessor,
    Request,
    build_opener,
)

import pytest

from amazon_processor.config.credentials import CredentialStore
from amazon_processor.config.locking import ProcessLock, ProcessBusyError
from amazon_processor.config.models import ModelRegistry
from amazon_processor.config.prompts import PromptError, PromptRegistry


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _settings_with_test_envs() -> dict:
    settings = json.loads(
        (PROJECT_ROOT / "config" / "settings.json").read_text(encoding="utf-8")
    )
    for credential_id, definition in settings["credentials"].items():
        definition["env"] = "TEST_ROUTE_" + credential_id.upper()
    return settings


def _write_config_tree(root: Path) -> tuple[Path, Path, Path]:
    settings = _settings_with_test_envs()
    settings_path = root / "config" / "settings.json"
    prompts = root / "config" / "prompts"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    import shutil

    shutil.copytree(PROJECT_ROOT / "config" / "prompts", prompts)
    env_path = root / ".env"
    env_path.write_text(
        "\n".join(
            f"{definition['env']}=secret-for-{credential_id}"
            for credential_id, definition in settings["credentials"].items()
        )
        + "\n",
        encoding="utf-8",
    )
    return settings_path, prompts, env_path


def _patch_control_paths(monkeypatch, tmp_path: Path):
    from amazon_processor.config import control

    settings_path, prompts, env_path = _write_config_tree(tmp_path)
    runtime = tmp_path / ".runtime"
    monkeypatch.setattr(control, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(control, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(control, "PROMPT_ROOT", prompts)
    monkeypatch.setattr(control, "MANIFEST_PATH", prompts / "manifest.json")
    monkeypatch.setattr(control, "ENV_PATH", env_path)
    monkeypatch.setattr(control, "RUNTIME_ROOT", runtime)
    monkeypatch.setattr(control, "BACKUP_ROOT", runtime / "config_backups")
    monkeypatch.setattr(control, "STAGING_ROOT", runtime / "config_staging")
    monkeypatch.setattr(control, "_reload_live_configuration", lambda: None)
    return control


def _payload_from_state(state: dict) -> dict:
    return {
        "revision": state["revision"],
        "settings": state["settings"],
        "prompts": {
            item["id"]: item["content"]
            for item in state["prompts"]
        },
        "secrets": {
            item["id"]: {"action": "keep"}
            for item in state["credentials"]
        },
    }


def test_named_credentials_can_be_reused_or_split_by_route(tmp_path) -> None:
    settings = _settings_with_test_envs()
    settings["credentials"]["agnes_backup"] = {
        "label": "Agnes 备用",
        "env": "TEST_ROUTE_AGNES_BACKUP",
    }
    settings["profiles"]["production"]["image"]["fallbacks"][0][
        "credential"
    ] = "agnes_backup"
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text(
        "\n".join([
            "TEST_ROUTE_DEEPSEEK_MAIN=deepseek-secret",
            "TEST_ROUTE_AGNES_MAIN=agnes-main-secret",
            "TEST_ROUTE_AGNES_BACKUP=agnes-backup-secret",
            "TEST_ROUTE_GPT_IMAGE_PROXY=gpt-secret",
        ]),
        encoding="utf-8",
    )

    registry = ModelRegistry.from_file(path)
    store = CredentialStore(registry, env_path=env, environ={})

    image_routes = registry.routes("image")
    assert image_routes[0].credential == "agnes_main"
    assert image_routes[1].credential == "agnes_backup"
    assert store.value(image_routes[0].credential) == "agnes-main-secret"
    assert store.value(image_routes[1].credential) == "agnes-backup-secret"
    assert store.missing_routes() == []


def test_composite_uses_configured_route_order_and_route_keys() -> None:
    from amazon_processor.providers import CompositeProvider
    from amazon_processor.providers.agnes import AgnesProvider
    from amazon_processor.providers.deepseek import DeepSeekProvider
    from amazon_processor.providers.gpt_image import GPTImageProvider
    from amazon_processor.providers.ollama_vision import OllamaVisionProvider

    def route(provider, model, key, credential, base_url):
        return {
            "provider": provider,
            "model": model,
            "api_key": key,
            "credential": credential,
            "base_url": base_url,
            "params": {},
        }

    provider = CompositeProvider({
        "routes": {
            "text": [
                route(
                    "deepseek",
                    "text-main",
                    "text-key",
                    "deepseek_main",
                    "https://text.example",
                ),
                route(
                    "deepseek",
                    "text-backup",
                    "text-backup-key",
                    "deepseek_backup",
                    "https://text-backup.example",
                ),
            ],
            "vision": [
                route(
                    "agnes",
                    "vision-main",
                    "vision-key",
                    "agnes_vision",
                    "https://vision.example",
                ),
                route(
                    "ollama",
                    "qwen3-vl:4b-instruct-q4_K_M",
                    "local-only",
                    "agnes_vision",
                    "http://127.0.0.1:11434",
                ),
            ],
            "image": [
                route(
                    "agnes",
                    "agnes-image-2.1-flash",
                    "agnes-primary-key",
                    "agnes_main",
                    "https://agnes.example",
                ),
                route(
                    "agnes",
                    "agnes-image-2.0-flash",
                    "agnes-backup-key",
                    "agnes_backup",
                    "https://agnes-backup.example",
                ),
                route(
                    "gpt",
                    "gpt-image-2",
                    "gpt-key",
                    "gpt_proxy",
                    "https://gpt.example",
                ),
            ],
        },
    })

    assert isinstance(provider._providers["text"], DeepSeekProvider)
    assert provider._providers["text"].MODEL == "text-main"
    assert provider._text_fallbacks[0].MODEL == "text-backup"
    assert isinstance(provider._providers["vision"], AgnesProvider)
    assert isinstance(provider._vision_fallbacks[0], OllamaVisionProvider)
    assert [
        item.IMAGE_MODEL
        for item in [
            provider._providers["image_gen"],
            *provider._image_gen_fallbacks,
        ]
    ] == [
        "agnes-image-2.1-flash",
        "agnes-image-2.0-flash",
        "gpt-image-2",
    ]
    assert isinstance(provider._image_gen_fallbacks[-1], GPTImageProvider)
    assert (
        provider._image_gen_fallbacks[0]._session.headers["Authorization"]
        == "Bearer agnes-backup-key"
    )
    assert (
        provider._image_gen_fallbacks[-1]._session.headers["Authorization"]
        == "Bearer gpt-key"
    )


def test_credential_public_state_never_returns_full_value(tmp_path) -> None:
    settings_path, _prompts, env = _write_config_tree(tmp_path)
    registry = ModelRegistry.from_file(settings_path)
    secret = "cpk-super-sensitive-value"
    first = registry.credentials()[0]
    env.write_text(f"{first.env}={secret}\n", encoding="utf-8")
    store = CredentialStore(registry, env_path=env, environ={})

    state = store.state(first.credential_id).as_public_dict()

    assert state["configured"] is True
    assert state["masked"].endswith("alue")
    assert secret not in json.dumps(state)


def test_prompt_manifest_rejects_missing_required_variable(tmp_path) -> None:
    root = tmp_path / "prompts"
    root.mkdir()
    (root / "title.txt").write_text("Optimize this title", encoding="utf-8")
    (root / "manifest.json").write_text(
        json.dumps({
            "version": 1,
            "prompts": [{
                "id": "amazon.title",
                "label": "Title",
                "category": "Amazon",
                "path": "title.txt",
                "variables": ["title"],
                "used_by": "test",
            }],
        }),
        encoding="utf-8",
    )

    with pytest.raises(PromptError, match="缺少变量 title"):
        PromptRegistry(root).get("amazon.title")


def test_configuration_save_is_transactional_and_creates_backup(
    tmp_path,
    monkeypatch,
) -> None:
    control = _patch_control_paths(monkeypatch, tmp_path)
    state = control.public_state()
    payload = _payload_from_state(state)
    payload["settings"]["runtime"]["max_rows"] = 3

    result = control.save_payload(payload)

    saved = json.loads(control.SETTINGS_PATH.read_text(encoding="utf-8"))
    assert saved["runtime"]["max_rows"] == 3
    assert result["saved"] is True
    assert len(control.list_backups()) == 1
    public = json.dumps(result["state"], ensure_ascii=False)
    assert "secret-for-deepseek_main" not in public
    assert "secret-for-agnes_main" not in public


def test_configuration_save_rolls_back_all_files_on_failure(
    tmp_path,
    monkeypatch,
) -> None:
    control = _patch_control_paths(monkeypatch, tmp_path)
    before_settings = control.SETTINGS_PATH.read_bytes()
    before_env = control.ENV_PATH.read_bytes()
    state = control.public_state()
    payload = _payload_from_state(state)
    payload["settings"]["runtime"]["max_rows"] = 8
    original_copy_tree = control._copy_prompt_tree
    calls = {"count": 0}

    def fail_once(source):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("simulated prompt write failure")
        return original_copy_tree(source)

    monkeypatch.setattr(control, "_copy_prompt_tree", fail_once)

    with pytest.raises(OSError, match="simulated"):
        control.save_payload(payload)

    assert control.SETTINGS_PATH.read_bytes() == before_settings
    assert control.ENV_PATH.read_bytes() == before_env


def test_configuration_save_refuses_stale_revision(
    tmp_path,
    monkeypatch,
) -> None:
    control = _patch_control_paths(monkeypatch, tmp_path)
    payload = _payload_from_state(control.public_state())
    payload["revision"] = "stale"

    with pytest.raises(control.ConfigurationConflict):
        control.save_payload(payload)


def test_processing_lock_blocks_configuration_save(
    tmp_path,
    monkeypatch,
) -> None:
    control = _patch_control_paths(monkeypatch, tmp_path)
    payload = _payload_from_state(control.public_state())
    lock = ProcessLock(control.RUNTIME_ROOT / "processor.lock")
    lock.acquire()
    try:
        with pytest.raises(ProcessBusyError):
            control.save_payload(payload)
    finally:
        lock.release()


def test_backup_can_be_restored_without_exposing_secrets(
    tmp_path,
    monkeypatch,
) -> None:
    control = _patch_control_paths(monkeypatch, tmp_path)
    state = control.public_state()
    first_payload = _payload_from_state(state)
    first_payload["settings"]["runtime"]["max_rows"] = 11
    control.save_payload(first_payload)
    backup_name = control.list_backups()[0]["name"]

    changed_state = control.public_state()
    second_payload = _payload_from_state(changed_state)
    second_payload["settings"]["runtime"]["max_rows"] = 22
    control.save_payload(second_payload)

    restored = control.restore_backup(
        backup_name,
        control.configuration_revision(),
    )

    assert restored["state"]["settings"]["runtime"]["max_rows"] == 0
    serialized = json.dumps(restored, ensure_ascii=False)
    assert "secret-for-agnes_main" not in serialized


def test_config_http_server_requires_bootstrap_cookie_and_same_origin() -> None:
    from amazon_processor.config.manager import create_server

    server = create_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(HTTPError) as forbidden:
            build_opener().open(server.origin + "/api/state")
        assert forbidden.value.code == 403

        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        opener.open(server.bootstrap_url)
        state = json.loads(opener.open(server.origin + "/api/state").read())
        assert state["ok"] is True
        assert "test-deepseek-key" not in json.dumps(state["credentials"])

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


def test_ai_instruction_fragments_live_only_in_prompt_files() -> None:
    source_files = [
        PROJECT_ROOT / "amazon_processor" / "review" / "translation.py",
        PROJECT_ROOT / "amazon_processor" / "providers" / "agnes.py",
        PROJECT_ROOT / "amazon_processor" / "providers" / "gpt_image.py",
        PROJECT_ROOT / "amazon_processor" / "images" / "gate.py",
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_files)

    assert "LISTING CONTEXT:" not in source
    assert "你是跨境电商商品文案翻译员" not in source
    assert "MAIN IMAGE ZERO-TEXT RULE" not in source
    assert "REFERENCE-PRESERVING LOCAL EDIT" not in source
    assert (
        PROJECT_ROOT / "config" / "prompts" / "images" / "listing_context.txt"
    ).read_text(encoding="utf-8").startswith("LISTING CONTEXT:")
    edit_prompt = (
        PROJECT_ROOT / "config" / "prompts" / "images" / "edit_request.txt"
    ).read_text(encoding="utf-8")
    assert "MAIN IMAGE ZERO-TEXT RULE" in edit_prompt
    assert "REFERENCE-PRESERVING LOCAL EDIT" in edit_prompt


def test_route_test_uses_saved_credentials_without_returning_them(
    tmp_path,
    monkeypatch,
) -> None:
    control = _patch_control_paths(monkeypatch, tmp_path)
    seen_credentials = []

    def fake_probe(operation, index, target, credential_value, *, timeout):
        seen_credentials.append(credential_value)
        return {
            "operation": operation,
            "route_index": index,
            "route_label": "主线路" if index == 0 else f"备用线路 {index}",
            "provider": target.provider,
            "model": target.model,
            "base_url": target.base_url,
            "status": "ok",
            "http_status": 200,
            "latency_ms": int(timeout),
            "message": "ok",
        }

    monkeypatch.setattr(control, "_probe_route", fake_probe)
    result = control.test_model_routes(timeout=2)

    assert result["tested"] == result["passed"]
    assert seen_credentials
    serialized = json.dumps(result, ensure_ascii=False)
    assert "secret-for-" not in serialized


def test_image_generation_cache_includes_listing_context(monkeypatch) -> None:
    from amazon_processor.images import cache

    calls = []

    def signature(_policy, *prompt_ids):
        calls.append(prompt_ids)
        return str(len(calls))

    monkeypatch.setattr(cache, "build_runtime_signature", signature)

    cache.current_cache_versions()

    assert calls[1] == (
        "images.main_text_free_review",
        "images.main_text_free_review_batch",
    )
    assert "images.listing_context" in calls[2]
    assert "images.edit_request" in calls[2]


def test_main_image_prompt_locks_reference_to_local_zero_text_editing() -> None:
    prompt = (
        PROJECT_ROOT
        / "config"
        / "prompts"
        / "images"
        / "main_product.txt"
    ).read_text(encoding="utf-8")

    assert "image-to-image editing, not image generation from scratch" in prompt
    assert "HARD REFERENCE LOCK" in prompt
    assert "every area outside the unwanted marks as locked" in prompt
    assert "Remove ALL text that is" in prompt
    assert "Edit the smallest possible local region" in prompt
    assert "Never use a large patch" in prompt
    assert "Do not recenter, zoom, rotate, restage, beautify" in prompt
    assert "Never reconstruct the whole image" in prompt
    assert "Replace every" in prompt
    assert "Never leave the original non-English" in prompt
    assert "Translate Chinese or any other" in prompt
    assert "must not preserve, sharpen, trace, or recreate it" in prompt
