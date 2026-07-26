"""表格格式适配器基类。

每种来源表格（eBay/Shopee/Amazon/店小秘）一个适配器，声明：
1. detect(ws) — 看表头识别是不是自己的格式
2. cols — 列号映射（列号从 1 起），把"标题/描述/主图..."映射到该格式的实际列

新增来源：复制本文件改成 shopee_tk.py / amazon_tk.py，填 detect 和 cols 即可，
主流程（process_ebay_tk.py）完全不用动。
"""

class TableAdapter:
    # 格式名（日志/识别用）
    name = "base"

    # 目标工作表名（None 表示用活动表）
    sheet_name = None

    # 列号映射（列号从 1 起）。子类必须填。
    # 键固定，值是该列在表格中的实际列号：
    #   title       产品标题（清洗+翻译）
    #   desc        产品描述（清洗+翻译+注入图片）
    #   price       价格（改名为"本地展示价"）
    #   local_price 原本地展示价（整列删除）
    #   stock       库存（不动）
    #   main_image  主图 url（图审+图生图替换）
    #   attachments 附图列列表（图审，有水印则清空）
    #   variant     变种图 url（图审+图生图替换，不删除）
    #   video       视频链接（清空）
    #   size_image  尺码图（纳入附图审查）
    #   source_url  来源链接（参考，不处理）
    cols = {}

    @classmethod
    def detect(cls, ws):
        """看第一行表头判断是不是自己的格式。返回 True/False。子类必须实现。"""
        raise NotImplementedError

    @classmethod
    def header(cls, ws, col):
        """取表头字符串（小写）。"""
        v = ws.cell(1, col).value
        return str(v).lower() if v else ''
