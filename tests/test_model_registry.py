import json

import pytest


def _profile_payload():
    return {
        "version": 1,
        "active_profile": "production",
        "profiles": {
            "production": {
                "text": {
                    "provider": "deepseek",
                    "base_url": "https://text.example",
                    "model": "text-v1",
                    "fallback_models": ["text-v0"],
                },
                "vision": {
                    "provider": "agnes",
                    "base_url": "https://vision.example",
                    "model": "vision-v1",
                },
                "image": {
                    "provider": "agnes",
                    "base_url": "https://image.example",
                    "model": "image-v2",
                    "fallbacks": [
                        {
                            "provider": "agnes",
                            "base_url": "https://image.example",
                            "model": "image-v1",
                        },
                        {
                            "provider": "gpt",
                            "base_url": "https://gpt.example",
                            "model": "gpt-image-v1",
                        },
                    ],
                },
            },
        },
    }


def test_model_registry_resolves_operations_and_fallbacks(tmp_path):
    from crosspilot.model_registry import ModelRegistry

    path = tmp_path / "models.json"
    path.write_text(json.dumps(_profile_payload()), encoding="utf-8")

    registry = ModelRegistry.from_file(path)

    assert registry.profile_name == "production"
    assert registry.target("text").model == "text-v1"
    assert registry.target("vision").base_url == "https://vision.example"
    assert [target.model for target in registry.fallbacks("image")] == [
        "image-v1",
        "gpt-image-v1",
    ]


def test_model_registry_rejects_missing_required_target(tmp_path):
    from crosspilot.model_registry import ModelConfigError, ModelRegistry

    payload = _profile_payload()
    del payload["profiles"]["production"]["vision"]
    path = tmp_path / "models.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ModelConfigError, match="vision"):
        ModelRegistry.from_file(path)


def test_model_registry_exports_legacy_config_values(tmp_path):
    from crosspilot.model_registry import ModelRegistry

    path = tmp_path / "models.json"
    path.write_text(json.dumps(_profile_payload()), encoding="utf-8")

    config = ModelRegistry.from_file(path).as_config()

    assert config["TEXT_PROVIDER"] == "deepseek"
    assert config["DEEPSEEK_TEXT_MODEL"] == "text-v1"
    assert config["DEEPSEEK_TEXT_FALLBACK_MODEL"] == "text-v0"
    assert config["AGNES_TEXT_MODEL"] == "vision-v1"
    assert config["AGNES_IMAGE_MODEL"] == "image-v2"
    assert config["AGNES_IMAGE_FALLBACK_MODEL"] == "image-v1"
    assert config["GPT_IMAGE_MODEL"] == "gpt-image-v1"


def test_model_registry_signature_changes_with_model(tmp_path):
    from crosspilot.model_registry import ModelRegistry

    payload = _profile_payload()
    path = tmp_path / "models.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    first = ModelRegistry.from_file(path).signature()

    payload["profiles"]["production"]["image"]["model"] = "image-v3"
    path.write_text(json.dumps(payload), encoding="utf-8")
    second = ModelRegistry.from_file(path).signature()

    assert first != second


def test_model_registry_lists_available_profiles(tmp_path):
    from crosspilot.model_registry import list_model_profiles

    payload = _profile_payload()
    payload["profiles"]["test"] = payload["profiles"]["production"]
    path = tmp_path / "models.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert list_model_profiles(path) == ("production", "test")
