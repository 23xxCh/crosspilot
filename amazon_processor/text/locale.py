"""Locale-aware text formatting and release validation."""
from __future__ import annotations

import re
import unicodedata

from ..markets import Market, get_market
from ..policy import COMPATIBILITY_BRANDS, compile_brand_pattern
from ..quality import split_keywords


_SPACE_RE = re.compile(r"\s+")
_BRAND_RE = compile_brand_pattern(COMPATIBILITY_BRANDS)
_OEM_RE = re.compile(
    r"\b(?:OEM|original|factory|genuine)\b|原厂|原装|正品",
    re.IGNORECASE,
)
_DESCRIPTION_LABELS = {
    "en": {
        "Material": "Material",
        "Size": "Size",
        "Color": "Color",
        "Compatibility": "Compatibility",
        "Quantity": "Quantity",
        "Specifications": "Specifications",
        "Features": "Features",
        "Package Includes": "Package Includes",
    },
    "es": {
        "Material": "Material",
        "Size": "Tamaño",
        "Color": "Color",
        "Compatibility": "Compatibilidad",
        "Quantity": "Cantidad",
        "Specifications": "Especificaciones",
        "Features": "Características",
        "Package Includes": "Contenido del paquete",
    },
    "pt": {
        "Material": "Material",
        "Size": "Tamanho",
        "Color": "Cor",
        "Compatibility": "Compatibilidade",
        "Quantity": "Quantidade",
        "Specifications": "Especificações",
        "Features": "Características",
        "Package Includes": "Conteúdo da embalagem",
    },
    "de": {
        "Material": "Material",
        "Size": "Größe",
        "Color": "Farbe",
        "Compatibility": "Kompatibilität",
        "Quantity": "Menge",
        "Specifications": "Spezifikationen",
        "Features": "Merkmale",
        "Package Includes": "Lieferumfang",
    },
    "fr": {
        "Material": "Matériau",
        "Size": "Taille",
        "Color": "Couleur",
        "Compatibility": "Compatibilité",
        "Quantity": "Quantité",
        "Specifications": "Spécifications",
        "Features": "Caractéristiques",
        "Package Includes": "Contenu de l'emballage",
    },
    "it": {
        "Material": "Materiale",
        "Size": "Dimensioni",
        "Color": "Colore",
        "Compatibility": "Compatibilità",
        "Quantity": "Quantità",
        "Specifications": "Specifiche",
        "Features": "Caratteristiche",
        "Package Includes": "Contenuto della confezione",
    },
}


def market_for_row(row: dict) -> Market:
    return get_market(row.get("site") or "US")


def market_prompt_values(row: dict) -> dict[str, str]:
    market = market_for_row(row)
    return {
        "site_code": market.code,
        "market_name": market.country,
        "target_language": market.language,
        "target_locale": market.locale,
        "compatibility_connector": market.compatibility_connector,
    }


def description_label(label: str, site: object) -> str:
    language_code = get_market(site or "US").language_code
    return _DESCRIPTION_LABELS.get(
        language_code,
        _DESCRIPTION_LABELS["en"],
    ).get(label, label)


def normalize_localized_title(value: object, site: object) -> str:
    """Apply language-neutral title limits without translating facts."""
    market = get_market(site or "US")
    title = _SPACE_RE.sub(" ", str(value or "")).strip(" ,;:-[]")
    title = re.sub(r"^\[?generic\]?\b[\s:,-]*", "Generic ", title, flags=re.I)
    brand = _BRAND_RE.search(title)
    if brand:
        if not re.match(r"^Generic\b", title, re.I):
            title = "Generic " + title
            brand = _BRAND_RE.search(title)
        assert brand is not None
        prefix = title[:brand.start()]
        suffix = title[brand.start():]
        prefix = re.sub(
            r"\b(?:for|para|pour|per|für)\b\s*$",
            "",
            prefix,
            flags=re.I,
        ).rstrip(" ,;:-")
        title = f"{prefix} {market.compatibility_connector} {suffix}"
    title = _SPACE_RE.sub(" ", title).strip(" ,;:-")
    while len(title) > 75 and " " in title:
        title = title.rsplit(" ", 1)[0].rstrip(" ,;:-")
    return title[:75].strip(" ,;:-")


def compatibility_format_is_valid(title: object, site: object) -> bool:
    value = str(title or "")
    brand = _BRAND_RE.search(value)
    if not brand:
        return True
    market = get_market(site or "US")
    return bool(
        re.match(r"^Generic\b", value, re.I)
        and re.search(
            rf"\b{re.escape(market.compatibility_connector)}\b\s+"
            rf"(?={re.escape(brand.group(0))}\b)",
            value,
            re.I,
        )
    )


def sanitize_localized_subtitle(value: object, site: object = None) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", ", ")
    text = _BRAND_RE.sub(" ", text)
    kept = []
    english = bool(site and get_market(site).language_code == "en")
    for char in text:
        category = unicodedata.category(char)
        if english:
            allowed = char in " ," or char.isascii() and char.isalnum()
        else:
            allowed = char in " ,-'" or category[0] in {"L", "N"}
        if allowed:
            kept.append(char)
        else:
            kept.append(" ")
    phrases = []
    seen = set()
    for raw in "".join(kept).split(","):
        phrase = _SPACE_RE.sub(" ", raw).strip(" ,;:-")
        key = phrase.casefold()
        if phrase and key not in seen:
            seen.add(key)
            phrases.append(phrase)
    result = ", ".join(phrases)
    while len(result) > 125 and ", " in result:
        result = result.rsplit(", ", 1)[0]
    return result[:125].strip(" ,;:-")


