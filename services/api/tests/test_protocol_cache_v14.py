"""协议与缓存：Responses API、Anthropic 缓存断点、缓存/推理 token 计量。

这一组测试盯的是"钱"和"前缀"：同一个请求多带几个字段，输入单价能差一个数量级；
前缀排错一个位置，缓存就等于没开。所以断言都是对着真实请求体与真实用量字段。
"""

import json

import httpx
import pytest

from blank_api.services.agent.graph import _insert_handoff, accumulate_usage, merge_usage
from blank_api.services.models.anthropic import AnthropicAdapter
from blank_api.services.models.base import AdapterConfig, ChatMessage, ToolSpecLite, Usage
from blank_api.services.models.openai_compat import OpenAICompatibleAdapter
from blank_api.services.models.openai_responses import OpenAIResponsesAdapter
from blank_api.services.models.pricing import estimate_cost

TOOLS = [
    ToolSpecLite("read_file", "读文件", {"type": "object", "properties": {"path": {"type": "string"}}}),
    ToolSpecLite("run_shell", "执行命令", {"type": "object", "properties": {"cmd": {"type": "string"}}}),
]


def _config(kind: str, **overrides) -> AdapterConfig:
    base = dict(
        name="t",
        kind=kind,
        base_url="https://example.test/v1",
        api_key="k",
        model="m",
        max_tokens=1024,
        supports_tools=True,
    )
    base.update(overrides)
    return AdapterConfig(**base)


def _client(handler):
    return lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)


# ---- Anthropic：缓存断点 ---------------------------------------------------


def test_anthropic_marks_cache_breakpoints() -> None:
    adapter = AnthropicAdapter(_config("anthropic"))
    payload = adapter._payload(
        [ChatMessage("system", "系统提示"), ChatMessage("user", "任务")], TOOLS, False
    )
    system_blocks = payload["system"]
    assert system_blocks[0]["cache_control"] == {"type": "ephemeral"}
    # 工具表是稳定前缀，最后一个也要打断点
    assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    # 对话尾部同样打断点：下一次调用能命中上一次的全部前缀
    assert payload["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_anthropic_cache_can_be_disabled() -> None:
    adapter = AnthropicAdapter(_config("anthropic", prompt_cache=False))
    payload = adapter._payload([ChatMessage("system", "系统提示"), ChatMessage("user", "任务")], TOOLS, False)
    assert "cache_control" not in payload["system"][0]
    assert "cache_control" not in payload["tools"][-1]


@pytest.mark.asyncio
async def test_anthropic_usage_includes_cache_tokens() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "好"}],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 900,
                    "cache_creation_input_tokens": 50,
                },
                "model": "claude",
            },
        )

    adapter = AnthropicAdapter(_config("anthropic", client_factory=_client(handler)))
    response = await adapter.invoke([ChatMessage("system", "s"), ChatMessage("user", "u")], tools=TOOLS)
    assert response.usage.cached_tokens == 900
    assert response.usage.cache_write_tokens == 50
    # input_tokens 不含缓存部分，总量要把三块加起来
    assert response.usage.prompt_tokens == 1050
    assert response.usage.cache_hit_rate == pytest.approx(0.857, abs=1e-3)


# ---- OpenAI 兼容：细分用量 --------------------------------------------------


@pytest.mark.asyncio
async def test_openai_compat_reads_cached_and_reasoning_tokens() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "好"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 200,
                    "prompt_tokens_details": {"cached_tokens": 800},
                    "completion_tokens_details": {"reasoning_tokens": 120},
                },
                "model": "gpt",
            },
        )

    adapter = OpenAICompatibleAdapter(_config("openai_compatible", client_factory=_client(handler)))
    response = await adapter.invoke([ChatMessage("user", "u")])
    assert response.usage.cached_tokens == 800
    assert response.usage.reasoning_tokens == 120
    # prompt_tokens 本来就含缓存部分，不重复相加
    assert response.usage.prompt_tokens == 1000


# ---- Responses API：请求形状 ------------------------------------------------


def test_responses_payload_shape() -> None:
    adapter = OpenAIResponsesAdapter(
        _config("openai_responses", model="gpt-5", reasoning_effort="medium", verbosity="low")
    )
    messages = [
        ChatMessage("system", "系统提示"),
        ChatMessage("system", "交接笔记"),
        ChatMessage("user", "读 a.txt"),
        ChatMessage(
            "assistant",
            "",
            tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\":\"a.txt\"}"}}],
        ),
        ChatMessage("tool", "文件内容", tool_call_id="call_1"),
    ]
    payload = adapter._payload(messages, TOOLS, False)

    # 第一条 system 进 instructions，后续 system 降级为 developer 条目
    assert payload["instructions"] == "系统提示"
    assert payload["input"][0]["role"] == "developer"
    assert payload["input"][0]["content"][0]["text"] == "交接笔记"
    # 工具调用与结果都是一等条目，靠 call_id 配对
    kinds = [item["type"] for item in payload["input"]]
    assert kinds == ["message", "message", "function_call", "function_call_output"]
    assert payload["input"][2]["call_id"] == payload["input"][3]["call_id"] == "call_1"
    # 工具 schema 是扁平定义，不套 function
    assert payload["tools"][0]["type"] == "function" and "function" not in payload["tools"][0]
    # 隐私与成本开关
    assert payload["store"] is False
    assert payload["prompt_cache_key"]
    # 推理模型的 temperature 会被拒，所以只在非推理档下发
    assert "temperature" not in payload
    assert payload["reasoning"] == {"effort": "medium"}
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["text"] == {"verbosity": "low"}


