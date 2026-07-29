"""Generate, validate, and re-assess safe image replacements."""
from __future__ import annotations

import os
import time
from typing import Any, Callable

from crosspilot.image_risk import assessment_status, unknown_image_assessment
from ...concurrency import adaptive_map
from ...model_provider import ProviderQuotaError
from ..amazon_quality import AMAZON_IMAGE_GEN_CONCURRENCY
from .assessment import safe_assess, validate_image_url
from .cache import is_current_assessment, save_cache


def cached_generation(
    cache: dict[str, Any],
    generation_version: str,
    kind: str,
    source_url: str,
) -> str:
    """Return only a current generated URL that passed structured review."""
    key = f"{kind}:{source_url}"
    generated = str(cache["gen_results"].get(key) or "")
    meta = cache["gen_meta"].get(key) or {}
    generated_assessment = meta.get("risk_assessment")
    if (
        generated
        and meta.get("prompt_version") == generation_version
        and is_current_assessment(generated_assessment)
        and assessment_status(generated_assessment) == "safe"
    ):
        return generated
    return ""


def _generation_route_limit() -> int:
    try:
        return max(
            1,
            min(
                3,
                int(
                    os.environ.get(
                        "CROSSPILOT_IMAGE_SAFETY_REGEN_LIMIT",
                        "2",
                    )
                ),
            ),
        )
    except ValueError:
        return 2


def generate_safe_replacements(
    targets: list[tuple[str, str]],
    *,
    cache: dict[str, Any],
    cache_path: str | None,
    generation_version: str,
    provider_getter: Callable[[], object],
    concurrency_stats: dict[str, Any],
) -> list[tuple[str, str]]:
    """Generate missing replacements and accept only decoded, reviewed images."""
    targets_to_generate = [
        item
        for item in targets
        if not cached_generation(
            cache,
            generation_version,
            item[1],
            item[0],
        )
    ]
    if not targets_to_generate:
        return targets_to_generate

    generation_routes = _generation_route_limit()

    def generate_one(item: tuple[str, str]) -> dict[str, Any]:
        source_url, kind = item
        provider = provider_getter()
        last_reason = "generation_failed"
        last_assessment = unknown_image_assessment("no generated candidate")
        for route_offset in range(generation_routes):
            try:
                generated = str(
                    provider.call_image_gen(
                        source_url,
                        is_variant=kind == "variant",
                        context="",
                        route_offset=route_offset,
                    )
                    or ""
                )
            except ProviderQuotaError:
                raise
            except Exception as exc:
                last_reason = type(exc).__name__
                continue
            valid, reason = validate_image_url(generated)
            if not valid:
                last_reason = reason
                continue
            last_assessment = safe_assess(provider, generated)
            if assessment_status(last_assessment) == "safe":
                return {
                    "url": generated,
                    "assessment": last_assessment,
                    "route_offset": route_offset,
                }
            last_reason = "generated_image_" + assessment_status(last_assessment)
        return {
            "url": "",
            "assessment": last_assessment,
            "failure_reason": last_reason,
        }

    def generate_done(item: tuple[str, str], result: object) -> None:
        source_url, kind = item
        key = f"{kind}:{source_url}"
        if isinstance(result, Exception) or not isinstance(result, dict):
            result = {
                "url": "",
                "assessment": unknown_image_assessment(
                    "generation worker failed"
                ),
                "failure_reason": "generation_worker_failed",
            }
        generated = str(result.get("url") or "")
        if generated:
            cache["gen_results"][key] = generated
            cache["gen_meta"][key] = {
                "kind": kind,
                "source_url": source_url,
                "prompt_version": generation_version,
                "risk_assessment": result["assessment"],
                "route_offset": int(result.get("route_offset") or 0),
                "ts": int(time.time()),
            }
            cache["gen_failures"].pop(key, None)
        else:
            cache["gen_failures"][key] = {
                "kind": kind,
                "source_url": source_url,
                "prompt_version": generation_version,
                "reason": str(
                    result.get("failure_reason") or "generation_failed"
                )[:120],
                "risk_assessment": result.get("assessment"),
                "ts": int(time.time()),
            }
        save_cache(cache_path, cache)

    print(
        f"风险主图/变种图修复并复审: {len(targets_to_generate)} 张...",
        flush=True,
    )
    _, generation_stats = adaptive_map(
        targets_to_generate,
        generate_one,
        operation="amazon_safe_image_gen",
        initial_workers=AMAZON_IMAGE_GEN_CONCURRENCY,
        min_workers=2,
        is_success=lambda value: (
            isinstance(value, dict) and bool(value.get("url"))
        ),
        on_result=generate_done,
        terminal_exceptions=(ProviderQuotaError,),
        backoff_s=2,
        max_backoff_s=15,
    )
    concurrency_stats["amazon_safe_image_gen"] = generation_stats
    return targets_to_generate


__all__ = ["cached_generation", "generate_safe_replacements"]
