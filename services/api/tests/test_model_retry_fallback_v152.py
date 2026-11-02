# -*- coding: utf-8 -*-
"""模型超时/断网的有限重试与备用模型切换，以及"重试不许重跑工具"。

这一批补的是三个具体缺口，不是"更健壮"这种说法：

1. 以前只有 429 会重试。真实环境里更常见的是**读超时**与**连接被重置**——
   一次抖动就让整轮任务失败，而模型那边可能只是这一次慢。
2. 重试必须**有界**：模型调用是同步等待，无限重试等于把界面挂住。
3. 重试与"至少一次"语义天然冲突：只要有一笔写操作已经执行，重跑就不再安全。
   所以判据里最重要的一条是"这一笔没有产生副作用"，这里专门给它反面用例。
"""

from __future__ import annotations

import pytest

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services import metrics
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import (
    TRANSIENT_MAX_DELAY_SECONDS,
    TRANSIENT_RETRIES,
    AgentSession,
    _transient_delay,
    _transient_may_retry,
)
from blank_api.services.models.base import (
    AdapterConfig,
    ChatMessage,
    ModelAdapter,
    ModelError,
    ModelResponse,
    ToolSpecLite,
    Usage,
)
from blank_api.services.models.routing import FallbackAdapter
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway

TOOLS = [ToolSpecLite("read_file", "读文件", {"type": "object", "properties": {}})]
TASK_ID = "task-retry"
MESSAGES = [ChatMessage(role="user", content="读一下 a.py")]


# ── 判据本身 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "error,retries,effects_before,effects_now,expected",
    [
        # 超时/断网：可重试，预算内就再问一次
        (ModelError("model_timeout", "慢", retryable=True), 0, 0, 0, True),
        (ModelError("model_unreachable", "断网", retryable=True), TRANSIENT_RETRIES - 1, 0, 0, True),
        # 预算用尽
        (ModelError("model_timeout", "慢", retryable=True), TRANSIENT_RETRIES, 0, 0, False),
        # 429 有自己的预算与 Retry-After，不走这条
        (ModelError("model_http_error", "限流", retryable=True, status=429), 0, 0, 0, False),
        # 问法/身份被拒：再来一遍还是同一句话
        (ModelError("model_http_error", "坏请求", retryable=False, status=400), 0, 0, 0, False),
        (ModelError("model_http_error", "鉴权", retryable=False, status=401), 0, 0, 0, False),
        # **最关键的一格**：这一笔期间执行过工具 → 绝不重试
        (ModelError("model_timeout", "慢", retryable=True), 0, 3, 4, False),
    ],
)
def test_retry_gate_table(error, retries, effects_before, effects_now, expected):
    assert _transient_may_retry(error, retries, effects_before, effects_now) is expected


def test_backoff_grows_is_capped_and_jittered():
    delays = [_transient_delay(attempt) for attempt in (1, 2, 3, 10)]
    assert delays[0] < delays[1] < delays[2]
    assert max(delays) <= TRANSIENT_MAX_DELAY_SECONDS
    # 抖动：同一档两次不应完全相等（否则同时挂掉的任务会齐步回来再打一遍）。
    assert _transient_delay(2) != _transient_delay(2)


# ── 会话层：真的重试、真的留痕 ──────────────────────────────────────────────


class FlakyAdapter(ModelAdapter):
    """前 N 次超时，之后成功。可以顺便"在调用期间执行一个工具"来模拟副作用。"""

    def __init__(self, failures: int, *, error: ModelError | None = None, side_effect=None) -> None:
        super().__init__(
            AdapterConfig(
                name="flaky",
                kind="openai_compatible",
                base_url="https://example.test/v1",
                api_key="k",
                model="m",
                supports_tools=True,
            )
        )
        self.failures = failures
        self.calls = 0
        self.error = error or ModelError("model_timeout", "读超时", retryable=True)
        self.side_effect = side_effect

    async def _attempt(self) -> ModelResponse:
        self.calls += 1
        if self.calls <= self.failures:
            if self.side_effect is not None:
                self.side_effect()
            raise self.error
        return ModelResponse(text="好了", model="m")

    async def invoke(self, messages, **kwargs):
        return await self._attempt()

    async def bind_tools(self, tools, messages, **kwargs):
        return await self._attempt()

    async def bind_tools_stream(self, tools, messages, *, on_delta=None, **kwargs):
        return await self._attempt()

    async def stream(self, messages, **kwargs):
        yield "好了"


