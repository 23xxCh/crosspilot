"""统一模型提供商接口 —— 所有 AI 调用都走这里。

使用方式:
    from model_provider import get_provider

    provider = get_provider()  # 自动从 keys.json 加载配置

    # 文本生成
    result = provider.call_text("Translate to Vietnamese: ...")

    # 图审
    needs_fix = provider.call_vision("https://...")

    # 图生图
    new_url = provider.call_image_gen("https://...")

配置方式 (keys.json):
    {
        "text_provider": "deepseek",      # 文本模型提供商
        "vision_provider": "agnes",        # 图审模型提供商
        "image_gen_provider": "agnes",     # 生图模型提供商
        "deepseek_key": "sk-...",
        "agnes_key": "cpk-..."
    }

如果要换模型，只需改 keys.json，代码完全不用动！
"""
from __future__ import annotations
import json
import os
import re
import time
import threading
from abc import ABC, abstractmethod
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter

# =============================================================================
# 配置加载
# =============================================================================

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_keys() -> dict[str, str]:
    """Load provider configuration, with deployment environment overrides."""
    kf = os.environ.get('CROSSPILOT_KEYS_PATH') or os.path.join(_ROOT, 'keys.json')
    config = {}
    try:
        with open(kf, encoding='utf-8') as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                config.update(loaded)
    except (OSError, json.JSONDecodeError):
        pass

    env_overrides = {
        'text_provider': 'CROSSPILOT_TEXT_PROVIDER',
        'vision_provider': 'CROSSPILOT_VISION_PROVIDER',
        'image_gen_provider': 'CROSSPILOT_IMAGE_GEN_PROVIDER',
        'deepseek_key': 'CROSSPILOT_DEEPSEEK_KEY',
        'agnes_key': 'CROSSPILOT_AGNES_KEY',
    }
    for field, env_name in env_overrides.items():
        value = os.environ.get(env_name)
        if value:
            config[field] = value
    return config


_KEYS = _load_keys()


def reload_keys():
    """热加载配置。"""
    global _KEYS
    _KEYS = _load_keys()


# =============================================================================
# 共享工具
# =============================================================================

def _is_quota_error(status_code: int | None, body: str = '') -> bool:
    """检查是否为额度错误。"""
    if status_code in (401, 402, 403):
        return True
    text = str(body or '').lower()
    keys = ('quota', 'insufficient', 'balance', 'billing', 'payment',
            'exceeded', '额度', '余额', '欠费', '用尽', '不足',
            'credit', 'out of credits')
    return any(k in text for k in keys)


class ProviderQuotaError(RuntimeError):
    """Provider credentials, balance, or quota prevent further useful retries."""


# =============================================================================
# 抽象基类
# =============================================================================

class ModelProvider(ABC):
    """模型提供商抽象基类。"""

    def set_attempt_hook(self, hook) -> None:
        """Attach a best-effort HTTP attempt observer used by CompositeProvider."""
        self._attempt_hook = hook

    def _record_attempt(
        self,
        operation: str,
        provider: str,
        status_code: int | None = None,
        ok: bool = False,
        retry: bool = False,
        error: Exception | None = None,
    ) -> None:
        hook = getattr(self, '_attempt_hook', None)
        if not hook:
            return
        try:
            hook(
                operation=operation,
                provider=provider,
                status_code=status_code,
                ok=ok,
                retry=retry,
                error=type(error).__name__ if error else None,
            )
        except Exception:
            pass

    @abstractmethod
    def call_text(self, prompt: str, max_tokens: int = 2048) -> Optional[str]:
        """调用文本模型。"""
        pass

    @abstractmethod
    def call_vision(self, image_url: str) -> Optional[bool]:
        """调用视觉模型进行图审。返回 True(需处理)/False(干净)/None(失败)。"""
        pass

    @abstractmethod
    def call_image_gen(self, image_url: str, size: str = "1024x1024", is_variant: bool = False) -> Optional[str]:
        """调用图生图模型。返回新图片 URL。"""
        pass


