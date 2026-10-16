"""Model routing primitives with conservative fallback semantics."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

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

    async def invoke(self, messages: Sequence[ChatMessage], **kwargs) -> ModelResponse:
        last_error: ModelError | None = None
        for adapter in self._candidates():
            try:
                response = await adapter.invoke(messages, **kwargs)
                self.last_provider = adapter.config.name
                return response
            except ModelError as exc:
                last_error = exc
                if not exc.retryable:
                    raise
        assert last_error is not None
        raise ModelError(
            "all_models_failed",
            "所有候选模型均调用失败：" + last_error.message,
            retryable=False,
        ) from last_error

    async def bind_tools(
        self, tools: Sequence[ToolSpecLite], messages: Sequence[ChatMessage], **kwargs
    ) -> ModelResponse:
        last_error: ModelError | None = None
        for adapter in self._candidates():
            if not adapter.config.supports_tools:
                last_error = ModelError("model_tools_unsupported", f"模型 {adapter.config.model} 不支持工具调用")
                continue
            try:
                response = await adapter.bind_tools(tools, messages, **kwargs)
                self.last_provider = adapter.config.name
                return response
            except ModelError as exc:
                last_error = exc
                if not exc.retryable:
                    raise
        assert last_error is not None
        raise ModelError("all_models_failed", "所有支持工具的候选模型均调用失败：" + last_error.message, retryable=False) from last_error

    async def stream(self, messages: Sequence[ChatMessage], **kwargs) -> AsyncIterator[str]:
        """Streaming fallback is selected before iteration begins."""
        for adapter in self._candidates():
            try:
                async for chunk in adapter.stream(messages, **kwargs):
                    self.last_provider = adapter.config.name
                    yield chunk
                return
            except ModelError as exc:
                if not exc.retryable:
                    raise
        raise ModelError("all_models_failed", "所有候选模型均无法建立流式连接", retryable=False)
