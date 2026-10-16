import asyncio
import pytest

from blank_api.services.models.base import AdapterConfig, ChatMessage, ModelAdapter, ModelError, ModelResponse
from blank_api.services.models.routing import FallbackAdapter


class FakeAdapter(ModelAdapter):
    def __init__(self, name: str, error: ModelError | None = None):
        super().__init__(AdapterConfig(name=name, kind="fake", base_url="", api_key="", model=name))
        self.error = error

    async def invoke(self, messages, **kwargs):
        if self.error:
            raise self.error
        return ModelResponse(text=self.config.name, model=self.config.model)

    async def stream(self, messages, **kwargs):
        if self.error:
            raise self.error
        yield self.config.name


def test_retryable_model_failure_uses_fallback():
    adapter = FallbackAdapter([FakeAdapter("primary", ModelError("timeout", "down", retryable=True)), FakeAdapter("backup")])
    result = asyncio.run(adapter.invoke([ChatMessage(role="user", content="hi")]))
    assert result.text == "backup"
    assert adapter.last_provider == "backup"


def test_non_retryable_model_failure_is_not_hidden():
    adapter = FallbackAdapter([FakeAdapter("primary", ModelError("bad_key", "invalid", retryable=False)), FakeAdapter("backup")])
    with pytest.raises(ModelError, match="invalid"):
        asyncio.run(adapter.invoke([ChatMessage(role="user", content="hi")]))
