"""流式模型调用半路失败必须翻译成 ModelError，而不是把任务端点打成 500。

起因是一次真实模型评测：DeepSeek 在 `bind_tools_stream` 的 `aiter_lines()` 里读超时，
异常一路穿出 `/tasks/{id}/run`，报告里只剩一句 `internal_error`。脚本化模型永远秒回，
这个缺口只有接上真实供应商才会露头——所以这里用会中途抛错的假客户端把它钉住。
"""

import httpx
import pytest

from blank_api.services.models.base import AdapterConfig, ChatMessage, ModelError, ToolSpecLite
from blank_api.services.models.openai_compat import OpenAICompatibleAdapter

MESSAGES = [ChatMessage(role="user", content="读一下 calc.py")]
TOOLS = [ToolSpecLite(name="read_file", description="读文件", input_schema={"type": "object"})]


class _MidStreamFailure(httpx.AsyncBaseTransport):
    """请求本身能发出去，流到一半才失败——真实慢模型就是这个形状。"""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        async def lines():
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            raise self.exc

        return httpx.Response(200, stream=_ChunkStream(lines()))


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, iterator) -> None:
        self.iterator = iterator

    async def __aiter__(self):
        async for chunk in self.iterator:
            yield chunk


def _adapter(exc: Exception) -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        AdapterConfig(
            name="慢模型",
            kind="openai_compatible",
            base_url="https://example.test/v1",
            api_key="k",
            model="deepseek-chat",
            supports_tools=True,
            client_factory=lambda **kw: httpx.AsyncClient(transport=_MidStreamFailure(exc)),
        )
    )


async def _drain(generator) -> None:
    async for _ in generator:
        pass


@pytest.mark.parametrize(
    "exc,code",
    [
        (httpx.ReadTimeout("slow"), "model_timeout"),
        (httpx.ReadError("reset"), "model_unreachable"),
    ],
)
async def test_stream_failure_becomes_retryable_model_error(exc, code):
    with pytest.raises(ModelError) as caught:
        await _drain(_adapter(exc).stream(MESSAGES))
    assert caught.value.code == code
    assert caught.value.retryable is True


@pytest.mark.parametrize(
    "exc,code",
    [
        (httpx.ReadTimeout("slow"), "model_timeout"),
        (httpx.ReadError("reset"), "model_unreachable"),
    ],
)
async def test_bind_tools_stream_failure_becomes_retryable_model_error(exc, code):
    adapter = _adapter(exc)
    with pytest.raises(ModelError) as caught:
        await adapter.bind_tools_stream(TOOLS, MESSAGES)
    assert caught.value.code == code
    assert caught.value.retryable is True


async def test_non_streaming_path_keeps_the_same_vocabulary():
    """非流式早就翻译过了；改动客户端出口不能把它换掉说法。"""
    adapter = _adapter(httpx.ConnectTimeout("no route"))
    with pytest.raises(ModelError) as caught:
        await adapter.invoke(MESSAGES, TOOLS)
    assert caught.value.code == "model_timeout"
