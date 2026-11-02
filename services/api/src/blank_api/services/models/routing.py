"""Model routing primitives with conservative fallback semantics."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from blank_api.helpers.middleware import breaker_for
from blank_api.services import metrics
from blank_api.services.models.base import (
    ChatMessage,
    ModelAdapter,
    ModelError,
    ModelResponse,
    ToolSpecLite,
)


class FallbackAdapter(ModelAdapter):
    """Try adapters in order, falling back only for retryable model errors."""

    def __init__(self, adapters: Sequence[ModelAdapter]) -> None:
        if not adapters:
            raise ValueError("至少需要一个模型适配器")
        super().__init__(adapters[0].config)
        self.adapters = tuple(adapters)
        self.last_provider = adapters[0].config.name

    def _candidates(self):
        yield from self.adapters

    @staticmethod
    def _guard(adapter: ModelAdapter) -> None:
        breaker = breaker_for(adapter.config.name)
        if not breaker.allow():
            raise ModelError(
                "circuit_breaker_open",
                f"供应商 {adapter.config.name} 暂时熔断，请稍后重试",
                retryable=True,
            )

    async def invoke(self, messages: Sequence[ChatMessage], **kwargs) -> ModelResponse:
        last_error: ModelError | None = None
        errors: list[str] = []
        for adapter in self._candidates():
            if adapter is not self.adapters[0]:
                # 走到这里说明主用已经失败过一次：换成备用模型这件事必须能被数出来，
                # 否则"我们配了兜底"只是一句配置层面的话，跑起来没人知道它生没生效。
                metrics.inc(metrics.MODEL_FALLBACK, **{"to": adapter.config.name})
            self._guard(adapter)
            try:
                response = await adapter.invoke(messages, **kwargs)
                breaker_for(adapter.config.name).succeed()
                self.last_provider = adapter.config.name
                return response
            except ModelError as exc:
                breaker_for(adapter.config.name).fail()
                metrics.inc(metrics.MODEL_FALLBACK_FAILED, **{"provider": adapter.config.name})
                # 每一家的失败原因都留痕：只保留最后一家，就说不清主用当初为什么挂。
                errors.append(f"{adapter.config.name}（{exc.code}）：{exc.message}")
                last_error = exc
                if not exc.retryable:
                    raise
        assert last_error is not None
        raise ModelError(
            "all_models_failed",
            "所有候选模型均调用失败：" + "；".join(errors),
            retryable=False,
        ) from last_error

    async def bind_tools(
        self, tools: Sequence[ToolSpecLite], messages: Sequence[ChatMessage], **kwargs
    ) -> ModelResponse:
        last_error: ModelError | None = None
        errors: list[str] = []
        for adapter in self._candidates():
            if not adapter.config.supports_tools:
                errors.append(f"{adapter.config.name}（model_tools_unsupported）：模型 {adapter.config.model} 不支持工具调用")
                last_error = ModelError("model_tools_unsupported", f"模型 {adapter.config.model} 不支持工具调用")
                continue
            if adapter is not self.adapters[0]:
                metrics.inc(metrics.MODEL_FALLBACK, **{"to": adapter.config.name})
            self._guard(adapter)
            try:
                response = await adapter.bind_tools(tools, messages, **kwargs)
                breaker_for(adapter.config.name).succeed()
                self.last_provider = adapter.config.name
                return response
            except ModelError as exc:
                breaker_for(adapter.config.name).fail()
                metrics.inc(metrics.MODEL_FALLBACK_FAILED, **{"provider": adapter.config.name})
                errors.append(f"{adapter.config.name}（{exc.code}）：{exc.message}")
                last_error = exc
                if not exc.retryable:
                    raise
        assert last_error is not None
        raise ModelError("all_models_failed", "所有支持工具的候选模型均调用失败：" + "；".join(errors), retryable=False) from last_error

    async def stream(self, messages: Sequence[ChatMessage], **kwargs) -> AsyncIterator[str]:
        """Streaming fallback is selected before iteration begins."""
        for adapter in self._candidates():
            self._guard(adapter)
            try:
                async for chunk in adapter.stream(messages, **kwargs):
                    self.last_provider = adapter.config.name
                    yield chunk
                breaker_for(adapter.config.name).succeed()
                return
            except ModelError as exc:
                breaker_for(adapter.config.name).fail()
                if not exc.retryable:
                    raise
        raise ModelError("all_models_failed", "所有候选模型均无法建立流式连接", retryable=False)