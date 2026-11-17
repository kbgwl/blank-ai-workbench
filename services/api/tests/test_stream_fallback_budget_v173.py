"""空流退路必须有自己的总时限，不能继承长跑实例的 1800s。

真机取证（2026-11-17，运维控制台题，评测实例 :8066，任务 01f1fa83）：
  seq20 00:55:28  model.stream_fallback  code=model_stream_empty
  seq21 01:25:28  model.transient_retry  attempt=1  code=model_timeout（整 30 分钟）
  seq23 01:55:29  model.transient_retry  attempt=2  code=model_timeout
  seq26 02:25:34  task.failed  code=model_timeout
评测端 30 分钟先放弃，交付目录只剩 .blank/context/agent-transcript.jsonl。
根因：退路整包调用用的是适配器的 timeout_seconds（该实例设成 1800），超时又算
可重试——同一端点挂满 90 分钟才把失败交回去。

这组判据钉三件事：退路有墙钟上限、上限取 min(配置, 退路封顶)、超时不再重试。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from blank_api.services.agent import graph
from blank_api.services.models.base import (
    AdapterConfig,
    ChatMessage,
    ModelAdapter,
    ModelError,
)


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def add(self, type_: str, node: str, payload: dict | None = None) -> None:
        self.events.append((type_, node, dict(payload or {})))


class _EmptyStreamAdapter(ModelAdapter):
    """流式为空的适配器 + 永不返回的整包调用（模拟挂死的网关）。"""

    def __init__(self, *, timeout_seconds: float, hang_seconds: float = 30.0) -> None:
        super().__init__(
            AdapterConfig(
                name="t",
                kind="openai_compatible",
                base_url="https://example.test/v1",
                api_key="k",
                model="m",
                supports_tools=True,
                timeout_seconds=timeout_seconds,
            )
        )
        self.attempts = 0
        self.hang_seconds = hang_seconds

    async def bind_tools_stream(self, *args, **kwargs):
        raise ModelError("model_stream_empty", "模型 m 的流式响应为空")

    async def bind_tools(self, *args, **kwargs):
        self.attempts += 1
        await asyncio.sleep(self.hang_seconds)
        raise AssertionError("整包桩不该跑完：退路时限必须先到")

    async def invoke(self, *args, **kwargs):
        raise AssertionError("不该走 invoke")

    async def stream(self, *args, **kwargs):
        raise AssertionError("不该走 stream")


def _harness() -> object:
    """只要 _call_adapter 用到的那几个属性，不建完整 Hunter。"""
    hunter = object.__new__(graph.AgentSession)
    hunter.stream_output = True
    hunter.hooks = None
    hunter.context = SimpleNamespace(task_id="t")
    hunter.recorder = _Recorder()
    hunter.effects = 0
    hunter.provider_name = "t"
    return hunter


async def _run_fallback(adapter: _EmptyStreamAdapter, *, outer_timeout: float = 5.0):
    hunter = _harness()
    return await asyncio.wait_for(
        graph.AgentSession._call_adapter(
            hunter,
            adapter,
            [],
            [ChatMessage(role="user", content="hi")],
            "execute_action",
            "coder",
        ),
        timeout=outer_timeout,
    )


def test_fallback_budget_is_capped_by_its_own_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置 1800s 时，退路仍要在自己的封顶内失败，而不是等满 30 分钟。"""
    monkeypatch.setattr(graph, "FALLBACK_TIMEOUT_SECONDS", 0.2)
    adapter = _EmptyStreamAdapter(timeout_seconds=1800)

    started = time.monotonic()
    with pytest.raises(ModelError) as info:
        asyncio.run(_run_fallback(adapter))
    elapsed = time.monotonic() - started

    assert info.value.code == "model_fallback_timeout", info.value
    assert info.value.retryable is False, "退路超时不该再原样重试"
    assert elapsed < 2.0, f"退路花了 {elapsed:.1f}s，封顶没生效"
    assert adapter.attempts == 1, f"整包调用试了 {adapter.attempts} 次，超时被当成可重试"


def test_fallback_uses_the_shorter_configured_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置比退路封顶更短时取配置——封顶是上限，不是固定值。"""
    monkeypatch.setattr(graph, "FALLBACK_TIMEOUT_SECONDS", 120.0)
    adapter = _EmptyStreamAdapter(timeout_seconds=0.1)

    started = time.monotonic()
    with pytest.raises(ModelError) as info:
        asyncio.run(_run_fallback(adapter))
    elapsed = time.monotonic() - started

    assert info.value.code == "model_fallback_timeout", info.value
    assert elapsed < 1.5, f"退路花了 {elapsed:.1f}s，没有采用更短的配置值"


def test_fallback_event_records_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """事件里要有这次退路的预算：只写「退路了」读不出它被允许挂多久。"""
    monkeypatch.setattr(graph, "FALLBACK_TIMEOUT_SECONDS", 0.2)
    adapter = _EmptyStreamAdapter(timeout_seconds=1800)
    hunter = _harness()

    with pytest.raises(ModelError):
        asyncio.run(
            asyncio.wait_for(
                graph.AgentSession._call_adapter(
                    hunter,
                    adapter,
                    [],
                    [ChatMessage(role="user", content="hi")],
                    "execute_action",
                    "coder",
                ),
                timeout=5.0,
            )
        )

    assert len(hunter.recorder.events) == 1, hunter.recorder.events
    type_, node, payload = hunter.recorder.events[0]
    assert type_ == "model.stream_fallback", type_
    assert node == "execute_action", node
    assert payload["code"] == "model_stream_empty", payload
    assert payload["budget_ms"] == 200, payload


def test_fallback_budget_helper_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graph, "FALLBACK_TIMEOUT_SECONDS", 120.0)
    assert graph._fallback_budget(1800) == 120.0
    assert graph._fallback_budget(5) == 5.0
    assert graph._fallback_budget(0) == 120.0
    assert graph._fallback_budget(-1) == 120.0
