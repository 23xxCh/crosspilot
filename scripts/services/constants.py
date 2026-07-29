"""共享常量：品牌列表等，避免 process_ebay_tk.py 和 process_amazon.py 重复定义。"""
import re

from crosspilot.prompt_registry import get_prompt_registry


IMAGE_POLICY_VERSION = 'remove_people_v1'

_prompts = get_prompt_registry()
PERSON_REMOVAL_INSTRUCTION = _prompts.get("images.person_removal")
IMAGE_REMEDIATION_REVIEW_PROMPT = _prompts.get("images.review")

# 可作为“适配品牌”保留在标题 for 后面的真实厂商品牌。
# 键统一为小写，值是标题中的规范写法。
COMPATIBILITY_BRAND_ALIASES = {
    # 汽车品牌（英文）
    'acura': 'Acura',
    'alfa romeo': 'Alfa Romeo',
    'audi': 'Audi',
    'bentley': 'Bentley',
    'bmw': 'BMW',
    'buick': 'Buick',
    'cadillac': 'Cadillac',
    'chevrolet': 'Chevrolet',
    'chrysler': 'Chrysler',
    'citroen': 'Citroen',
    'dacia': 'Dacia',
    'daewoo': 'Daewoo',
    'daihatsu': 'Daihatsu',
    'dodge': 'Dodge',
    'fiat': 'Fiat',
    'ford': 'Ford',
    'gmc': 'GMC',
    'holden': 'Holden',
    'honda': 'Honda',
    'hummer': 'Hummer',
    'hyundai': 'Hyundai',
    'infiniti': 'Infiniti',
    'isuzu': 'Isuzu',
    'jaguar': 'Jaguar',
    'jeep': 'Jeep',
    'kia': 'Kia',
    'land rover': 'Land Rover',
    'lexus': 'Lexus',
    'lincoln': 'Lincoln',
    'maserati': 'Maserati',
    'mazda': 'Mazda',
    'mercedes': 'Mercedes-Benz',
    'mercedes benz': 'Mercedes-Benz',
    'mercedes-benz': 'Mercedes-Benz',
    'benz': 'Mercedes-Benz',
    'mini cooper': 'MINI Cooper',
    'mitsubishi': 'Mitsubishi',
    'nissan': 'Nissan',
    'opel': 'Opel',
    'peugeot': 'Peugeot',
    'porsche': 'Porsche',
    'renault': 'Renault',
    'saab': 'Saab',
    'scion': 'Scion',
    'skoda': 'Skoda',
    'subaru': 'Subaru',
    'suzuki': 'Suzuki',
    'tesla': 'Tesla',
    'toyota': 'Toyota',
    'vauxhall': 'Vauxhall',
    'volkswagen': 'Volkswagen',
    'volvo': 'Volvo',
    'vw': 'Volkswagen',
    # 消费电子品牌
    'redmi': 'Redmi',
    'xiaomi': 'Xiaomi',
    # 中文别名
    '宝马': 'BMW',
    '保时捷': 'Porsche',
    '小米': 'Xiaomi',
    '红米': 'Redmi',
    '丰田': 'Toyota',
    '本田': 'Honda',
    '奔驰': 'Mercedes-Benz',
    '奥迪': 'Audi',
    '大众': 'Volkswagen',
    '福特': 'Ford',
    '现代': 'Hyundai',
    '日产': 'Nissan',
    '起亚': 'Kia',
    '马自达': 'Mazda',
    '雷克萨斯': 'Lexus',
    '铃木': 'Suzuki',
}

COMPATIBILITY_BRANDS = list(COMPATIBILITY_BRAND_ALIASES)

# 这些只是平台、店铺或营销清理词，绝不能作为适配品牌写到 for 后面。
STRIP_ONLY_BRANDS = [
    'joyon', 'shopee', 'lazada',
    'xmen', 'diy', 'smiling', 'htghtg', 'yrbwd', 'lemontree', 'jojo',
    'zaofahua',
]

# 向后兼容：描述、关键词和 eBay 管道仍可一次清理全部品牌类词。
BRANDS = list(dict.fromkeys([
    *COMPATIBILITY_BRANDS,
    *STRIP_ONLY_BRANDS,
]))


def compile_brand_pattern(brands=None):
    """ASCII 品牌只匹配完整词，避免 audi/ford 破坏 audio/affordable。"""
    if brands is None:
        brands = BRANDS
    parts = []
    for brand in sorted(brands, key=len, reverse=True):
        escaped = re.escape(brand)
        if brand.isascii():
            parts.append(rf'(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])')
        else:
            parts.append(escaped)
    return re.compile('|'.join(parts), re.IGNORECASE)
