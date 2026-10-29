"""派出去的钱也算在这轮头上：并行批次的用量归属、水位继承与预算合计。

REA 拆 Codex 的 rollout（docs/REA-WAVE4-CODEX-RUNTIME.md §4.1）时，它的每一笔开销都带着
「属于哪一次编排、由谁引起」，主线程的水位与预算因此看得见委派。我们这边的现状是三条缺陷，
全部读代码确认过、不是猜的：

1. `spawn_agents`（fan_out）派出的子代理把 token 花在自己的线程里，主线程的 `usage`
   只合并 `session.ask` 的返回，于是 token 预算对委派完全隐形——四个子代理各跑八步、
   烧掉比主线程整轮还多的钱，`budget.warning`/`budget.exceeded` 一次都不会响；
2. `_spawn_agents` 的工具结果里只有 count/succeeded/merge/agents，**没有 usage**：
   连"这批花了多少"都交不回主代理，前端与证据包只能靠事件流重放去猜；
3. 子代理的上下文管家原来是裸构造的，不继承父会话从供应商窗口算出来的水位——同一趟任务
   两套口径，派得越多越看不见窗口在哪儿。

下面每一条判定都能失败：期望值来自表里的行、事件 payload 与函数返回，不写死后比对。
批次号（`parallel_group`）为空的行读作"不属于任何一批"，不是"第 0 批"。
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.limits import DEFAULT_TOKEN_BUDGET, TOKEN_WRAP_UP_RATIO
from blank_api.models.model_provider import ModelProvider, ModelUsage
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.evidence import build_evidence, parallel_group_rollup
from blank_api.services.agent.graph import AgentSession, execute_action_node
from blank_api.services.models.base import AdapterConfig, ModelAdapter, ModelResponse, Usage
from blank_api.services.subagent import (
    SubAgentRequest,
    SubAgentResult,
    SubAgentRunner,
    _inherited_watermark,
    batch_usage,
)
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, ok
from blank_api.services.tools.delegate import delegate_tools
from blank_api.services.tools.gateway import ToolGateway

TASK_ID = "task-parbudget-1"
PROVIDER_ID = "provider-parbudget"
RUN_ID = "task-parbudget-1:attempt:1"
ROOT_THREAD = "thread-main-parbudget"


class _Silent(ModelAdapter):
    def __init__(self, name: str = "mock") -> None:
        super().__init__(
            AdapterConfig(name=name, kind="openai_compatible", base_url="", api_key="", model=name)
        )

    async def invoke(self, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def stream(self, messages, **kwargs):  # pragma: no cover
        yield ""

    async def bind_tools(self, tools, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError


class _OneShot(_Silent):
    """一步出结论：子代理走一步、落一笔、结束。"""

    async def bind_tools(self, tools, messages, **kwargs):
        return ModelResponse(
            text="已完成", usage=Usage(prompt_tokens=300, completion_tokens=30), model=self.config.name
        )


async def _seed() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="project-parbudget", name="并行批次归账"))
        session.add(
            ModelProvider(
                id=PROVIDER_ID,
                name="deepseek",
                kind="openai_compatible",
                base_url="https://example.invalid",
                default_model="mock",
                supports_tools=True,
            )
        )
        session.add(
            Task(id=TASK_ID, project_id="project-parbudget", title="并行批次", goal="并行批次", max_steps=10)
        )
        await session.commit()


@pytest.fixture
async def seeded():
    await _seed()


def _factory():
    return get_session_factory()


def _parent(tmp_path, *, window: int = 0) -> AgentSession:
    factory = _factory()
    session = AgentSession(
        # 子代理拿的是父会话的适配器，所以这里就必须是一个真会回答的假适配器
        adapter=_OneShot(),
        gateway=ToolGateway([]),
        context=ToolContext(root=tmp_path, task_id=TASK_ID, session_factory=factory),
        recorder=EventRecorder(factory, TASK_ID, trace_id=ROOT_THREAD, run_id=RUN_ID),
        session_factory=factory,
        provider_id=PROVIDER_ID,
        provider_name="deepseek",
        goal="并行批次归账",
        trace_id=ROOT_THREAD,
        max_steps=10,
    )
    # 主会话的水位：真值来自供应商窗口（runtime 装配），这里直接摆在管家上
    session.compactor.context_window = window
    return session


async def _rows() -> list:
    async with _factory()() as session:
        return list(
            (await session.execute(ModelUsage.__table__.select().order_by(ModelUsage.prompt_tokens))).fetchall()
        )


async def _events(kind: str = "") -> list[dict]:
    async with _factory()() as session:
        rows = (await session.execute(AgentEvent.__table__.select().order_by(AgentEvent.seq))).fetchall()
    out = [{"type": row.type, "payload": row.payload or {}} for row in rows]
    return [item for item in out if not kind or item["type"] == kind]


async def _fan_out(tmp_path, *, count: int = 3, window: int = 0):
    session = _parent(tmp_path, window=window)
    runner = SubAgentRunner().bind(session)
    results = await runner.fan_out(
        [SubAgentRequest(goal=f"子任务 {index}", isolate=False, max_steps=1) for index in range(count)],
        concurrency=count,
    )
    return session, results


# --------------------------------------------------------------------------- #
# 1. 批次号成为账本上的一等列
# --------------------------------------------------------------------------- #


async def test_fan_out_children_share_one_batch_id_on_the_ledger(seeded, tmp_path):
    """同一次 fan_out 的每一行账带同一个批次号，且与 agent_path 里那段 par-{组} 对得上。

    原来只能靠解析 `agent_path` 才知道谁和谁是一批；批次号进列之后，
    「这一批花了多少」是一句按列筛出来的事实，不是一次字符串切割。
    """
    _, results = await _fan_out(tmp_path, count=3)
    assert all(result.ok for result in results), [item.error for item in results]
    groups = {result.parallel_group for result in results}
    assert len(groups) == 1, "一次编排的三位必须同属一批"
    group = groups.pop()
    assert group and len(group) == 6

    rows = await _rows()
    child_rows = [row for row in rows if str(row.node or "") == "subagent"]
    assert child_rows, "子代理一笔账都没落：#33 的账断了"
    assert {row.parallel_group for row in child_rows} == {group}
    for row in child_rows:
        assert row.agent_path.split("/par-")[1].startswith(group), (
            "路径里的批次号与列上的必须一致，否则两处各有各的批次"
        )
    # 逐个的 `subagent.completed` 也要各自报上批次：汇总那条是按它算的，
    # 少一处写入就是"这一条不知道自己是哪一批派来的"。
    completed = [(item["payload"].get("thread_id"), item["payload"].get("parallel_group")) for item in await _events("subagent.completed")]
    assert len(completed) == 3, [item for item, _ in completed]
    assert {value for _, value in completed} == {group}
    by_thread = {str(row.trace_id): str(row.parallel_group) for row in child_rows}
    assert all(by_thread[str(thread)] == value for thread, value in completed), (
        "事件上的批次必须等于同一行账上的批次：两处各说一套就没人知道信谁"
    )


async def test_single_spawn_carries_no_batch_rather_than_a_fake_one(seeded, tmp_path):
    """单发子代理不属于任何一批：列上是空串，不是"第 0 批"，也不是缺键。"""
    session = _parent(tmp_path)
    result = await SubAgentRunner().bind(session)(
        SubAgentRequest(goal="只看一眼 README", isolate=False, max_steps=1)
    )
    assert result.ok, result.error
    assert result.parallel_group == ""
    row = (await _rows())[0]
    assert row.parallel_group == ""
    record = (await _events("model.usage"))[0]["payload"]
    assert record["attribution"]["parallel_group"] == "", "空也要显式写出来：缺键与「没有批次」是两种读法"


async def test_batch_id_is_the_same_source_on_row_and_event(seeded, tmp_path):
    """行与事件同源于那一个值：两处各推一次，迟早对不上。"""
    _, results = await _fan_out(tmp_path, count=2)
    group = results[0].parallel_group
    for row, record in zip(await _rows(), await _events("model.usage")):
        assert record["payload"]["attribution"]["parallel_group"] == row.parallel_group == group


# --------------------------------------------------------------------------- #
# 2. 批次的合计要有一个可指认的出处
# --------------------------------------------------------------------------- #


async def test_fan_out_emits_one_batch_event_naming_every_child(seeded, tmp_path):
    """`subagents.completed`：一次说清这批派了谁、各自花了多少、合计多少。

    逐个的 `subagent.completed` 各带自己的用量，但那是散着的 N 行；
    「这批合计多少」原先没有任何一行直接答得出。
    """
    _, results = await _fan_out(tmp_path, count=3)
    group = results[0].parallel_group
    # 只认本批，不认"全表第一条"：别的用例留下的批次事件同样落在这个库里，
    # 按序号取第一条就等于让"谁先写"决定这条判据的结果（全量跑时红过一次，
    # 单跑永远绿——正是取 [0] 的后果）。顺带把"本批恰好一条"也钉住：
    # 同一批发两条，前端和证据包都会把合计加两遍。
    emitted = [
        event["payload"]
        for event in await _events("subagents.completed")
        if event["payload"].get("group") == group
    ]
    assert len(emitted) == 1, f"本批应当恰好一条批次事件，实际 {len(emitted)} 条"
    batch = emitted[0]
    assert batch["group"] == group
    assert batch["count"] == 3 and batch["succeeded"] == 3
    children = batch["children"]
    assert len(children) == 3
    assert {item["agent_path"] for item in children} == {item.agent_path for item in results}
    assert len({item["thread_id"] for item in children}) == 3, "三条线程必须是三个不同的 id"
    expected_prompt = sum(item.usage.get("prompt_tokens", 0) for item in results)
    expected_completion = sum(item.usage.get("completion_tokens", 0) for item in results)
    assert batch["usage"]["prompt_tokens"] == expected_prompt
    assert batch["usage"]["completion_tokens"] == expected_completion
    assert batch["usage"]["total_tokens"] == expected_prompt + expected_completion
    assert batch["usage"]["calls"] == sum(item.usage.get("calls", 0) for item in results)
    # 批次与回合的归属也要在这条上：按轮筛这批时不用再去别处找
    assert batch["parent_thread_id"] == ROOT_THREAD and batch["root_thread_id"] == ROOT_THREAD


async def test_batch_total_refuses_to_sum_overlapping_latencies():
    """并行子代理的耗时是重叠的，相加得到的比真实墙钟还大——宁可不给这个数。"""
    results = [
        SubAgentResult(ok=True, agent_path="root/par-a-1", usage={"prompt_tokens": 10, "completion_tokens": 2, "calls": 1, "latency_ms": 900}),
        SubAgentResult(ok=True, agent_path="root/par-a-2", usage={"prompt_tokens": 20, "completion_tokens": 3, "calls": 1, "latency_ms": 800}),
    ]
    total = batch_usage(results)
    assert total["prompt_tokens"] == 30 and total["completion_tokens"] == 5
    assert "latency_ms" not in total, "把重叠的耗时加成一个数，是记了一笔假账"
    # 承重的是 merge_usage 那份白名单：合计里冒出任何没声明的键，都是把孩子的自述当成了账。
    assert set(total) <= {
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "total_tokens",
        "calls",
        "provider",
        "model",
    }, sorted(total)
    assert batch_usage([]) == {}, "谁都没花钱时给空字典，不是零填充"


# --------------------------------------------------------------------------- #
# 3. 子代理跟着父会话的水位
# --------------------------------------------------------------------------- #


async def test_child_inherits_the_parents_window(seeded, tmp_path):
    """父会话按供应商窗口压水位时，子代理不能自己退回字符预算。

    这条断的是"同一趟任务两套口径"：派得越多，两套口径的差别越贵。
    """
    _, results = await _fan_out(tmp_path, count=2, window=64_000)
    completed = await _events("subagent.completed")
    assert len(completed) == 2
    assert {item["payload"]["context_window"] for item in completed} == {64_000}
    batch = (await _events("subagents.completed"))[0]["payload"]
    assert batch["context_window"] == 64_000


def test_inherited_watermark_only_moves_values_the_parent_actually_set():
    """0 / 没设的键不许连同 0 一起搬：0 的 trigger_ratio 会把水位线落在 0，每步都压。"""
    assert _inherited_watermark(SimpleNamespace()) == {}
    assert _inherited_watermark(SimpleNamespace(compactor=SimpleNamespace(
        context_window=0, trigger_ratio=0.0, reserve_tokens=0, max_chars=0
    ))) == {}
    moved = _inherited_watermark(SimpleNamespace(compactor=SimpleNamespace(
        context_window=65_536, trigger_ratio=0.8, reserve_tokens=4_096, max_chars=60_000
    )))
    assert moved == {
        "context_window": 65_536,
        "trigger_ratio": 0.8,
        "reserve_tokens": 4_096,
        "max_chars": 60_000,
    }
    # 搬过去的键必须是管家真有的字段名，写错一个就是 TypeError
    from blank_api.services.agent.context import ContextCompactor

    assert ContextCompactor(**moved).context_window == 65_536


# --------------------------------------------------------------------------- #
# 4. 预算看见派出去的钱（而不污染三级口径）
# --------------------------------------------------------------------------- #


def _schema() -> dict:
    return {"type": "object", "properties": {}, "additionalProperties": False}


def _delegate_tool(name: str, *, usage: dict | None, ok_flag: bool = True) -> ToolDefinition:
    async def handler(context: ToolContext, arguments: dict) -> ToolOutcome:
        if not ok_flag:
            return ToolOutcome(ok=False, status="error", message="子代理失败", data={"usage": usage or {}})
        return ok(f"{name} ok", usage=usage) if usage is not None else ok(f"{name} ok")

    return ToolDefinition(
        name=name,
        description=f"测试用 {name}",
        input_schema=_schema(),
        risk_level="medium",
        handler=handler,
        timeout_seconds=5.0,
    )


def _call(name: str) -> dict:
    return {"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function", "function": {"name": name, "arguments": "{}"}}


def _act_session(tmp_path, tools, *, budget: int, tool_calls=None) -> AgentSession:
    """执行节点的夹具：真库、真事件记录器，模型换成只会说一句「收尾」的假答复。

    不接库就写不出 budget.* 事件，判定也就没得核对；这里刻意让事件落进表里，
    断言全部从表读，不从内存里的临时列表读。
    """
    factory = _factory()
    session = AgentSession(
        adapter=_Silent(),
        gateway=ToolGateway(tools),
        context=ToolContext(
            root=tmp_path,
            task_id=TASK_ID,
            session_factory=factory,
            approval_policy=lambda *_: True,
        ),
        recorder=EventRecorder(factory, TASK_ID, trace_id=ROOT_THREAD, run_id=RUN_ID),
        session_factory=factory,
        provider_id=PROVIDER_ID,
        provider_name="mock",
        goal="并行批次归账",
        trace_id=ROOT_THREAD,
        max_steps=10,
    )
    session.token_budget = budget
    prompts: list[str] = []

    async def fake_ask(state, prompt, node, *, messages=None):
        prompts.append(prompt)
        return {
            "text": "收尾",
            "tool_calls": list(tool_calls or []),
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "calls": 1},
            "messages": list(messages or []) + [{"role": "assistant", "content": "收尾"}],
        }

    session.ask = fake_ask
    session._prompts = prompts
    return session


async def test_delegated_spend_trips_a_budget_the_main_thread_never_saw(seeded, tmp_path):
    """主线程自己只花了一百多 token，子代理花了 121k：预算必须因此耗尽。

    原来这一条一次警都不会报——`usage` 里根本没有那 121k。
    预算刻意用一个小值（`session.token_budget` 本来就是每任务可配的），
    要证的是"越过水位的那一笔钱是孩子花的"，不是默认值有多大。
    """
    limit = 50_000
    child = {"prompt_tokens": 120_000, "completion_tokens": 1_000, "calls": 8}
    session = _act_session(
        tmp_path,
        [_delegate_tool("spawn_agents", usage=child)],
        budget=limit,
        tool_calls=[_call("spawn_agents")],
    )
    result = await execute_action_node(
        {"messages": [{"role": "user", "content": "分头看几个模块"}], "steps": 0, "pending": [_call("spawn_agents")]},
        session,
    )
    exceeded = [item["payload"] for item in await _events("budget.exceeded")]
    assert exceeded, "派出去的钱没进预算：预算对委派完全隐形"
    assert exceeded[0]["reason"] == "派给子代理的 token 用完"
    assert exceeded[0]["tokens_self"] < limit <= exceeded[0]["tokens"], "主线程自己远没用完，耗尽的必须是委派"
    assert exceeded[0]["tokens_delegated"] == 121_000
    assert exceeded[0]["tokens"] == exceeded[0]["tokens_self"] + exceeded[0]["tokens_delegated"]
    assert exceeded[0]["token_budget"] == limit
    assert result["pending"] == [], "预算耗尽后不能再接受新的工具调用"
    assert result["budget_exhausted"] is True


async def test_delegated_spend_does_not_enter_the_three_level_usage(seeded, tmp_path):
    """#33 的三级口径仍然只说主线程自己：并进去之后「单次/本回合/整个任务」就不可比了。"""
    child = {"prompt_tokens": 5_000, "completion_tokens": 500, "calls": 3}
    session = _act_session(tmp_path, [_delegate_tool("spawn_agent", usage=child)], budget=DEFAULT_TOKEN_BUDGET)
    result = await execute_action_node(
        {
            "messages": [{"role": "user", "content": "看看"}],
            "steps": 0,
            "pending": [_call("spawn_agent")],
            "usage": {"prompt_tokens": 1_000, "completion_tokens": 100, "calls": 2},
        },
        session,
    )
    # 本回合自己：1100+110（这一步的 ask），绝不含孩子的 5500
    assert result["usage"]["prompt_tokens"] == 1_100
    assert result["usage"]["total_tokens"] == 1_210
    assert result["delegated_usage"]["prompt_tokens"] == 5_000
    assert result["delegated_usage"]["total_tokens"] == 5_500


