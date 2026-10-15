from collections.abc import Callable

import httpx

from blank_api.api.errors import ApiError
from blank_api.models.model_provider import ModelProvider
from blank_api.services.crypto import decrypt_secret
from blank_api.services.models.anthropic import AnthropicAdapter
from blank_api.services.models.base import AdapterConfig, ModelAdapter
from blank_api.services.models.ollama import OllamaAdapter
from blank_api.services.models.openai_compat import OpenAICompatibleAdapter

KINDS: dict[str, type[ModelAdapter]] = {
    "openai_compatible": OpenAICompatibleAdapter,
    "anthropic": AnthropicAdapter,
    "ollama": OllamaAdapter,
}


def build_adapter(
    provider: ModelProvider,
    client_factory: Callable[..., httpx.AsyncClient] | None = None,
) -> ModelAdapter:
    adapter_cls = KINDS.get(provider.kind)
    if adapter_cls is None:
        raise ApiError(
            code="provider_kind_unsupported",
            message=f"不支持的供应商类型：{provider.kind}",
            status_code=422,
        )
    return adapter_cls(
        AdapterConfig(
            name=provider.name,
            kind=provider.kind,
            base_url=provider.base_url,
            api_key=decrypt_secret(provider.api_key_encrypted) if provider.api_key_encrypted else "",
            model=provider.default_model,
            temperature=provider.temperature,
            max_tokens=provider.max_tokens,
            supports_tools=provider.supports_tools,
            supports_vision=provider.supports_vision,
            client_factory=client_factory or httpx.AsyncClient,
        )
    )