def test_responses_cache_key_depends_only_on_system_prompt() -> None:
    adapter = OpenAIResponsesAdapter(_config("openai_responses"))
    same_a = adapter._payload([ChatMessage("system", "S"), ChatMessage("user", "任务一")], None, False)
    same_b = adapter._payload([ChatMessage("system", "S"), ChatMessage("user", "任务二")], None, False)
    other = adapter._payload([ChatMessage("system", "别的"), ChatMessage("user", "任务一")], None, False)
    # 系统提示相同就命中同一份缓存，跟任务内容无关
    assert same_a["prompt_cache_key"] == same_b["prompt_cache_key"]
    assert other["prompt_cache_key"] != same_a["prompt_cache_key"]
    # 非推理模型照常下发 temperature
    assert same_a["temperature"] == 0.2


@pytest.mark.asyncio
async def test_responses_invoke_parses_output_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["store"] is False
        return httpx.Response(
            200,
            json={
                "model": "gpt-5",
                "status": "completed",
                "output": [
                    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "读一下"}]},
                    {"type": "function_call", "call_id": "call_9", "name": "read_file", "arguments": "{\"path\":\"a.txt\"}"},
                ],
                "usage": {
                    "input_tokens": 500,
                    "output_tokens": 60,
                    "input_tokens_details": {"cached_tokens": 400},
                    "output_tokens_details": {"reasoning_tokens": 30},
                },
            },
        )

    adapter = OpenAIResponsesAdapter(_config("openai_responses", client_factory=_client(handler)))
    response = await adapter.invoke([ChatMessage("user", "u")], tools=TOOLS)
    assert response.text == "读一下"
    assert response.tool_calls[0]["id"] == "call_9"
    assert response.tool_calls[0]["function"]["name"] == "read_file"
    assert response.usage.cached_tokens == 400
    assert response.usage.reasoning_tokens == 30
    assert response.finish_reason == "completed"


@pytest.mark.asyncio
async def test_responses_stream_yields_text_deltas() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        chunks = b""
        for piece in ["你", "好"]:
            chunks += b"data: " + json.dumps({"type": "response.output_text.delta", "delta": piece}).encode() + b"\n\n"
        chunks += b"data: [DONE]\n\n"
        return httpx.Response(200, content=chunks)

    adapter = OpenAIResponsesAdapter(_config("openai_responses", client_factory=_client(handler)))
    got = "".join([piece async for piece in adapter.stream([ChatMessage("user", "u")], tools=TOOLS)])
    assert got == "你好"


# ---- 前缀稳定性 ------------------------------------------------------------


def test_handoff_keeps_prefix_stable() -> None:
    messages = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "目标"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "下一步"},
    ]
    injected = _insert_handoff(messages, "笔记")
    # 前半段原样不动：缓存按最长相同前缀算，动中间等于全废
    assert injected[:3] == messages[:3]
    # 笔记落在最后一条指令之前，指令仍是最后一条
    assert injected[-2]["content"] == "笔记"
    assert injected[-1]["content"] == "下一步"


# ---- 成本与聚合 ------------------------------------------------------------


def test_estimate_cost_discounts_cached_tokens() -> None:
    full = estimate_cost("claude-3-5-sonnet", 1_000_000, 0)
    half = estimate_cost("claude-3-5-sonnet", 1_000_000, 0, 500_000)
    # 命中一半按 0.1 倍计价 → 总价约为原来的 55%
    assert half.amount < full.amount
    assert half.amount == pytest.approx(full.amount * 0.55, rel=1e-6)
    # 未收录缓存倍率的模型不打折，也不报错
    assert estimate_cost("qwen-plus", 1_000_000, 0, 500_000).amount == pytest.approx(
        estimate_cost("qwen-plus", 1_000_000, 0).amount
    )
    # 缓存数据异常（超过总量）时按总量封顶，不会算出负数
    assert estimate_cost("gpt-4o", 100, 0, 999).amount >= 0


def test_usage_cache_hit_rate_and_merge() -> None:
    usage = Usage(prompt_tokens=1000, completion_tokens=100, cached_tokens=750, reasoning_tokens=40)
    assert usage.cache_hit_rate == 0.75
    assert Usage().cache_hit_rate == 0.0
    merged = merge_usage(accumulate_usage(usage), accumulate_usage(usage))
    assert merged["cached_tokens"] == 1500
    assert merged["reasoning_tokens"] == 80
    assert merged["calls"] == 2


def test_build_adapter_carries_output_knobs_from_provider_row() -> None:
    """供应商行上的档位要真的传到适配器，否则配置页填了也不生效。"""
    from types import SimpleNamespace

    from blank_api.services.models.registry import build_adapter

    provider = SimpleNamespace(
        name="p",
        kind="openai_responses",
        base_url="https://example.test/v1",
        api_key_encrypted="",
        default_model="gpt-5",
        temperature=0.2,
        max_tokens=512,
        context_window=128000,
        supports_tools=True,
        supports_vision=False,
        reasoning_effort="high",
        verbosity="low",
    )
    adapter = build_adapter(provider)
    assert isinstance(adapter, OpenAIResponsesAdapter)
    assert adapter.config.reasoning_effort == "high"
    assert adapter.config.verbosity == "low"