async def test_delegation_accumulates_across_steps(seeded, tmp_path):
    """这一步派出去的，下一步的预算仍然要看得见：批次不是一步性的。"""
    child = {"prompt_tokens": 9_000, "completion_tokens": 900, "calls": 2}
    session = _act_session(tmp_path, [_delegate_tool("spawn_agents", usage=child)], budget=DEFAULT_TOKEN_BUDGET)
    first = await execute_action_node(
        {"messages": [], "steps": 0, "pending": [_call("spawn_agents")]},
        session,
    )
    second = await execute_action_node(
        {
            "messages": first["messages"],
            "steps": first["steps"],
            "pending": [_call("spawn_agents")],
            "usage": first["usage"],
            "delegated_usage": first["delegated_usage"],
        },
        session,
    )
    assert first["delegated_usage"]["total_tokens"] == 9_900
    assert second["delegated_usage"]["total_tokens"] == 19_800, "上一步派出去的被这一步覆盖掉了"
    assert second["delegated_usage"]["calls"] == 4


async def test_only_delegation_tools_bill_the_batch(seeded, tmp_path):
    """别的工具就算 data 里带了 usage 也不算委派：那是工具的自述，不是派出去的线程。"""
    session = _act_session(
        tmp_path,
        [_delegate_tool("read_file", usage={"prompt_tokens": 77_000, "completion_tokens": 7, "calls": 1})],
        budget=DEFAULT_TOKEN_BUDGET,
    )
    result = await execute_action_node(
        {"messages": [], "steps": 0, "pending": [_call("read_file")]},
        session,
    )
    assert result["delegated_usage"] == {}
    assert not await _events("budget.warning") and not await _events("budget.exceeded")