@pytest.fixture(autouse=True)
async def schema():
    metrics.reset()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="p-retry", name="重试"))
        session.add(Task(id=TASK_ID, project_id="p-retry", title="重试", goal="重试"))
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
        goal="重试",
    )


async def _events(kind: str) -> list[dict]:
    from sqlalchemy import select

    async with get_session_factory()() as session:
        rows = (
            await session.execute(select(AgentEvent).where(AgentEvent.type == kind))
        ).scalars().all()
    return [row.payload for row in rows]


async def test_timeout_is_retried_then_succeeds(tmp_path):
    adapter = FlakyAdapter(failures=1)
    session = _session(tmp_path, adapter)

    response = await session._call_non_stream(adapter, TOOLS, MESSAGES, "execute_action", "coder")

    assert response.text == "好了"
    assert adapter.calls == 2
    payloads = await _events("model.transient_retry")
    assert len(payloads) == 1
    assert payloads[0]["code"] == "model_timeout"
    assert payloads[0]["attempt"] == 1
    assert metrics.counter(metrics.MODEL_TIMEOUT) == 1
    assert metrics.counter(metrics.MODEL_RETRY) == 1


async def test_retry_budget_is_bounded(tmp_path):
    adapter = FlakyAdapter(failures=99)
    session = _session(tmp_path, adapter)

    with pytest.raises(ModelError) as caught:
        await session._call_non_stream(adapter, TOOLS, MESSAGES, "execute_action", "coder")

    assert caught.value.code == "model_timeout"
    # 首启一次 + 重试预算次，一次都不许多。
    assert adapter.calls == TRANSIENT_RETRIES + 1
    assert len(await _events("model.transient_retry")) == TRANSIENT_RETRIES


async def test_each_attempt_of_a_retry_takes_its_own_usage_row(tmp_path):
    """重试两次调用必须记两行账（P0-2「retry 每次调用单独记录」）。

    只记成功那一笔时，"这个任务调了几次模型"在台账上永远是 1，限流、
    网络抖动和模型真的在思考就分不开；失败尝试拿不到 usage，记全零 +
    error + usage_source=unknown，表示"没拿到"而不是"免费"。
    """
    from sqlalchemy import select

    from blank_api.models.model_provider import ModelUsage

    adapter = FlakyAdapter(failures=1)
    session = _session(tmp_path, adapter)

    response, call_usage = await session.call_model(
        [{"role": "user", "content": "改一下 a.py"}], "execute_action"
    )

    assert response.text == "好了"
    assert adapter.calls == 2
    async with get_session_factory()() as db:
        rows = (
            await db.execute(select(ModelUsage).where(ModelUsage.task_id == TASK_ID))
        ).scalars().all()
    err_rows = [r for r in rows if r.status == "error"]
    ok_rows = [r for r in rows if r.status == "ok"]
    assert len(err_rows) == 1, "被重试吃掉的那次调用也要占一行"
    assert len(ok_rows) == 1
    assert err_rows[0].prompt_tokens == 0
    assert err_rows[0].usage_source == "unknown"
    # 返回值是"这一次调用"的用量，不该把被重试的那次算进来；
    # 轮累计（turn_usage）按真实调用数算 2 次。
    assert call_usage["calls"] == 1
    assert session.turn_usage["calls"] == 2


