"""health.py API 预检测试 — mock 网络调用。"""
import pytest
from unittest.mock import Mock, patch


class TestHealthCheck:
    """预检逻辑测试。"""

    def test_all_pass_when_services_ok(self, monkeypatch):
        """所有服务可用时返回 all_ok=True。"""
        mock_response = Mock(ok=True, status_code=200)
        mock_response.json.return_value = {
            'choices': [{'message': {'content': 'pong'}}]
        }

        def mock_post(*args, **kwargs):
            return mock_response

        monkeypatch.setattr('requests.post', mock_post)
        monkeypatch.setattr('crosspilot.health.requests.post', mock_post)

        from crosspilot.health import run_health_check
        results = run_health_check(
            deepseek_key='test-ds',
            agnes_key='test-ag',
            text_provider='deepseek',
            image_provider='agnes',
            gpt_image_key='test-gpt',
        )

        assert len(results) >= 3  # deepseek + agnes text + agnes image + gpt
        assert any(r.name == 'DeepSeek Text' for r in results)

    def test_no_keys_returns_error(self, monkeypatch):
        """无 API key 时返回错误结果。"""
        from crosspilot.health import run_health_check
        results = run_health_check(
            deepseek_key='',
            agnes_key='',
            text_provider='deepseek',
        )
        assert len(results) == 1
        assert not results[0].ok
        assert 'No API' in results[0].name

    def test_deepseek_unavailable_reported(self, monkeypatch):
        """DeepSeek 不可用时正确报告。"""
        mock_response = Mock(ok=False, status_code=401, text='unauthorized')

        def mock_post(*args, **kwargs):
            return mock_response

        monkeypatch.setattr('requests.post', mock_post)
        monkeypatch.setattr('crosspilot.health.requests.post', mock_post)

        from crosspilot.health import run_health_check
        results = run_health_check(
            deepseek_key='bad-key',
            agnes_key='',
            text_provider='deepseek',
        )

        ds = [r for r in results if r.name == 'DeepSeek Text']
        if ds:
            assert not ds[0].ok

    def test_agnes_image_gen_timeout_handled(self, monkeypatch):
        """Agnes 生图超时时正确报告。"""
        import requests as req

        def mock_post(*args, **kwargs):
            raise req.exceptions.ReadTimeout('timed out')

        monkeypatch.setattr('requests.post', mock_post)
        monkeypatch.setattr('crosspilot.health.requests.post', mock_post)

        from crosspilot.health import run_health_check
        results = run_health_check(
            deepseek_key='test-ds',
            agnes_key='test-ag',
            text_provider='deepseek',
            image_provider='agnes',
        )

        img = [r for r in results if 'Image Gen' in r.name]
        if img:
            assert not img[0].ok

    def test_agnes_image_422_is_not_reported_as_healthy(self, monkeypatch):
        """A validation error proves reachability, not usable image generation."""
        response = Mock(ok=False, status_code=422, text='invalid size')
        monkeypatch.setattr(
            'crosspilot.health.requests.post',
            lambda *args, **kwargs: response,
        )

        from crosspilot.health import _ping_agnes_image
        result = _ping_agnes_image('test-agnes')

        assert result.ok is False

    def test_text_uses_agnes_when_configured(self, monkeypatch):
        """text_provider=agnes 时不检查 DeepSeek。"""
        mock_response = Mock(ok=True, status_code=200)
        mock_response.json.return_value = {
            'choices': [{'message': {'content': 'pong'}}]
        }
        monkeypatch.setattr('requests.post', lambda *a, **kw: mock_response)
        monkeypatch.setattr('crosspilot.health.requests.post', lambda *a, **kw: mock_response)

        from crosspilot.health import run_health_check
        results = run_health_check(
            deepseek_key='',
            agnes_key='test-ag',
            text_provider='agnes',
            image_provider='agnes',
        )

        names = {r.name for r in results}
        assert 'DeepSeek Text' not in names
        assert 'Agnes Text' in names


class TestPrintHealthReport:
    """预检报告输出测试。"""

    def test_all_ok_returns_true(self):
        from crosspilot.health import HealthResult, print_health_report
        results = [
            HealthResult('Svc1', True, 100, '100ms'),
            HealthResult('Svc2', True, 200, '200ms'),
        ]
        assert print_health_report(results) is True

    def test_any_fail_returns_false(self):
        from crosspilot.health import HealthResult, print_health_report
        results = [
            HealthResult('Svc1', True, 100, '100ms'),
            HealthResult('Svc2', False, 5000, 'timeout'),
        ]
        assert print_health_report(results) is False