async def test_failed_delegation_is_not_billed(seeded, tmp_path):
    """子代理没跑成（ok=False）时不能把它自述的用量算进预算：那笔钱没花出去。"""
    session = _act_session(
        tmp_path,
        [_delegate_tool("spawn_agents", usage={"prompt_tokens": 80_000, "completion_tokens": 1}, ok_flag=False)],
        budget=DEFAULT_TOKEN_BUDGET,
    )
    result = await execute_action_node(
        {"messages": [], "steps": 0, "pending": [_call("spawn_agents")]},
        session,
    )
    assert result["delegated_usage"] == {}


async def test_wrap_up_prompt_tells_the_model_what_the_children_cost(seeded, tmp_path):
    """收尾提示要点名委派：不说这一句，模型会把"余量变少"归因到自己多走一步，接着再派一批。"""
    child = {"prompt_tokens": int(DEFAULT_TOKEN_BUDGET * TOKEN_WRAP_UP_RATIO), "completion_tokens": 0, "calls": 2}
    session = _act_session(tmp_path, [_delegate_tool("spawn_agents", usage=child)], budget=DEFAULT_TOKEN_BUDGET)
    await execute_action_node(
        {"messages": [], "steps": 0, "pending": [_call("spawn_agents")]},
        session,
    )
    warnings = [item["payload"] for item in await _events("budget.warning")]
    assert warnings and warnings[0]["tokens_delegated"] == child["prompt_tokens"]
    assert any("子代理花掉的" in prompt for prompt in session._prompts), "提示里没有委派那笔钱"

    quiet = _act_session(tmp_path, [_delegate_tool("spawn_agents", usage=None)], budget=DEFAULT_TOKEN_BUDGET)
    await execute_action_node({"messages": [], "steps": 0, "pending": [_call("spawn_agents")]}, quiet)
    assert not any("子代理花掉的" in prompt for prompt in quiet._prompts), "没派子代理就不要说派了"