async def test_side_effect_during_the_call_blocks_the_retry(tmp_path):
    """调用期间执行了工具 → 不重试。

    这是"不能重复执行已经完成的非幂等工具"的闸门：模型调用本身不执行工具，
    但一旦哪天接上"边流边派工具"，副作用检查必须先生效。这里让假适配器在
    失败前手动把会话的执行计数 +1 来模拟那种形状。
    """
    session = _session(tmp_path, FlakyAdapter(failures=1))
    adapter = FlakyAdapter(failures=1, side_effect=lambda: setattr(session, "effects", session.effects + 1))
    session.adapter = adapter

    with pytest.raises(ModelError):
        await session._call_non_stream(adapter, TOOLS, MESSAGES, "execute_action", "coder")

    assert adapter.calls == 1, "有副作用时必须立刻放弃重试"
    assert await _events("model.transient_retry") == []


async def test_non_retryable_status_is_not_retried(tmp_path):
    adapter = FlakyAdapter(
        failures=5, error=ModelError("model_http_error", "坏请求", retryable=False, status=400)
    )
    session = _session(tmp_path, adapter)

    with pytest.raises(ModelError):
        await session._call_non_stream(adapter, TOOLS, MESSAGES, "execute_action", "coder")

    assert adapter.calls == 1


async def test_streaming_path_also_retries_before_any_output(tmp_path):
    adapter = FlakyAdapter(failures=1)
    session = _session(tmp_path, adapter)
    session.stream_output = True

    response = await session._call_adapter(adapter, TOOLS, MESSAGES, "execute_action", "coder")

    assert response.text == "好了"
    assert adapter.calls == 2
    assert len(await _events("model.transient_retry")) == 1


# ── 备用模型 ────────────────────────────────────────────────────────────────


class BoomAdapter(FlakyAdapter):
    def __init__(self, name: str, error: ModelError) -> None:
        super().__init__(failures=99, error=error)
        self.config = AdapterConfig(
            name=name,
            kind="openai_compatible",
            base_url="https://example.test/v1",
            api_key="k",
            model="m",
            supports_tools=True,
        )


async def test_fallback_switches_provider_on_timeout():
    primary = BoomAdapter("主用", ModelError("model_timeout", "读超时", retryable=True))
    backup = FlakyAdapter(failures=0)
    backup.config = AdapterConfig(
        name="备用",
        kind="openai_compatible",
        base_url="https://example.test/v1",
        api_key="k",
        model="m2",
        supports_tools=True,
    )
    adapter = FallbackAdapter([primary, backup])

    response = await adapter.bind_tools(TOOLS, MESSAGES)

    assert response.text == "好了"
    assert adapter.last_provider == "备用"
    assert primary.calls == 1
    assert backup.calls == 1
    assert metrics.counter(metrics.MODEL_FALLBACK) == 1


async def test_fallback_does_not_switch_on_a_bad_request():
    """400 是"问法被拒"：换一家模型也还是这句话，直接失败比多打一次请求诚实。"""
    primary = BoomAdapter("主用", ModelError("model_http_error", "坏请求", retryable=False, status=400))
    backup = FlakyAdapter(failures=0)
    adapter = FallbackAdapter([primary, backup])

    with pytest.raises(ModelError):
        await adapter.bind_tools(TOOLS, MESSAGES)

    assert backup.calls == 0
    assert metrics.counter(metrics.MODEL_FALLBACK) == 0


async def test_all_providers_failing_reports_one_clear_error():
    primary = BoomAdapter("主用", ModelError("model_timeout", "读超时", retryable=True))
    backup = BoomAdapter("备用", ModelError("model_unreachable", "断网", retryable=True))
    adapter = FallbackAdapter([primary, backup])

    with pytest.raises(ModelError) as caught:
        await adapter.bind_tools(TOOLS, MESSAGES)

    assert caught.value.code == "all_models_failed"
    assert "备用" in caught.value.message or "断网" in caught.value.message


# ── §7 切换证据：fallback 发生 + 错误保留 + token 按模型分别统计 ─────────────


class UsageBackup(FlakyAdapter):
    """一次成功的备用调用，并带回真实的 usage（来源=provider）。"""

    async def _attempt(self) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            text="好了",
            model="m-backup",
            usage=Usage(prompt_tokens=120, completion_tokens=30, source="provider"),
        )


