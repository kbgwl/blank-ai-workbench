"""流在工具调用参数中途被截断：不许派发，也不许写回上下文。

真机 23f93669（2026-11-17，运维控制台复跑）：
  - 生成 app.js 的那次流在 5m49s 才出第一个字，随后在 `write_file` 参数的
    第 31009 个字符处被截断（JSON 停在 `...getContext('experiment`）；
  - 适配器把半截参数当完整响应返回，工具层解析成空对象后报「缺少必填参数：path」；
  - 那条残缺的 assistant 消息被写回上下文，下一次整包请求被供应商以
    400 `invalid_prompt` 拒绝（同一对话直探复现：带与不带工具回执都是 400）。

判据：参数不是完整 JSON 就抛 `model_stream_truncated`（可重试的流式失败），
既不派发工具，也不退回整包——整包只会把同一份坏上下文再发给网关一次。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from blank_api.services.agent import graph
from blank_api.services.models.base import (
    AdapterConfig,
    ChatMessage,
    ModelError,
    ToolSpecLite,
)
from blank_api.services.models.openai_compat import OpenAICompatibleAdapter
from blank_api.services.models.openai_responses import OpenAIResponsesAdapter
from blank_api.services.models.streaming import invalid_tool_arguments

SPECS = [ToolSpecLite(name="write_file", description="写文件", input_schema={"type": "object"})]
PARTIAL = '{"content":"\'use strict\';\n(function () { var ctx = canvas.getContext(\'experiment'
VALID = '{"path":"app.js","content":"void 0;"}'


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def add(self, type_: str, node: str, payload: dict | None = None) -> None:
        self.events.append((type_, node, dict(payload or {})))


def _sse(*events: dict) -> bytes:
    return "".join(f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events).encode()


def _responses_adapter(body: bytes) -> OpenAIResponsesAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    return OpenAIResponsesAdapter(
        AdapterConfig(
            name="spark",
            kind="openai_responses",
            base_url="https://example.test/v1",
            api_key="k",
            model="spark-x2.6",
            supports_tools=True,
            client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw),
        )
    )


def _compat_adapter(body: bytes) -> OpenAICompatibleAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    return OpenAICompatibleAdapter(
        AdapterConfig(
            name="ds",
            kind="openai_compatible",
            base_url="https://example.test/v1",
            api_key="k",
            model="deepseek-chat",
            supports_tools=True,
            client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw),
        )
    )


RESPONSES_TRUNCATED = _sse(
    {"type": "response.created"},
    {
        "type": "response.output_item.added",
        "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "write_file"},
    },
    {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": PARTIAL},
)

RESPONSES_COMPLETE = _sse(
    {
        "type": "response.output_item.added",
        "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "write_file"},
    },
    {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": VALID},
)

COMPAT_TRUNCATED = _sse(
    {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "function": {"name": "write_file", "arguments": PARTIAL},
                        }
                    ]
                },
                "finish_reason": None,
            }
        ]
    }
)


async def _error(adapter) -> ModelError:
    try:
        await adapter.bind_tools_stream(SPECS, [ChatMessage(role="user", content="hi")])
    except ModelError as error:
        return error
    raise AssertionError("截断的流没抛 ModelError")


def test_responses_truncated_arguments_are_rejected() -> None:
    error = asyncio.run(_error(_responses_adapter(RESPONSES_TRUNCATED)))
    assert error.code == "model_stream_truncated", error.code
    assert error.retryable is True, "截断是可重试的流式失败"
    assert "write_file" in error.message, error.message
    assert str(len(PARTIAL)) in error.message, error.message
    assert graph._fallback_can_help(error) is False, "不许把同一份坏上下文退回整包"


def test_responses_complete_arguments_still_pass() -> None:
    adapter = _responses_adapter(RESPONSES_COMPLETE)
    response = asyncio.run(
        adapter.bind_tools_stream(SPECS, [ChatMessage(role="user", content="hi")])
    )
    assert len(response.tool_calls) == 1, response.tool_calls
    arguments = response.tool_calls[0]["function"]["arguments"]
    assert json.loads(arguments)["path"] == "app.js", arguments


def test_compat_truncated_arguments_are_rejected() -> None:
    error = asyncio.run(_error(_compat_adapter(COMPAT_TRUNCATED)))
    assert error.code == "model_stream_truncated", error.code
    assert "write_file" in error.message, error.message


def test_helper_flags_only_incomplete_json() -> None:
    assert invalid_tool_arguments([{"function": {"name": "a", "arguments": PARTIAL}}]) == [
        ("a", len(PARTIAL))
    ]
    assert invalid_tool_arguments([{"function": {"name": "a", "arguments": VALID}}]) == []
    assert invalid_tool_arguments([{"function": {"name": "a", "arguments": "{}"}}]) == []


def test_truncated_stream_never_becomes_a_fallback_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """事件里只能看到重试，不能看到 model.stream_fallback（退整包）。"""
    monkeypatch.setattr(graph, "TRANSIENT_RETRIES", 0)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=RESPONSES_TRUNCATED, headers={"content-type": "text/event-stream"})

    adapter = OpenAIResponsesAdapter(
        AdapterConfig(
            name="spark",
            kind="openai_responses",
            base_url="https://example.test/v1",
            api_key="k",
            model="spark-x2.6",
            supports_tools=True,
            client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw),
        )
    )
    hunter = object.__new__(graph.AgentSession)
    hunter.stream_output = True
    hunter.hooks = None
    hunter.context = SimpleNamespace(task_id="t")
    hunter.recorder = _Recorder()
    hunter.effects = 0
    hunter.provider_name = "spark"

    with pytest.raises(ModelError) as info:
        asyncio.run(
            graph.AgentSession._call_adapter(
                hunter,
                adapter,
                SPECS,
                [ChatMessage(role="user", content="hi")],
                "execute_action",
                "coder",
            )
        )

    assert info.value.code == "model_stream_truncated"
    assert len(seen) == 1, f"只应发一次流式请求，实际 {len(seen)} 次"
    types = [event[0] for event in hunter.recorder.events]
    assert "model.stream_fallback" not in types, types
