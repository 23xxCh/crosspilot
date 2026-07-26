# 表格格式适配器 — 新增来源指南

系统用**适配器模式**支持多种来源表格（eBay/Shopee/Amazon/店小秘…）。
主流程 `process_ebay_tk.py` 不写死任何列号，启动时自动识别格式并注入列映射。

## 新增一个来源（如 Shopee→TikTok）

**两步，不用动主流程：**

### 1. 复制模板，填 detect + cols

复制 `ebay_tk.py` 为 `shopee_tk.py`，改两处：

```python
from .base import TableAdapter

class ShopeeTkAdapter(TableAdapter):
    name = "Shopee→TikTok"

    # 列号映射（列号从 1 起）——按你 Shopee 表格的实际列填
    cols = {
        'title': 2,           # 产品标题 在哪一列
        'desc': 3,            # 产品描述 在哪一列
        'price': 15,          # 价格 在哪一列
        'local_price': 16,    # 原本地展示价（要删的那列）
        'stock': 17,          # 库存
        'main_image': 18,     # 主图 url 在哪一列
        'attachments': [19,20,21,22,23,24,25,26,28],  # 附图列（列表）
        'variant': 29,        # 变种图
        'video': 27,          # 视频链接
        'size_image': 28,     # 尺码图
        'source_url': 36,     # 来源链接
    }

    @classmethod
    def detect(cls, ws):
        # 用几个关键表头识别这是 Shopee 表格（按你的表头关键字写）
        h2 = cls.header(ws, 2)
        h3 = cls.header(ws, 3)
        return ('标题' in h2 and '描述' in h3)
```

**没有某列怎么办？** 比如 Shopee 表没有"尺码图"：
- `attachments` 里别放那列就行
- 实在没有的字段（如 `source_url`），填一个空列号并在 detect 里说明（流程里 source_url 只是参考不处理）

### 2. 注册到 `__init__.py`

```python
from .shopee_tk import ShopeeTkAdapter

ADAPTERS = [
    EbayTkAdapter,
    ShopeeTkAdapter,   # 加进来
]
```

完成。下次拖 Shopee 表格进来就会自动识别。

## 适配器接口说明

| 键 | 含义 | 流程对它做什么 |
|----|------|---------------|
| `title` | 产品标题 | 规则去品牌 + 翻译越南语 |
| `desc` | 产品描述(HTML) | AI 清洗 + 翻译 + 注入图片 + 删模板图 |
| `price` | 价格列 | 表头改名"本地展示价" |
| `local_price` | 原本地展示价列 | **整列删除** |
| `stock` | 库存 | 不动 |
| `main_image` | 主图 url | 图审，有水印、品牌覆盖或人物则重生并抹除人物 |
| `attachments` | 附图列列表 | 图审，有水印、品牌覆盖或人物则清空 |
| `variant` | 变种图 url | 图审，有水印、品牌覆盖或人物则重生并抹除人物 |
| `video` | 视频链接 | 清空 |
| `size_image` | 尺码图 | 纳入附图审查（在 attachments 里） |
| `source_url` | 来源链接 | 仅参考，不处理 |

## 处理流程对所有格式通用

识别格式后，10 阶段处理完全一致：
图审(水印/品牌/人物) → 主图/变种生图抹除人物 → 问题附图清空 → 标题清洗翻译 →
描述清洗翻译 → 图片注入 → 模板图清理 → 视频清空 → 价格列 → 保存

**不同格式只影响"列在哪"，不影响"怎么处理"。**
