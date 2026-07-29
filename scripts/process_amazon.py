#!/usr/bin/env python3
"""Amazon collection-table refill pipeline orchestration and compatibility API."""
from __future__ import annotations

import inspect
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import openpyxl

from adapters import detect_adapter
from model_provider import (
    ProviderQuotaError,
    get_provider,
    reload_provider as _reload_provider,
)
from pipeline_log import PipelineMetrics, log as _log, new_request_id
from pipelines.amazon_constants import *
from pipelines.amazon_io import (
    _stage_read,
    _stage_read_json,
    _stage_write_output,
    _validate_amazon_input,
    _validate_amazon_output,
)
from pipelines import amazon_review_gen as _amazon_review_gen
from pipelines import amazon_stages as _amazon_stages

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QuotaExhaustedError = ProviderQuotaError
_atomic_save_cache = _amazon_review_gen._atomic_save_cache
_clamp_title = _amazon_stages._clamp_title
_normalize_title = _amazon_stages._normalize_title
_parse_bullet_json = _amazon_stages._parse_bullet_json
_valid_bullet_payload = _amazon_stages._valid_bullet_payload


def reload_credentials():
    """Reload keys.json after settings are saved by the web app."""
    _reload_provider()


AMAZON_STAGES = ['读取表格', '审图+生图', '标题优化', '描述清洗', 'Bullet+关键词', '写回填表']