# --------------------------------------------------------------------------- #
# 5. 工具结果把批次的账交回主代理
# --------------------------------------------------------------------------- #


async def test_spawn_agents_result_reports_the_batch_and_its_total(tmp_path):
    """`spawn_agents` 的 data 里原先只有 count/succeeded/merge/agents：钱在哪里没人说。"""
    session, _ = await _fan_out(tmp_path, count=2)
    context = replace(session.context, subagent_runner=SubAgentRunner().bind(session))
    tool = next(item for item in delegate_tools() if item.name == "spawn_agents")
    outcome = await tool.handler(
        context,
        {"tasks": [{"goal": "看 A"}, {"goal": "看 B"}]},
    )
    data = outcome.data
    assert data["parallel_group"] and len(data["parallel_group"]) == 6
    assert data["usage"]["calls"] >= 2, "批次合计一笔都没有：主代理拿不到这批的总价"
    assert all(item.get("usage") for item in data["agents"]), "逐个的用量也要在，才能核对合计"
    threads = {item["thread_id"] for item in data["agents"]}
    assert len(threads) == data["count"], "每位子代理的线程 id 必须各不相同，才能指认是谁花的"
    assert all(len(item) == 32 for item in threads)
    assert data["usage"]["total_tokens"] == sum(item["usage"]["total_tokens"] for item in data["agents"])


