"""config.py 优先级测试 — .env > 环境变量 > 默认值。"""
import pytest, os, tempfile
from pathlib import Path


class TestConfigPriority:
    """配置加载优先级验证。"""

    def test_defaults_are_used_when_not_configured(self, tmp_path, monkeypatch):
        """未在 .env 或 keys.json 中配置的字段使用默认值。"""
        empty_env = tmp_path / '.env'
        empty_env.write_text('', encoding='utf-8')
        monkeypatch.setenv('CROSSPILOT_ENV', str(empty_env))

        from crosspilot.config import reload_config, load_config
        reload_config()
        cfg = load_config()

        # keys.json 存在时提供 DEEPSEEK_KEY/AGNES_KEY
        # 未配置的字段应使用默认值
        assert cfg['TEXT_PROVIDER'] == 'deepseek'
        assert cfg['IMAGE_GEN_CONCURRENCY'] == '20'
        assert cfg['QUALITY_GATE'] == 'false'
        assert cfg['OUTPUT_REPORT'] == 'true'
        assert cfg['IMAGE_GEN_ATTEMPTS'] == '3'
        assert cfg['AGNES_503_RETRY_LIMIT'] == '1'
        assert cfg['AGNES_503_BACKOFF_MAX_S'] == '8'
        assert cfg['AGNES_503_CIRCUIT_COOLDOWN_S'] == '120'

    def test_env_var_overrides_default(self, tmp_path, monkeypatch):
        """环境变量应覆盖默认值。"""
        empty_env = tmp_path / '.env'
        empty_env.write_text('', encoding='utf-8')
        monkeypatch.setenv('CROSSPILOT_ENV', str(empty_env))
        monkeypatch.setenv('CROSSPILOT_TEXT_CONCURRENCY', '200')

        from crosspilot.config import reload_config, load_config
        reload_config()
        cfg = load_config()

        assert cfg['TEXT_CONCURRENCY'] == '200'

    def test_env_file_overrides_default(self, tmp_path, monkeypatch):
        """.env 文件应覆盖默认值。"""
        env_file = tmp_path / '.env'
        env_file.write_text('TEXT_CONCURRENCY=150\nTEXT_PROVIDER=agnes\n', encoding='utf-8')

        monkeypatch.setenv('CROSSPILOT_ENV', str(env_file))
        monkeypatch.delenv('CROSSPILOT_TEXT_CONCURRENCY', raising=False)

        from crosspilot.config import reload_config, load_config
        reload_config()
        cfg = load_config()

        assert cfg['TEXT_CONCURRENCY'] == '150'
        assert cfg['TEXT_PROVIDER'] == 'agnes'

    def test_prompt_profile_is_loaded_from_env_file(self, tmp_path, monkeypatch):
        env_file = tmp_path / '.env'
        env_file.write_text('PROMPT_PROFILE=test\n', encoding='utf-8')
        monkeypatch.setenv('CROSSPILOT_ENV', str(env_file))
        monkeypatch.delenv('CROSSPILOT_PROMPT_PROFILE', raising=False)

        from crosspilot.config import reload_config, load_config

        reload_config()

        assert load_config()['PROMPT_PROFILE'] == 'test'

    def test_env_var_overrides_env_file(self, tmp_path, monkeypatch):
        """环境变量优先级 > .env 文件。"""
        env_file = tmp_path / '.env'
        env_file.write_text('TEXT_CONCURRENCY=150\n', encoding='utf-8')

        monkeypatch.setenv('CROSSPILOT_ENV', str(env_file))
        monkeypatch.setenv('CROSSPILOT_TEXT_CONCURRENCY', '999')

        from crosspilot.config import reload_config, load_config
        reload_config()
        cfg = load_config()

        assert cfg['TEXT_CONCURRENCY'] == '999'

    def test_bool_parsing(self, tmp_path, monkeypatch):
        """布尔值解析正确。"""
        empty_env = tmp_path / '.env'
        empty_env.write_text('', encoding='utf-8')
        monkeypatch.setenv('CROSSPILOT_ENV', str(empty_env))
        monkeypatch.setenv('CROSSPILOT_SKIP_IMAGE_GEN', 'true')

        from crosspilot.config import reload_config, get_bool
        reload_config()

        assert get_bool('SKIP_IMAGE_GEN') is True
        assert get_bool('NONEXISTENT', default=True) is True
        assert get_bool('NONEXISTENT', default=False) is False

    def test_int_parsing_with_fallback(self, tmp_path, monkeypatch):
        """整数解析带默认值。"""
        empty_env = tmp_path / '.env'
        empty_env.write_text('', encoding='utf-8')
        monkeypatch.setenv('CROSSPILOT_ENV', str(empty_env))

        from crosspilot.config import reload_config, get_int
        reload_config()

        assert get_int('TEXT_CONCURRENCY') == 100
        assert get_int('NONEXISTENT', default=42) == 42
        assert get_int('NONEXISTENT') == 0

    def test_reload_busts_cache(self, tmp_path, monkeypatch):
        """reload_config 应清空缓存并重新加载。"""
        env1 = tmp_path / 'env1'
        env1.write_text('DEEPSEEK_KEY=key-from-file\n', encoding='utf-8')

        monkeypatch.setenv('CROSSPILOT_ENV', str(env1))
        monkeypatch.delenv('CROSSPILOT_DEEPSEEK_KEY', raising=False)

        from crosspilot.config import reload_config, load_config
        reload_config()
        cfg1 = load_config()
        assert cfg1['DEEPSEEK_KEY'] == 'key-from-file'

        # 覆盖环境变量
        monkeypatch.setenv('CROSSPILOT_DEEPSEEK_KEY', 'override-key')
        reload_config()
        cfg2 = load_config()
        assert cfg2['DEEPSEEK_KEY'] == 'override-key'


