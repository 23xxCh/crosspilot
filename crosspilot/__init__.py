"""CrossPilot — 跨境电商 Listing 全自动清洗平台。

用法:
    python -m crosspilot ebay "输入.xlsx"
    python -m crosspilot amazon "输入.xlsx"
    python -m crosspilot gen "输入.json" [--concurrency 20]
    python -m crosspilot web [--port 8765]
"""

from .version import __version__

__all__ = ['__version__']
