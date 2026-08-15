"""Amazon JSON collection-table processor."""

from .pipeline import process_json
from .runtime import RunResult

__all__ = ["RunResult", "process_json"]