async def test_fallback_switch_records_usage_under_backup_model(tmp_path):
    """主模型失败 → 切备用成功：model.fallback 事件要发，token 要记在备用模型名下。

    "我们配了兜底"必须有三个可数的事实：切换事件存在、成功那笔的用量挂在
    备用模型的账上（而不是笼统记在供应商名下）、usage_source 跟着数字走。
    """
    from sqlalchemy import select

    from blank_api.models.model_provider import ModelUsage

    primary = BoomAdapter("兜底测试-主", ModelError("model_timeout", "读超时", retryable=True))
    backup = UsageBackup(failures=0)
    backup.config = AdapterConfig(
        name="兜底测试-备",
        kind="openai_compatible",
        base_url="https://example.test/v1",
        api_key="k",
        model="m-backup",
        supports_tools=True,
    )
    adapter = FallbackAdapter([primary, backup])
    session = _session(tmp_path, adapter)

    response, call_usage = await session.call_model(
        [{"role": "user", "content": "读一下 a.py"}], "execute_action"
    )

    # 切换真的发生了
    assert adapter.last_provider == "兜底测试-备"
    assert response.model == "m-backup"
    assert metrics.counter(metrics.MODEL_FALLBACK) == 1
    # 切换事件带 from/to，审计能数出来
    payloads = await _events("model.fallback")
    assert len(payloads) == 1
    assert payloads[0]["from"] == "兜底测试-主"
    assert payloads[0]["to"] == "兜底测试-备"
    # token 记在备用模型名下，来源=provider
    async with get_session_factory()() as db:
        rows = (await db.execute(select(ModelUsage).where(ModelUsage.task_id == TASK_ID))).scalars().all()
    ok_rows = [r for r in rows if r.status == "ok"]
    assert len(ok_rows) == 1
    assert ok_rows[0].model == "m-backup"
    assert ok_rows[0].prompt_tokens == 120
    assert ok_rows[0].completion_tokens == 30
    assert ok_rows[0].usage_source == "provider"
    assert call_usage["total_tokens"] == 150
    # 主用那一笔失败调用也单独入账：全零 + error + unknown，call 数才数得清
    err_rows = [r for r in rows if r.status == "error"]
    assert len(err_rows) == 1
    assert err_rows[0].model == "m"          # 主用模型名（BoomAdapter 的 model）
    assert err_rows[0].prompt_tokens == 0
    assert err_rows[0].usage_source == "unknown"


async def test_all_models_failed_records_error_usage_and_keeps_cause(tmp_path):
    """全部候选失败：错误信息保留主模型原因，且失败调用以全零 usage 入账。

    "失败也要记账"是口径的一部分：失败那笔请求根本没花 token（全零+error），
    但没有这条记录，"任务为什么失败、花了多少次请求"就无从对账。
    """
    from sqlalchemy import select

    from blank_api.models.model_provider import ModelUsage

    primary = BoomAdapter("兜底测试-主", ModelError("model_timeout", "读超时", retryable=True))
    backup = BoomAdapter("兜底测试-备", ModelError("model_unreachable", "断网", retryable=True))
    adapter = FallbackAdapter([primary, backup])
    session = _session(tmp_path, adapter)

    with pytest.raises(ModelError) as caught:
        await session.call_model([{"role": "user", "content": "读一下 a.py"}], "execute_action")

    # 错误保留：最外层的错要能一路指回主模型为什么挂
    assert caught.value.code == "all_models_failed"
    assert "读超时" in caught.value.message
    # 失败调用入账：status=error、全零 usage
    async with get_session_factory()() as db:
        rows = (await db.execute(select(ModelUsage).where(ModelUsage.task_id == TASK_ID))).scalars().all()
    err_rows = [r for r in rows if r.status == "error"]
    assert len(err_rows) == 1
    assert err_rows[0].prompt_tokens == 0
    assert err_rows[0].completion_tokens == 0
    assert err_rows[0].usage_source == "unknown"
