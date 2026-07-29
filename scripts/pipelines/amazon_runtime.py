"""Amazon pipeline run context, progress reporting, and stage execution."""
from __future__ import annotations

from dataclasses import dataclass, field
import inspect
import json
import os
import threading
import time
from typing import Any, Callable

from scripts.pipeline_log import PipelineMetrics, log as _log


AMAZON_STAGES = [
    "读取表格",
    "审图+生图",
    "标题优化",
    "描述清洗",
    "Bullet+关键词",
    "写回填表",
]

_DEGRADED_ISSUE_CODES = {
    "标题优化": "title_ai_fallback",
    "描述清洗": "description_ai_fallback",
    "Bullet+关键词": "bullet_rule_fallback",
}


class AmazonStatusReporter:
    """Persist one run's externally visible progress state."""

    def __init__(self, table_path: str) -> None:
        self.status_path = os.path.splitext(table_path)[0] + "_status.json"
        self.started_at = time.time()
        self.stage_index = 0
        self.stage_started_at = self.started_at
        self.current_stage = AMAZON_STAGES[0]
        self.total = 0

    def stage(self, name: str, current: int = 0, total: int = 0) -> None:
        self.stage_index = AMAZON_STAGES.index(name)
        self.current_stage = name
        self.stage_started_at = time.time()
        self.total = total
        self.update(current, total)

    def update(self, current: int, total: int | None = None) -> None:
        if total is not None:
            self.total = total
        elapsed = time.time() - self.stage_started_at
        eta = (
            int(elapsed / current * (self.total - current))
            if current and self.total
            else 0
        )
        self._write({
            "status": "running",
            "stage": self.current_stage,
            "stage_index": self.stage_index + 1,
            "stage_total": len(AMAZON_STAGES),
            "current": current,
            "total": self.total,
            "percent": (
                int(current / self.total * 100)
                if self.total
                else 0
            ),
            "eta_s": eta,
        })

    def failed(self, name: str, error: Exception) -> None:
        self._write({
            "status": "failed",
            "stage": "错误",
            "stage_index": (
                AMAZON_STAGES.index(name) + 1
                if name in AMAZON_STAGES
                else self.stage_index + 1
            ),
            "stage_total": len(AMAZON_STAGES),
            "error": str(error),
        })

    def finish(
        self,
        output: str,
        validation: dict | None = None,
        metrics: dict | None = None,
    ) -> None:
        validation = validation or {"passed": True, "issues": []}
        needs_review = not validation.get("passed", False)
        self._write({
            "status": "needs_review" if needs_review else "done",
            "stage": "待人工复核" if needs_review else "完成",
            "stage_index": len(AMAZON_STAGES),
            "stage_total": len(AMAZON_STAGES),
            "current": 1,
            "total": 1,
            "percent": 100,
            "eta_s": 0,
            "output": output,
            "validation": validation,
            "metrics": metrics or {},
            "error": (
                f"输出存在 {len(validation.get('issues', []))} 项"
                "质量问题，请复核后使用"
                if needs_review
                else None
            ),
        })

    def _write(self, data: dict) -> None:
        data["total_elapsed_s"] = int(time.time() - self.started_at)
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        temp_path = (
            self.status_path + f".{threading.get_ident()}.tmp"
        )
        for _ in range(3):
            try:
                with open(temp_path, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, ensure_ascii=False, indent=2)
                os.replace(temp_path, self.status_path)
                break
            except (PermissionError, OSError):
                time.sleep(0.1)


@dataclass
class AmazonRunContext:
    """Own mutable state and stage instrumentation for one Amazon run."""

    source_path: str
    request_id: str
    provider: Any
    status: AmazonStatusReporter
    metrics: PipelineMetrics = field(default_factory=PipelineMetrics)
    data: list[dict] = field(default_factory=list)
    quality_issues: list = field(default_factory=list)
    runtime_metrics: dict = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        source_path: str,
        request_id: str,
        provider: Any,
    ) -> "AmazonRunContext":
        return cls(
            source_path=source_path,
            request_id=request_id,
            provider=provider,
            status=AmazonStatusReporter(source_path),
        )

    def transform(
        self,
        name: str,
        function: Callable,
        *args,
        **kwargs,
    ) -> list[dict]:
        """Run a data stage and replace the current row collection."""
        result = self.execute(
            name,
            function,
            self.data,
            *args,
            **kwargs,
        )
        self.data = result
        return self.data

    def execute(
        self,
        name: str,
        function: Callable,
        *args,
        **kwargs,
    ):
        """Run one stage with progress, failure, and downgrade metrics."""
        started_at = time.time()
        item_count = len(self.data)
        self.status.stage(name, 0, item_count)
        try:
            call_kwargs = {"progress": self.status.update}
            try:
                parameters = inspect.signature(function).parameters
                accepts_any = any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                )
                if accepts_any:
                    call_kwargs.update(kwargs)
                else:
                    call_kwargs.update({
                        key: value
                        for key, value in kwargs.items()
                        if key in parameters
                    })
            except (TypeError, ValueError):
                call_kwargs.update(kwargs)
            result = function(*args, **call_kwargs)
            rows = result if isinstance(result, list) else self.data
            issue_code = _DEGRADED_ISSUE_CODES.get(name)
            degraded = (
                sum(
                    1
                    for row in rows
                    if any(
                        issue.get("code") == issue_code
                        for issue in row.get("_quality_issues", [])
                    )
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
            self.status.failed(name, exc)
            raise


__all__ = [
    "AMAZON_STAGES",
    "AmazonRunContext",
    "AmazonStatusReporter",
]
