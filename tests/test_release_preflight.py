from crosspilot import __version__
from scripts import release_preflight


def test_runtime_version_uses_canonical_source():
    assert __version__ == release_preflight.read_version()


def test_release_preflight_accepts_matching_tag():
    assert release_preflight.validate_release(f'v{__version__}') == []


def test_release_preflight_rejects_mismatched_tag():
    errors = release_preflight.validate_release('v99.0.0')
    assert any('不一致' in error for error in errors)


def test_dockerfile_installs_complete_runtime_package():
    dockerfile = (release_preflight.ROOT / 'Dockerfile').read_text(encoding='utf-8')

    assert 'COPY crosspilot/ ./crosspilot/' in dockerfile
    assert 'uv sync --frozen --no-dev --no-install-project' in dockerfile
    assert '"uv", "run", "--no-sync", "uvicorn"' in dockerfile


def test_canary_uses_supported_provider_credentials():
    canary = (
        release_preflight.ROOT / '.github' / 'workflows' / 'canary.yml'
    ).read_text(encoding='utf-8')

    assert 'CROSSPILOT_DEEPSEEK_KEY' in canary
    assert 'CROSSPILOT_DMX_KEY' not in canary