# =============================================================================
# DeepSeek 提供商
# =============================================================================

class DeepSeekProvider(ModelProvider):
    """DeepSeek API 提供商。"""

    BASE_URL = "https://api.deepseek.com"
    MODEL = "deepseek-v4-flash"
    FALLBACK_MODEL = "deepseek-v4-pro"

    def __init__(self, api_key: str):
        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=0)
        self._session.mount('https://', adapter)
        self._session.headers.update({
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        })

    def call_text(self, prompt: str, max_tokens: int = 2048,
                  retries: int = 3) -> Optional[str]:
        """调用 DeepSeek。含重试+fallback模型。"""
        attempt_number = 0
        for model in [self.MODEL, self.FALLBACK_MODEL]:
            for attempt in range(retries):
                is_retry = attempt_number > 0
                recorded = False
                try:
                    r = self._session.post(
                        f'{self.BASE_URL}/v1/chat/completions',
                        json={
                            "model": model,
                            "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": max_tokens
                        },
                        timeout=60
                    )
                    self._record_attempt(
                        'text',
                        'deepseek',
                        r.status_code,
                        r.ok,
                        retry=is_retry,
                    )
                    recorded = True
                    attempt_number += 1
                    if r.ok:
                        msg = r.json().get('choices', [{}])[0].get('message', {})
                        # v4-pro returns reasoning_content, v4-flash returns content
                        return msg.get('content', '') or msg.get('reasoning_content', '')
                    if _is_quota_error(r.status_code, r.text):
                        raise ProviderQuotaError(
                            f'DeepSeek 额度或鉴权不可用（HTTP {r.status_code}）'
                        )
                    if r.status_code == 429 and attempt < retries - 1:
                        time.sleep(10 * (attempt + 1))
                        continue
                except ProviderQuotaError:
                    raise
                except Exception as e:
                    if not recorded:
                        self._record_attempt(
                            'text',
                            'deepseek',
                            None,
                            False,
                            retry=is_retry,
                            error=e,
                        )
                        attempt_number += 1
                    pass
                if attempt < retries - 1:
                    time.sleep(2)
        return None

    def call_vision(self, image_url: str) -> Optional[bool]:
        """DeepSeek 不支持图审，返回 None 让其他 provider 处理。"""
        return None

    def call_image_gen(self, image_url: str, size: str = "1024x1024", is_variant: bool = False) -> Optional[str]:
        """DeepSeek 不支持图生图，返回 None 让其他 provider 处理。"""
        return None


# =============================================================================
# Agnes 提供商
# =============================================================================

