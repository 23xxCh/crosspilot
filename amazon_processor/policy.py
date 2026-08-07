"""Amazon 文案与图片处理共用的确定性业务策略。"""
import re

from .config.prompts import get_prompt_registry


IMAGE_POLICY_VERSION = 'reference_edit_text_translate_v1'

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
    '特斯拉': 'Tesla',
    '讴歌': 'Acura',
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
    '雪佛兰': 'Chevrolet',
    '吉普': 'Jeep',
    '道奇': 'Dodge',
    '别克': 'Buick',
    '凯迪拉克': 'Cadillac',
    '林肯': 'Lincoln',
    '沃尔沃': 'Volvo',
}

COMPATIBILITY_BRANDS = list(COMPATIBILITY_BRAND_ALIASES)

# 这些只是平台、店铺或营销清理词，绝不能作为适配品牌写到 for 后面。
STRIP_ONLY_BRANDS = [
    'joyon', 'shopee', 'lazada',
    'xmen', 'diy', 'smiling', 'htghtg', 'yrbwd', 'lemontree', 'jojo',
    'zaofahua',
]

# 描述与关键词需要一次清理全部品牌类词。
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


_BUTTON_BATTERY_PATTERN = (
    r"(?:button\s+batter(?:y|ies)|"
    r"(?:pila|bater[ií]a)s?\s+(?:de\s+)?bot[oó]n|"
    r"(?:pilha|bateria)s?\s+(?:tipo\s+)?bot[aã]o|"
    r"knopf(?:zelle|zellen|batterie|batterien)|"
    r"pile?s?\s+bouton|"
    r"batteri[ae]\s+a\s+bottone)"
)
_AUDIENCE_PATTERN = (
    r"(?:boys?|girls?|kids|"
    r"niñ[oa]s?|menina?s?|meninos?|crianças?|"
    r"junge[n]?|mädchen|kinder|"
    r"garçons?|filles?|enfants?|"
    r"ragazz[oaie]|bambin[oaie])"
)
PROHIBITED_LISTING_TERMS_RE = re.compile(
    rf"\b(?:{_BUTTON_BATTERY_PATTERN}|{_AUDIENCE_PATTERN})\b",
    re.IGNORECASE,
)
_BUTTON_BATTERY_RE = re.compile(
    rf"\b{_BUTTON_BATTERY_PATTERN}\b",
    re.IGNORECASE,
)
_AUDIENCE_TERM_RE = re.compile(
    rf"\b{_AUDIENCE_PATTERN}\b",
    re.IGNORECASE,
)
_BATTERY_WORD_RE = re.compile(
    r"\b(?:batter(?:y|ies)|bater[ií]as?|pilas?|pilhas?|"
    r"batterie[n]?|piles?)\b",
    re.IGNORECASE,
)
_BATTERY_SPEC_RE = re.compile(
    r"(?:\bBattery\s*:\s*)?(?:Built[- ]?in\s+)?"
    r"button\s+batter(?:y|ies)\b"
    r"(?:\s+Battery\s+Life\s*:\s*.*?)?"
    r"(?=\s+(?:Material|Size|Colou?r|Waterproof|Quantity|Package|"
    r"Item|Voltage|Power)\s*:|[.!?;\n]|$)",
    re.IGNORECASE,
)
_LABELED_BATTERY_RE = re.compile(
    r"\bBattery(?:\s+Life)?\s*:\s*.*?"
    r"(?=\s+[A-Z][A-Za-z ]{1,20}\s*:|[.!?;\n]|$)",
    re.IGNORECASE,
)


def _clean_listing_spacing(value: object, *, multiline: bool = False) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _AUDIENCE_TERM_RE.sub(" ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,;])(?:\s*[,;])+", r"\1", text)
    text = re.sub(
        r"\b(?:for|with|and|or)\s+(?=(?:for|with|and|or)\b)",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?:^|(?<=[,.;!?]))\s*(?:and|or)\b\s*",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:for|with|and|or)\b(?=\s*[,.;:!?]|\s*$)",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"[ \t]+", " ", text)
    if multiline:
        lines = [line.strip(" ,;:-") for line in text.split("\n")]
        text = "\n".join(lines)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
    return text.strip(" ,;:-")


