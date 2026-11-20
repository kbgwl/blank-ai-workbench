"""工具参数被输出上限切在半截：放大预算 → 提示拆小 → 可读失败，而不是 HTTP 500。

真机 a69c01d2（2026-11-20）：完全访问档搭 shopbench，模型用 write_file 写一个
约 10706 字的 style.css，流在参数中途结束。旧行为有三处错：
  1. `model_stream_truncated` 标着 retryable=True，被 `_transient_may_retry`
     当成"原样重发"，连发 6 次（attempt 1/2 循环），每次都停在同样的字数；
  2. 重试循环没有截断专门分支，兜底裸 `raise`；
  3. 异常冒到 run_task → FastAPI 回 HTTP 500，把真实原因盖成"请求失败"。

四家同类（Claude Code / Codex / opencode / DeepSeek harness）没有一家原样重发
截断的调用。判据钉住我们的恢复路径：先放大预算，再提示拆小，两次仍失败就给
一句能据以行动的话，绝不原样重发、绝不冒 500。
"""

from __future__ import annotations

import pytest

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import Task
from blank_api.services import metrics
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import (
    MAX_BUDGET_RECOVERIES,
    MAX_TOOL_ARGS_SPLIT_RECOVERIES,
    TRUNCATED_TOOL_ARGS_PROMPT,
    AgentSession,
    _transient_may_retry,
)
from blank_api.services.models.base import (
    AdapterConfig,
    ModelAdapter,
    ModelError,
    ModelResponse,
)
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway

TASK_ID = "task-tool-args"


class TruncatedArgsAdapter(ModelAdapter):
    """前 `failures` 次抛参数截断，之后正常。记录每次下发的预算与收到的消息。"""

    def __init__(self, failures: int, *, model: str = "spark-x2.6") -> None:
        super().__init__(
            AdapterConfig(
                name="trunc-args",
                kind="openai_compatible",
                base_url="https://example.test/v1",
                api_key="k",
                model=model,
                max_tokens=8192,
                supports_tools=True,
            )
        )
        self.failures = failures
        self.calls = 0
        self.sent_budgets: list[int] = []
        self.seen_last_messages: list[str] = []

    async def _attempt(self, messages) -> ModelResponse:
        self.calls += 1
        self.sent_budgets.append(self.payload_max_tokens())
        self.seen_last_messages.append(str(getattr(messages[-1], "content", "")))
        if self.calls <= self.failures:
            raise ModelError(
                "model_stream_truncated",
                "流在工具调用 write_file 的参数中途结束：10706 字不是完整 JSON",
                retryable=True,
                finish_reason="length",
            )
        return ModelResponse(text="拆成小块写好了", model=self.config.model, finish_reason="stop")

    async def invoke(self, messages, **kwargs):
        return await self._attempt(messages)

    async def bind_tools(self, tools, messages, **kwargs):
        return await self._attempt(messages)

    async def bind_tools_stream(self, tools, messages, *, on_delta=None, **kwargs):
        return await self._attempt(messages)

    async def stream(self, messages, **kwargs):
        yield "ok"


@pytest.fixture(autouse=True)
async def schema():
    metrics.reset()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="p-args", name="参数"))
        session.add(Task(id=TASK_ID, project_id="p-args", title="参数", goal="参数"))
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
        goal="参数",
    )


def test_truncated_error_is_not_replayed_verbatim():
    """截断不能再走通用的原样重发——那正是连发 6 次的病根。"""
    error = ModelError(
        "model_stream_truncated", "参数中途结束", retryable=True, finish_reason="length"
    )
    assert _transient_may_retry(error, retries=0, effects_before=0, effects_now=0) is False
    # 对照：普通的可重试错误仍然照旧重试，别把这一条改宽
    transient = ModelError("model_unreachable", "连不上", retryable=True)
    assert _transient_may_retry(transient, retries=0, effects_before=0, effects_now=0) is True


async def test_truncation_grows_budget_then_hints_split_then_succeeds(tmp_path):
    """先放大预算；还切就提示拆小；模型改成小块后这次能成功。"""
    # 预算放大 2 次 + 拆小提示 2 次都失败，第 5 次才成功
    adapter = TruncatedArgsAdapter(failures=MAX_BUDGET_RECOVERIES + MAX_TOOL_ARGS_SPLIT_RECOVERIES)
    session = _session(tmp_path, adapter)

    response, _ = await session.call_model(
        [{"role": "user", "content": "写一个大文件"}], "execute_action"
    )

    assert response.text == "拆成小块写好了"
    # 先是一次原请求 + 放大预算的次数；放大阶段没有注入"拆小"提示
    assert adapter.sent_budgets[0] == 8192
    assert all(b > 8192 for b in adapter.sent_budgets[1 : MAX_BUDGET_RECOVERIES + 1])
    # 拆小提示确实下发给了模型
    assert TRUNCATED_TOOL_ARGS_PROMPT in adapter.seen_last_messages


async def test_truncation_exhausted_gives_actionable_error_not_500(tmp_path):
    """放大 + 拆小都救不回来：给能据以行动的错误，而不是裸 ModelError 冒成 500。"""
    adapter = TruncatedArgsAdapter(failures=99)
    session = _session(tmp_path, adapter)

    with pytest.raises(ModelError) as caught:
        await session.call_model([{"role": "user", "content": "写一个大文件"}], "execute_action")

    error = caught.value
    # 不能再是原始截断错误往上冒——那一路会让 run_task 回 HTTP 500
    assert error.code == "model_tool_args_exhausted"
    assert "拆成多次小写入" in error.message
    assert "输出上限" in error.message
    # 次数有界：首启 + 放大次数 + 拆小次数，一次都不许多
    assert adapter.calls == 1 + MAX_BUDGET_RECOVERIES + MAX_TOOL_ARGS_SPLIT_RECOVERIES


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
