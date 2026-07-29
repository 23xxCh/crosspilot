"""concurrency.py 测试 — adaptive_map, configured_concurrency。"""
import pytest
from scripts.concurrency import (
    adaptive_map, configured_concurrency, configured_rpm,
    env_float, env_int,
)


class TestAdaptiveMap:
    """adaptive_map 自适应并发测试。"""

    def test_empty_items_returns_empty(self):
        results, stats = adaptive_map(
            [], lambda x: x, operation='test', initial_workers=4,
        )
        assert results == []
        assert stats['items'] == 0

    def test_all_success_no_downgrade(self):
        sleeps = []
        results, stats = adaptive_map(
            range(10),
            lambda x: f'ok-{x}',
            operation='test',
            initial_workers=4,
            min_workers=2,
            sleep_fn=sleeps.append,
        )

        assert len(results) == 10
        assert stats['reductions'] == 0
        assert sleeps == []

    def test_high_failure_triggers_downgrade(self):
        sleeps = []
        results, stats = adaptive_map(
            range(16),  # 4 workers, 4 batches of 4
            lambda x: None if x < 12 else f'ok-{x}',
            operation='test',
            initial_workers=4,
            min_workers=2,
            backoff_s=2,
            max_backoff_s=8,
            sleep_fn=sleeps.append,
        )

        assert len(results) == 16
        assert stats['reductions'] >= 1
        assert len(sleeps) >= 1

    def test_downgrade_stops_at_min_workers(self):
        sleeps = []
        results, stats = adaptive_map(
            range(20),
            lambda x: None,  # all fail
            operation='test',
            initial_workers=4,
            min_workers=2,
            backoff_s=1,
            sleep_fn=sleeps.append,
        )

        assert stats['final_workers'] >= 2
        assert stats['reductions'] >= 1

    def test_custom_success_predicate(self):
        def pred(r):
            return isinstance(r, int) and r > 0

        results, stats = adaptive_map(
            [1, -1, 2, -2, 3, -3],
            lambda x: x,
            operation='test',
            initial_workers=2,
            is_success=pred,
        )

        assert len(results) == 6

    def test_on_result_called_per_item(self):
        calls = []

        def on_result(item, result):
            calls.append((item, result))

        items = range(5)
        adaptive_map(
            items, lambda x: x * 10,
            operation='test', initial_workers=2,
            on_result=on_result,
        )

        assert len(calls) == 5
        assert calls[0] == (0, 0)
        assert calls[4] == (4, 40)

    def test_terminal_exception_propagates(self):
        class MyFatal(Exception):
            pass

        with pytest.raises(MyFatal):
            adaptive_map(
                range(10),
                lambda x: (_ for _ in ()).throw(MyFatal('boom')),
                operation='test',
                initial_workers=2,
                terminal_exceptions=(MyFatal,),
            )

    def test_stats_includes_events(self):
        results, stats = adaptive_map(
            range(8),
            lambda x: None if x < 4 else f'ok-{x}',
            operation='image_gen',
            initial_workers=2,
            min_workers=1,
            backoff_s=1,
            sleep_fn=lambda _: None,
        )

        assert stats['operation'] == 'image_gen'
        assert stats['initial_workers'] == 2
        assert 'events' in stats


class TestConfiguredConcurrency:
    """configured_concurrency / configured_rpm 配置读取测试。"""

    def test_default_value(self, monkeypatch):
        monkeypatch.delenv('CROSSPILOT_TEST_CONCURRENCY', raising=False)
        val = configured_concurrency('test', 10)
        assert val == 10

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv('CROSSPILOT_TEST_CONCURRENCY', '25')
        val = configured_concurrency('test', 10)
        assert val == 25

    def test_clamp_to_minimum(self, monkeypatch):
        monkeypatch.setenv('CROSSPILOT_TEST_CONCURRENCY', '0')
        val = configured_concurrency('test', 10, minimum=1)
        assert val == 1

    def test_clamp_to_maximum(self, monkeypatch):
        monkeypatch.setenv('CROSSPILOT_TEST_CONCURRENCY', '200')
        val = configured_concurrency('test', 10, maximum=50)
        assert val == 50

    def test_image_generation_supports_token_plan_concurrency(self, monkeypatch):
        monkeypatch.setenv('CROSSPILOT_IMAGE_GEN_CONCURRENCY', '40')

        val = configured_concurrency('image_gen', 20, maximum=40)

        assert val == 40

    def test_image_generation_reads_unprefixed_env_file(
        self,
        tmp_path,
        monkeypatch,
    ):
        env_file = tmp_path / '.env'
        env_file.write_text(
            'IMAGE_GEN_CONCURRENCY=31\n',
            encoding='utf-8',
        )
        monkeypatch.setenv('CROSSPILOT_ENV', str(env_file))
        monkeypatch.delenv(
            'CROSSPILOT_IMAGE_GEN_CONCURRENCY',
            raising=False,
        )
        from crosspilot.config import reload_config
        reload_config()

        val = configured_concurrency(
            'image_gen',
            20,
            maximum=40,
        )

        assert val == 31

    def test_invalid_env_returns_default(self, monkeypatch):
        monkeypatch.setenv('CROSSPILOT_TEST_CONCURRENCY', 'abc')
        val = configured_concurrency('test', 10)
        assert val == 10

    def test_configured_rpm(self, monkeypatch):
        monkeypatch.setenv('CROSSPILOT_IMAGE_GEN_RPM', '50.0')
        val = configured_rpm('image_gen', 100.0)
        assert val == 50.0

    def test_env_float_clamp(self, monkeypatch):
        monkeypatch.setenv('CROSSPILOT_IMAGE_GEN_RPM', '0.1')
        val = configured_rpm('image_gen', 100.0, minimum=1.0)
        assert val == 1.0
