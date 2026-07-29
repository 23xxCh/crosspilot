"""CSV review report generation for completed CrossPilot tasks."""
from __future__ import annotations

import csv
import io
import re
from typing import Any


REPORT_COLUMNS = [
    'section',
    'type',
    'severity',
    'row',
    'field',
    'message',
    'value',
    'action',
]

_FIELD_ALIASES = {
    'Bullet': ('Bullet', 'bullets'),
    'keywords': ('keywords', '关键词'),
    'title': ('title', '产品标题'),
    'description': ('description', 'desc', '产品描述'),
    'main_image': ('main_image', 'main_img', '产品图片链接'),
    'attachment_image': ('attachment_image', 'attachments'),
    'variant_image': ('variant_image', '变种图片链接'),
    'image': ('main_image', 'attachment_image', 'variant_image', 'image'),
}


def _pct(value: Any) -> str:
    if value is None:
        return ''
    try:
        return f'{float(value) * 100:.1f}%'
    except (TypeError, ValueError):
        return str(value)


def _row_number(message: str) -> str:
    match = re.search(r'第\s*(\d+)\s*行', str(message or ''))
    return match.group(1) if match else ''


def _field_from_message(message: str) -> str:
    text = str(message or '')
    fields = [
        ('Bullet', 'Bullet'),
        ('关键词', 'keywords'),
        ('标题', 'title'),
        ('描述', 'description'),
        ('主图', 'main_image'),
        ('附图', 'attachment_image'),
        ('变种', 'variant_image'),
        ('图审', 'image_review'),
        ('生图', 'image_generation'),
        ('图片', 'image'),
    ]
    for needle, field in fields:
        if needle in text:
            return field
    return ''


def _action_for_issue(message: str) -> str:
    text = str(message or '')
    if 'Bullet' in text:
        return '检查 5 条 Bullet 是否真实、唯一、无品牌残留'
    if '关键词' in text:
        return '检查关键词是否正好 10 个、相关、无品牌'
    if '标题' in text:
        return '检查标题长度、规格保留、品牌兼容表达'
    if '描述' in text:
        return '检查描述是否保留尺寸/型号/数量等硬规格'
    if '图审' in text or '图片' in text:
        return '检查图片是否仍有水印、人物、品牌或生成失败'
    return '人工复核该项后再使用输出'


def _positive(value) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _append_metric(rows, name, value, action='', severity='info'):
    rows.append({
        'section': 'metrics',
        'type': 'metric',
        'severity': severity,
        'row': '',
        'field': name,
        'message': name,
        'value': value,
        'action': action,
    })


def _validation_issues(task):
    stats = task.get('stats') if isinstance(task.get('stats'), dict) else {}
    validation = stats.get('validation') if isinstance(stats.get('validation'), dict) else {}
    issues = validation.get('issues')
    if not isinstance(issues, list):
        issues = validation.get('warnings') if isinstance(validation.get('warnings'), list) else []
    return [str(issue) for issue in issues if str(issue or '').strip()]


def _validation_audit(task):
    stats = task.get('stats') if isinstance(task.get('stats'), dict) else {}
    validation = stats.get('validation') if isinstance(stats.get('validation'), dict) else {}
    audit = validation.get('audit')
    return audit if isinstance(audit, list) else []


def _quality_issue_count(quality, issues):
    try:
        metric_count = int(quality.get('issue_count', 0))
    except (TypeError, ValueError):
        metric_count = 0
    return max(metric_count, len(issues))


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _audit_warning_count(audit) -> int:
    count = 0
    for item in audit:
        if not isinstance(item, dict):
            continue
        severity = _audit_severity(item)
        method = str(item.get('method') or '').lower()
        if severity in {'warning', 'review', 'error'} or method in {'fallback', 'review'}:
            count += 1
    return count


def _stage_degraded_count(metrics) -> int:
    stages = metrics.get('stages') if isinstance(metrics.get('stages'), dict) else {}
    degraded = 0
    for values in stages.values():
        if not isinstance(values, dict):
            continue
        items = int(_num(values.get('items'), 0))
        success = int(_num(values.get('success'), items))
        degraded += max(0, items - success)
    return degraded


def _reason(code, label, count, points):
    return {
        'code': code,
        'label': label,
        'count': count,
        'points': int(points),
    }


