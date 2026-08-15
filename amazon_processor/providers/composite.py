"""Operation routing, fallback, metrics, and circuit breaking."""
from __future__ import annotations

import threading
import time
from typing import Optional

from .agnes import AgnesProvider
from .support import ModelProvider, CongestionPolicy
from .deepseek import DeepSeekProvider
from .support import (
    ProviderAuthError,
    ProviderCircuitOpenError,
    ProviderError,
    ProviderQuotaError,
)
from .gpt_image import GPTImageProvider
from .ollama_vision import OllamaVisionProvider


class CompositeProvider(ModelProvider):
    """Route text, vision, and image operations to configured providers."""

    def __init__(self, config: dict) -> None:
        self._config = config
        self._providers: dict[str, ModelProvider] = {}
        self._metrics_lock = threading.Lock()
        self._metrics = {
            "api_calls": 0,
            "api_errors": 0,
            "latency_s": 0.0,
            "http_attempts": 0,
            "http_errors": 0,
            "http_retries": 0,
            "http_status": {},
            "circuit_open": 0,
            "fallback_attempts": 0,
            "fallback_successes": 0,
            "fallback_failures": 0,
            "fallback_routes": {},
            "by_operation": {},
        }
        self._circuit = {}
        self._circuit_threshold = max(
            1,
            int(config.get("circuit_failure_threshold", 8)),
        )
        self._circuit_cooldown_s = max(
            1,
            int(config.get("circuit_cooldown_s", 60)),
        )
        self._agnes_congestion_policy = (
            CongestionPolicy.from_mapping(config)
        )
        self._text_fallbacks: list[ModelProvider] = []
        self._vision_fallbacks: list[ModelProvider] = []
        self._image_gen_fallbacks: list[ModelProvider] = []
        self._fallback_image_gen = None
        if config.get("routes"):
            self._configure_route_table(config["routes"])
        else:
            self._configure_text(config)
            self._configure_vision(config)
            self._configure_image(config)

        providers = (
            list(self._providers.values())
            + self._text_fallbacks
            + self._vision_fallbacks
            + self._image_gen_fallbacks
        )
        for provider in {
            id(item): item for item in providers
        }.values():
            provider.set_attempt_hook(self._record_attempt)
            provider.set_circuit_hook(
                self._record_provider_circuit_open
            )

    def _configure_route_table(self, routes: dict) -> None:
        route_names = {
            "text": ("text", self._text_fallbacks),
            "vision": ("vision", self._vision_fallbacks),
            "image": ("image_gen", self._image_gen_fallbacks),
        }
        for operation, (provider_key, fallbacks) in route_names.items():
            items = routes.get(operation)
            if not isinstance(items, list) or not items:
                raise ValueError(f"模型线路缺少 {operation}")
            providers = [
                self._build_route_provider(operation, item)
                for item in items
            ]
            self._providers[provider_key] = providers[0]
            fallbacks.extend(providers[1:])

    def _build_route_provider(
        self,
        operation: str,
        route: dict,
    ) -> ModelProvider:
        provider = str(route.get("provider") or "").strip().lower()
        api_key = str(route.get("api_key") or "")
        base_url = str(route.get("base_url") or "")
        model = str(route.get("model") or "")
        credential = str(route.get("credential") or "")
        if not api_key:
            raise ValueError(
                f"{operation} 线路凭据未配置: {credential or '<empty>'}"
            )
        if operation == "text" and provider == "deepseek":
            return DeepSeekProvider(
                api_key,
                base_url=base_url,
                model=model,
                fallback_model="",
            )
        if operation == "vision" and provider == "ollama":
            return OllamaVisionProvider(
                api_key,
                base_url=base_url,
                model=model,
            )
        if provider == "agnes":
            kwargs = {
                "base_url": base_url,
                "congestion_policy": self._agnes_congestion_policy,
            }
            if operation == "text":
                kwargs["text_model"] = model
            elif operation == "vision":
                kwargs["text_model"] = model
                kwargs["vision_model"] = model
            elif operation == "image":
                kwargs["image_model"] = model
            else:
                raise ValueError(f"不支持的模型操作: {operation}")
            return AgnesProvider(api_key, **kwargs)
        if operation == "image" and provider == "gpt":
            return GPTImageProvider(
                api_key,
                base_url=base_url,
                image_model=model,
            )
        raise ValueError(
            f"{operation} 不支持模型提供商: {provider or '<empty>'}"
        )

    def _configure_text(self, config: dict) -> None:
        provider = str(
            config.get("text_provider", "deepseek")
        ).strip().lower()
        if provider == "deepseek" and config.get("deepseek_key"):
            self._providers["text"] = DeepSeekProvider(
                config["deepseek_key"],
                base_url=config.get("deepseek_base_url"),
                model=config.get("deepseek_text_model"),
                fallback_model=config.get(
                    "deepseek_text_fallback_model"
                ),
            )
            return
        if provider == "agnes" and config.get("agnes_key"):
            self._providers["text"] = AgnesProvider(
                config["agnes_key"],
                base_url=(
                    config.get("agnes_text_base_url")
                    or config.get("agnes_base_url")
                ),
                text_model=config.get("agnes_text_model"),
                congestion_policy=self._agnes_congestion_policy,
            )
            return
        raise ValueError(f"未配置文本模型提供商: {provider}")

    def _configure_vision(self, config: dict) -> None:
        provider = str(
            config.get("vision_provider", "agnes")
        ).strip().lower()
        if provider == "agnes" and config.get("agnes_key"):
            vision_model = (
                config.get("agnes_vision_model")
                or config.get("agnes_text_model")
            )
            self._providers["vision"] = AgnesProvider(
                config["agnes_key"],
                base_url=(
                    config.get("agnes_vision_base_url")
                    or config.get("agnes_base_url")
                ),
                text_model=vision_model,
                vision_model=vision_model,
                congestion_policy=self._agnes_congestion_policy,
            )
            return
        raise ValueError(f"未配置图审模型提供商: {provider}")

    def _configure_image(self, config: dict) -> None:
        provider = str(
            config.get("image_gen_provider", "agnes") or "agnes"
        ).strip().lower()
        gpt_key = (
            config.get("gpt_image_key")
            or config.get("GPT_IMAGE_KEY")
        )
        agnes_key = config.get("agnes_key", "")
        agnes_base_url = config.get(
            "agnes_image_base_url",
            config.get("agnes_base_url", AgnesProvider.BASE_URL),
        )
        agnes_model = config.get(
            "agnes_image_model",
            AgnesProvider.IMAGE_MODEL,
        )
        agnes_fallback_model = config.get(
            "agnes_image_fallback_model",
            "",
        )
        gpt_base_url = config.get(
            "gpt_image_base_url",
            GPTImageProvider.BASE_URL,
        )
        gpt_model = config.get(
            "gpt_image_model",
            GPTImageProvider.IMAGE_MODEL,
        )
        def agnes(model: str) -> AgnesProvider:
            return AgnesProvider(
                agnes_key,
                image_model=model,
                base_url=agnes_base_url,
                congestion_policy=self._agnes_congestion_policy,
            )

        def gpt() -> GPTImageProvider:
            return GPTImageProvider(
                gpt_key,
                base_url=gpt_base_url,
                image_model=gpt_model,
            )

        if provider == "agnes" and agnes_key:
            self._providers["image_gen"] = agnes(agnes_model)
            if (
                agnes_fallback_model
                and agnes_fallback_model != agnes_model
            ):
                self._image_gen_fallbacks.append(
                    agnes(agnes_fallback_model)
                )
            if gpt_key:
                self._image_gen_fallbacks.append(gpt())
            return

        if provider == "gpt" and gpt_key:
            self._providers["image_gen"] = gpt()
            if agnes_key:
                self._image_gen_fallbacks.append(agnes(agnes_model))
                if (
                    agnes_fallback_model
                    and agnes_fallback_model != agnes_model
                ):
                    self._image_gen_fallbacks.append(
                        agnes(agnes_fallback_model)
                    )
            return

        raise ValueError(f"未配置生图模型提供商: {provider}")

    def _provider_name(self, operation: str) -> str:
        provider = self._providers.get(operation)
        if provider is None:
            return "unknown"
        name = provider.__class__.__name__.replace(
            "Provider",
            "",
        ).lower()
        return name or "unknown"

    def _circuit_key(self, operation: str) -> str:
        return f"{operation}:{self._provider_name(operation)}"

    def _is_circuit_open(self, operation: str) -> bool:
        key = self._circuit_key(operation)
        state = self._circuit.get(key) or {}
        opened_until = float(state.get("opened_until") or 0)
        now = time.time()
        if opened_until > now:
            return True
        if opened_until:
            state["opened_until"] = 0
            state["failures"] = 0
            self._circuit[key] = state
        return False

    def _record_circuit_result(
        self,
        operation: str,
        success: bool,
        terminal: bool = False,
    ) -> None:
        key = self._circuit_key(operation)
        state = self._circuit.setdefault(
            key,
            {"failures": 0, "opened_until": 0},
        )
        if success:
            state["failures"] = 0
            state["opened_until"] = 0
            return
        state["failures"] = int(state.get("failures") or 0) + 1
        if terminal or state["failures"] >= self._circuit_threshold:
            state["opened_until"] = (
                time.time() + self._circuit_cooldown_s
            )

    @staticmethod
    def _new_operation_metrics() -> dict:
        return {
            "calls": 0,
            "errors": 0,
            "latency_s": 0.0,
            "http_attempts": 0,
            "http_errors": 0,
            "http_retries": 0,
            "circuit_open": 0,
            "status": {},
            "error_types": {},
            "http_error_types": {},
        }

    def _record_logical_call(
        self,
        operation: str,
        elapsed: float,
        success: bool,
        *,
        circuit_open: bool = False,
        error_type: str | None = None,
    ) -> None:
        with self._metrics_lock:
            self._metrics["api_calls"] += 1
            self._metrics["latency_s"] += elapsed
            if not success:
                self._metrics["api_errors"] += 1
            if circuit_open:
                self._metrics["circuit_open"] += 1
            metrics = self._metrics["by_operation"].setdefault(
                operation,
                self._new_operation_metrics(),
            )
            metrics["calls"] += 1
            metrics["latency_s"] += elapsed
            if not success:
                metrics["errors"] += 1
            if circuit_open:
                metrics["circuit_open"] += 1
            if error_type:
                metrics["http_error_types"][error_type] = (
                    metrics["http_error_types"].get(error_type, 0) + 1
                )

    def _record_attempt(
        self,
        operation,
        provider,
        status_code=None,
        ok=False,
        retry=False,
        error=None,
        rate_wait_s=0.0,
    ) -> None:
        del provider, rate_wait_s
        status_key = (
            str(status_code)
            if status_code is not None
            else "exception"
        )
        with self._metrics_lock:
            self._metrics["http_attempts"] += 1
            if not ok:
                self._metrics["http_errors"] += 1
            if retry:
                self._metrics["http_retries"] += 1
            self._metrics["http_status"][status_key] = (
                self._metrics["http_status"].get(status_key, 0) + 1
            )
            metrics = self._metrics["by_operation"].setdefault(
                operation,
                self._new_operation_metrics(),
            )
            metrics["http_attempts"] += 1
            if not ok:
                metrics["http_errors"] += 1
            if retry:
                metrics["http_retries"] += 1
            metrics["status"][status_key] = (
                metrics["status"].get(status_key, 0) + 1
            )
            if error:
                error_type = (
                    error
                    if isinstance(error, str)
                    else type(error).__name__
                )
                metrics["error_types"][error_type] = (
                    metrics["error_types"].get(error_type, 0) + 1
                )

    def _record_provider_circuit_open(
        self,
        *,
        operation,
        provider,
    ) -> None:
        del provider
        with self._metrics_lock:
            self._metrics["circuit_open"] += 1
            metrics = self._metrics["by_operation"].setdefault(
                operation,
                self._new_operation_metrics(),
            )
            metrics["circuit_open"] += 1

    @staticmethod
    def _provider_route(provider: ModelProvider) -> str:
        name = provider.__class__.__name__.replace(
            "Provider",
            "",
        ).lower() or "unknown"
        model = (
            getattr(provider, "IMAGE_MODEL", None)
            or getattr(provider, "MODEL", None)
            or "unknown"
        )
        return f"{name}:{model}"

    def _record_fallback(
        self,
        provider: ModelProvider,
        *,
        success: bool,
    ) -> None:
        route = self._provider_route(provider)
        with self._metrics_lock:
            self._metrics["fallback_attempts"] += 1
            outcome = "fallback_successes" if success else "fallback_failures"
            self._metrics[outcome] += 1
            route_metrics = self._metrics["fallback_routes"].setdefault(
                route,
                {
                    "attempts": 0,
                    "successes": 0,
                    "failures": 0,
                },
            )
            route_metrics["attempts"] += 1
            result_key = "successes" if success else "failures"
            route_metrics[result_key] += 1

    def _call(
        self,
        operation: str,
        fn,
        *args,
        use_circuit: bool = True,
        **kwargs,
    ):
        started = time.perf_counter()
        if use_circuit and self._is_circuit_open(operation):
            self._record_logical_call(
                operation,
                0.0,
                False,
                circuit_open=True,
            )
            raise ProviderCircuitOpenError(
                "模型线路熔断冷却中，当前任务应稍后续跑",
                provider=self._provider_name(operation),
                operation=operation,
            )
        success = False
        terminal = False
        error_type = None
        try:
            result = fn(*args, **kwargs)
            success = result is not None and result != ""
            return result
        except ProviderError as exc:
            terminal = isinstance(
                exc,
                (ProviderAuthError, ProviderQuotaError),
            )
            error_type = type(exc).__name__
            raise
        finally:
            elapsed = time.perf_counter() - started
            self._record_logical_call(
                operation,
                elapsed,
                success,
                error_type=error_type,
            )
            if use_circuit:
                self._record_circuit_result(
                    operation,
                    success,
                    terminal=terminal,
                )

    def metrics_snapshot(self) -> dict:
        with self._metrics_lock:
            by_operation = {}
            for operation, values in (
                self._metrics["by_operation"].items()
            ):
                calls = values["calls"]
                by_operation[operation] = {
                    "calls": calls,
                    "errors": values["errors"],
                    "latency_s": round(values["latency_s"], 3),
                    "avg_latency_s": round(
                        values["latency_s"] / max(calls, 1),
                        3,
                    ),
                    "http_attempts": values.get(
                        "http_attempts",
                        0,
                    ),
                    "http_errors": values.get("http_errors", 0),
                    "http_retries": values.get("http_retries", 0),
                    "circuit_open": values.get("circuit_open", 0),
                    "status": dict(values.get("status") or {}),
                    "error_types": dict(
                        values.get("error_types") or {}
                    ),
                    "http_error_types": dict(
                        values.get("http_error_types") or {}
                    ),
                }
            calls = self._metrics["api_calls"]
            return {
                "api_calls": calls,
                "api_errors": self._metrics["api_errors"],
                "api_success_rate": (
                    round(
                        1 - self._metrics["api_errors"] / calls,
                        3,
                    )
                    if calls
                    else None
                ),
                "latency_s": round(self._metrics["latency_s"], 3),
                "http_attempts": self._metrics["http_attempts"],
                "http_errors": self._metrics["http_errors"],
                "http_retries": self._metrics["http_retries"],
                "http_status": dict(self._metrics["http_status"]),
                "circuit_open": self._metrics["circuit_open"],
                "fallback_attempts": self._metrics["fallback_attempts"],
                "fallback_successes": self._metrics["fallback_successes"],
                "fallback_failures": self._metrics["fallback_failures"],
                "fallback_routes": {
                    route: dict(values)
                    for route, values in self._metrics[
                        "fallback_routes"
                    ].items()
                },
                "by_operation": by_operation,
            }

    def call_text(
        self,
        prompt: str,
        max_tokens: int = 2048,
        **kwargs,
    ) -> Optional[str]:
        def call_with_fallbacks():
            return self._call_provider_chain(
                [self._providers["text"], *self._text_fallbacks],
                "call_text",
                prompt,
                max_tokens,
                **kwargs,
            )

        return self._call("text", call_with_fallbacks)

    def call_vision(
        self,
        image_url: str,
        **kwargs,
    ) -> Optional[bool]:
        def call_with_fallbacks():
            return self._call_provider_chain(
                [self._providers["vision"], *self._vision_fallbacks],
                "call_vision",
                image_url,
                **kwargs,
            )

        return self._call("vision", call_with_fallbacks)

    def assess_image(
        self,
        image_url: str,
        *,
        confirmation: bool = False,
        policy: str = "general",
        **kwargs,
    ) -> Optional[dict]:
        def call_with_fallbacks():
            return self._call_provider_chain(
                [self._providers["vision"], *self._vision_fallbacks],
                "assess_image",
                image_url,
                confirmation=confirmation,
                policy=policy,
                **kwargs,
            )

        return self._call("vision", call_with_fallbacks)

    def assess_images(
        self,
        image_urls: list[str],
        *,
        policy: str = "general",
        **kwargs,
    ) -> Optional[list[dict]]:
        def call_with_fallbacks():
            return self._call_provider_chain(
                [self._providers["vision"], *self._vision_fallbacks],
                "assess_images",
                image_urls,
                policy=policy,
                **kwargs,
            )

        return self._call("vision", call_with_fallbacks)

    def _call_provider_chain(
        self,
        providers: list[ModelProvider],
        method_name: str,
        *args,
        **kwargs,
    ):
        last_error: ProviderError | None = None
        for index, provider in enumerate(providers):
            is_fallback = index > 0
            try:
                result = getattr(provider, method_name)(*args, **kwargs)
            except ProviderError as exc:
                if is_fallback:
                    self._record_fallback(provider, success=False)
                last_error = exc
                continue
            if result is not None and result != "":
                if is_fallback:
                    self._record_fallback(provider, success=True)
                return result
            if is_fallback:
                self._record_fallback(provider, success=False)
        if last_error is not None:
            raise last_error
        return None

    def call_image_gen(
        self,
        image_url: str,
        size: str = "1024x1024",
        is_variant: bool = False,
        context: str = "",
        route_offset: int | None = None,
        image_route: str | None = None,
        **kwargs,
    ) -> Optional[str]:
        def call_with_fallbacks():
            last_error: ProviderError | None = None
            providers = [
                self._providers["image_gen"],
                *self._image_gen_fallbacks,
            ]
            if (
                self._fallback_image_gen is not None
                and self._fallback_image_gen not in providers
            ):
                providers.append(self._fallback_image_gen)
            requested_route = str(image_route or "").strip().lower()
            requested_route = {
                "translate": "gpt",
                "translation": "gpt",
                "remove": "agnes",
                "removal": "agnes",
            }.get(requested_route, requested_route)
            if requested_route not in {"", "gpt", "agnes"}:
                raise ValueError(f"不支持的生图线路: {requested_route}")
            indexed_providers = list(enumerate(providers))
            if requested_route == "gpt":
                indexed_providers = [
                    item for item in indexed_providers
                    if isinstance(item[1], GPTImageProvider)
                ]
            elif requested_route == "agnes":
                indexed_providers = [
                    item for item in indexed_providers
                    if isinstance(item[1], AgnesProvider)
                ]
            if route_offset is None:
                selected_providers = indexed_providers
            else:
                start_index = max(
                    0,
                    min(int(route_offset), len(indexed_providers)),
                )
                selected_providers = indexed_providers[
                    start_index:start_index + 1
                ]
            for index, provider in selected_providers:
                is_fallback = index > 0
                try:
                    result = provider.call_image_gen(
                        image_url,
                        size,
                        is_variant=is_variant,
                        context=context,
                        **kwargs,
                    )
                except ProviderError as exc:
                    if is_fallback:
                        self._record_fallback(
                            provider,
                            success=False,
                        )
                    last_error = exc
                    continue
                if result:
                    if is_fallback:
                        self._record_fallback(
                            provider,
                            success=True,
                        )
                    return result
                if is_fallback:
                    self._record_fallback(
                        provider,
                        success=False,
                    )
            if last_error is not None:
                raise last_error
            return None

        return self._call(
            "image_gen",
            call_with_fallbacks,
            use_circuit=route_offset is None,
        )
