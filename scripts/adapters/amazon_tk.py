"""Amazon 采集表 → 回填表 适配器（7 列采集表）。"""
from .base import TableAdapter


class AmazonTkAdapter(TableAdapter):
    name = "Amazon→回填表"
    sheet_name = None  # 使用活动表

    cols = {
        'title': 2,           # 产品标题
        'desc': 3,            # 产品描述
        'main_image': 6,      # 产品图片链接（原样复制，不下载）
        'variant': 7,         # 变种图片链接（原样复制，不下载）
        # 以下字段 Amazon 不需要
        'price': None,
        'local_price': None,
        'stock': None,
        'video': None,
        'attachments': [],
        'size_image': None,
        'source_url': None,
    }

    @classmethod
    def detect(cls, ws):
        h2 = cls.header(ws, 2)
        h3 = cls.header(ws, 3)
        return ('产品标题' in h2 and '产品描述' in h3)
