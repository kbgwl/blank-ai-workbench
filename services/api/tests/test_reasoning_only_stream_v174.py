"""「空流」要分清是网关不认流，还是模型只想不说。

真机 01f1fa83（2026-11-17）的链：工具回执之后，流式请求 8 分钟零正文 →
被记成 model_stream_empty → 退路整包调用 30 分钟零字节超时 → 重试两次，共 90 分钟。

直接用同一个供应商复现（E:/tmp/probe-spark-stream.py，同一份对话、带工具回执）：
  response.reasoning_summary_text.delta × 272，首条 6.0s、末条 57.7s，
  第一条正文 58.0s，response.completed 66.1s。
也就是说流是通的，正文前面全是思考；整包调用则在整个思考期间一个字节都不发。

规则：收到过思考增量再判空，不许退整包（同一段思考只会再跑一遍），
报错里要带思考增量数与字数，事件里也要留。
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
from blank_api.services.models.openai_responses import OpenAIResponsesAdapter

SPECS = [ToolSpecLite(name="list_files", description="列目录", input_schema={"type": "object"})]


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def add(self, type_: str, node: str, payload: dict | None = None) -> None:
        self.events.append((type_, node, dict(payload or {})))


def _adapter(body: bytes) -> tuple[OpenAIResponsesAdapter, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    config = AdapterConfig(
        name="spark",
        kind="openai_responses",
        base_url="https://example.test/v1",
        api_key="k",
        model="spark-x2.6",
        supports_tools=True,
        client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw),
    )
    return OpenAIResponsesAdapter(config), seen


def _sse(*events: dict) -> bytes:
    return "".join(f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events).encode()


REASONING_ONLY = _sse(
    {"type": "response.created"},
    {"type": "response.reasoning_summary_text.delta", "delta": "先想第一步"},
    {"type": "response.reasoning_summary_text.delta", "delta": "再想第二步"},
    {"type": "response.reasoning_summary_text.delta", "delta": "还没想完"},
)

TRULY_EMPTY = b""

REASONING_THEN_TEXT = _sse(
    {"type": "response.reasoning_summary_text.delta", "delta": "想一下"},
    {"type": "response.output_text.delta", "delta": "好"},
    {
        "type": "response.completed",
        "response": {"id": "r1", "model": "spark-x2.6", "status": "completed", "output": []},
    },
)


async def _call(adapter: OpenAIResponsesAdapter) -> ModelError:
    try:
        await adapter.bind_tools_stream(SPECS, [ChatMessage(role="user", content="hi")])
    except ModelError as error:
        return error
    raise AssertionError("流式调用没抛 ModelError")


def test_reasoning_only_stream_reports_counts_and_is_not_empty() -> None:
    adapter, _ = _adapter(REASONING_ONLY)
    error = asyncio.run(_call(adapter))
    assert error.code == "model_stream_empty"
    assert error.reasoning_events == 3, error.reasoning_events
    assert error.reasoning_chars == len("先想第一步再想第二步还没想完"), error.reasoning_chars
    # 报错要能读出"只想不说"，不能还写「流式响应为空」。
    assert "思考增量" in error.message, error.message
    assert "3 个" in error.message, error.message


def test_reasoning_only_stream_is_not_a_gateway_stream_problem() -> None:
    adapter, _ = _adapter(REASONING_ONLY)
    error = asyncio.run(_call(adapter))
    assert graph._fallback_can_help(error) is False
    # 反向：真的一行都没有时，仍然允许退整包（网关不认流的形态）。
    empty_adapter, _ = _adapter(TRULY_EMPTY)
    empty = asyncio.run(_call(empty_adapter))
    assert empty.reasoning_events == 0
    assert graph._fallback_can_help(empty) is True


def test_reasoning_then_text_is_a_normal_response() -> None:
    adapter, _ = _adapter(REASONING_THEN_TEXT)
    response = asyncio.run(
        adapter.bind_tools_stream(SPECS, [ChatMessage(role="user", content="hi")])
    )
    assert response.text == "好", response.text


def test_no_non_stream_retry_when_the_stream_was_thinking() -> None:
    """事件里不许出现 model.stream_fallback，只能出现 skipped；整包请求一次都不发。"""
    adapter, seen = _adapter(REASONING_ONLY)
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

    assert info.value.code == "model_stream_empty"
    assert len(seen) == 1, f"退路不该再发请求，实际发了 {len(seen)} 次"
    types = [event[0] for event in hunter.recorder.events]
    assert "model.stream_fallback" not in types, types
    assert "model.stream_fallback_skipped" in types, types
    skipped = [e for e in hunter.recorder.events if e[0] == "model.stream_fallback_skipped"][0]
    assert skipped[2]["reasoning_events"] == 3, skipped[2]
    assert skipped[2]["reasoning_chars"] > 0, skipped[2]