def build_quality_score(task: dict[str, Any]) -> dict[str, Any]:
    """Compute a compact task-level quality/risk score for list and review UI."""
    status = str(task.get('status') or '')
    if status in {'queued', 'running'}:
        return {
            'score': None,
            'grade': 'pending',
            'label': '未完成',
            'severity': 'info',
            'reasons': [_reason('pending', '任务尚未完成', 1, 0)],
        }
    if status == 'cancelled':
        return {
            'score': None,
            'grade': 'cancelled',
            'label': '已取消',
            'severity': 'info',
            'reasons': [_reason('cancelled', '任务已取消', 1, 0)],
        }
    if status == 'failed':
        return {
            'score': 0,
            'grade': 'critical',
            'label': '失败',
            'severity': 'danger',
            'reasons': [_reason('failed', '任务失败', 1, 100)],
        }

    stats = task.get('stats') if isinstance(task.get('stats'), dict) else {}
    metrics = stats.get('metrics') if isinstance(stats.get('metrics'), dict) else {}
    quality = metrics.get('quality') if isinstance(metrics.get('quality'), dict) else {}
    concurrency = (
        metrics.get('concurrency')
        if isinstance(metrics.get('concurrency'), dict)
        else {}
    )
    issues = _validation_issues(task)
    audit = _validation_audit(task)
    issue_count = _quality_issue_count(quality, issues)
    audit_warnings = _audit_warning_count(audit)
    audit_pressure = len(audit) // 20
    degraded = _stage_degraded_count(metrics)
    http_retries = int(_num(metrics.get('http_retries'), 0))
    circuit_open = int(_num(metrics.get('circuit_open'), 0))
    reductions = int(_num(concurrency.get('reductions'), 0))

    reasons = []
    penalties = 0

    def add(code, label, count, points):
        nonlocal penalties
        points = int(max(0, points))
        if count and points:
            penalties += points
            reasons.append(_reason(code, label, count, points))

    add('needs_review', '任务要求人工复核', 1 if status == 'needs_review' else 0, 10)
    add('validation_issues', '质量问题', issue_count, min(45, issue_count * 18))
    add('audit_warnings', '高风险审计项', audit_warnings, min(18, audit_warnings * 4))
    add('audit_pressure', '审计项较多', audit_pressure, min(5, audit_pressure))
    add('http_retries', 'HTTP 重试', http_retries, min(15, http_retries * 3))
    add('circuit_open', '熔断拦截', circuit_open, min(20, circuit_open * 10))
    add('concurrency_reductions', '并发降级', reductions, min(15, reductions * 5))
    add('stage_degraded', '阶段降级项', degraded, min(20, degraded * 2))

    success_rate = metrics.get('api_success_rate')
    if success_rate is not None:
        rate = max(0.0, min(1.0, _num(success_rate, 1.0)))
        if rate < 0.98:
            add('api_success_rate', 'AI 成功率偏低', round(rate, 3), min(20, round((1 - rate) * 50)))

    score = max(0, min(100, 100 - penalties))
    if score >= 90:
        grade, label, severity = 'pass', '可用', 'ok'
    elif score >= 75:
        grade, label, severity = 'sample', '抽检', 'warn'
    elif score >= 50:
        grade, label, severity = 'review', '复核', 'warn'
    else:
        grade, label, severity = 'critical', '高危', 'danger'
    if status == 'needs_review' and grade in {'pass', 'sample'}:
        grade, label, severity = 'review', '复核', 'warn'

    return {
        'score': score,
        'grade': grade,
        'label': label,
        'severity': severity,
        'reasons': reasons,
    }


def _short(value: Any, limit=220) -> str:
    text = str(value or '')
    text = re.sub(r'\s+', ' ', text).strip()
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _context_index(row_contexts):
    index = {}
    for context in row_contexts or []:
        if not isinstance(context, dict):
            continue
        for key in ('row', 'source_row', 'output_row', 'data_row'):
            value = context.get(key)
            if value in (None, ''):
                continue
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            index.setdefault(number, context)
    return index


def _context_for_row(row: str, contexts):
    if not row:
        return None
    try:
        number = int(row)
    except (TypeError, ValueError):
        return None
    for candidate in (number, number - 1, number - 2):
        if candidate in contexts:
            return contexts[candidate]
    return None


def _field_context(context, field):
    if not context:
        return None
    fields = context.get('fields') if isinstance(context.get('fields'), dict) else {}
    for candidate in (field, *_FIELD_ALIASES.get(field, ())):
        value = fields.get(candidate)
        if isinstance(value, dict):
            return value
    return None