def _strip_button_battery_description(value: object) -> str:
    text = str(value or "")
    text = _BATTERY_SPEC_RE.sub(" ", text)
    text = _LABELED_BATTERY_RE.sub(" ", text)
    kept_lines = []
    for line in text.splitlines():
        parts = re.split(r"(?<=[.!?])\s+", line)
        kept_lines.append(
            " ".join(
                part for part in parts
                if not _BATTERY_WORD_RE.search(part)
                and not _BUTTON_BATTERY_RE.search(part)
            )
        )
    return _clean_listing_spacing("\n".join(kept_lines), multiline=True)


def _sanitize_simple_text(value: object, *, remove_battery: bool) -> str:
    text = str(value or "")
    if remove_battery:
        text = _BUTTON_BATTERY_RE.sub(" ", text)
        text = re.sub(
            r"\b(?:built[- ]?in|replaceable)\s+batter(?:y|ies)\b",
            " ",
            text,
            flags=re.IGNORECASE,
        )
    return _clean_listing_spacing(text)


def enforce_prohibited_listing_terms(rows: list[dict]) -> list[dict]:
    """Remove prohibited marketplace terms after every AI text stage."""
    from .quality import (
        fingerprint_text,
        normalize_keywords_for_row,
        split_keywords,
    )
    from .text.listing import source_bullet_candidates

    for row in rows:
        source_values = [
            row.get("title", ""),
            row.get("subtitle", ""),
            row.get("desc", ""),
            *(row.get("bullets") or []),
            row.get("keywords", ""),
        ]
        if not any(
            PROHIBITED_LISTING_TERMS_RE.search(str(value or ""))
            for value in source_values
        ):
            continue
        remove_battery = any(
            _BUTTON_BATTERY_RE.search(str(value or ""))
            for value in source_values
        )

        row["title"] = _sanitize_simple_text(
            row.get("title", ""),
            remove_battery=remove_battery,
        )
        row["subtitle"] = ", ".join(
            cleaned
            for part in split_keywords(row.get("subtitle", ""))
            if not (remove_battery and _BATTERY_WORD_RE.search(part))
            if (cleaned := _sanitize_simple_text(
                part,
                remove_battery=remove_battery,
            ))
        )
        row["desc"] = (
            _strip_button_battery_description(row.get("desc", ""))
            if remove_battery
            else _clean_listing_spacing(row.get("desc", ""), multiline=True)
        )

        bullets = list(row.get("bullets") or [])[:5]
        bullets.extend([""] * (5 - len(bullets)))
        for index, bullet in enumerate(bullets):
            if remove_battery and _BATTERY_WORD_RE.search(str(bullet or "")):
                bullets[index] = ""
            else:
                bullets[index] = _sanitize_simple_text(
                    bullet,
                    remove_battery=remove_battery,
                )
        row["bullets"] = bullets

        keyword_parts = []
        for part in split_keywords(row.get("keywords", "")):
            if remove_battery and _BATTERY_WORD_RE.search(part):
                continue
            cleaned = _sanitize_simple_text(
                part,
                remove_battery=remove_battery,
            )
            if cleaned:
                keyword_parts.append(cleaned)
        row["keywords"] = ", ".join(keyword_parts)

        used = {
            fingerprint_text(bullet)
            for bullet in row["bullets"]
            if str(bullet).strip()
        }
        candidates = source_bullet_candidates(row)
        for index, bullet in enumerate(row["bullets"]):
            if str(bullet).strip():
                continue
            while candidates:
                candidate = _sanitize_simple_text(
                    candidates.pop(0),
                    remove_battery=remove_battery,
                )
                candidate = re.sub(
                    r"^(?:Product identification|Listing detail|Catalog detail|"
                    r"Product information|Source specification)\s*:\s*",
                    "",
                    candidate,
                    flags=re.IGNORECASE,
                )
                fingerprint = fingerprint_text(candidate)
                if (
                    candidate
                    and not PROHIBITED_LISTING_TERMS_RE.search(candidate)
                    and not (remove_battery and _BATTERY_WORD_RE.search(candidate))
                    and fingerprint != fingerprint_text(row.get("title", ""))
                    and fingerprint not in used
                ):
                    row["bullets"][index] = candidate
                    used.add(fingerprint)
                    break

        if str(row.get("site") or "US") in {"US", "UK", "CA"}:
            normalize_keywords_for_row(row)
        else:
            from .text.locale import normalize_localized_listing_fields

            normalize_localized_listing_fields(row)
    return rows
