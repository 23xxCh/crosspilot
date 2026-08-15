"""Amazon marketplace locale definitions loaded from unified settings."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import threading


SETTINGS_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.json"
_lock = threading.Lock()
_cache: tuple[int, int, dict[str, "Market"]] | None = None


@dataclass(frozen=True)
class Market:
    code: str
    country: str
    language: str
    language_code: str
    locale: str
    compatibility_connector: str


def _load_markets() -> dict[str, Market]:
    global _cache
    try:
        stat = SETTINGS_PATH.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError as exc:
        raise ValueError(f"站点配置不存在: {SETTINGS_PATH}") from exc
    with _lock:
        if _cache and _cache[:2] == signature:
            return dict(_cache[2])
        try:
            payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"站点配置无法读取: {exc}") from exc
        values = payload.get("markets") if isinstance(payload, dict) else None
        if not isinstance(values, dict) or not values:
            raise ValueError("config/settings.json 缺少 markets")
        markets: dict[str, Market] = {}
        for raw_code, raw in values.items():
            code = str(raw_code or "").strip().upper()
            if not code or not isinstance(raw, dict):
                raise ValueError(f"站点配置无效: {raw_code}")
            fields = {
                name: str(raw.get(name) or "").strip()
                for name in (
                    "country",
                    "language",
                    "language_code",
                    "locale",
                    "compatibility_connector",
                )
            }
            missing = [name for name, value in fields.items() if not value]
            if missing:
                raise ValueError(
                    f"站点 {code} 缺少配置: {', '.join(missing)}"
                )
            markets[code] = Market(code=code, **fields)
        _cache = (*signature, markets)
        return dict(markets)


def market_codes() -> tuple[str, ...]:
    return tuple(_load_markets())


def get_market(code: object) -> Market:
    normalized = str(code or "").strip().upper()
    try:
        return _load_markets()[normalized]
    except KeyError as exc:
        allowed = ", ".join(market_codes())
        raise ValueError(
            f"不支持的产品站点: {normalized or '<empty>'}；允许: {allowed}"
        ) from exc


def normalize_market_code(code: object) -> str:
    return get_market(code).code


def market_signature() -> str:
    payload = {
        code: market.__dict__
        for code, market in sorted(_load_markets().items())
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


__all__ = [
    "Market",
    "get_market",
    "market_codes",
    "market_signature",
    "normalize_market_code",
]