class TestConfigEdgeCases:
    """边缘案例测试。"""

    def test_empty_env_var_falls_back_to_default(self, tmp_path, monkeypatch):
        empty_env = tmp_path / '.env'
        empty_env.write_text('TEXT_CONCURRENCY=\n', encoding='utf-8')
        monkeypatch.setenv('CROSSPILOT_ENV', str(empty_env))
        from crosspilot.config import reload_config, get_int
        reload_config()
        assert get_int('TEXT_CONCURRENCY') == 100

    def test_invalid_int_returns_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv('CROSSPILOT_TEXT_CONCURRENCY', 'not_a_number')
        empty_env = tmp_path / '.env'
        empty_env.write_text('', encoding='utf-8')
        monkeypatch.setenv('CROSSPILOT_ENV', str(empty_env))
        from crosspilot.config import reload_config, get_int
        reload_config()
        assert get_int('TEXT_CONCURRENCY', default=100) == 100

    def test_comment_lines_ignored(self, tmp_path, monkeypatch):
        env_file = tmp_path / '.env'
        env_file.write_text('# comment\nTEXT_CONCURRENCY=42\n\n# another\n', encoding='utf-8')
        monkeypatch.setenv('CROSSPILOT_ENV', str(env_file))
        from crosspilot.config import reload_config, load_config
        reload_config()
        assert load_config()['TEXT_CONCURRENCY'] == '42'

    def test_quoted_values_unwrapped(self, tmp_path, monkeypatch):
        env_file = tmp_path / '.env'
        env_file.write_text('DEEPSEEK_KEY="sk-quoted"\n', encoding='utf-8')
        monkeypatch.setenv('CROSSPILOT_ENV', str(env_file))
        from crosspilot.config import reload_config, load_config
        reload_config()
        assert load_config()['DEEPSEEK_KEY'] == 'sk-quoted'

    def test_save_env_values_preserves_other_lines_and_reloads(
        self,
        tmp_path,
        monkeypatch,
    ):
        env_file = tmp_path / '.env'
        env_file.write_text(
            '# keep this comment\nTEXT_PROVIDER=deepseek\nTEXT_CONCURRENCY=42\n',
            encoding='utf-8',
        )
        monkeypatch.setenv('CROSSPILOT_ENV', str(env_file))

        from crosspilot.config import load_config, save_env_values

        save_env_values({
            'TEXT_PROVIDER': 'agnes',
            'AGNES_IMAGE_MODEL': 'agnes-image-next',
        })

        content = env_file.read_text(encoding='utf-8')
        assert '# keep this comment' in content
        assert 'TEXT_CONCURRENCY=42' in content
        assert 'TEXT_PROVIDER=agnes' in content
        assert 'AGNES_IMAGE_MODEL=agnes-image-next' in content
        assert load_config()['TEXT_PROVIDER'] == 'agnes'

    def test_save_env_values_rejects_newlines(self, tmp_path, monkeypatch):
        env_file = tmp_path / '.env'
        monkeypatch.setenv('CROSSPILOT_ENV', str(env_file))

        from crosspilot.config import save_env_values

        with pytest.raises(ValueError, match='换行'):
            save_env_values({'TEXT_PROVIDER': 'agnes\nAGNES_KEY=bad'})
