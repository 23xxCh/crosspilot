import pytest


def test_prompt_registry_loads_and_renders_named_prompt():
    from crosspilot.prompt_registry import get_prompt_registry

    registry = get_prompt_registry()

    raw = registry.get("amazon.title_optimize")
    rendered = registry.render(
        "amazon.title_optimize",
        title="For Example Product 12V",
    )

    assert "{title}" in raw
    assert "For Example Product 12V" in rendered


def test_prompt_registry_rejects_unknown_prompt():
    from crosspilot.prompt_registry import PromptNotFoundError, get_prompt_registry

    with pytest.raises(PromptNotFoundError):
        get_prompt_registry().get("missing.prompt")


def test_prompt_registry_reports_missing_template_variable():
    from crosspilot.prompt_registry import PromptRenderError, get_prompt_registry

    with pytest.raises(PromptRenderError, match="title"):
        get_prompt_registry().render("amazon.title_optimize")


def test_prompt_signature_changes_when_content_changes(tmp_path):
    from crosspilot.prompt_registry import PromptRegistry

    (tmp_path / "demo").mkdir()
    prompt_path = tmp_path / "demo" / "prompt.txt"
    prompt_path.write_text("Hello {name}", encoding="utf-8")
    registry = PromptRegistry(tmp_path, {"demo.prompt": "demo/prompt.txt"})
    first = registry.signature("demo.prompt")

    prompt_path.write_text("Hi {name}", encoding="utf-8")
    registry.reload()
    second = registry.signature("demo.prompt")

    assert first != second


def test_prompt_registry_auto_reloads_changed_file(tmp_path):
    from crosspilot.prompt_registry import PromptRegistry

    (tmp_path / "demo").mkdir()
    prompt_path = tmp_path / "demo" / "prompt.txt"
    prompt_path.write_text("Version one {name}", encoding="utf-8")
    registry = PromptRegistry(
        tmp_path,
        {"demo.prompt": "demo/prompt.txt"},
    )
    assert registry.get("demo.prompt").startswith("Version one")

    prompt_path.write_text(
        "Version two with more content {name}",
        encoding="utf-8",
    )

    assert registry.get("demo.prompt").startswith("Version two")


def test_combined_signature_tracks_prompt_and_model(monkeypatch):
    from crosspilot.prompt_registry import build_runtime_signature

    monkeypatch.setattr(
        "crosspilot.prompt_registry.model_signature",
        lambda: "model-a",
    )
    first = build_runtime_signature("policy-v1", "images.main_product")

    monkeypatch.setattr(
        "crosspilot.prompt_registry.model_signature",
        lambda: "model-b",
    )
    second = build_runtime_signature("policy-v1", "images.main_product")

    assert first != second


@pytest.mark.parametrize(
    "prompt_id",
    ["images.main_product", "images.variant"],
)
def test_image_generation_prompt_preserves_product_intrinsic_artwork(
    prompt_id,
):
    """Sold artwork/text must not be mistaken for an external watermark."""
    from crosspilot.prompt_registry import get_prompt_registry

    prompt = get_prompt_registry().get(prompt_id).lower()

    assert "product-intrinsic" in prompt
    assert "exact item count" in prompt
    assert "flat sticker" in prompt
    assert "external watermark" in prompt
    assert "no readable text of any kind" not in prompt


def test_image_quality_gate_prompt_checks_semantic_fidelity():
    from crosspilot.prompt_registry import get_prompt_registry

    prompt = get_prompt_registry().get("images.quality_gate").lower()

    assert "source reference" in prompt
    assert "generated candidate" in prompt
    assert "exact item count" in prompt
    assert "flat" in prompt
    assert '"accepted"' in prompt


def _editable_registry(tmp_path, *, profile="test"):
    from crosspilot.prompt_registry import PromptRegistry

    defaults = tmp_path / "defaults"
    (defaults / "demo").mkdir(parents=True)
    (defaults / "demo" / "prompt.txt").write_text(
        "Default {name}",
        encoding="utf-8",
    )
    return PromptRegistry(
        defaults,
        {"demo.prompt": "demo/prompt.txt"},
        override_root=tmp_path / "overrides",
        history_root=tmp_path / "history",
        profile=profile,
    )


def test_prompt_overrides_are_isolated_by_profile(tmp_path):
    registry = _editable_registry(tmp_path)

    registry.save_override("demo.prompt", "Test copy {name}")
    assert registry.get("demo.prompt") == "Test copy {name}"
    assert registry.source("demo.prompt") == "override"

    registry.set_profile("production")
    assert registry.get("demo.prompt") == "Default {name}"
    assert registry.source("demo.prompt") == "default"


@pytest.mark.parametrize(
    "content, expected",
    [
        ("Missing variable", "模板变量"),
        ("Extra {name} {sku}", "模板变量"),
        ("Broken {name", "模板语法"),
        ("", "不能为空"),
    ],
)
def test_prompt_override_validation_protects_call_contract(
    tmp_path,
    content,
    expected,
):
    from crosspilot.prompt_registry import PromptValidationError

    registry = _editable_registry(tmp_path)

    with pytest.raises(PromptValidationError, match=expected):
        registry.save_override("demo.prompt", content)


def test_prompt_history_and_rollback_restore_previous_content(tmp_path):
    registry = _editable_registry(tmp_path)
    registry.save_override("demo.prompt", "Version one {name}")
    registry.save_override("demo.prompt", "Version two {name}")

    history = registry.history("demo.prompt")
    assert history
    assert history[0]["reason"] == "edit"

    registry.rollback("demo.prompt", history[0]["revision_id"])

    assert registry.get("demo.prompt") == "Version one {name}"
    assert registry.history("demo.prompt")[0]["reason"] == "rollback"


def test_prompt_reset_override_restores_packaged_default(tmp_path):
    registry = _editable_registry(tmp_path)
    registry.save_override("demo.prompt", "Custom {name}")

    registry.reset_override("demo.prompt")

    assert registry.get("demo.prompt") == "Default {name}"
    assert registry.source("demo.prompt") == "default"


def test_prompt_registry_never_accepts_unregistered_path(tmp_path):
    from crosspilot.prompt_registry import PromptNotFoundError

    registry = _editable_registry(tmp_path)

    with pytest.raises(PromptNotFoundError):
        registry.save_override("../../outside", "Unsafe {name}")
