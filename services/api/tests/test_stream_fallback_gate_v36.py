"""流式退路只在「换个说法有可能变成功」时走。

#53 端到端实测到的形状：供应商明说 429（限流）与超时不答时，旧逻辑照样把同一番话
换成整包再打一遍——请求翻倍、用户多等一整轮，答案一个字都不会变。
这一格守退路的准入条件，也守"有意不退要留痕"。
"""

import httpx
import pytest

from blank_api.services.agent.graph import _fallback_can_help
from blank_api.services.models.base import AdapterConfig, ChatMessage, ModelError, ToolSpecLite
from blank_api.services.models.openai_compat import OpenAICompatibleAdapter


@pytest.mark.parametrize(
    ("code", "status", "expected"),
    [
        # 该退：失败的原因是"流式这一条路"本身
        ("model_stream_empty", 0, True),
        ("model_unreachable", 0, True),
        ("model_http_error", 400, True),
        # 不该退：供应商拒的是"现在/这个身份能不能问"，与问法无关
        ("model_http_error", 429, False),
        ("model_http_error", 500, False),
        ("model_http_error", 401, False),
        ("model_http_error", 404, False),
        ("model_timeout", 0, False),
        # 读不出状态码的 model_http_error 一律不退：宁可立刻报原因，不要多打一次
        ("model_http_error", 0, False),
        ("model_tools_unsupported", 0, False),
    ],
)
def test_the_fallback_gate(code: str, status: int, expected: bool) -> None:
    error = ModelError(code, f"probe {code}", status=status)
    assert _fallback_can_help(error) is expected, f"{code}/{status} 判成了 {not expected}"


def test_status_defaults_to_unknown_not_zero_meaning_400() -> None:
    """状态码缺省是"没走到有状态码那一步"，不能与 400 混为一谈。"""
    assert ModelError("model_http_error", "no status").status == 0
    assert _fallback_can_help(ModelError("model_http_error", "no status")) is False


# -------------------------------------------------- 状态码要从真响应骑到错误上
#
# 上面那组只证明「给定 status，闸门怎么判」。这一组证明另一半：适配器从 HTTP 响应
# 里读出的状态码，确实带在 ModelError 上——否则 400 到了闸门那里是 0（未知），
# 按规则不退，用户看到的是一次多余的失败。


def _gateway(status: int, retry_after: str | None = None) -> OpenAICompatibleAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"Retry-After": retry_after} if retry_after is not None else {}
        return httpx.Response(status, json={"error": {"message": f"网关 {status}"}}, headers=headers)

    config = AdapterConfig(
        name="t",
        kind="openai_compatible",
        base_url="https://example.test/v1",
        api_key="k",
        model="m",
        supports_tools=True,
        client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw),
    )
    return OpenAICompatibleAdapter(config)


async def _error_from_stream(status: int, retry_after: str | None = None) -> ModelError:
    try:
        async for _ in _gateway(status, retry_after).stream([ChatMessage(role="user", content="hi")]):
            pass
    except ModelError as error:
        return error
    raise AssertionError(f"状态 {status} 的流式响应没抛 ModelError")


async def _error_from_tools_stream(status: int, retry_after: str | None = None) -> ModelError:
    """Agent 真正走的那一条：带工具的流式。退路闸门读的就是这里抛出的错误。"""
    try:
        await _gateway(status, retry_after).bind_tools_stream(
            [ToolSpecLite(name="read_file", description="d", input_schema={})],
            [ChatMessage(role="user", content="hi")],
        )
    except ModelError as error:
        return error
    raise AssertionError(f"状态 {status} 的带工具流式没抛 ModelError")


async def _error_from_invoke(status: int, retry_after: str | None = None) -> ModelError:
    try:
        await _gateway(status, retry_after).invoke([ChatMessage(role="user", content="hi")])
    except ModelError as error:
        return error
    raise AssertionError(f"状态 {status} 的整包响应没抛 ModelError")


@pytest.mark.parametrize(
    ("status", "can_help"),
    [(400, True), (429, False), (503, False)],
)
@pytest.mark.parametrize(
    "lane",
    [
        pytest.param("/stream", id="stream"),
        pytest.param("/tools_stream", id="tools_stream"),
        pytest.param("/invoke", id="invoke"),
    ],
)
async def test_the_response_status_reaches_the_gate(status: int, can_help: bool, lane: str) -> None:
    lanes = {
        "/stream": _error_from_stream,
        "/tools_stream": _error_from_tools_stream,
        "/invoke": _error_from_invoke,
    }
    error = await lanes[lane](status)
    assert error.code == "model_http_error", f"{lane} {status} 报成了 {error.code}"
    assert error.status == status, f"{lane} 状态码没骑上错误：读到 {error.status}"
    assert _fallback_can_help(error) is can_help


# -------------------------------------------------- Retry-After 也要骑到错误上
#
# #57 起，429 的等待要听供应商的 `Retry-After`。适配器有三条抛错的路
# （普通流、带工具流、整包），每一条都得把表头读出来；漏一条，那条路上
# 的 429 就退回盲猜的 0.5s——事件里看起来有退避，实际没听供应商的。


@pytest.mark.parametrize(
    "lane",
    [
        pytest.param("/stream", id="stream"),
        pytest.param("/tools_stream", id="tools_stream"),
        pytest.param("/invoke", id="invoke"),
    ],
)
async def test_retry_after_header_rides_on_the_error(lane: str) -> None:
    lanes = {
        "/stream": _error_from_stream,
        "/tools_stream": _error_from_tools_stream,
        "/invoke": _error_from_invoke,
    }
    error = await lanes[lane](429, retry_after="3")
    assert error.status == 429
    assert error.retry_after == 3.0, f"{lane} 没把 Retry-After 带出来：读到 {error.retry_after}"


async def test_no_retry_after_header_reads_as_zero_not_a_guess() -> None:
    error = await _error_from_invoke(429)
    assert error.retry_after == 0.0, "没给表头就该是「没说」，由退避自己算"
