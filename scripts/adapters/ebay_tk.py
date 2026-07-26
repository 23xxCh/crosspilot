"""eBay → TikTok Shop 越南站 表格适配器（45 列店小秘模板）。"""
from .base import TableAdapter


class EbayTkAdapter(TableAdapter):
    name = "eBay→TikTok"
    sheet_name = 'tiktok_chanpin_'

    cols = {
        'title': 2,           # 产品标题
        'desc': 3,            # Tiktok产品描述
        'price': 15,          # 价格(站点币种) → 改名"本地展示价"
        'local_price': 16,    # 本地展示价 → 删除整列
        'stock': 17,          # 库存 → 不动
        'main_image': 18,     # 主图(url)地址
        'attachments': [19, 20, 21, 22, 23, 24, 25, 26, 28],  # 附图1-8 + 尺码图
        'variant': 29,        # 变种主题1图片
        'video': 27,          # 视频连接
        'size_image': 28,     # 尺码图
        'source_url': 36,     # 来源Url
    }

    @classmethod
    def detect(cls, ws):
        # 关键列组合识别：产品标题 + Tiktok产品描述 + 主图(url)地址 + 来源Url
        h2 = cls.header(ws, 2)
        h3 = cls.header(ws, 3)
        h18 = cls.header(ws, 18)
        h36 = cls.header(ws, 36)
        return ('标题' in h2 and '描述' in h3 and '主图' in h18 and '来源' in h36)
