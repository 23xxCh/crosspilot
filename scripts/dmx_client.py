"""DMXAPI 共用客户端：图生图、图审、文本翻译共用的底层函数。
避免 process_ebay_tk.py 和 image_gen.py 中重复代码。"""
import json, os, time

DMX_BASE = "https://www.dmxapi.cn"

# 模型常量
WAN_MODEL = "wan2.7-image"
DOUBAO_MODEL = "doubao-seedream-5.0-lite"
GPT_IMAGE_MODEL = "gpt-image-2"

GEN_PROMPT = (
    "Based on the reference image, keep the exact same product unchanged. "
    "Only remove watermarks, logos, and brand text. "
    "Preserve product appearance, color, shape, and composition exactly."
)


def _post_json(session, endpoint, payload, timeout=60):
    """POST JSON 到 DMXAPI，使用共享 Session。返回 dict，失败返回 None。"""
    import requests
    try:
        r = session.post(f'{DMX_BASE}{endpoint}', json=payload,
                        timeout=(min(5, timeout // 3), timeout))
        return r.json() if r.ok else None
    except Exception:
        return None


def _extract_image_url(obj):
    """从 DMXAPI 响应中提取图片 URL。兼容万相/豆包/gpt-image-2 三种结构。"""
    if not isinstance(obj, dict):
        return ''
    if obj.get('data') and isinstance(obj['data'], list) and obj['data']:
        u = obj['data'][0].get('url', '')
        if u: return u
    for out in obj.get('output', []):
        iu = out.get('image_url')
        if isinstance(iu, dict) and iu.get('url', '').startswith('http'):
            return iu['url']
        for c in out.get('content', []):
            if c.get('type') == 'image' and c.get('text', '').startswith('http'):
                return c['text']
    return ''


def gen_wan(session, image_url, retries=2):
    """万相图生图 URL→URL。失败返回空串。"""
    for attempt in range(retries):
        try:
            payload = {
                "model": WAN_MODEL,
                "input": {"messages": [{"role": "user", "content": [
                    {"image": image_url}, {"text": GEN_PROMPT}]}]},
                "parameters": {"size": "1024*1024", "watermark": False}
            }
            obj = _post_json(session, "/v1/responses", payload, timeout=25)
            url = _extract_image_url(obj)
            if url: return url
        except Exception: pass
        if attempt < retries - 1: time.sleep(3 * (attempt + 1))
    return ''


def gen_doubao(session, image_url, retries=2):
    """豆包图生图 URL→URL。失败返回空串。"""
    for attempt in range(retries):
        try:
            payload = {
                "model": DOUBAO_MODEL,
                "input": GEN_PROMPT,
                "image": image_url,
                "size": "2K",
                "response_format": "url",
                "watermark": False
            }
            obj = _post_json(session, "/v1/responses", payload, timeout=25)
            url = _extract_image_url(obj)
            if url: return url
        except Exception: pass
        if attempt < retries - 1: time.sleep(3 * (attempt + 1))
    return ''


def generate_image(session, image_url):
    """图生图 fallback：万相 → 豆包。全失败返回 None。"""
    url = gen_wan(session, image_url)
    if url: return url
    return gen_doubao(session, image_url) or None