def _unicode_fingerprint(value: object) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKC", str(value or "")).casefold()
        if char.isalnum()
    )


def _clean_localized_phrase(value: object, limit: int) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = _BRAND_RE.sub(" ", text)
    text = _OEM_RE.sub(" ", text)
    kept = []
    for char in text:
        category = unicodedata.category(char)
        if char in " ,-'/%+×." or category[0] in {"L", "N"}:
            kept.append(char)
        else:
            kept.append(" ")
    result = _SPACE_RE.sub(" ", "".join(kept)).strip(" ,;:-")
    if len(result) <= limit:
        return result
    shortened = result[:limit]
    return shortened.rsplit(" ", 1)[0].strip(" ,;:-") or result[:limit]


def normalize_localized_listing_fields(row: dict) -> dict:
    """Preserve Unicode text while enforcing counts and de-duplication."""
    bullets = []
    seen = set()
    for raw in list(row.get("bullets") or [])[:5]:
        bullet = _clean_localized_phrase(raw, 200)
        fingerprint = _unicode_fingerprint(bullet)
        if not fingerprint or fingerprint in seen:
            bullet = ""
        else:
            seen.add(fingerprint)
        bullets.append(bullet)
    bullets.extend([""] * (5 - len(bullets)))
    row["bullets"] = bullets[:5]

    terms = []
    seen = set()
    for raw in split_keywords(row.get("keywords", "")):
        term = _clean_localized_phrase(raw, 60).rstrip(".")
        fingerprint = _unicode_fingerprint(term)
        if not fingerprint or fingerprint in seen:
            continue
        trial = ", ".join([*terms, term])
        if len(trial) > 250:
            continue
        seen.add(fingerprint)
        terms.append(term)
        if len(terms) == 10:
            break
    row["keywords"] = ", ".join(terms)
    return row


def _detected_language(
    text: str,
    *,
    min_letters: int = 80,
) -> tuple[str, float] | None:
    letters = sum(char.isalpha() for char in text)
    if letters < min_letters:
        return None
    try:
        from langdetect import DetectorFactory, detect_langs

        DetectorFactory.seed = 0
        detected = detect_langs(text)
    except Exception:
        return None
    if not detected:
        return None
    return detected[0].lang, float(detected[0].prob)


def localization_violations(row: dict) -> list[str]:
    """Return release-blocking locale and shape violations for one row."""
    market = market_for_row(row)
    title = str(row.get("title") or "").strip()
    subtitle = str(row.get("subtitle") or "").strip()
    description = str(row.get("desc") or "").strip()
    bullets = [str(item or "").strip() for item in row.get("bullets") or []]
    keywords = split_keywords(row.get("keywords", ""))
    violations = []
    if not title or len(title) > 75:
        violations.append("title_length")
    if not compatibility_format_is_valid(title, market.code):
        violations.append("compatibility_format")
    if len(title) >= 75 and subtitle:
        violations.append("subtitle_not_allowed")
    if len(subtitle) > 125:
        violations.append("subtitle_length")
    if subtitle and sanitize_localized_subtitle(subtitle, market.code) != subtitle:
        violations.append("subtitle_characters")
    if not description or len(description) > 500:
        violations.append("description_length")
    if len(bullets) != 5 or any(not item or len(item) > 200 for item in bullets):
        violations.append("bullets_shape")
    if len(keywords) != 10:
        violations.append("keywords_count")
    if len(str(row.get("keywords") or "")) > 250:
        violations.append("keywords_length")
    non_title_copy = " ".join([subtitle, description, *bullets, *keywords])
    if _BRAND_RE.search(non_title_copy) or _OEM_RE.search(non_title_copy):
        violations.append("listing_brand_or_oem")
    aggregate = " ".join([title, subtitle, description, *bullets, *keywords])
    detected = _detected_language(aggregate)
    if (
        detected is not None
        and detected[0] != market.language_code
        and detected[1] >= 0.70
    ):
        violations.append(
            f"language:{detected[0]}->{market.language_code}"
        )
    language_fields = {
        "title": (title, 35),
        "subtitle": (subtitle, 50),
        "description": (description, 60),
        "bullets": (" ".join(bullets), 80),
        "keywords": (" ".join(keywords), 60),
    }
    for field, (text, min_letters) in language_fields.items():
        detected = _detected_language(text, min_letters=min_letters)
        if (
            detected is not None
            and detected[0] != market.language_code
            and detected[1] >= 0.80
        ):
            violations.append(
                f"language_{field}:{detected[0]}->{market.language_code}"
            )
    for index, line in enumerate(description.splitlines(), start=1):
        detected = _detected_language(line, min_letters=40)
        if (
            detected is not None
            and detected[0] == "en"
            and market.language_code != "en"
            and detected[1] >= 0.80
        ):
            violations.append(
                f"language_description_line_{index}:"
                f"{detected[0]}->{market.language_code}"
            )
    return violations


__all__ = [
    "compatibility_format_is_valid",
    "description_label",
    "localization_violations",
    "market_for_row",
    "market_prompt_values",
    "normalize_localized_listing_fields",
    "normalize_localized_title",
    "sanitize_localized_subtitle",
]
