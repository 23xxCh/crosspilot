"""Amazon description cleanup, fact protection, and garbage filtering."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import re

from ..config.prompts import get_prompt_registry
from ..providers import (
    ProviderQuotaError,
    get_provider as _default_get_provider,
)
from ..log import log as _log
from ..quality import (
    AMAZON_DESC_CONCURRENCY,
    BRAND_RE as _BRAND_RE,
    IMG_RE as _IMG_RE,
    LOGISTICS_RE as _LOGISTICS_RE,
    OEM_RE as _OEM_RE,
    RETURN_RE as _RETURN_RE,
    add_audit as _add_audit,
    add_quality_issue as _add_quality_issue,
    missing_factual_markers as _missing_factual_markers,
)


QuotaExhaustedError = ProviderQuotaError
_prompts = get_prompt_registry()


def clean_descriptions(data, progress=None, provider_getter=None):
    """Rule-clean descriptions, then remove residual templates with AI."""
    print(f"描述清洗 {len(data)} 条...", flush=True)
    changed = 0
    for index, row in enumerate(data):
        description = row["desc"]
        if description:
            original = description
            description = _BRAND_RE.sub("", description)
            description = _OEM_RE.sub("", description)
            description = _LOGISTICS_RE.sub("", description)
            description = _RETURN_RE.sub("", description)
            description = _IMG_RE.sub("", description)
            description = re.sub(
                r"\n{3,}",
                "\n\n",
                description,
            ).strip()
            if description != original:
                row["desc"] = description
                changed += 1
                _add_audit(
                    row,
                    "描述清洗",
                    "description",
                    original,
                    description,
                    method="rule",
                    reason="remove_brand_policy_or_images",
                    action="确认描述仍保留尺寸/型号/数量等硬规格",
                )
        if progress:
            progress(index + 1, max(1, len(data) * 2))
    print(f"  规则清洗: {changed} 行", flush=True)

    def clean_one(index, description):
        try:
            provider = (
                provider_getter or _default_get_provider
            )()
            result = provider.call_text(
                _prompts.render(
                    "amazon.description_clean",
                    description=description,
                ),
                max_tokens=2048,
            )
            if result:
                return index, result.strip()
        except QuotaExhaustedError:
            raise
        except Exception as exc:
            _log.warn(
                "Amazon描述AI清洗异常",
                row=index,
                error=str(exc),
            )
        return index, ""

    items = [
        (index, row["desc"])
        for index, row in enumerate(data)
        if row.get("desc")
    ]
    done = 0
    with ThreadPoolExecutor(
        max_workers=min(
            AMAZON_DESC_CONCURRENCY,
            max(1, len(items)),
        )
    ) as pool:
        futures = {
            pool.submit(clean_one, index, description): index
            for index, description in items
        }
        for future in as_completed(futures):
            try:
                index, cleaned = future.result()
            except QuotaExhaustedError:
                for pending in futures:
                    pending.cancel()
                raise
            except Exception as exc:
                index = futures[future]
                cleaned = ""
                _log.warn(
                    "Amazon描述AI清洗异常",
                    row=index,
                    error=str(exc)[:100],
                )
            if cleaned:
                cleaned = (
                    _IMG_RE.sub("", cleaned)
                    .replace("__IMG__", "")
                )
                cleaned = _BRAND_RE.sub("", cleaned).strip()
                if cleaned:
                    original = str(
                        data[index].get("desc", "")
                    ).lower()
                    has_cross_sell = any(
                        marker in original
                        for marker in (
                            " usd",
                            "store categories",
                            "welcome to",
                            "payment",
                            "shipping policy",
                        )
                    )
                    if not has_cross_sell:
                        missing = _missing_factual_markers(
                            data[index].get("desc", ""),
                            cleaned,
                        )
                        if missing:
                            _add_audit(
                                data[index],
                                "描述清洗",
                                "description",
                                data[index].get("desc", ""),
                                cleaned,
                                method="ai_rejected",
                                reason="description_fact_loss",
                                severity="warning",
                                action=(
                                    "已拒绝 AI 描述（丢失关键规格："
                                    + ", ".join(missing[:5])
                                    + "），保留规则结果"
                                ),
                            )
                            cleaned = ""
                    if cleaned:
                        before_description = data[index].get(
                            "desc",
                            "",
                        )
                        data[index]["desc"] = cleaned
                        _add_audit(
                            data[index],
                            "描述清洗",
                            "description",
                            before_description,
                            cleaned,
                            method="ai",
                            reason="model_clean",
                            action="抽样确认 AI 清洗未删除关键规格",
                        )
                else:
                    _add_quality_issue(
                        data[index],
                        "description_ai_fallback",
                        "描述模型结果清洗后为空，已保留规则清洗结果",
                    )
            else:
                _add_quality_issue(
                    data[index],
                    "description_ai_fallback",
                    "描述模型未返回有效结果，已保留规则清洗结果",
                )
            done += 1
            if progress:
                progress(
                    len(data) + done,
                    max(1, len(data) * 2),
                )
    for row in data:
        if row.get("desc"):
            before_description = row["desc"]
            row["desc"] = _BRAND_RE.sub(
                "",
                row["desc"],
            ).strip()
            if row["desc"] != before_description:
                _add_audit(
                    row,
                    "描述清洗",
                    "description",
                    before_description,
                    row["desc"],
                    method="rule",
                    reason="final_brand_strip",
                    action="确认品牌清理没有误删兼容车型信息",
                )
        if row.get("keywords"):
            before_keywords = row["keywords"]
            row["keywords"] = _BRAND_RE.sub(
                "",
                row["keywords"],
            ).strip()
            if row["keywords"] != before_keywords:
                _add_audit(
                    row,
                    "Bullet+关键词",
                    "keywords",
                    before_keywords,
                    row["keywords"],
                    method="rule",
                    reason="final_brand_strip",
                    action="确认关键词仍有 10 个有效搜索词",
                )
    if progress:
        progress(1, 1)
    return data


def remove_dirty_descriptions(data):
    """Remove rows whose description is only cross-selling boilerplate."""
    product_signals = (
        "material",
        "size",
        "dimension",
        "color",
        "weight",
        "feature",
        "package include",
        "made of",
        "install",
        "compatible",
        "fit",
        "材质",
        "尺寸",
        "规格",
        "颜色",
        "重量",
        "特点",
        "包装",
        "安装",
        "适用",
    )
    garbage_only_patterns = (
        "store categoriesstore categories",
        "add me to favourite",
        "visit our store",
        "welcome to my store",
        "please contact us before",
        "our goal is customer",
    )
    dirty_ids = []
    for row in data:
        description = str(row.get("desc", "")).lower()
        has_product = any(
            signal in description
            for signal in product_signals
        ) or (
            len(description) > 50 and " USD" not in description
        )
        is_pure_garbage = (
            any(
                pattern in description
                for pattern in garbage_only_patterns
            )
            and not has_product
        )
        if not is_pure_garbage:
            continue
        row.setdefault("_quality_issues", []).append({
            "code": "dirty_description",
            "message": "描述为纯交叉销售模板，无产品内容",
        })
        dirty_ids.append(str(row.get("id", "")))
    dirty_id_set = set(dirty_ids)
    retained = [
        row
        for row in data
        if str(row.get("id", "")) not in dirty_id_set
    ]
    return retained, dirty_ids


__all__ = [
    "clean_descriptions",
    "remove_dirty_descriptions",
]
