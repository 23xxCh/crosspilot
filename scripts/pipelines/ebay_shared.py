"""eBay 管道共享模块：API keys、服务实例、常量、工具函数。
使用 model_provider 进行所有 AI 调用，与具体模型解耦。
"""
import json
import os
import re
import time
import tempfile
import threading

from crosspilot.prompt_registry import get_prompt_registry

from ..pipeline_log import log as _log, new_request_id, PipelineMetrics
from ..model_provider import (
    ProviderQuotaError,
    get_provider,
    reload_provider as _reload_provider,
)
from ..services import TranslationService
from ..services.constants import compile_brand_pattern
import requests as _requests
from requests.adapters import HTTPAdapter as _HTTPAdapter
from ..concurrency import configured_concurrency

# === 常量 ===
TMP_DIR = tempfile.gettempdir()
GEN_CONCURRENCY = configured_concurrency('image_gen', 15, maximum=30)
TEXT_CONCURRENCY = configured_concurrency('text', 20, maximum=50)
REVIEW_CONCURRENCY = configured_concurrency('review', 100, maximum=150)
MAX_RETRIES = 8

# === API keys 加载 ===
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_keys():
    keys = {}
    kf = os.environ.get('CROSSPILOT_KEYS_PATH') or os.path.join(_ROOT, 'keys.json')
    if os.path.exists(kf):
        try:
            with open(kf, encoding='utf-8') as f:
                keys = json.load(f)
        except Exception as e:
            _log.warn("keys.json 读取失败", error=str(e))
    return keys


_KEYS = _load_keys()


def reload_credentials():
    """重新读取 keys.json，并刷新 model_provider。"""
    _reload_provider()


# 初始化时打印配置信息
def _print_provider_info():
    """打印当前模型配置。"""
    try:
        provider = get_provider()
        print(f"Model Provider 已初始化")
        print(f"  文本模型: {provider._providers.get('text', 'N/A')}")
        print(f"  图审模型: {provider._providers.get('vision', 'N/A')}")
        print(f"  生图模型: {provider._providers.get('image_gen', 'N/A')}")
    except Exception as e:
        print(f"Model Provider 初始化失败: {e}")


# === HTTP Session（保留用于其他 HTTP 调用）===
_http = _requests.Session()
_adapter = _HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=0)
_http.mount('https://', _adapter)

# === Service instances & column mapping ===
_translate_svc = TranslationService()

_cols = threading.local()


def _init_col_defaults():
    _cols.main = 18
    _cols.att = [19,20,21,22,23,24,25,26,28]
    _cols.variant = 29
    _cols.title = 2
    _cols.desc = 3
    _cols.price = 15
    _cols.local_price = 16
    _cols.stock = 17
    _cols.video = 27


_init_col_defaults()


def _apply_adapter_cols(cols):
    _cols.main = cols['main_image']
    _cols.att = list(cols['attachments'])
    _cols.variant = cols['variant']
    _cols.title = cols['title']
    _cols.desc = cols['desc']
    _cols.price = cols['price']
    _cols.local_price = cols['local_price']
    _cols.stock = cols['stock']
    _cols.video = cols['video']


_DASHBOARD_HOOK = None


