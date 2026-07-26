"""兼容层：保持旧接口，内部使用 model_provider。

这个文件现在只是 model_provider 的包装，保持向后兼容。
新代码应该直接使用 model_provider。
"""
from __future__ import annotations
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model_provider import get_provider, reload_provider as _reload_provider

# =============================================================================
# 兼容旧接口
# =============================================================================

# Agnes 常量（保持兼容）
AGNES_MODEL = "agnes-image-2.1-flash"
AGNES_BASE = "https://apihub.agnes-ai.com"
AGNES_TEXT_MODEL = "agnes-2.0-flash"

# Prompts（保持兼容）
PERSON_REMOVAL_INSTRUCTION = (
    "Remove every person and all human presence from the image, including models, "
    "faces, heads, hands, arms, legs, feet, bodies, silhouettes, reflections, "
    "mannequins, and people depicted in the background or printed graphics. "
    "Reconstruct any product area hidden by a person so the product remains complete "
    "and realistic. Do not add any person, mannequin, or human body part."
)

AGNES_MAIN_PROMPT = (
    "Create a 1600x1600 e-commerce main product photo from the reference image. "
    "Pure white background (#FFFFFF), no shadows, no watermarks, no borders, "
    "no text, no logos, no brand marks. " + PERSON_REMOVAL_INSTRUCTION + " "
    "Product occupies approximately 85% of the frame, "
    "centered, front view clearly visible, do not crop key parts. Even studio lighting, "
    "realistic product texture and material quality. "
    "CRITICAL: Strictly preserve the product's exact shape, contours, dimensions, "
    "hole positions, and external structure. Do NOT modify the product design, "
    "do not change size proportions, openings, or exterior structure."
)

AGNES_VARIANT_PROMPT = (
    "Generate a clean product variant photo from the reference image. "
    "Remove ALL brand names, logos, watermarks, store IDs, and overlaid text. "
    + PERSON_REMOVAL_INSTRUCTION + " "
    "Keep the product itself unchanged in color, shape, texture, and composition. "
    "No brand marks, no logos, no watermarks."
)

AGNES_PROMPT = AGNES_MAIN_PROMPT


class AgnesQuotaError(RuntimeError):
    """Agnes API 额度/余额耗尽。"""


class DeepSeekQuotaError(RuntimeError):
    """DeepSeek API 额度/余额耗尽。"""


def reload_credentials():
    """热加载配置。"""
    _reload_provider()


def deepseek_call(session, payload: dict, retries: int = 3, timeout: int = 60) -> str | None:
    """兼容接口：调用 DeepSeek 进行文本生成。

    Args:
        session: 保留参数以兼容旧代码，实际使用 model_provider
        payload: API payload
        retries: 重试次数
        timeout: 超时时间

    Returns:
        生成的文本内容
    """
    provider = get_provider()
    prompt = payload.get('messages', [{}])[0].get('content', '')
    max_tokens = payload.get('max_tokens', 2048)
    return provider.call_text(prompt, max_tokens)


def agnes_review(session, image_url: str, retries: int = 3) -> bool | None:
    """兼容接口：Agnes 图审。

    Args:
        session: 保留参数以兼容旧代码
        image_url: 图片 URL
        retries: 重试次数

    Returns:
        True(需处理)/False(干净)/None(失败)
    """
    provider = get_provider()
    return provider.call_vision(image_url)


def gen_agnes_rate_limited(session, image_url: str, retries: int = 5,
                           prompt: str | None = None, size: str = "1600x1600") -> str:
    """兼容接口：Agnes 图生图。

    Args:
        session: 保留参数以兼容旧代码
        image_url: 原图 URL
        retries: 重试次数
        prompt: 保留参数，实际使用 model_provider 的默认 prompt
        size: 图片尺寸

    Returns:
        新图片 URL
    """
    provider = get_provider()
    return provider.call_image_gen(image_url, size) or ''


# 保持旧别名
gen_agnes = gen_agnes_rate_limited


# 已移除的函数（返回空/None以保持兼容）
def gen_wan(*args, **kwargs) -> str:
    """已移除：万相生图。"""
    return ''


def gen_doubao(*args, **kwargs) -> str:
    """已移除：豆包生图。"""
    return ''


def generate_image(*args, **kwargs) -> str | None:
    """已移除：万相→豆包 fallback。请使用 gen_agnes_rate_limited。"""
    return None


def gen_gemini_image(*args, **kwargs) -> str:
    """已移除：Gemini 生图。"""
    return ''


# 内部工具函数（保持兼容）
def _agnes_is_quota(status_code: int | None, body: str = '') -> bool:
    """检查是否为 Agnes 额度错误。"""
    if status_code in (401, 402, 403):
        return True
    text = str(body or '').lower()
    keys = (
        'quota', 'insufficient', 'balance', 'billing', 'payment',
        'exceeded', '额度', '余额', '欠费', '用尽', '不足',
        'credit', 'out of credits', 'payment required',
    )
    if any(k in text for k in keys):
        return True
    if status_code == 429 and any(k in text for k in ('quota', '额度', 'exceeded', 'billing')):
        return True
    return False


# 限速相关（model_provider 内部已处理，这里保留空函数兼容）
def _text_acquire():
    """已弃用：model_provider 内部处理限速。"""
    pass
