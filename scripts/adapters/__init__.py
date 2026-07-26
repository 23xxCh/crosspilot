"""适配器注册表 + 自动识别。

新增来源格式时：把新适配器 import 并加进 ADAPTERS 列表即可。
"""
from .ebay_tk import EbayTkAdapter
from .amazon_tk import AmazonTkAdapter

# 按优先级排列，先命中的先用
ADAPTERS = [
    EbayTkAdapter,
    AmazonTkAdapter,
    # ShopeeTkAdapter,   # 有 Shopee 样表后补上
]


def detect_adapter(ws):
    """遍历所有适配器，返回第一个识别成功的；都不认识返回 None。"""
    for ad in ADAPTERS:
        try:
            if ad.detect(ws):
                return ad
        except Exception as e:
            import sys
            print(f"[WARN] 适配器 {ad.__name__} 检测异常: {e}", file=sys.stderr)
            continue
    return None