class AgnesProvider(ModelProvider):
    """Agnes API 提供商。"""

    BASE_URL = "https://apihub.agnes-ai.com"
    TEXT_MODEL = "agnes-2.0-flash"
    IMAGE_MODEL = "agnes-image-2.1-flash"

    # 限速器
    _text_lock = threading.Lock()
    _text_last = [0.0]
    _text_interval = 60.0 / 1000  # 1000 RPM

    _image_lock = threading.Lock()
    _image_last = [0.0]
    _image_interval = 60.0 / 100  # 100 RPM = 0.6s/req

    REVIEW_PROMPT = (
        "Inspect this product image. Answer YES if any of these issues exist:\n"
        "1. SELLER WATERMARK: store ID, seller name, seller URL, overlaid watermark.\n"
        "2. BRAND NAME/LOGO: any brand text or logo visible anywhere — packaging, label, product surface, background. This includes car brands (BMW, Toyota, Honda, Mercedes, Ford, Audi, Porsche, Hyundai, Nissan, Kia, Mazda, Lexus, Benz, VW, Volkswagen), product brands (OUHOE, Color Easy, Mothers, Meguiars, Armor All, Chemical Guys, etc.), and logos of any kind.\n"
        "3. PERSON: model, face, head, hand, arm, leg, body, silhouette, reflection, mannequin, background person.\n"
        "4. NON-PRODUCT TEXT: any readable text that is NOT a product specification, dimension, or technical label.\n\n"
        "IMPORTANT: Generic decorative words like \"Sports\", \"Limited Edition\", \"Turbo\" are usually product design elements — do NOT flag these as brand names unless they are clearly a registered trademark (e.g. the Nike swoosh, the Toyota emblem).\n\n"
        "Answer YES or NO only."
    )

    MAIN_IMAGE_PROMPT = (
        "Create a 1600x1600 e-commerce main product photo. Rules:\n"
        "- Pure white background (#FFFFFF). No shadows, borders, frames.\n"
        "- Absolutely NO readable text of any kind — no brand names, no logos, no watermarks, no labels, no words on the product or packaging.\n"
        "- Remove every person and human body part.\n"
        "- Product occupies ~85% of frame, centered, front view.\n"
        "- CRITICAL: Preserve product shape, contours, holes, dimensions exactly. Change only text/logos/background.\n"
        "- Professional studio lighting, realistic texture."
    )

    VARIANT_IMAGE_PROMPT = (
        "Generate a clean product variant photo. Rules:\n"
        "- Absolutely NO readable text of any kind — no brand names, logos, watermarks, labels, or words anywhere.\n"
        "- Remove every person and human body part.\n"
        "- Keep original background, lighting, composition. Only erase text/logos/people.\n"
        "- CRITICAL: Do NOT change product shape, color, texture, or structure."
    )

    def __init__(self, api_key: str):
        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=30, pool_maxsize=30, max_retries=0)
        self._session.mount('https://', adapter)
        self._session.headers.update({
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        })

    def _acquire_text(self):
        """文本 API 限速。"""
        with self._text_lock:
            wait = self._text_interval - (time.time() - self._text_last[0])
            if wait > 0:
                time.sleep(wait)
            self._text_last[0] = time.time()

    def _acquire_image(self):
        """图片 API 限速。100 RPM = 0.6s 间隔。"""
        with self._image_lock:
            wait = self._image_interval - (time.time() - self._image_last[0])
            if wait > 0:
                time.sleep(wait)
            self._image_last[0] = time.time()

    def call_text(self, prompt: str, max_tokens: int = 2048) -> Optional[str]:
        """调用 Agnes 文本模型。"""
        self._acquire_text()
        is_retry = False
        recorded = False
        try:
            r = self._session.post(
                f'{self.BASE_URL}/v1/chat/completions',
                json={
                    "model": self.TEXT_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens
                },
                timeout=30
            )
            self._record_attempt(
                'text',
                'agnes',
                r.status_code,
                r.ok,
                retry=is_retry,
            )
            recorded = True
            if r.ok:
                return r.json().get('choices', [{}])[0].get('message', {}).get('content', '')
            if _is_quota_error(r.status_code, r.text):
                raise ProviderQuotaError(
                    f'Agnes 额度或鉴权不可用（HTTP {r.status_code}）'
                )
        except ProviderQuotaError:
            raise
        except Exception as e:
            if not recorded:
                self._record_attempt(
                    'text',
                    'agnes',
                    None,
                    False,
                    retry=is_retry,
                    error=e,
                )
            pass
        return None

    def call_vision(self, image_url: str, retries: int = 3) -> Optional[bool]:
        """调用 Agnes 视觉模型进行图审。含重试。"""
        attempt_number = 0
        for attempt in range(retries):
            self._acquire_text()
            is_retry = attempt_number > 0
            recorded = False
            try:
                r = self._session.post(
                    f'{self.BASE_URL}/v1/chat/completions',
                    json={
                        "model": self.TEXT_MODEL,
                        "messages": [{"role": "user", "content": [
                            {"type": "image_url", "image_url": {"url": image_url}},
                            {"type": "text", "text": self.REVIEW_PROMPT}
                        ]}],
                        "temperature": 0,
                        "max_tokens": 10
                    },
                    timeout=60
                )
                self._record_attempt(
                    'vision',
                    'agnes',
                    r.status_code,
                    r.ok,
                    retry=is_retry,
                )
                recorded = True
                attempt_number += 1
                if r.ok:
                    content = r.json().get('choices', [{}])[0].get('message', {}).get('content', '')
                    if content:
                        answer = re.match(r'^\s*(YES|NO)\b', content, re.IGNORECASE)
                        if answer:
                            return answer.group(1).upper() == 'YES'
                if _is_quota_error(r.status_code, r.text):
                    raise ProviderQuotaError(
                        f'Agnes 额度或鉴权不可用（HTTP {r.status_code}）'
                    )
                if r.status_code in (429, 503) and attempt < retries - 1:
                    wait = 60 * (2 ** attempt)
                    time.sleep(wait)
                    continue
            except ProviderQuotaError:
                raise
            except Exception as e:
                if not recorded:
                    self._record_attempt(
                        'vision',
                        'agnes',
                        None,
                        False,
                        retry=is_retry,
                        error=e,
                    )
                    attempt_number += 1
                pass
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
        return None

    def call_image_gen(self, image_url: str, size: str = "1024x1024",
                        retries: int = 5, is_variant: bool = False) -> Optional[str]:
        """调用 Agnes 图生图。503 队列满等 120s→240s→480s 重试。"""
        prompt = self.VARIANT_IMAGE_PROMPT if is_variant else self.MAIN_IMAGE_PROMPT
        attempt_number = 0
        for attempt in range(retries):
            self._acquire_image()
            is_retry = attempt_number > 0
            recorded = False
            try:
                r = self._session.post(
                    f'{self.BASE_URL}/v1/images/generations',
                    json={
                        "model": self.IMAGE_MODEL,
                        "prompt": prompt,
                        "size": size,
                        "extra_body": {
                            "image": [image_url],
                            "response_format": "url"
                        }
                    },
                    timeout=300
                )
                self._record_attempt(
                    'image_gen',
                    'agnes',
                    r.status_code,
                    r.ok,
                    retry=is_retry,
                )
                recorded = True
                attempt_number += 1
                if r.ok:
                    data = r.json().get('data', [])
                    if data and data[0].get('url'):
                        return data[0]['url']
                if _is_quota_error(r.status_code, r.text):
                    raise ProviderQuotaError(
                        f'Agnes 额度或鉴权不可用（HTTP {r.status_code}）'
                    )
                if r.status_code in (429, 503) and attempt < retries - 1:
                    wait = min(480, 120 * (2 ** attempt))
                    print(f'  [Agnes] {r.status_code} retry {wait}s ({attempt+1}/{retries})', flush=True)
                    time.sleep(wait)
                    continue
            except ProviderQuotaError:
                raise
            except Exception as e:
                if not recorded:
                    self._record_attempt(
                        'image_gen',
                        'agnes',
                        None,
                        False,
                        retry=is_retry,
                        error=e,
                    )
                    attempt_number += 1
                pass
            if attempt < retries - 1:
                time.sleep(5)
        return None


