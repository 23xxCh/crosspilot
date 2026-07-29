"""Compatibility bootstrap for executing a packaged script by file path."""
from __future__ import annotations

from pathlib import Path
import sys


def ensure_package_imports() -> None:
    """Expose the repository root for legacy ``python scripts/x.py`` calls."""
    repository_root = str(Path(__file__).resolve().parents[1])
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)


__all__ = ["ensure_package_imports"]
