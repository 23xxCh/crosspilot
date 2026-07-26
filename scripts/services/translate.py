"""Translation and text cleaning service via DMXAPI chat completions."""
import json, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pipeline_log import log as _log

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
    "Return JSON array: [{{\"index\": N, \"translation\": \"...\"}}] only. No other text.\n\n"
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
    """Text translation and cleaning service using DMXAPI.
    Supports batch processing (25 texts/batch, 20 concurrent) with model fallback chain."""

    TEXT_MODEL = "mimo-v2.5"
    TEXT_FALLBACK_MODELS = ["deepseek-v4-flash", "hy3", "step-3.5-flash"]
    TEXT_CONCURRENCY = 20
    MAX_RETRIES = 8

    def __init__(self, http_session):
        self._http = http_session

    @staticmethod
    def _err_msg(obj):
        return str(obj.get('error', {}).get('message', '')
                   if isinstance(obj.get('error'), dict) else obj.get('message', ''))

    def dmx_call(self, payload, max_tokens_override=None):
        """Call DMXAPI chat completions with model fallback chain."""
        primary_model = payload.get('model', self.TEXT_MODEL)
        models_to_try = [primary_model] + [m for m in self.TEXT_FALLBACK_MODELS if m != primary_model]

        if max_tokens_override:
            payload = {**payload, "max_completion_tokens": max_tokens_override}

        for model_idx, model in enumerate(models_to_try):
            payload['model'] = model
            retries = self.MAX_RETRIES if model_idx == 0 else 2
            for attempt in range(retries):
                try:
                    r = self._http.post(
                        'https://www.dmxapi.cn/v1/chat/completions',
                        json=payload, timeout=40
                    )
                    if not r.ok:
                        obj = r.json() if r.text else {}
                        msg = self._err_msg(obj).lower()
                        if 'rate' in msg or 'limit' in msg:
                            time.sleep(10)
                            continue
                        if attempt < retries - 1:
                            time.sleep(5)
                        continue
                    obj = r.json()
                    content = obj.get('choices', [{}])[0].get('message', {}).get('content', '')
                    if content:
                        if model_idx > 0:
                            print(f"[dmx] 备用模型 {model} 成功", flush=True)
                        return content
                    if attempt < retries - 1:
                        time.sleep(3)
                except Exception as e:
                    _log.warn("dmx_call HTTP 异常", error=str(e))
                    if attempt < retries - 1:
                        time.sleep(3)
        return None

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
        """Translate single text to Vietnamese. Tries all fallback models for Chinese input."""
        if not text or not str(text).strip():
            return text
        text = str(text)
        has_chinese = bool(_CHINESE_RE.search(text))
        actual_prompt = self._select_prompt(text, prompt)
        payload_base = {
            "max_completion_tokens": 2048,
            "messages": [{"role": "user", "content": actual_prompt.format(text)}]
        }

        models_to_try = [self.TEXT_MODEL] + [m for m in self.TEXT_FALLBACK_MODELS if m != self.TEXT_MODEL]
        for model in models_to_try:
            try:
                content = self.dmx_call({"model": model, **payload_base}, max_tokens_override=2048)
                if content:
                    result = self._strip_code_fence(content)
                    if has_chinese and _CHINESE_RE.search(result):
                        _log.warn("translate_text 中文残留，换模型重试", model=model, text_preview=text[:60])
                        continue
                    return result
            except Exception as e:
                _log.warn("translate_text 调用异常", model=model, error=str(e))
                continue

        _log.warn("translate_text 全部模型失败，保留原文", text_preview=text[:60])
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
        for attempt in range(2):
            try:
                indexed = [f"[{j}] {t}" for j, t in enumerate(batch)]
                prompt = prompt_template.format(count=len(batch), texts="\n".join(indexed))
                raw = self.dmx_call(
                    {"model": self.TEXT_MODEL, "max_completion_tokens": 4096,
                     "messages": [{"role": "user", "content": prompt}]},
                    max_tokens_override=4096
                )
                parsed = self._parse_batch_response(raw)
                is_translate = 'translate' in label
                for idx, trans in parsed.items():
                    if idx < len(batch) and trans:
                        src = batch[idx]
                        if is_translate and _CHINESE_RE.search(src) and _CHINESE_RE.search(trans):
                            _log.warn(f"{label} 未翻成越南语", batch_index=idx, text_preview=src[:60])
                            continue
                        results[src] = trans
                break
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
