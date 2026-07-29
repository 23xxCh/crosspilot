"""使用 model_provider 的示例代码。

这个文件展示了如何在新代码中使用 model_provider，
以及如何将旧代码迁移到新的架构。
"""
if __package__ in {None, ""}:
    from _bootstrap import ensure_package_imports
    ensure_package_imports()

# =============================================================================
# 新代码使用方式（推荐）
# =============================================================================

def example_new_code():
    """新代码应该这样写 —— 完全与具体模型解耦。"""
    from scripts.model_provider import get_provider

    provider = get_provider()

    # 文本生成
    result = provider.call_text("Translate to Vietnamese: Hello world")
    print(f"文本结果: {result}")

    # 图审
    needs_fix = provider.call_vision("https://example.com/image.jpg")
    print(f"需要整改: {needs_fix}")

    # 图生图
    new_url = provider.call_image_gen("https://example.com/image.jpg", size="1600x1600")
    print(f"新图片: {new_url}")


# =============================================================================
# 如何添加新的模型提供商
# =============================================================================

def example_add_new_provider():
    """
    如果要添加 OpenAI 支持，只需：

    1. 在 model_provider.py 中添加新类：

        class OpenAIProvider(ModelProvider):
            BASE_URL = "https://api.openai.com"

            def call_text(self, prompt, max_tokens=2048):
                # 实现...
                pass

            def call_vision(self, image_url):
                # 实现...
                pass

            def call_image_gen(self, image_url, size="1600x1600"):
                # 实现...
                pass

    2. 在 CompositeProvider.__init__ 中添加路由逻辑

    3. 在 keys.json 中配置：
        {
            "text_provider": "openai",
            "openai_key": "sk-..."
        }

    完成！不需要修改任何业务代码。
    """
    pass


# =============================================================================
# 切换模型的正确方式
# =============================================================================

def example_switch_model():
    """
    假设现在要把文本模型从 DeepSeek 换成 OpenAI：

    只需要修改 keys.json：
        {
            "text_provider": "openai",      # 从 "deepseek" 改成 "openai"
            "vision_provider": "agnes",      # 保持不变
            "image_gen_provider": "agnes",   # 保持不变
            "openai_key": "sk-...",          # 添加 OpenAI key
            "agnes_key": "cpk-..."           # 保持不变
        }

    代码一行不改！
    """
    pass


# =============================================================================
# 迁移旧代码的指南
# =============================================================================

"""
旧代码迁移对照表：

旧代码 (dmx_client.py):
    from scripts.dmx_client import deepseek_call
    result = deepseek_call(session, payload)

新代码:
    from scripts.model_provider import get_provider
    provider = get_provider()
    result = provider.call_text(prompt)

旧代码 (ebay_shared.py):
    from scripts.pipelines.ebay_shared import (
        dmx_call,
        review_single,
        _gen_image,
    )
    text = dmx_call(payload)
    needs_fix = review_single(url)
    new_url = _gen_image(session, url)

新代码:
    from scripts.model_provider import get_provider
    provider = get_provider()
    text = provider.call_text(prompt)
    needs_fix = provider.call_vision(url)
    new_url = provider.call_image_gen(url)

旧代码 (process_amazon.py):
    from scripts.dmx_client import deepseek_call, gen_agnes_rate_limited
    text = deepseek_call(session, payload)
    new_url = gen_agnes_rate_limited(session, url)

新代码:
    from scripts.model_provider import get_provider
    provider = get_provider()
    text = provider.call_text(prompt)
    new_url = provider.call_image_gen(url)
"""
