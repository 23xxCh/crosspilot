"""共享常量：品牌列表等，避免 process_ebay_tk.py 和 process_amazon.py 重复定义。"""
import re

IMAGE_POLICY_VERSION = 'remove_people_v1'

PERSON_REMOVAL_INSTRUCTION = (
    "Remove every person and all human presence from the image, including models, "
    "faces, heads, hands, arms, legs, feet, bodies, silhouettes, reflections, "
    "mannequins, and people depicted in the background or printed graphics. "
    "Reconstruct any product area hidden by a person so the product remains complete "
    "and realistic. Do not add any person, mannequin, or human body part."
)

IMAGE_REMEDIATION_REVIEW_PROMPT = (
    "Inspect this e-commerce product image. Answer YES if the image needs remediation "
    "for either of these reasons:\n"
    "1. It contains a seller watermark, store ID, overlaid brand name/logo, or other "
    "seller-owned text that is not part of the product.\n"
    "2. It contains ANY person or human presence, including a model, face, head, hand, "
    "arm, leg, foot, body, silhouette, reflection, mannequin, or a person depicted in "
    "the background or printed graphics.\n\n"
    "Answer NO only when there is no human presence and no seller watermark/overlay. "
    "Product specifications, dimensions, feature callouts, and text physically printed "
    "on the product are not seller watermarks by themselves.\n\n"
    "Answer YES or NO only."
)

# 品牌名 + 平台名 + eBay 卖家 ID（标题/描述清洗时移除）
BRANDS = [
    # 汽车品牌（英文）
    'bmw', 'porsche', 'toyota', 'honda', 'mercedes', 'audi', 'vw', 'ford', 'hyundai',
    'nissan', 'kia', 'mazda', 'lexus', 'benz',
    # 消费电子品牌
    'xiaomi', 'redmi',
    # 平台名
    'joyon', 'shopee', 'lazada',
    # eBay 卖家 ID 常见模式
    'xmen', 'diy', 'smiling', 'htghtg', 'yrbwd', 'lemontree', 'jojo', 'zaofahua',
    # 汽车品牌（中文）
    '宝马', '保时捷', '小米', '红米', '丰田', '本田', '奔驰', '奥迪', '大众', '福特', '现代', '日产', '起亚', '马自达', '雷克萨斯',
]


def compile_brand_pattern(brands=BRANDS):
    """ASCII 品牌只匹配完整词，避免 audi/ford 破坏 audio/affordable。"""
    parts = []
    for brand in sorted(brands, key=len, reverse=True):
        escaped = re.escape(brand)
        if brand.isascii():
            parts.append(rf'(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])')
        else:
            parts.append(escaped)
    return re.compile('|'.join(parts), re.IGNORECASE)