class AmazonStatusReporter:
    def __init__(self, table_path):
        self.status_path = os.path.splitext(table_path)[0] + '_status.json'
        self.started_at = time.time()
        self.stage_index = 0
        self.stage_started_at = self.started_at
        self.current_stage = AMAZON_STAGES[0]
        self.total = 0

    def stage(self, name, current=0, total=0):
        self.stage_index = AMAZON_STAGES.index(name)
        self.current_stage = name
        self.stage_started_at = time.time()
        self.total = total
        self.update(current, total)

    def update(self, current, total=None):
        if total is not None:
            self.total = total
        elapsed = time.time() - self.stage_started_at
        eta = int(elapsed / current * (self.total - current)) if current and self.total else 0
        self._write({
            'status': 'running',
            'stage': self.current_stage,
            'stage_index': self.stage_index + 1,
            'stage_total': len(AMAZON_STAGES),
            'current': current,
            'total': self.total,
            'percent': int(current / self.total * 100) if self.total else 0,
            'eta_s': eta,
        })

    def failed(self, name, error):
        self._write({
            'status': 'failed',
            'stage': '错误',
            'stage_index': AMAZON_STAGES.index(name) + 1 if name in AMAZON_STAGES else self.stage_index + 1,
            'stage_total': len(AMAZON_STAGES),
            'error': str(error),
        })

    def finish(self, output, validation=None, metrics=None):
        validation = validation or {'passed': True, 'issues': []}
        needs_review = not validation.get('passed', False)
        self._write({
            'status': 'needs_review' if needs_review else 'done',
            'stage': '待人工复核' if needs_review else '完成',
            'stage_index': len(AMAZON_STAGES),
            'stage_total': len(AMAZON_STAGES),
            'current': 1,
            'total': 1,
            'percent': 100,
            'eta_s': 0,
            'output': output,
            'validation': validation,
            'metrics': metrics or {},
            'error': (
                f"输出存在 {len(validation.get('issues', []))} 项质量问题，请复核后使用"
                if needs_review else None
            ),
        })

    def _write(self, data):
        data['total_elapsed_s'] = int(time.time() - self.started_at)
        data['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        temp_path = self.status_path + f'.{threading.get_ident()}.tmp'
        for _ in range(3):
            try:
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(temp_path, self.status_path)
                break
            except (PermissionError, OSError):
                time.sleep(0.1)


def _stage_optimize_titles(data, progress=None):
    return _amazon_stages._stage_optimize_titles(
        data,
        progress=progress,
        provider_getter=get_provider,
    )


def _stage_clean_descs(data, progress=None):
    return _amazon_stages._stage_clean_descs(
        data,
        progress=progress,
        provider_getter=get_provider,
    )


def _stage_generate_bullets_keywords(data, progress=None):
    return _amazon_stages._stage_generate_bullets_keywords(
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
    return _amazon_review_gen._stage_review_and_gen(
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


def _main_impl(tp: str) -> str:
    """Amazon 管道：支持模板 JSON/XLSX，处理图片、文本并按原格式回填。"""
    from services.amazon_json import _removed_dirty_ids as _rdi
    _rdi.clear()  # 每轮清零，防止累积
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

    status = AmazonStatusReporter(tp)
    status.stage('读取表格')

    try:
        is_json = tp.lower().endswith('.json')
        if is_json:
            print("表格格式: JSON | 读取中...")
            data = _stage_read_json(None, tp, progress=status.update)
        else:
            wb = openpyxl.load_workbook(tp, data_only=True)
            try:
                ws = wb.active
                adapter = detect_adapter(ws)
                if not adapter or 'Amazon' not in adapter.name:
                    raise ValueError("无法识别为 Amazon 采集表格式")
                print(f"表格格式: {adapter.name} | {ws.max_row - 1} 行")
                data = _stage_read(ws, adapter, progress=status.update)
            finally:
                wb.close()
        _validate_amazon_input(data)
    except Exception as e:
        _log.error("Amazon阶段 [读取表格] 失败", error=str(e), exc_info=True)
        status.failed('读取表格', e)
        raise

    metrics = PipelineMetrics()

    def _run(name, fn, *args, **kwargs):
        t0 = time.time()
        status.stage(name, 0, len(data))
        try:
            call_kwargs = {'progress': status.update}
            try:
                params = inspect.signature(fn).parameters
                accepts_any = any(
                    param.kind == inspect.Parameter.VAR_KEYWORD
                    for param in params.values()
                )
                if accepts_any:
                    call_kwargs.update(kwargs)
                else:
                    call_kwargs.update({
                        key: value for key, value in kwargs.items()
                        if key in params
                    })
            except (TypeError, ValueError):
                call_kwargs.update(kwargs)
            result = fn(*args, **call_kwargs)
            issue_code = {
                '标题优化': 'title_ai_fallback',
                '描述清洗': 'description_ai_fallback',
                'Bullet+关键词': 'bullet_rule_fallback',
            }.get(name)
            degraded = (
                sum(
                    1 for row in data
                    if any(
                        issue.get('code') == issue_code
                        for issue in row.get('_quality_issues', [])
                    )
                )
                if issue_code else 0
            )
            metrics.record_stage(
                name,
                time.time() - t0,
                len(data),
                max(0, len(data) - degraded),
            )
            return result
        except Exception as e:
            _log.error(f"Amazon阶段 [{name}] 失败", error=str(e), exc_info=True)
            status.failed(name, e)
            raise

    cache_path = os.path.splitext(tp)[0] + '_amz_cache.json'
    # 主图/变种全部必生 (不审), 附图有水印则删 (可选审图, 配额不够自动跳过)
    quality_issues = []
    runtime_metrics = {}
    data = _run(
        '审图+生图',
        _stage_review_and_gen,
        data,
        cache_path,
        quality_issues,
        runtime_metrics=runtime_metrics,
    )
    data = _run('标题优化', _stage_optimize_titles, data)
    data = _run('描述清洗', _stage_clean_descs, data)
    data = _run('Bullet+关键词', _stage_generate_bullets_keywords, data)

    # 脏描述检测：只标记完全无产品内容的纯交叉销售
    # 有产品关键词的 → 正常清洗；纯交叉销售垃圾 → 标记删除
    _product_signals = (
        'material', 'size', 'dimension', 'color', 'weight', 'feature',
        'package include', 'made of', 'install', 'compatible', 'fit',
        '材质', '尺寸', '规格', '颜色', '重量', '特点', '包装', '安装', '适用',
    )
    _garbage_only_ptns = ('store categoriesstore categories', 'add me to favourite',
                          'visit our store', 'welcome to my store',
                          'please contact us before', 'our goal is customer')
    for row in data:
        desc = str(row.get('desc', '')).lower()
        has_product = any(s in desc for s in _product_signals) or (
            len(desc) > 50 and ' USD' not in desc)
        is_pure_garbage = any(g in desc for g in _garbage_only_ptns) and not has_product
        if is_pure_garbage:
            row.setdefault('_quality_issues', []).append(
                {'code': 'dirty_description', 'message': '描述为纯交叉销售模板，无产品内容'})

    # 零容忍：纯交叉销售产品从输出删除
    dirty_ids = [str(r.get('id', '')) for r in data if any(
        i.get('code') == 'dirty_description' for i in r.get('_quality_issues', []))]
    if dirty_ids:
        data = [r for r in data if str(r.get('id', '')) not in dirty_ids]
        from services.amazon_json import _removed_dirty_ids as _rdi
        _rdi.clear()
        if dirty_ids: _rdi.extend(dirty_ids)
        print(f'\\n[零容忍] 删除 {len(dirty_ids)} 行脏描述，保留 {len(data)} 行', flush=True)

    quality_issues.extend(_summarize_row_quality_issues(data))
    output = _run('写回填表', _stage_write_output, data, tp)
    if output.lower().endswith('.xlsx'):
        validation = _validate_amazon_output(
            output,
            len(data),
            extra_issues=quality_issues,
        )
    else:
        validation = _validate_amazon_rows(data, extra_issues=quality_issues)
    validation = _attach_audit_to_validation(validation, data)
    if hasattr(provider, 'metrics_snapshot'):
        metrics.set_provider_metrics(provider.metrics_snapshot())
    metrics.set_concurrency_metrics(runtime_metrics.get('concurrency'))
    metrics.set_image_quality_gate_metrics(
        runtime_metrics.get('image_quality_gate')
    )
    metrics.set_image_remediation_metrics(
        runtime_metrics.get('image_remediation')
    )
    metrics.set_quality_metrics(validation)
    metrics_data = metrics.to_dict()
    status.finish(output, validation, metrics_data)

    try:
        metrics_path = os.path.splitext(output)[0] + '_metrics.json'
        with open(metrics_path, 'w', encoding='utf-8') as f:
            json.dump(metrics_data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    print(f"输出: {output}")
    return output


def main():
    if len(sys.argv) < 2:
        print("用法: uv run python scripts/process_amazon.py \"<采集表.xlsx|json>\"")
        sys.exit(1)
    tp = sys.argv[1]
    _main_impl(tp)


if __name__ == '__main__':
    main()
