#!/usr/bin/env python3
"""Amazon collection-table refill pipeline orchestration and compatibility API."""
from __future__ import annotations

import os
import sys

if __package__ in {None, ""}:
    from _bootstrap import ensure_package_imports
    ensure_package_imports()

import openpyxl

from scripts.adapters import detect_adapter
from scripts.model_provider import (
    ProviderQuotaError,
    get_provider,
    reload_provider as _reload_provider,
)
from scripts.pipeline_log import log as _log, new_request_id
from scripts.pipelines.amazon_quality import (
    attach_audit_to_validation as _attach_audit_to_validation,
    dedupe_terms as _dedupe_terms,
    normalize_keywords_for_row as _normalize_keywords_for_row,
    split_keywords as _split_keywords,
    summarize_row_quality_issues as _summarize_row_quality_issues,
    validate_amazon_rows as _validate_amazon_rows,
)
from scripts.pipelines.amazon_io import (
    _stage_read,
    _stage_read_json,
    _stage_write_output,
    _validate_amazon_input,
    _validate_amazon_output,
)
from scripts.pipelines import amazon_image_safety as _amazon_image_safety
from scripts.pipelines import amazon_text as _amazon_text
from scripts.pipelines.amazon_delivery import (
    _assert_formal_images_are_safe,
    _atomic_write_json,
    _create_review_package,
    _review_root_for_output,
    _write_latest_review_entry,
    deliver_amazon_output,
)
from scripts.pipelines.amazon_runtime import (
    AMAZON_STAGES,
    AmazonRunContext,
    AmazonStatusReporter,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QuotaExhaustedError = ProviderQuotaError
_clamp_title = _amazon_text.clamp_title
_normalize_title = _amazon_text.normalize_title
_parse_bullet_json = _amazon_text.parse_bullet_json
_valid_bullet_payload = _amazon_text.valid_bullet_payload
_remove_dirty_descriptions = _amazon_text.remove_dirty_descriptions


def reload_credentials():
    """Reload keys.json after settings are saved by the web app."""
    _reload_provider()


def _stage_optimize_titles(data, progress=None):
    return _amazon_text.optimize_titles(
        data,
        progress=progress,
        provider_getter=get_provider,
    )


def _stage_clean_descs(data, progress=None):
    return _amazon_text.clean_descriptions(
        data,
        progress=progress,
        provider_getter=get_provider,
    )


def _stage_generate_bullets_keywords(data, progress=None):
    return _amazon_text.generate_bullets_keywords(
        data,
        progress=progress,
        provider_getter=get_provider,
    )


def _stage_review_and_gen(
    data,
    cache_path=None,
    quality_issues=None,
    progress=None,
    runtime_metrics=None,
):
    return _amazon_image_safety.run_structured_image_safety_gate(
        data,
        cache_path=cache_path,
        quality_issues=quality_issues,
        progress=progress,
        runtime_metrics=runtime_metrics,
        provider_getter=get_provider,
    )


def _main(tp: str) -> str:
    """Web 层调用入口：接收文件路径，返回输出路径。"""
    return _main_impl(tp)


def run_amazon_pipeline(tp: str) -> str:
    """Amazon 管道：支持模板 JSON/XLSX，处理图片、文本并按原格式回填。"""
    from scripts.services.amazon_json import _removed_dirty_ids as _rdi

    _rdi.clear()
    rid = new_request_id()
    _log.info("Amazon管道启动", request_id=rid, file=os.path.basename(tp))
    print(f"=== Amazon 采集表 → 回填表 === [rid={rid}]")
    print(f"输入: {tp}")

    reload_credentials()
    # model_provider 会在首次调用时自动检查配置
    try:
        provider = get_provider()
    except ValueError as e:
        raise ValueError(f"配置错误: {e}")

    context = AmazonRunContext.create(tp, rid, provider)
    context.status.stage('读取表格')

    try:
        is_json = tp.lower().endswith('.json')
        if is_json:
            print("表格格式: JSON | 读取中...")
            data = _stage_read_json(
                None,
                tp,
                progress=context.status.update,
            )
        else:
            wb = openpyxl.load_workbook(tp, data_only=True)
            try:
                ws = wb.active
                adapter = detect_adapter(ws)
                if not adapter or 'Amazon' not in adapter.name:
                    raise ValueError("无法识别为 Amazon 采集表格式")
                print(f"表格格式: {adapter.name} | {ws.max_row - 1} 行")
                data = _stage_read(
                    ws,
                    adapter,
                    progress=context.status.update,
                )
            finally:
                wb.close()
        _validate_amazon_input(data)
    except Exception as e:
        _log.error("Amazon阶段 [读取表格] 失败", error=str(e), exc_info=True)
        context.status.failed('读取表格', e)
        raise
    context.data = data

    cache_path = os.path.splitext(tp)[0] + '_amz_cache.json'
    context.transform(
        '审图+生图',
        _stage_review_and_gen,
        cache_path,
        context.quality_issues,
        runtime_metrics=context.runtime_metrics,
    )
    quarantined_ids = [
        str(item.get('product_id') or '')
        for item in context.runtime_metrics.get(
            'quarantined_products',
            [],
        )
        if str(item.get('product_id') or '')
    ]
    for product_id in quarantined_ids:
        if product_id not in _rdi:
            _rdi.append(product_id)
    context.transform('标题优化', _stage_optimize_titles)
    context.transform('描述清洗', _stage_clean_descs)
    context.transform(
        'Bullet+关键词',
        _stage_generate_bullets_keywords,
    )

    context.data, dirty_ids = _remove_dirty_descriptions(
        context.data
    )
    if dirty_ids:
        for product_id in dirty_ids:
            if product_id not in _rdi:
                _rdi.append(product_id)
        print(
            f'\\n[零容忍] 删除 {len(dirty_ids)} 行脏描述，'
            f'保留 {len(context.data)} 行',
            flush=True,
        )

    return deliver_amazon_output(context)


def _main_impl(tp: str) -> str:
    """Compatibility adapter for callers using the former private name."""
    return run_amazon_pipeline(tp)


def main():
    if len(sys.argv) < 2:
        print("用法: uv run python scripts/process_amazon.py \"<采集表.xlsx|json>\"")
        sys.exit(1)
    tp = sys.argv[1]
    run_amazon_pipeline(tp)


if __name__ == '__main__':
    main()
