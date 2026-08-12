"""Run state, instrumentation, and the public result contract."""
from __future__ import annotations

from dataclasses import dataclass, field
import inspect
from pathlib import Path
import time
from typing import Any, Callable

from .log import PipelineMetrics, log as _log


STAGES = (
    "读取采集表",
    "审图与生图",
    "标题优化",
    "描述清洗",
    "Bullet与关键词",
    "多站点文案校验",
    "交付回填表",
)

_DEGRADED_ISSUE_CODES = {
    "标题优化": "title_ai_fallback",
    "描述清洗": "description_ai_fallback",
    "Bullet与关键词": "bullet_rule_fallback",
}


@dataclass(frozen=True)
class RunResult:
    """Everything a caller needs after one successful processing run."""

    output_path: Path | None
    review_path: Path
    review_data_path: Path
    archived_path: Path | None
    retained_products: int
    quarantined_products: int
    elapsed_s: float
    published: bool = True
    pending_product_ids: tuple[str, ...] = ()
    isolated_product_ids: tuple[str, ...] = ()
    exception_path: Path | None = None


@dataclass
class RunContext:
    """Own all mutable state for one Amazon JSON run."""

    source_path: Path
    request_id: str
    provider: Any
    started_at: float = field(default_factory=time.time)
    metrics: PipelineMetrics = field(default_factory=PipelineMetrics)
    data: list[dict[str, Any]] = field(default_factory=list)
    quality_issues: list[str] = field(default_factory=list)
    runtime_metrics: dict[str, Any] = field(default_factory=dict)

    def transform(
        self,
        name: str,
        function: Callable,
        *args: object,
        **kwargs: object,
    ) -> list[dict[str, Any]]:
        result = self.execute(name, function, self.data, *args, **kwargs)
        self.data = result
        return self.data

    def execute(
        self,
        name: str,
        function: Callable,
        *args: object,
        **kwargs: object,
    ) -> Any:
        """Run one stage while keeping progress and downgrade details local."""
        started_at = time.time()
        item_count = len(self.data)

        def progress(current: int, total: int) -> None:
            if total and (current == total or current % max(1, total // 20) == 0):
                print(f"[{name}] {current}/{total}", flush=True)

        try:
            call_kwargs: dict[str, object] = {}
            try:
                parameters = inspect.signature(function).parameters
                accepts_any = any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                )
                if accepts_any or "progress" in parameters:
                    call_kwargs["progress"] = progress
                call_kwargs.update(
                    kwargs
                    if accepts_any
                    else {
                        key: value
                        for key, value in kwargs.items()
                        if key in parameters
                    }
                )
            except (TypeError, ValueError):
                call_kwargs.update(kwargs)
            result = function(*args, **call_kwargs)
            rows = result if isinstance(result, list) else self.data
            issue_code = _DEGRADED_ISSUE_CODES.get(name)
            degraded = (
                sum(
                    any(
                        issue.get("code") == issue_code
                        for issue in row.get("_quality_issues", [])
                    )
                    for row in rows
                )
                if issue_code
                else 0
            )
            self.metrics.record_stage(
                name,
                time.time() - started_at,
                item_count,
                max(0, item_count - degraded),
            )
            return result
        except Exception as exc:
            _log.error(
                f"Amazon阶段 [{name}] 失败",
                error=str(exc),
                exc_info=True,
            )
            raise


__all__ = ["RunContext", "RunResult", "STAGES"]