# === StatusReporter ===
class StatusReporter:
    STAGES = ['提取图片URL', 'Agnes图审', '图生图', '附图清空', '标题清洗+翻译',
              '描述AI清洗', '描述翻译', '嵌入+注入图片', '视频+模板图清理', '价格列+保存']

    def __init__(self, table_path):
        self.status_path = os.path.splitext(table_path)[0] + '_status.json'
        self.t_start = time.time()
        self.stage_idx = 0
        self.stage_t0 = self.t_start
        self.total = 0

    def _write(self, current):
        elapsed = time.time() - self.stage_t0
        total_elapsed = time.time() - self.t_start
        eta = int(elapsed / current * (self.total - current)) if current > 0 and self.total > 0 else 0
        pct = int(current / self.total * 100) if self.total > 0 else 0
        data = {
            'status': 'running',
            'stage': self.STAGES[self.stage_idx],
            'stage_index': self.stage_idx + 1,
            'stage_total': len(self.STAGES),
            'current': current, 'total': self.total, 'percent': pct,
            'elapsed_s': int(elapsed), 'eta_s': eta,
            'total_elapsed_s': int(total_elapsed),
            'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        try:
            with open(self.status_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            _log.warn("status.json 写入失败", error=str(e))
        return data

    def start_stage(self, name, total=0):
        try:
            self.stage_idx = self.STAGES.index(name)
        except ValueError:
            self.stage_idx = len(self.STAGES) - 1
        self.total = total
        self.stage_t0 = time.time()
        ts = time.strftime('%H:%M:%S')
        print(f"[{ts}] ===== 阶段 {self.stage_idx+1}/{len(self.STAGES)}: {name} ({total} 项) =====", flush=True)
        self._write(0)

    def update(self, current, force=False):
        should_log = force or current == self.total or current % 20 == 0 or (
            self.total > 0 and current % max(1, self.total // 10) == 0)
        if should_log:
            d = self._write(current)
            print(f"[{time.strftime('%H:%M:%S')}] {d['stage']} {current}/{self.total} "
                  f"({d['percent']}%) | 已用 {d['elapsed_s']}s | 预计剩余 {d['eta_s']}s", flush=True)
            if _DASHBOARD_HOOK:
                try: _DASHBOARD_HOOK()
                except Exception as e:
                    _log.warn("dashboard_hook 失败", error=str(e)[:100])

    def finish(self, output_path):
        try:
            with open(self.status_path, 'w', encoding='utf-8') as f:
                json.dump({'stage': '完成', 'output': output_path,
                           'status': 'done', 'percent': 100, 'eta_s': 0,
                           'total_elapsed_s': int(time.time() - self.t_start),
                           'updated_at': time.strftime('%Y-%m-%d %H:%M:%S')},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            _log.warn("status.json finish 写入失败", error=str(e))


# === Text helpers ===
def _err_msg(obj):
    return _translate_svc._err_msg(obj)


def deepseek_text_call(payload, max_tokens_override=None):
    """调用文本模型（通过 model_provider）。"""
    provider = get_provider()
    prompt = payload.get('messages', [{}])[0].get('content', '')
    max_tokens = max_tokens_override or payload.get('max_tokens', 2048)
    return provider.call_text(prompt, max_tokens)


# === Review helpers ===
def review_single(image_url: str) -> bool | None:
    """检测水印、品牌覆盖或人物（通过 model_provider）。"""
    provider = get_provider()
    return provider.call_vision(image_url)


_BRAND_PATTERN = compile_brand_pattern()


def rule_strip_brands(t: str | None) -> str | None:
    if not t: return t
    t = _BRAND_PATTERN.sub('', str(t))
    return re.sub(r'\s+', ' ', t).strip()


# === Text cleaning & translation ===
_prompts = get_prompt_registry()
DESC_PROMPT = _prompts.get("ebay.description_clean")

_CHINESE_RE = re.compile(r'[一-鿿]')


def _strip_code_fence(content):
    return _translate_svc._strip_code_fence(content)


def clean_text_ai(text, prompt):
    if not text or not str(text).strip(): return text
    text = str(text)
    try:
        provider = get_provider()
        content = provider.call_text(
            _translate_svc.render_text_prompt(prompt, text),
            max_tokens=2048,
        )
        if content is not None:
            content = _strip_code_fence(content)
            if content: return rule_strip_brands(content)
    except ProviderQuotaError:
        raise
    except Exception as e:
        _log.warn("clean_text_ai 异常", error=str(e))
    return rule_strip_brands(text)


TRANSLATE_PROMPT = _prompts.get("translation.text")
TITLE_TRANSLATE_PROMPT = _prompts.get("translation.title")


def _select_prompt(text, default_prompt):
    return _translate_svc._select_prompt(text, default_prompt)


def translate_text(text, prompt):
    # Resolve the service lazily so credentials changed from the settings page
    # take effect without retaining the provider captured at module import time.
    return TranslationService().translate_text(text, prompt)


# Image tag regex for extracting img URLs from HTML
IMG_TAG_RE = re.compile(r'<img[^>]*src=["\']([^"\']*)["\'][^>]*>', re.IGNORECASE)


def embed_new_images_in_desc(html, img_queue, gen_results, cleared_att_urls):
    if not html: return html
    it = iter(img_queue)
    def _replace(_m):
        try:
            src = next(it)
        except StopIteration:
            return ''
        if src in gen_results: return f'<img src="{gen_results[src]}"/>'
        if src in cleared_att_urls: return ''
        return f'<img src="{src}"/>'
    result = re.sub(r'__IMG__', _replace, html)
    return result


# === Image generation ===
def _gen_image(image_url: str, size: str = "1600x1600", is_variant: bool = False) -> str:
    """图生图（通过 model_provider）。"""
    provider = get_provider()
    return provider.call_image_gen(image_url, size, is_variant=is_variant) or ''


# === Batch text processing ===
_BATCH_SIZE = 25


def _parse_batch_response(raw, expected_count):
    return _translate_svc._parse_batch_response(raw)


def _process_batch(batch, prompt_template, label):
    return TranslationService()._process_batch(batch, prompt_template, label)


def _batch_process(texts, prompt_template, label):
    return TranslationService().batch_process(texts, prompt_template, label)


def batch_translate_texts(texts):
    return TranslationService().batch_translate(texts)


def batch_clean_texts(texts):
    return TranslationService().batch_clean(texts)
