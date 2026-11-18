"""上下文硬超限时的兜底：裁掉前半段历史，再问一次。

为什么需要：我们只在**预估水位**过高时压缩（`compactor.over_trigger`），
但供应商的计数口径可能比我们保守——它先拒了（400 context_length_exceeded），
而这一轮还没到我们的水位。此时换模型、换说法都没用，只能把历史变短。

对齐 Claude / Codex 的 auto-compact：它们做摘要压缩，我们直接裁前半段。
差别在于**摘要本身也是一次模型请求**，把同一份超长上下文发过去大概率还是 400，
等于把一次失败变成两次；裁剪是本地动作，一定成功。
"""

from __future__ import annotations

import pytest

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services import metrics
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import AgentSession
from blank_api.services.models.base import (
    AdapterConfig,
    ModelAdapter,
    ModelError,
    ModelResponse,
)
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway

TASK_ID = "task-ctx-overflow"


class OverflowOnceAdapter(ModelAdapter):
    """第一次拒"上下文太长"，之后正常。"""

    def __init__(self) -> None:
        super().__init__(
            AdapterConfig(
                name="overflow",
                kind="openai_compatible",
                base_url="https://example.test/v1",
                api_key="k",
                model="fake-model",
                max_tokens=8192,
                supports_tools=True,
            )
        )
        self.calls = 0
        self.seen_counts: list[int] = []

    async def _attempt(self, messages) -> ModelResponse:
        self.calls += 1
        self.seen_counts.append(len(messages))
        if self.calls == 1:
            raise ModelError(
                "context_overflow",
                "上下文超出模型窗口（HTTP 400）：maximum context length is 128000 tokens",
                retryable=False,
                status=400,
            )
        return ModelResponse(text="好了", model=self.config.model, finish_reason="stop")

    async def invoke(self, messages, **kwargs):
        return await self._attempt(messages)

    async def bind_tools(self, tools, messages, **kwargs):
        return await self._attempt(messages)

    async def bind_tools_stream(self, tools, messages, *, on_delta=None, **kwargs):
        return await self._attempt(messages)

    async def stream(self, messages, **kwargs):
        yield "好了"


class AlwaysOverflowAdapter(OverflowOnceAdapter):
    """每一轮都拒：裁历史也救不了（单条消息本身太长），必须如实失败。"""

    async def _attempt(self, messages) -> ModelResponse:
        self.calls += 1
        self.seen_counts.append(len(messages))
        raise ModelError(
            "context_overflow",
            "上下文超出模型窗口（HTTP 400）",
            retryable=False,
            status=400,
        )


@pytest.fixture(autouse=True)
async def schema():
    metrics.reset()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="p-ctx", name="上下文"))
        session.add(Task(id=TASK_ID, project_id="p-ctx", title="上下文", goal="上下文"))
        await session.commit()
    yield
    metrics.reset()


def _session(tmp_path, adapter: ModelAdapter) -> AgentSession:
    return AgentSession(
        adapter=adapter,
        gateway=ToolGateway([]),
        context=ToolContext(root=tmp_path, task_id=TASK_ID, session_factory=get_session_factory()),
        recorder=EventRecorder(get_session_factory(), TASK_ID),
        session_factory=get_session_factory(),
        provider_id="p",
        provider_name=adapter.config.name,
        goal="上下文",
    )


def _history() -> list[dict]:
    # 8 条非 system 消息：裁剪一半后还有 4 条，够继续对话
    return [{"role": "user" if index % 2 == 0 else "assistant", "content": f"第 {index} 段"} for index in range(8)]


async def _events(kind: str) -> list[dict]:
    from sqlalchemy import select

    async with get_session_factory()() as session:
        rows = (await session.execute(select(AgentEvent).where(AgentEvent.type == kind))).scalars().all()
    return [row.payload for row in rows]


@pytest.mark.asyncio
async def test_overflow_trims_history_and_retries_once(tmp_path) -> None:
    adapter = OverflowOnceAdapter()
    session = _session(tmp_path, adapter)

    response, _ = await session.call_model(_history(), "execute_action")

    assert response.text == "好了"
    assert adapter.calls == 2
    # 第二次带过去的消息必须更少，否则等于没裁
    assert adapter.seen_counts[1] < adapter.seen_counts[0]
    trimmed = await _events("context.overflow_trimmed")
    assert len(trimmed) == 1
    assert trimmed[0]["dropped_messages"] > 0
    assert trimmed[0]["kept_messages"] > 0


@pytest.mark.asyncio
async def test_overflow_still_fails_when_trimming_cannot_help(tmp_path) -> None:
    adapter = AlwaysOverflowAdapter()
    session = _session(tmp_path, adapter)

    with pytest.raises(ModelError) as caught:
        await session.call_model(_history(), "execute_action")

    assert caught.value.code == "context_overflow"
    # 只补一次，不许多打
    assert adapter.calls == 2