def _value_for_issue(row: str, field: str, contexts) -> str:
    context = _context_for_row(row, contexts)
    field_data = _field_context(context, field)
    if field_data:
        parts = []
        original = _short(field_data.get('original'))
        processed = _short(field_data.get('processed'))
        current = _short(field_data.get('current'))
        if original:
            parts.append('原始: ' + original)
        if processed:
            parts.append('处理后: ' + processed)
        if not parts and current:
            parts.append('当前: ' + current)
        if parts:
            return ' | '.join(parts)

    title = _short((context or {}).get('title') or (context or {}).get('processed_title'))
    return '标题: ' + title if title else ''


def _audit_severity(item):
    severity = str(item.get('severity') or '').strip()
    if severity in {'review', 'warning', 'error', 'info'}:
        return severity
    method = str(item.get('method') or '').lower()
    if method in {'fallback', 'review'}:
        return 'warning'
    return 'info'


def _audit_message(item):
    stage = str(item.get('stage') or '阶段审计')
    field = str(item.get('field') or '')
    method = str(item.get('method') or '')
    reason = str(item.get('reason') or '')
    detail = ' / '.join(part for part in (method, reason) if part)
    suffix = f'（{detail}）' if detail else ''
    return f'{stage}: {field}{suffix}'


def _append_audit_rows(rows, task):
    for item in _validation_audit(task):
        if not isinstance(item, dict):
            continue
        before = _short(item.get('before'))
        after = _short(item.get('after'))
        value_parts = []
        if before:
            value_parts.append('原始: ' + before)
        if after:
            value_parts.append('处理后: ' + after)
        rows.append({
            'section': 'audit',
            'type': 'change',
            'severity': _audit_severity(item),
            'row': str(item.get('row') or ''),
            'field': str(item.get('field') or ''),
            'message': _audit_message(item),
            'value': ' | '.join(value_parts),
            'action': str(
                item.get('action')
                or '按阶段审计记录抽样确认该字段变化是否符合预期'
            ),
        })


