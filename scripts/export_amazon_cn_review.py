#!/usr/bin/env python3
"""Compatibility Adapter for the Amazon final-review package Module."""
from __future__ import annotations

import sys

if __package__ in {None, ""}:
    from _bootstrap import ensure_package_imports
    ensure_package_imports()

from scripts.review_package import (
    _atomic_json,
    _load_json,
    _source_row,
    _translation_signature,
    _valid_translation,
    build_quarantine_rows,
    build_review_rows,
    download_all_images,
    export_review,
    prepare_shared_review_cache,
    render_html,
    translate_payload,
)


def main(argv: list[str] | None = None) -> int:
    """Compatibility adapter for the unified CrossPilot CLI."""
    from crosspilot.cli import main as cli_main

    arguments = list(sys.argv[1:] if argv is None else argv)
    return cli_main(['review', *arguments])


if __name__ == '__main__':
    raise SystemExit(main())


__all__ = [
    "_atomic_json",
    "_load_json",
    "_source_row",
    "_translation_signature",
    "_valid_translation",
    "build_quarantine_rows",
    "build_review_rows",
    "download_all_images",
    "export_review",
    "main",
    "prepare_shared_review_cache",
    "render_html",
    "translate_payload",
]