async def test_evidence_rollups_the_ledger_by_batch_and_keeps_unbatched_separate(seeded, tmp_path):
    """证据包要能指出「那一次 fan_out 花了多少」，同时不把单发、主线程、老账冒充成第 0 批。

    这里刻意再单发一个子代理：它 `node=subagent` 但不属于任何一批。少了这一位，
    "派出的钱按批次列筛"和"按 node 筛"两种写法会得出同一个数，委派账就没人看着了。
    """
    await _fan_out(tmp_path, count=3)
    parent = _parent(tmp_path)
    single = await SubAgentRunner().bind(parent)(SubAgentRequest(goal="只看一眼 README", isolate=False, max_steps=1))
    assert single.ok and single.parallel_group == "", single.error
    await parent._record_usage("execute_action", "mock", Usage(prompt_tokens=111, completion_tokens=11), 10, "ok")

    rows = await _rows()
    groups = parallel_group_rollup(rows)
    assert len(groups) == 1, "三位子代理必须折成一批，而不是三条各成一批"
    batch = groups[0]
    assert batch["group"] and batch["threads"] == 3 and batch["calls"] == 3
    child_prompt = sum(row.prompt_tokens for row in rows if row.parallel_group)
    assert batch["prompt_tokens"] == child_prompt
    assert len(batch["agent_paths"]) == 3

    delegated_rows = [row for row in rows if str(row.node or "") == "subagent"]
    self_rows = [row for row in rows if str(row.node or "") != "subagent"]
    delegated_total = sum(int(row.prompt_tokens or 0) + int(row.completion_tokens or 0) for row in delegated_rows)
    usage = (await build_evidence(_factory(), TASK_ID))["usage"]
    assert {"parallel_groups", "batched_calls", "unbatched_calls", "delegated_calls", "delegated_tokens"} <= set(usage)
    assert usage["batched_calls"] == 3 and usage["unbatched_calls"] == len(rows) - 3
    # 委派按 node 现数：单发那位不在任何一批里，却实实在在是派出去的钱。
    assert usage["delegated_calls"] == len(delegated_rows) == 4
    assert usage["delegated_tokens"] == delegated_total
    assert delegated_total > child_prompt + sum(row.completion_tokens for row in rows if row.parallel_group), (
        "单发那一笔必须让按 node 的合计比按批次的合计大：不然两种写法分不开"
    )
    # 合计与逐行必须闭环：总 - 派出 = 主线程自己那笔
    assert usage["total_tokens"] == delegated_total + sum(
        int(row.prompt_tokens or 0) + int(row.completion_tokens or 0) for row in self_rows
    )
    assert usage["parallel_groups"] == groups


def test_rollup_keeps_rows_without_a_batch_out_instead_of_inventing_group_zero():
    """没有批次号的行不进任何一批：那是"没说"，不是"第 0 批"。"""
    rows = [
        SimpleNamespace(
            parallel_group="", trace_id="t1", agent_path="root", prompt_tokens=10, completion_tokens=1,
            cached_tokens=0, cache_write_tokens=0, reasoning_tokens=0,
        ),
        SimpleNamespace(
            parallel_group="abc123", trace_id="t2", agent_path="root/par-abc123-1", prompt_tokens=20,
            completion_tokens=2, cached_tokens=0, cache_write_tokens=0, reasoning_tokens=0,
        ),
    ]
    groups = parallel_group_rollup(rows)
    assert [item["group"] for item in groups] == ["abc123"]
    assert groups[0]["prompt_tokens"] == 20 and groups[0]["calls"] == 1
