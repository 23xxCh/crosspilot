"""Bounded Amazon row audit records and user-facing quality summaries."""
from __future__ import annotations

import re


ROW_QUALITY_LABELS = {
    "missing_source_description": "源产品描述缺失",
    "title_ai_fallback": "标题 AI 优化降级为规则处理",
    "title_fact_loss": "标题关键规格可能丢失",
    "description_ai_fallback": "描述 AI 清洗降级为规则处理",
    "description_fact_loss": "描述关键规格可能丢失",
    "bullet_rule_fallback": "Bullet/关键词由规则补全",
    "bullet_quality_warning": "Bullet 内容质量需要复核",
    "keyword_quality_warning": "关键词内容质量需要复核",
    "main_image_generation_failed": "风险主图生成失败并保留原图",
    "variant_image_generation_failed": "风险变种图生成失败并保留原图",
}

MAX_ROW_AUDIT_ITEMS = 20
MAX_VALIDATION_AUDIT_ITEMS = 120

ISSUE_AUDIT_META = {
    "missing_source_description": (
        "读取表格",
        "description",
        "review",
    ),
    "title_ai_fallback": ("标题优化", "title", "fallback"),
    "title_fact_loss": ("标题优化", "title", "review"),
    "description_ai_fallback": (
        "描述清洗",
        "description",
        "fallback",
    ),
    "description_fact_loss": (
        "描述清洗",
        "description",
        "review",
    ),
    "bullet_rule_fallback": (
        "Bullet+关键词",
        "Bullet",
        "fallback",
    ),
    "bullet_quality_warning": (
        "Bullet+关键词",
        "Bullet",
        "review",
    ),
    "keyword_quality_warning": (
        "Bullet+关键词",
        "keywords",
        "review",
    ),
}


def audit_text(value, limit=180):
    if isinstance(value, (list, tuple)):
        text = " | ".join(
            str(item or "").strip()
            for item in value
            if str(item or "").strip()
        )
    else:
        text = str(value or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def add_audit(
    row,
    stage,
    field,
    before,
    after,
    *,
    method="rule",
    reason="",
    severity="info",
    action="",
):
    """Attach one de-duplicated, bounded audit entry to a row."""
    before_text = audit_text(before)
    after_text = audit_text(after)
    reason = str(reason or "").strip()
    if (
        before_text == after_text
        and severity not in {"warning", "review", "error"}
    ):
        return
    audit = row.setdefault("_audit", [])
    if len(audit) >= MAX_ROW_AUDIT_ITEMS:
        return
    key = (
        stage,
        field,
        method,
        reason,
        before_text,
        after_text,
    )
    for item in audit:
        existing = (
            item.get("stage"),
            item.get("field"),
            item.get("method"),
            item.get("reason"),
            item.get("before"),
            item.get("after"),
        )
        if existing == key:
            return
    audit.append({
        "stage": str(stage or ""),
        "field": str(field or ""),
        "method": str(method or ""),
        "reason": reason,
        "before": before_text,
        "after": after_text,
        "severity": severity,
        "action": str(action or ""),
    })


def add_quality_issue(row, code, message):
    """Attach an issue and its corresponding audit evidence once."""
    issues = row.setdefault("_quality_issues", [])
    if any(issue.get("code") == code for issue in issues):
        return
    issues.append({"code": code, "message": message})
    stage, field, method = ISSUE_AUDIT_META.get(
        code,
        ("质量检查", "", "review"),
    )
    if field:
        value = (
            row.get("desc")
            if field == "description"
            else row.get(field, "")
        )
        add_audit(
            row,
            stage,
            field,
            value,
            value,
            method=method,
            reason=code,
            severity="warning",
            action=message,
        )


def summarize_audit_trail(
    data,
    max_items=MAX_VALIDATION_AUDIT_ITEMS,
):
    items = []
    for row_number, row in enumerate(data, 1):
        for entry in row.get("_audit", []):
            if not isinstance(entry, dict):
                continue
            normalized = {
                "row": row_number,
                "stage": str(entry.get("stage") or ""),
                "field": str(entry.get("field") or ""),
                "method": str(entry.get("method") or ""),
                "reason": str(entry.get("reason") or ""),
                "before": audit_text(entry.get("before")),
                "after": audit_text(entry.get("after")),
                "severity": str(
                    entry.get("severity") or "info"
                ),
                "action": str(entry.get("action") or ""),
            }
            if normalized["stage"] and normalized["field"]:
                items.append(normalized)
            if len(items) >= max_items:
                return items
    return items


def attach_audit_to_validation(validation, data):
    validation = dict(validation or {})
    audit = summarize_audit_trail(data)
    if audit:
        validation["audit"] = audit
        validation["audit_truncated"] = (
            len(audit) >= MAX_VALIDATION_AUDIT_ITEMS
        )
    return validation


def summarize_row_quality_issues(data, max_examples=5):
    """Aggregate row degradations into bounded review messages."""
    grouped = {}
    for row_number, row in enumerate(data, 1):
        seen_codes = set()
        for issue in row.get("_quality_issues", []):
            if not isinstance(issue, dict):
                continue
            code = str(issue.get("code") or "").strip()
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            grouped.setdefault(code, []).append(row_number)

    summary = []
    for code, row_numbers in grouped.items():
        label = ROW_QUALITY_LABELS.get(code, code)
        examples = "、".join(
            f"第 {number} 行"
            for number in row_numbers[:max_examples]
        )
        suffix = (
            "等"
            if len(row_numbers) > max_examples
            else ""
        )
        summary.append(
            f"{label}：{len(row_numbers)} 行"
            f"（{examples}{suffix}），请抽样复核"
        )
    return summary


__all__ = [
    "ISSUE_AUDIT_META",
    "MAX_ROW_AUDIT_ITEMS",
    "MAX_VALIDATION_AUDIT_ITEMS",
    "ROW_QUALITY_LABELS",
    "add_audit",
    "add_quality_issue",
    "attach_audit_to_validation",
    "audit_text",
    "summarize_audit_trail",
    "summarize_row_quality_issues",
]
