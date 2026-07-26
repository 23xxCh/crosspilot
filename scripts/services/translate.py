"""Translation and text cleaning service via model_provider.

这个文件现在使用 model_provider 进行所有文本调用，与具体模型解耦。
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pipeline_log import log as _log
from model_provider import ProviderQuotaError, get_provider

_CHINESE_RE = re.compile(r'[一-鿿]')
_BATCH_SIZE = 25

TRANSLATE_BATCH_PROMPT = (
    "Translate the following {count} texts to Vietnamese.\n"
    "Rules:\n"
    "- Preserve ALL product info: material, dimensions, specs, color, quantity, model numbers\n"
    "- Keep HTML tags (p, ul, li, br, img) and punctuation intact\n"
    "- Keep __IMG__ placeholder unchanged\n"
    "- If already Vietnamese, return unchanged\n"
    "Return JSON array: [{{\"index\": N, \"translation\": \"...\"}}] only. No other text.\n\n"
    "Texts:\n{texts}"
)

TRANSLATE_BATCH_PROMPT_CN = (
    "Translate the following {count} Chinese texts to Vietnamese.\n"
    "CRITICAL: Convert ALL Chinese (中文) characters to Vietnamese. No Chinese characters may remain in any output.\n"
    "Rules:\n"
    "- Preserve ONLY proper nouns: brand names, model numbers, part numbers (e.g. LED, ABS, BMW, Honda)\n"
    "- Preserve product info: material, dimensions, specs, color, quantity, model numbers\n"
    "- Keep HTML tags (p, ul, li, br, img) and punctuation intact\n"
    "- Keep __IMG__ placeholder unchanged\n"
    "Return JSON array: [{{\"index\": N, \"translation\": \"...\"}}] only.\n\n"
    "Texts:\n{texts}"
)

CLEAN_BATCH_PROMPT = (
    "Clean the following {count} product descriptions.\n"
    "Rules:\n"
    "- Remove ALL third-party brand names (BMW, Toyota, etc.), store names, trademarks\n"
    "- Remove return policy, payment, shipping, FAQ sections\n"
    "- Replace <img ...> tags with __IMG__ placeholder\n"
    "- Keep only product features and specifications\n"
    "Return JSON array: [{{\"index\": N, \"translation\": \"...\"}}] only.\n\n"
    "Texts:\n{texts}"
)

TITLE_TRANSLATE_PROMPT = (
    "Translate the following product title to Vietnamese.\n"
    "Rules:\n"
    "- KEEP product model numbers, brand-compatible vehicle names/years, part numbers, and technical codes in original form (do not translate proper nouns/specs).\n"
    "- Translate descriptive words, connectors, prepositions, and general terms to Vietnamese naturally.\n"
    "- Do NOT add, remove, or rewrite any original product meaning.\n"
    "Output only the translated title, no explanation or quotes.\n\n"
    "Title: {}"
)

TRANSLATE_PROMPT = (
    "Translate the following product text to Vietnamese.\n"
    "Rules:\n"
    "- Preserve ALL product information: material, dimensions, specifications, color, quantity, type, "
    "model numbers, product names, technical specs — keep numbers/units/model codes UNCHANGED.\n"
    "- Keep the original formatting, HTML tags (p, ul, li, br, img) and punctuation intact.\n"
    "- IMPORTANT: Keep the placeholder __IMG__ EXACTLY as-is (do not translate or remove it).\n"
    "- If already in Vietnamese, return unchanged.\n"
    "Output Vietnamese translation directly, no explanation or prefix/suffix.\n\n"
    "Text: {}"
)


class TranslationService:
    """Text translation and cleaning service via model_provider.
    Supports batch processing (25 texts/batch, 20 concurrent)."""

    TEXT_CONCURRENCY = 20
    MAX_RETRIES = 3

    def __init__(self, http_session=None):
        """初始化翻译服务。

        Args:
            http_session: 保留参数以兼容旧代码，实际使用 model_provider
        """
        self._provider = get_provider()

    def dmx_call(self, payload, max_tokens_override=None):
        """兼容旧接口：调用文本模型。

        Args:
            payload: API payload (包含 messages, model 等)
            max_tokens_override: 最大 token 数

        Returns:
            生成的文本内容
        """
        prompt = payload.get('messages', [{}])[0].get('content', '')
        max_tokens = max_tokens_override or payload.get('max_tokens', 2048)
        return self._provider.call_text(prompt, max_tokens)

    @staticmethod
    def _strip_code_fence(text):
        """Remove markdown code fences and surrounding quotes."""
        if not text:
            return text
        text = re.sub(r'^```\w*\s*', '', text)
        text = re.sub(r'\s*```$', '', text).strip()
        text = re.sub(r'^(?:["\'""])+', '', text)
        text = re.sub(r'(?:["\'""])+$', '', text)
        return text

    @staticmethod
    def _select_prompt(text, default_prompt):
        """Chinese detection → use aggressive Chinese→Vietnamese prompt."""
        if not _CHINESE_RE.search(text):
            return default_prompt
        return (
            "Translate the following Chinese product text to Vietnamese.\n"
            "- CRITICAL: Convert ALL Chinese (中文) characters to Vietnamese. No Chinese characters may remain.\n"
            "- Preserve ONLY proper nouns: brand names, model numbers, part numbers (e.g. LED, ABS, BMW, Honda).\n"
            "- Translate ALL other words to Vietnamese naturally.\n"
            "Output Vietnamese translation directly, no explanation or quotes.\n\n"
            "Text: {}"
        )

    def translate_text(self, text, prompt):
        """Translate single text to Vietnamese using model_provider."""
        if not text or not str(text).strip():
            return text
        text = str(text)
        has_chinese = bool(_CHINESE_RE.search(text))
        actual_prompt = self._select_prompt(text, prompt)

        try:
            content = self._provider.call_text(actual_prompt.format(text), max_tokens=2048)
            if content:
                result = self._strip_code_fence(content)
                if has_chinese and _CHINESE_RE.search(result):
                    _log.warn("translate_text 中文残留", text_preview=text[:60])
                    return text
                return result
        except ProviderQuotaError:
            raise
        except Exception as e:
            _log.warn("translate_text 调用异常", error=str(e))

        return text

    def _parse_batch_response(self, raw):
        """Parse JSON array from batch API response."""
        if not raw:
            return {}
        try:
            items = json.loads(raw)
            if isinstance(items, list):
                return {item['index']: item.get('translation', '')
                        for item in items if isinstance(item, dict) and 'index' in item}
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
        m = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', raw, re.DOTALL)
        if m:
            try:
                items = json.loads(m.group(1))
                return {item['index']: item.get('translation', '')
                        for item in items if isinstance(item, dict) and 'index' in item}
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        return {}

    def _process_batch(self, batch, prompt_template, label):
        """Process one batch, return {original: result}."""
        results = {}
        expected = len(set(batch))
        for attempt in range(2):
            try:
                indexed = [f"[{j}] {t}" for j, t in enumerate(batch)]
                prompt = prompt_template.format(count=len(batch), texts="\n".join(indexed))
                raw = self._provider.call_text(prompt, max_tokens=4096)
                parsed = self._parse_batch_response(raw)
                is_translate = 'translate' in label
                for idx, trans in parsed.items():
                    if idx < len(batch) and trans:
                        src = batch[idx]
                        if is_translate and _CHINESE_RE.search(src) and _CHINESE_RE.search(trans):
                            _log.warn(f"{label} 未翻成越南语", batch_index=idx, text_preview=src[:60])
                            continue
                        results[src] = trans
                if len(results) >= expected:
                    break
                if attempt == 0:
                    _log.warn(
                        f"{label} 响应不完整，重试",
                        expected=expected,
                        received=len(results),
                    )
            except ProviderQuotaError:
                raise
            except Exception as e:
                if attempt == 0:
                    _log.warn(f"{label} 重试", batch_size=len(batch), error=str(e))
        return results

    def batch_process(self, texts, prompt_template, label):
        """Generic batch processor with auto CN detection and 20 concurrent workers."""
        if not texts:
            return {}
        batches = [texts[i:i + _BATCH_SIZE] for i in range(0, len(texts), _BATCH_SIZE)]
        results = {}
        with ThreadPoolExecutor(max_workers=self.TEXT_CONCURRENCY) as pool:
            futures = {}
            for b in batches:
                pt = prompt_template
                if prompt_template is TRANSLATE_BATCH_PROMPT and any(_CHINESE_RE.search(t) for t in b):
                    pt = TRANSLATE_BATCH_PROMPT_CN
                futures[pool.submit(self._process_batch, b, pt, label)] = b
            for future in as_completed(futures):
                results.update(future.result())
        return results

    def batch_translate(self, texts):
        """Batch translate texts to Vietnamese."""
        return self.batch_process(texts, TRANSLATE_BATCH_PROMPT, "batch_translate")

    def batch_clean(self, texts):
        """Batch clean product descriptions."""
        return self.batch_process(texts, CLEAN_BATCH_PROMPT, "batch_clean")
