from collections.abc import Callable

import httpx

from blank_api.api.errors import ApiError
from blank_api.models.model_provider import ModelProvider
from blank_api.services.crypto import decrypt_secret
from blank_api.services.features import is_enabled
from blank_api.services.models.anthropic import AnthropicAdapter
from blank_api.services.models.base import AdapterConfig, ModelAdapter
from blank_api.services.models.ollama import OllamaAdapter
from blank_api.services.models.openai_compat import OpenAICompatibleAdapter
from blank_api.services.models.openai_responses import OpenAIResponsesAdapter

KINDS: dict[str, type[ModelAdapter]] = {
    "openai_compatible": OpenAICompatibleAdapter,
    # Responses API：与 chat/completions 是两套消息模型，见适配器文件头
    "openai_responses": OpenAIResponsesAdapter,
    "anthropic": AnthropicAdapter,
    "ollama": OllamaAdapter,
}


def build_adapter(
    provider: ModelProvider,
    client_factory: Callable[..., httpx.AsyncClient] | None = None,
    prompt_cache: bool | None = None,
) -> ModelAdapter:
    """``prompt_cache=None`` 表示按特性开关走；显式传布尔可以强制打开/关闭。

    测试与"测试连接"按钮需要确定行为，不能让一个全局设置悄悄改掉它们的断言，
    所以这里保留显式覆盖的口子。
    """
    adapter_cls = KINDS.get(provider.kind)
    if adapter_cls is None:
        raise ApiError(
            code="provider_kind_unsupported",
            message=f"不支持的供应商类型：{provider.kind}",
            status_code=422,
        )
    cache_enabled = is_enabled("prompt_cache") if prompt_cache is None else prompt_cache
    return adapter_cls(
        AdapterConfig(
            name=provider.name,
            kind=provider.kind,
            base_url=provider.base_url,
            api_key=decrypt_secret(provider.api_key_encrypted) if provider.api_key_encrypted else "",
            model=provider.default_model,
            temperature=provider.temperature,
            max_tokens=provider.max_tokens,
            context_window=int(getattr(provider, "context_window", 0) or 0),
            supports_tools=provider.supports_tools,
            supports_vision=provider.supports_vision,
            reasoning_effort=getattr(provider, "reasoning_effort", "") or "",
            verbosity=getattr(provider, "verbosity", "") or "",
            prompt_cache=cache_enabled,
            client_factory=client_factory or httpx.AsyncClient,
        )
    )
