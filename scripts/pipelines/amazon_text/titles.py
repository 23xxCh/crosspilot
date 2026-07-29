"""Amazon title normalization and search-traffic refinement."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import re

from crosspilot.prompt_registry import get_prompt_registry
from scripts.model_provider import (
    ProviderQuotaError,
    get_provider as _default_get_provider,
)
from scripts.pipeline_log import log as _log
from scripts.services.amazon_titles import normalize_amazon_title
from scripts.pipelines.amazon_quality import (
    AMAZON_TITLE_CONCURRENCY,
    add_audit as _add_audit,
    add_quality_issue as _add_quality_issue,
    missing_factual_markers as _missing_factual_markers,
    unexpected_brand_markers as _unexpected_brand_markers,
)


QuotaExhaustedError = ProviderQuotaError
_prompts = get_prompt_registry()


def optimize_titles(data, progress=None, provider_getter=None):
    """Apply the Generic compatibility contract and refine search ordering."""
    print(f"标题优化 {len(data)} 条...", flush=True)
    changed = 0
    for index, row in enumerate(data):
        original_title = str(row["title"] or "").strip()
        title = original_title
        if not title:
            continue
        title = normalize_title(title)
        if title != row["title"]:
            row["title"] = title
            changed += 1
            _add_audit(
                row,
                "标题优化",
                "title",
                original_title,
                title,
                method="rule",
                reason="normalize_title",
                action="确认标题兼容表达和 75 字符限制",
            )
        if progress:
            progress(index + 1, max(1, len(data) * 2))
    print(f"  规则优化: {changed} 行", flush=True)

    to_optimize = [
        (index, row)
        for index, row in enumerate(data)
        if row["title"]
    ]
    if to_optimize:
        print(
            f"  API 优化 {len(to_optimize)} 条"
            f"（DeepSeek，{AMAZON_TITLE_CONCURRENCY} 并发）...",
            flush=True,
        )

        def optimize_one(index, title):
            try:
                provider = (
                    provider_getter or _default_get_provider
                )()
                result = provider.call_text(
                    _prompts.render(
                        "amazon.title_optimize",
                        title=title,
                    ),
                    max_tokens=128,
                )
                if result:
                    return index, result.strip()
            except QuotaExhaustedError:
                raise
            except Exception as exc:
                _log.warn("标题优化异常", error=str(exc))
            return index, None

        done = 0
        with ThreadPoolExecutor(
            max_workers=AMAZON_TITLE_CONCURRENCY
        ) as pool:
            futures = {
                pool.submit(
                    optimize_one,
                    index,
                    row["title"],
                ): index
                for index, row in to_optimize
            }
            for future in as_completed(futures):
                try:
                    index, new_title = future.result()
                except QuotaExhaustedError:
                    for pending in futures:
                        pending.cancel()
                    raise
                if new_title:
                    if re.search(
                        r"(optimize|need to|brand is generic|Rules:|这里|以下)",
                        new_title,
                        re.IGNORECASE,
                    ):
                        _log.warn(
                            "标题优化返回meta文本，回退规则处理",
                            row=index,
                        )
                        _add_quality_issue(
                            data[index],
                            "title_ai_fallback",
                            "标题模型返回了说明性文本，已使用规则结果",
                        )
                    else:
                        normalized = normalize_title(new_title)
                        missing = _missing_factual_markers(
                            data[index].get("title", ""),
                            normalized,
                        )
                        unexpected_brands = _unexpected_brand_markers(
                            data[index].get("title", ""),
                            normalized,
                        )
                        if missing or unexpected_brands:
                            reason = (
                                "title_fact_loss"
                                if missing
                                else "title_brand_hallucination"
                            )
                            details = (
                                "丢失关键规格："
                                + ", ".join(missing[:5])
                                if missing
                                else "新增源标题没有的品牌："
                                + ", ".join(unexpected_brands[:5])
                            )
                            _add_audit(
                                data[index],
                                "标题优化",
                                "title",
                                data[index].get("title", ""),
                                normalized,
                                method="ai_rejected",
                                reason=reason,
                                severity="warning",
                                action=(
                                    "已拒绝 AI 标题（"
                                    + details
                                    + "），保留规则结果"
                                ),
                            )
                        else:
                            before_title = data[index].get(
                                "title",
                                "",
                            )
                            data[index]["title"] = normalized
                            _add_audit(
                                data[index],
                                "标题优化",
                                "title",
                                before_title,
                                normalized,
                                method="ai",
                                reason="model_refine",
                                action=(
                                    "抽样确认 AI 标题未丢失规格/数量/"
                                    "适配信息"
                                ),
                            )
                else:
                    _add_quality_issue(
                        data[index],
                        "title_ai_fallback",
                        "标题模型未返回有效结果，已使用规则结果",
                    )
                done += 1
                if progress:
                    progress(
                        len(data) + done,
                        max(1, len(data) * 2),
                    )
                if done % 10 == 0:
                    print(
                        f"    API标题: {done}/{len(to_optimize)}",
                        flush=True,
                    )
    for row in data:
        before_title = row.get("title", "")
        row["title"] = normalize_title(before_title)
        if row["title"] != before_title:
            _add_audit(
                row,
                "标题优化",
                "title",
                before_title,
                row["title"],
                method="rule",
                reason="final_normalize",
                action="确认最终标题仍保留硬规格",
            )
    if progress:
        progress(1, 1)
    return data


def clamp_title(title):
    title = str(title or "").strip()
    if len(title) <= 75:
        return title
    shortened = title[:75]
    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]
    return shortened[:75].strip()


def normalize_title(title):
    return normalize_amazon_title(title)


__all__ = [
    "clamp_title",
    "normalize_title",
    "optimize_titles",
]