def build_review_report_rows(
        task: dict[str, Any],
        row_contexts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build actionable review rows shared by CSV export and the web console."""
    stats = task.get('stats') if isinstance(task.get('stats'), dict) else {}
    metrics = stats.get('metrics') if isinstance(stats.get('metrics'), dict) else {}
    quality = metrics.get('quality') if isinstance(metrics.get('quality'), dict) else {}
    cache = metrics.get('cache') if isinstance(metrics.get('cache'), dict) else {}
    concurrency = (
        metrics.get('concurrency')
        if isinstance(metrics.get('concurrency'), dict)
        else {}
    )
    issues = _validation_issues(task)
    rows: list[dict[str, Any]] = []
    contexts = _context_index(row_contexts or [])

    rows.append({
        'section': 'summary',
        'type': 'task',
        'severity': 'review' if task.get('status') == 'needs_review' else 'info',
        'row': '',
        'field': 'task',
        'message': str(task.get('error') or '任务复核摘要'),
        'value': (
            f"status={task.get('status') or ''}; "
            f"pipeline={task.get('pipeline') or ''}; "
            f"filename={task.get('filename') or ''}"
        ),
        'action': '优先处理 validation 区域中的行级问题' if issues else '',
    })

    if issues:
        for issue in issues:
            row = _row_number(issue)
            field = _field_from_message(issue)
            rows.append({
                'section': 'validation',
                'type': 'issue',
                'severity': 'review',
                'row': row,
                'field': field,
                'message': issue,
                'value': _value_for_issue(row, field, contexts),
                'action': _action_for_issue(issue),
            })
    elif task.get('error'):
        rows.append({
            'section': 'validation',
            'type': 'task_error',
            'severity': 'error',
            'row': '',
            'field': '',
            'message': str(task.get('error')),
            'value': '',
            'action': '根据错误信息修复后重试任务',
        })

    _append_audit_rows(rows, task)

    issue_count = _quality_issue_count(quality, issues)
    _append_metric(
        rows,
        'quality_issue_count',
        issue_count,
        '先处理 validation 区域中的行级质量问题',
        'warning' if issue_count else 'info',
    )
    _append_metric(rows, 'api_calls', metrics.get('api_calls', ''))
    _append_metric(rows, 'api_success_rate', _pct(metrics.get('api_success_rate')))
    _append_metric(rows, 'http_attempts', metrics.get('http_attempts', ''))
    rate_wait_s = metrics.get('rate_wait_s', '')
    _append_metric(
        rows,
        'rate_wait_s',
        rate_wait_s,
        '表示为遵守 RPM 主动等待的时间；很高时说明瓶颈在供应商限速，不是本地并发',
        'warning' if _num(rate_wait_s, 0) > 300 else 'info',
    )
    http_retries = metrics.get('http_retries', '')
    _append_metric(
        rows,
        'http_retries',
        http_retries,
        '如大于 0，检查网络/API 稳定性，必要时降低并发后重跑',
        'warning' if _positive(http_retries) else 'info',
    )
    circuit_open = metrics.get('circuit_open', '')
    _append_metric(
        rows,
        'circuit_open',
        circuit_open,
        '如大于 0，检查 API key/余额/服务稳定性',
        'warning' if _positive(circuit_open) else 'info',
    )
    _append_metric(
        rows,
        'cache_hit_rate',
        _pct(cache.get('hit_rate')),
        '命中率低时优先确认是否改过 prompt 或清过缓存',
    )
    concurrency_reductions = concurrency.get('reductions', '')
    _append_metric(
        rows,
        'concurrency_reductions',
        concurrency_reductions,
        '如大于 0，检查 429/503、网络波动或降低并发上限',
        'warning' if _positive(concurrency_reductions) else 'info',
    )

    by_operation = (
        concurrency.get('by_operation')
        if isinstance(concurrency.get('by_operation'), dict)
        else {}
    )
    for operation, values in by_operation.items():
        if not isinstance(values, dict):
            continue
        rows.append({
            'section': 'concurrency',
            'type': 'operation',
            'severity': 'warning' if values.get('reductions') else 'info',
            'row': '',
            'field': str(operation),
            'message': (
                f"workers {values.get('initial_workers', '')}"
                f" -> {values.get('final_workers', '')}"
            ),
            'value': (
                f"reductions={values.get('reductions', 0)}; "
                f"failures={values.get('failures', 0)}; "
                f"attempts={values.get('attempts', 1)}; "
                f"attempted_items={values.get('attempted_items', values.get('items', ''))}"
            ),
            'action': '失败率高时已自动降并发，复核 API 稳定性',
        })

    return rows


def build_review_data(
        task: dict[str, Any],
        row_contexts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return JSON-ready review data for the task detail workbench."""
    rows = build_review_report_rows(task, row_contexts=row_contexts)
    stats = task.get('stats') if isinstance(task.get('stats'), dict) else {}
    metrics = stats.get('metrics') if isinstance(stats.get('metrics'), dict) else {}
    quality = metrics.get('quality') if isinstance(metrics.get('quality'), dict) else {}
    cache = metrics.get('cache') if isinstance(metrics.get('cache'), dict) else {}
    concurrency = (
        metrics.get('concurrency')
        if isinstance(metrics.get('concurrency'), dict)
        else {}
    )
    validation_rows = [
        row for row in rows
        if row.get('section') == 'validation'
    ]
    audit_rows = [
        row for row in rows
        if row.get('section') == 'audit'
    ]
    warning_metrics = [
        row for row in rows
        if row.get('section') in {'metrics', 'concurrency'}
        and row.get('severity') in {'warning', 'error', 'review'}
    ]
    issues = _validation_issues(task)
    summary = {
        'status': task.get('status') or '',
        'filename': task.get('filename') or '',
        'pipeline': task.get('pipeline') or '',
        'quality_score': build_quality_score(task),
        'issue_count': _quality_issue_count(quality, issues),
        'validation_item_count': len(validation_rows),
        'audit_item_count': len(audit_rows),
        'warning_metric_count': len(warning_metrics),
        'http_retries': metrics.get('http_retries', 0),
        'rate_wait_s': metrics.get('rate_wait_s', 0),
        'circuit_open': metrics.get('circuit_open', 0),
        'concurrency_reductions': concurrency.get('reductions', 0),
        'cache_hit_rate': _pct(cache.get('hit_rate')),
    }
    return {
        'summary': summary,
        'items': rows,
    }


def build_review_report_csv(
        task: dict[str, Any],
        row_contexts: list[dict[str, Any]] | None = None,
) -> str:
    """Build a UTF-8 CSV string with actionable review evidence."""
    rows = build_review_report_rows(task, row_contexts=row_contexts)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=REPORT_COLUMNS, lineterminator='\n')
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, '') for column in REPORT_COLUMNS})
    return output.getvalue()
