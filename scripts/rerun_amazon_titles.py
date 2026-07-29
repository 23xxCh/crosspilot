#!/usr/bin/env python3
"""Re-run only Amazon title optimization on an existing refill JSON."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil

if __package__ in {None, ""}:
    from _bootstrap import ensure_package_imports
    ensure_package_imports()

from crosspilot.prompt_registry import get_prompt_registry
from scripts.model_provider import get_provider, reload_provider
from scripts.pipelines.amazon_text import optimize_titles
from scripts.services.amazon_json import (
    AMAZON_JSON_OUTPUT_FIELDS,
    validate_columnar_payload,
)


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8-sig") as handle:
        value = json.load(handle)
    validate_columnar_payload(
        value,
        required_fields=AMAZON_JSON_OUTPUT_FIELDS,
    )
    return value


def _hash_without_titles(payload: dict) -> str:
    value = {
        key: item
        for key, item in payload.items()
        if key != "产品标题"
    }
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    try:
        temp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _render_changes(changes: list[dict], summary: dict) -> str:
    rows = []
    for item in changes:
        rows.append(
            "<tr>"
            f"<td>{item['row']}</td>"
            f"<td>{html.escape(item['product_id'])}</td>"
            f"<td>{item['old_length']}</td>"
            f"<td>{item['new_length']}</td>"
            f"<td>{html.escape(item['old_title'])}</td>"
            f"<td>{html.escape(item['new_title'])}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Amazon 标题重跑变更清单</title>
<style>
body{{font:14px/1.5 Arial,"Microsoft YaHei",sans-serif;margin:22px;color:#17202a}}
table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccd5dc;padding:7px;vertical-align:top}}
th{{position:sticky;top:0;background:#17202a;color:white}}tr:nth-child(even){{background:#f6f8f9}}
.summary{{margin-bottom:15px;padding:12px;background:#eef6f2;border-radius:8px}}
</style></head><body>
<h1>Amazon 标题重跑变更清单</h1>
<div class="summary">商品 {summary['products']}；标题变更
{summary['changed']}；70–75 字符 {summary['target_length_count']}；
最长 {summary['max_length']} 字符。</div>
<table><thead><tr><th>行</th><th>商品 ID</th><th>原长度</th>
<th>新长度</th><th>原标题</th><th>新标题</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></body></html>"""


def rerun_titles(
    formal_json: str | Path,
    *,
    dry_run: bool = False,
) -> dict:
    path = Path(formal_json).resolve()
    payload = _load(path)
    original_titles = list(payload["产品标题"])
    non_title_hash = _hash_without_titles(payload)
    rows = [
        {
            "id": str(payload["商品id"][index]),
            "title": str(title or ""),
        }
        for index, title in enumerate(original_titles)
    ]
    reload_provider()
    optimized = optimize_titles(
        rows,
        provider_getter=get_provider,
    )
    new_titles = [str(row.get("title") or "").strip() for row in optimized]
    invalid = [
        {
            "row": index + 1,
            "product_id": str(payload["商品id"][index]),
            "title": title,
            "reason": (
                "empty"
                if not title
                else "over_75"
                if len(title) > 75
                else "invalid_for_generic"
            ),
        }
        for index, title in enumerate(new_titles)
        if (
            not title
            or len(title) > 75
            or re.search(r"\bFor\s+Generic\b", title, re.IGNORECASE)
            or title.lower().count(" for ") > 1
            or title.lower().endswith(" for")
            or "[" in title
            or "]" in title
        )
    ]
    if invalid:
        raise ValueError(
            f"标题重跑产生 {len(invalid)} 条不合格标题，"
            f"首条: {invalid[0]}"
        )
    payload["产品标题"] = new_titles
    validate_columnar_payload(
        payload,
        required_fields=AMAZON_JSON_OUTPUT_FIELDS,
    )
    if _hash_without_titles(payload) != non_title_hash:
        raise ValueError("标题重跑意外修改了非标题字段，已停止写入")

    changes = [
        {
            "row": index + 1,
            "product_id": str(payload["商品id"][index]),
            "old_title": original,
            "new_title": new,
            "old_length": len(original),
            "new_length": len(new),
        }
        for index, (original, new) in enumerate(
            zip(original_titles, new_titles)
        )
        if original != new
    ]
    lengths = [len(title) for title in new_titles]
    provider = get_provider()
    registry = get_prompt_registry()
    summary = {
        "products": len(new_titles),
        "changed": len(changes),
        "unchanged": len(new_titles) - len(changes),
        "target_length_count": sum(70 <= value <= 75 for value in lengths),
        "under_70_count": sum(value < 70 for value in lengths),
        "max_length": max(lengths, default=0),
        "min_length": min(lengths, default=0),
        "prompt": registry.metadata("amazon.title_optimize"),
        "provider_metrics": (
            provider.metrics_snapshot()
            if hasattr(provider, "metrics_snapshot") else {}
        ),
        "dry_run": dry_run,
    }
    if dry_run:
        return {"summary": summary, "changes": changes}

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = (
        path.parent
        / "归档_标题重跑"
        / f"标题重跑_{stamp}"
    )
    archive.mkdir(parents=True, exist_ok=False)
    backup = archive / f"{path.stem}_标题重跑前.json"
    shutil.copy2(path, backup)
    _atomic_json(path, payload)
    report = {
        "version": 1,
        "processed_at": datetime.now().isoformat(timespec="seconds"),
        "formal_json": str(path),
        "backup": str(backup),
        "summary": summary,
        "changes": changes,
    }
    report_path = archive / "标题变更清单.json"
    _atomic_json(report_path, report)
    html_path = archive / "标题变更清单.html"
    html_path.write_text(
        _render_changes(changes, summary),
        encoding="utf-8",
    )
    return {
        "formal_json": str(path),
        "backup": str(backup),
        "report": str(report_path),
        "html": str(html_path),
        "summary": summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("formal_json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(
        rerun_titles(args.formal_json, dry_run=args.dry_run),
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
