from __future__ import annotations

import json
from pathlib import Path

import pytest

from amazon_processor.config.credentials import CredentialStore
from amazon_processor.config.env import reload_config
from amazon_processor.config.models import ModelConfigError, ModelRegistry
from amazon_processor.config.prompts import PromptRegistry
from amazon_processor.images.cache import current_cache_versions


ROOT = Path(__file__).resolve().parents[1]


def test_production_config_uses_only_official_deepseek(monkeypatch) -> None:
    registry = ModelRegistry.from_file(ROOT / "config" / "settings.json")
    assert registry.target("text").provider == "deepseek"
    assert registry.target("vision").provider == "deepseek"
    assert registry.target("vision").model == "deepseek-v4-flash-vision-exp"
    assert registry.fallbacks("vision") == ()
    assert [item.env for item in registry.credentials()] == ["DEEPSEEK_KEY"]
    monkeypatch.setenv("DEEPSEEK_KEY", "secret-test-value")
    store = CredentialStore(registry, env_path=ROOT / "missing.env")
    assert store.value("deepseek_main") == "secret-test-value"
    assert store.missing_routes() == []
    assert reload_config()["REVIEW_BATCH_SIZE"] == "3"


def test_registry_rejects_image_operation_and_non_deepseek_provider(
    tmp_path: Path,
) -> None:
    settings = json.loads(
        (ROOT / "config" / "settings.json").read_text(encoding="utf-8")
    )
    settings["profiles"]["production"]["vision"]["provider"] = "agnes"
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    with pytest.raises(ModelConfigError, match="不支持"):
        ModelRegistry.from_file(path)
    registry = ModelRegistry.from_file(ROOT / "config" / "settings.json")
    with pytest.raises(ModelConfigError, match="不支持的模型操作"):
        registry.target("image")


def test_prompt_manifest_has_no_generation_prompts() -> None:
    registry = PromptRegistry(ROOT / "config" / "prompts")
    ids = {item.prompt_id for item in registry.specs()}
    assert "images.risk_assessment" in ids
    assert "images.main_text_free_review_batch" in ids
    assert not any("generation" in item or "manual_edit" in item for item in ids)
    manifest = json.loads(
        (ROOT / "config" / "prompts" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert all(item["category"] != "图片生成" for item in manifest["prompts"])


def test_model_and_prompt_changes_affect_signatures(tmp_path: Path) -> None:
    settings = json.loads(
        (ROOT / "config" / "settings.json").read_text(encoding="utf-8")
    )
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps(settings), encoding="utf-8")
    settings["profiles"]["production"]["vision"]["model"] = "changed-model"
    second.write_text(json.dumps(settings), encoding="utf-8")
    assert ModelRegistry.from_file(first).signature() != ModelRegistry.from_file(
        second
    ).signature()
    review_version, main_version = current_cache_versions()
    assert len(review_version) == 16
    assert len(main_version) == 16
    assert review_version != main_version


def test_active_python_has_no_server_or_image_generation_entry() -> None:
    forbidden_files = [
        "server_worker.py",
        "api_server.py",
        "image_lab.py",
        "providers/agnes.py",
        "providers/ollama_vision.py",
        "providers/gpt_image.py",
    ]
    assert not any((ROOT / "amazon_processor" / value).exists() for value in forbidden_files)
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "amazon_processor").rglob("*.py")
    )
    assert "call_image_gen" not in source