# =============================================================================
# 组合提供商（根据配置自动路由）
# =============================================================================

class CompositeProvider(ModelProvider):
    """组合多个提供商，按功能路由。"""

    def __init__(self, config: dict):
        """
        config 格式:
        {
            "text_provider": "deepseek" | "agnes",
            "vision_provider": "agnes",
            "image_gen_provider": "agnes",
            "deepseek_key": "...",
            "agnes_key": "..."
        }
        """
        self._config = config
        self._providers: dict[str, ModelProvider] = {}
        self._metrics_lock = threading.Lock()
        self._metrics = {
            'api_calls': 0,
            'api_errors': 0,
            'latency_s': 0.0,
            'http_attempts': 0,
            'http_errors': 0,
            'http_retries': 0,
            'http_status': {},
            'circuit_open': 0,
            'by_operation': {},
        }
        self._circuit = {}
        self._circuit_threshold = max(
            1,
            int(os.environ.get('CROSSPILOT_CIRCUIT_FAILURE_THRESHOLD', '8')),
        )
        self._circuit_cooldown_s = max(
            1,
            int(os.environ.get('CROSSPILOT_CIRCUIT_COOLDOWN_S', '60')),
        )

        # 初始化文本提供商
        text_provider = config.get('text_provider', 'deepseek')
        if text_provider == 'deepseek' and config.get('deepseek_key'):
            self._providers['text'] = DeepSeekProvider(config['deepseek_key'])
        elif text_provider == 'agnes' and config.get('agnes_key'):
            self._providers['text'] = AgnesProvider(config['agnes_key'])
        else:
            raise ValueError(f"未配置文本模型提供商: {text_provider}")

        # 初始化图审提供商
        vision_provider = config.get('vision_provider', 'agnes')
        if vision_provider == 'agnes' and config.get('agnes_key'):
            self._providers['vision'] = AgnesProvider(config['agnes_key'])
        else:
            raise ValueError(f"未配置图审模型提供商: {vision_provider}")

        # 初始化生图提供商
        image_gen_provider = config.get('image_gen_provider', 'agnes')
        if image_gen_provider == 'agnes' and config.get('agnes_key'):
            self._providers['image_gen'] = AgnesProvider(config['agnes_key'])
        else:
            raise ValueError(f"未配置生图模型提供商: {image_gen_provider}")

        for provider in self._providers.values():
            if hasattr(provider, 'set_attempt_hook'):
                provider.set_attempt_hook(self._record_attempt)

    def _provider_name(self, operation):
        provider = self._providers.get(operation)
        if provider is None:
            return 'unknown'
        name = provider.__class__.__name__.replace('Provider', '').lower()
        return name or 'unknown'

    def _circuit_key(self, operation):
        return f'{operation}:{self._provider_name(operation)}'

    def _is_circuit_open(self, operation):
        key = self._circuit_key(operation)
        state = self._circuit.get(key) or {}
        opened_until = float(state.get('opened_until') or 0)
        now = time.time()
        if opened_until > now:
            return True
        if opened_until:
            state['opened_until'] = 0
            state['failures'] = 0
            self._circuit[key] = state
        return False

    def _record_circuit_result(self, operation, success, terminal=False):
        key = self._circuit_key(operation)
        state = self._circuit.setdefault(key, {
            'failures': 0,
            'opened_until': 0,
        })
        if success:
            state['failures'] = 0
            state['opened_until'] = 0
            return
        state['failures'] = int(state.get('failures') or 0) + 1
        if terminal or state['failures'] >= self._circuit_threshold:
            state['opened_until'] = time.time() + self._circuit_cooldown_s

    def _record_logical_call(self, operation, elapsed, success, circuit_open=False):
        with self._metrics_lock:
            self._metrics['api_calls'] += 1
            self._metrics['latency_s'] += elapsed
            if not success:
                self._metrics['api_errors'] += 1
            if circuit_open:
                self._metrics['circuit_open'] += 1
            operation_metrics = self._metrics['by_operation'].setdefault(
                operation,
                {
                    'calls': 0,
                    'errors': 0,
                    'latency_s': 0.0,
                    'http_attempts': 0,
                    'http_errors': 0,
                    'http_retries': 0,
                    'circuit_open': 0,
                    'status': {},
                },
            )
            operation_metrics['calls'] += 1
            operation_metrics['latency_s'] += elapsed
            if not success:
                operation_metrics['errors'] += 1
            if circuit_open:
                operation_metrics['circuit_open'] += 1

    def _record_attempt(
        self,
        operation,
        provider,
        status_code=None,
        ok=False,
        retry=False,
        error=None,
    ):
        status_key = str(status_code) if status_code is not None else 'exception'
        with self._metrics_lock:
            self._metrics['http_attempts'] += 1
            if not ok:
                self._metrics['http_errors'] += 1
            if retry:
                self._metrics['http_retries'] += 1
            self._metrics['http_status'][status_key] = (
                self._metrics['http_status'].get(status_key, 0) + 1
            )
            operation_metrics = self._metrics['by_operation'].setdefault(
                operation,
                {
                    'calls': 0,
                    'errors': 0,
                    'latency_s': 0.0,
                    'http_attempts': 0,
                    'http_errors': 0,
                    'http_retries': 0,
                    'circuit_open': 0,
                    'status': {},
                },
            )
            operation_metrics['http_attempts'] += 1
            if not ok:
                operation_metrics['http_errors'] += 1
            if retry:
                operation_metrics['http_retries'] += 1
            operation_metrics['status'][status_key] = (
                operation_metrics['status'].get(status_key, 0) + 1
            )

    def _call(self, operation, fn, *args, **kwargs):
        started = time.perf_counter()
        if self._is_circuit_open(operation):
            self._record_logical_call(operation, 0.0, False, circuit_open=True)
            return None
        success = False
        terminal = False
        try:
            result = fn(*args, **kwargs)
            success = result is not None and result != ''
            return result
        except ProviderQuotaError:
            terminal = True
            raise
        except Exception:
            raise
        finally:
            elapsed = time.perf_counter() - started
            self._record_logical_call(operation, elapsed, success)
            self._record_circuit_result(operation, success, terminal=terminal)

    def metrics_snapshot(self):
        """Return a thread-safe snapshot of provider calls and HTTP attempts."""
        with self._metrics_lock:
            by_operation = {}
            for operation, values in self._metrics['by_operation'].items():
                calls = values['calls']
                by_operation[operation] = {
                    'calls': calls,
                    'errors': values['errors'],
                    'latency_s': round(values['latency_s'], 3),
                    'avg_latency_s': round(values['latency_s'] / max(calls, 1), 3),
                    'http_attempts': values.get('http_attempts', 0),
                    'http_errors': values.get('http_errors', 0),
                    'http_retries': values.get('http_retries', 0),
                    'circuit_open': values.get('circuit_open', 0),
                    'status': dict(values.get('status') or {}),
                }
            calls = self._metrics['api_calls']
            return {
                'api_calls': calls,
                'api_errors': self._metrics['api_errors'],
                'api_success_rate': (
                    round(1 - self._metrics['api_errors'] / calls, 3)
                    if calls else None
                ),
                'latency_s': round(self._metrics['latency_s'], 3),
                'http_attempts': self._metrics['http_attempts'],
                'http_errors': self._metrics['http_errors'],
                'http_retries': self._metrics['http_retries'],
                'http_status': dict(self._metrics['http_status']),
                'circuit_open': self._metrics['circuit_open'],
                'by_operation': by_operation,
            }

    def call_text(self, prompt: str, max_tokens: int = 2048, **kwargs) -> Optional[str]:
        return self._call(
            'text',
            self._providers['text'].call_text,
            prompt,
            max_tokens,
            **kwargs,
        )

    def call_vision(self, image_url: str, **kwargs) -> Optional[bool]:
        return self._call(
            'vision',
            self._providers['vision'].call_vision,
            image_url,
            **kwargs,
        )

    def call_image_gen(self, image_url: str, size: str = "1024x1024", is_variant: bool = False, **kwargs) -> Optional[str]:
        return self._call(
            'image_gen',
            self._providers['image_gen'].call_image_gen,
            image_url,
            size,
            is_variant=is_variant,
            **kwargs,
        )


# =============================================================================
# 全局单例
# =============================================================================

_provider: Optional[CompositeProvider] = None
_provider_lock = threading.Lock()


def get_provider() -> CompositeProvider:
    """获取全局模型提供商实例（线程安全）。"""
    global _provider
    with _provider_lock:
        if _provider is None:
            if not _KEYS.get('deepseek_key') and not _KEYS.get('agnes_key'):
                raise ValueError(
                    "未配置 API 密钥。请在 keys.json 中配置:\n"
                    '{\n'
                    '  "text_provider": "deepseek",\n'
                    '  "vision_provider": "agnes",\n'
                    '  "image_gen_provider": "agnes",\n'
                    '  "deepseek_key": "sk-...",\n'
                    '  "agnes_key": "cpk-..."\n'
                    '}'
                )
            _provider = CompositeProvider(_KEYS)
        return _provider


def reload_provider():
    """重新加载配置（线程安全）。"""
    global _provider
    with _provider_lock:
        _provider = None
        reload_keys()
