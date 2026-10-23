"""子代理的模型调用必须落进与主代理同一套账。

上一批把三级口径（单次 / 本回合 / 整个任务）接在了 `AgentSession` 上，而子代理走的是
`subagent.py` 里另一条循环：它只把数字累加在本地，既不进 `model_usage` 表，也不发
`model.usage` 事件。结果是——一个把活派下去的任务，账上看不见被派出去的那部分钱，
三级口径里"任务级"永远比真实花费少。Codex 的 `token_usage_record` 是按线程记的，
子线程的用量并进同一个任务账本，所以这里对齐：

1. 子代理每次调用一行 + 一条三级事件，带 `depth`/`agent_path`，`trace_id` 是子线程自己的；
2. 父代理下一条事件必须出现 **turn ≠ task 的真分叉**（回合级只算自己这一路，任务级算全部）；
3. 子代理失败的调用同样进账（那笔请求花了时间、没花 token）；
4. `SubAgentResult.usage` 与表里的行同源，缓存/推理字段不再被丢掉；
5. 事件条数 = 行数（父+子一起算），谁双发、谁漏发都会被这条抓住；
6. 证据包的求和把子代理算进来，且与事件里的任务级指向同一批行；
7. 附带撞出来的老 bug：并行子代理共用一个 `EventRecorder`，seq 是"读最大值再插"，
   两个协程会撞 `UNIQUE(task_id, seq)` 并**静默丢事件**——时间线是唯一的证据来源。
"""

import asyncio
from types import SimpleNamespace

import pytest

from blank_api.db import get_session_factory
from blank_api.models.model_provider import ModelProvider, ModelUsage
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.evidence import build_evidence
from blank_api.services.agent.graph import AgentSession
from blank_api.services.models.base import (
    AdapterConfig,
    ModelAdapter,
    ModelError,
    ModelResponse,
    Usage,
)
from blank_api.services.subagent import SubAgentRequest, SubAgentRunner, run_subagent
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway

TASK_ID = "task-subusage-1"
PROVIDER_ID = "provider-subusage"
LEVEL_KEYS = {
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
    "calls",
}


class ScriptedAdapter(ModelAdapter):
    """按脚本返回整条 ModelResponse；None 表示这次调用直接失败。"""

    def __init__(self, name: str, script: list[ModelResponse | None]) -> None:
        super().__init__(
            AdapterConfig(
                name=name,
                kind="openai_compatible",
                base_url="",
                api_key="",
                model=name,
                supports_tools=True,
            )
        )
        self.script = script
        self.calls = 0

    async def invoke(self, messages, **kwargs):  # pragma: no cover - 不走这条路
        raise NotImplementedError

    async def stream(self, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def bind_tools(self, tools, messages, **kwargs):
        item = self.script[self.calls]
        self.calls += 1
        if item is None:
            raise ModelError("rate_limited", "限流", retryable=True)
        return item


def _response(text: str, usage: Usage, tool_calls: list[dict] | None = None) -> ModelResponse:
    return ModelResponse(text=text, usage=usage, model="mock", tool_calls=tool_calls or [])


def _tool_call(name: str) -> dict:
    return {"id": f"call-{name}", "type": "function", "function": {"name": name, "arguments": "{}"}}


async def _seed() -> None:
    from blank_api.db import Base, get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="project-subusage", name="子代理用量"))
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
            Task(id=TASK_ID, project_id="project-subusage", title="子代理用量", goal="子代理用量", max_steps=10)
        )
        await session.commit()


@pytest.fixture
async def seeded():
    await _seed()


def _factory():
    return get_session_factory()


def _parent(tmp_path, adapter: ModelAdapter) -> AgentSession:
    factory = _factory()
    return AgentSession(
        adapter=adapter,
        gateway=ToolGateway([]),
        context=ToolContext(root=tmp_path, task_id=TASK_ID, session_factory=factory),
        recorder=EventRecorder(factory, TASK_ID),
        session_factory=factory,
        provider_id=PROVIDER_ID,
        provider_name="deepseek",
        goal="子代理用量",
        max_steps=10,
    )


async def _child(adapter: ModelAdapter, tmp_path, *, agent_path: str = "root/sub-1", depth: int = 1):
    factory = _factory()
    return await run_subagent(
        adapter=adapter,
        tools=[],
        root=tmp_path,
        task_id=TASK_ID,
        session_factory=factory,
        recorder=EventRecorder(factory, TASK_ID),
        request=SubAgentRequest(goal="读取 README 并复述", max_steps=4),
        agent_path=agent_path,
        depth=depth,
        parent_thread_id="root-thread",
        provider_id=PROVIDER_ID,
        provider_name="deepseek",
    )


async def _records() -> list[dict]:
    async with _factory()() as session:
        rows = (await session.execute(AgentEvent.__table__.select().order_by(AgentEvent.seq))).fetchall()
    return [row.payload for row in rows if row.type == "model.usage"]


async def _rows() -> list:
    async with _factory()() as session:
        rows = (await session.execute(ModelUsage.__table__.select())).fetchall()
    return list(rows)


async def test_each_subagent_call_lands_a_row_and_a_three_level_event(seeded, tmp_path):
    """两步的子代理 = 两行 + 两条事件；子代理的事件同样是三级齐全的。"""
    adapter = ScriptedAdapter(
        "mock",
        [
            _response(
                "先看一眼",
                Usage(prompt_tokens=300, completion_tokens=30, cached_tokens=200, reasoning_tokens=9),
                [_tool_call("read_file")],
            ),
            _response("README 是一句话说明", Usage(prompt_tokens=400, completion_tokens=40, cached_tokens=300)),
        ],
    )
    result = await _child(adapter, tmp_path)

    rows = await _rows()
    records = await _records()
    assert result.ok, result.error
    assert len(rows) == 2, "子代理原来不落库：这两行是这次改动的核心"
    assert len(records) == 2, "一次调用一条事件，父子都得守住这条"
    for row in rows:
        assert row.task_id == TASK_ID
        assert row.node == "subagent"
        assert row.provider_id == PROVIDER_ID, "钱挂在哪个账号上必须说得出，不能落空"
    assert [row.prompt_tokens for row in rows] == [300, 400]
    assert [row.cached_tokens for row in rows] == [200, 300]
    assert [row.reasoning_tokens for row in rows] == [9, 0]

    assert all({"call", "turn", "task"} <= set(item) for item in records)
    assert {item["role"] for item in records} == {"subagent"}
    assert all(item["depth"] == 1 and item["agent_path"] == "root/sub-1" for item in records)
    assert all(item["trace_id"] and item["trace_id"] != "root-thread" for item in records), (
        "子代理是一条独立线程：借用父线程 id 就把两个 agent 的账混成一个人"
    )
    assert records[1]["turn"]["prompt_tokens"] == 700
    assert records[1]["turn"]["calls"] == 2
    assert records[1]["task"]["prompt_tokens"] == 700, "任务里只有这个子代理，任务级应与它相等"


async def test_parent_event_shows_a_real_turn_task_divergence(seeded, tmp_path):
    """父→子→父（同一个 AgentSession）：回合级只算自己这一路，任务级必须把子代理算进来。

    这条是真分叉的现场——上一批只有单会话任务，turn 与 task 数值相等，
    「三级」到底有没有各自的意义没在线上形状上证过。
    """
    root_adapter = ScriptedAdapter(
        "mock",
        [
            _response("派个人去查", Usage(prompt_tokens=1000, completion_tokens=100)),
            _response("收工", Usage(prompt_tokens=100, completion_tokens=10)),
        ],
    )
    parent = _parent(tmp_path, root_adapter)
    await parent.call_model([{"role": "user", "content": "第一步"}], "execute_action", step=0)

    child_adapter = ScriptedAdapter(
        "mock",
        [
            _response("查一下", Usage(prompt_tokens=300, completion_tokens=30), [_tool_call("read_file")]),
            _response("结论", Usage(prompt_tokens=200, completion_tokens=20, cached_tokens=200)),
        ],
    )
    await _child(child_adapter, tmp_path)

    await parent.call_model([{"role": "user", "content": "收尾"}], "summarize_result", step=1)

    records = await _records()
    assert len(records) == 4, "父子四条链路都要在同一个序列里留痕"
    rows = await _rows()
    assert len(rows) == len(records), f"事件 {len(records)} 条 vs 表 {len(rows)} 行：不等就是双发或漏发"
    assert [item["role"] for item in records] == ["coder", "subagent", "subagent", "reviewer"]
    assert records[0]["agent_path"] == "root" and records[3]["agent_path"] == "root"

    child_second = records[2]
    assert child_second["turn"]["prompt_tokens"] == 500
    assert child_second["task"]["prompt_tokens"] == 1500, "任务级漏掉父代理 = 三级只是三个副本"
    last = records[-1]
    assert last["turn"]["prompt_tokens"] == 1100, "回合级把子代理的 500 也算进来就是重复记账"
    assert last["turn"]["calls"] == 2
    assert last["task"]["prompt_tokens"] == 1600, "任务级看不见被派出去的钱：一个任务真实花掉的比账上多"
    assert last["task"]["cached_tokens"] == 200
    assert last["task"]["calls"] == 4
    assert last["turn"]["prompt_tokens"] < last["task"]["prompt_tokens"]


async def test_failed_subagent_call_is_recorded_too(seeded, tmp_path):
    """子代理调不通也要留痕：那笔请求花了时间、没花 token，只有这条记录说得出。"""
    adapter = ScriptedAdapter("mock", [None])
    result = await _child(adapter, tmp_path)
    assert result.ok is False
    assert result.error == "限流"
    records = await _records()
    rows = await _rows()
    assert len(records) == 1 and len(rows) == 1
    assert records[0]["call"]["status"] == "error"
    assert records[0]["call"]["prompt_tokens"] == 0
    assert records[0]["task"]["calls"] == 1
    assert records[0]["task"]["total_tokens"] == 0
    assert records[0]["depth"] == 1 and records[0]["agent_path"] == "root/sub-1"


async def test_subagent_result_usage_shares_the_ledger(seeded, tmp_path):
    """`SubAgentResult.usage` 与表里的行同源，缓存/推理不再被丢掉。"""
    adapter = ScriptedAdapter(
        "mock",
        [
            _response(
                "第一步",
                Usage(
                    prompt_tokens=500,
                    completion_tokens=50,
                    cached_tokens=450,
                    cache_write_tokens=50,
                    reasoning_tokens=20,
                ),
                [_tool_call("read_file")],
            ),
            _response("结论", Usage(prompt_tokens=250, completion_tokens=25, cached_tokens=250)),
        ],
    )
    result = await _child(adapter, tmp_path)
    assert LEVEL_KEYS <= set(result.usage), "只报 prompt/completion，缓存命中这条线就永远断着"
    assert result.usage["prompt_tokens"] == 750
    assert result.usage["completion_tokens"] == 75
    assert result.usage["total_tokens"] == 825
    assert result.usage["cached_tokens"] == 700
    assert result.usage["cache_write_tokens"] == 50
    assert result.usage["reasoning_tokens"] == 20
    assert result.usage["calls"] == 2
    rows = await _rows()
    assert sum(row.prompt_tokens for row in rows) == result.usage["prompt_tokens"]
    assert sum(row.cached_tokens for row in rows) == result.usage["cached_tokens"]


async def test_two_children_share_task_totals_but_keep_their_threads(seeded, tmp_path):
    """并行编排：两个子代理各自一条线程，任务级把两份都合起来，回合级各算各的。"""
    first = ScriptedAdapter("mock", [_response("A 完成", Usage(prompt_tokens=600, completion_tokens=60))])
    second = ScriptedAdapter(
        "mock", [_response("B 完成", Usage(prompt_tokens=700, completion_tokens=70, cached_tokens=700))]
    )
    results = await asyncio.gather(
        _child(first, tmp_path, agent_path="root/par-a-1"),
        _child(second, tmp_path, agent_path="root/par-b-2"),
    )
    assert all(item.ok for item in results), [item.error for item in results]

    records = await _records()
    assert len(records) == 2
    by_path = {item["agent_path"]: item for item in records}
    assert set(by_path) == {"root/par-a-1", "root/par-b-2"}
    assert by_path["root/par-a-1"]["trace_id"] != by_path["root/par-b-2"]["trace_id"], (
        "两条线程共用一个 trace_id，事后就分不出这 1300 token 是谁花的"
    )
    for item in records:
        own = item["call"]["prompt_tokens"]
        assert item["turn"]["prompt_tokens"] == own, "并发时把别人的调用算进自己的回合 = 回合级失去意义"
        assert item["turn"]["calls"] == 1
        # 并发下谁先落库不确定，任务级只能是"只有自己"或"两个都有"，不能是第三种数
        assert item["task"]["prompt_tokens"] in (own, 1300), item["task"]
        assert item["task"]["calls"] in (1, 2)
    assert max(item["task"]["prompt_tokens"] for item in records) == 1300, "任务级始终没并上兄弟线程"
    rows = await _rows()
    assert sum(row.prompt_tokens for row in rows) == 1300
    assert sum(row.cached_tokens for row in rows) == 700


async def test_evidence_adds_the_childs_money_to_the_same_source(seeded, tmp_path):
    """证据包（前端读的那一份）与事件里的任务级必须指向同一批行。"""
    parent = _parent(
        tmp_path, ScriptedAdapter("mock", [_response("派出去", Usage(prompt_tokens=1000, completion_tokens=100))])
    )
    await parent.call_model([{"role": "user", "content": "第一步"}], "execute_action", step=0)
    await _child(
        ScriptedAdapter("mock", [_response("子代理结论", Usage(prompt_tokens=800, completion_tokens=80, cached_tokens=600))]),
        tmp_path,
    )

    usage = (await build_evidence(_factory(), TASK_ID))["usage"]
    assert usage["prompt_tokens"] == 1800, "证据包少算子代理：用户看到的成本比真花掉的低"
    assert usage["cached_tokens"] == 600
    assert usage["calls"] == 2
    assert usage["records"] == 2
    assert "deepseek" in usage["providers"]
    last = (await _records())[-1]
    assert usage["levels"]["task"] == last["task"], (
        f"证据包求和 {usage['levels']['task']} 与事件任务级 {last['task']} 不同源"
    )


async def test_subagent_without_a_task_id_still_keeps_its_own_thread_account(seeded, tmp_path):
    """没有 task_id 的会话（离线跑图、评测）不能查出一张空表当总数：任务级退回本回合。"""

    async def _silent(*args, **kwargs):
        return 0

    factory = _factory()
    result = await run_subagent(
        adapter=ScriptedAdapter("mock", [_response("结论", Usage(prompt_tokens=120, completion_tokens=12))]),
        tools=[],
        root=tmp_path,
        task_id="",
        session_factory=factory,
        recorder=SimpleNamespace(add=_silent),
        request=SubAgentRequest(goal="x", max_steps=2),
        agent_path="root/sub-orphan",
        depth=1,
        parent_thread_id="t",
        provider_id=PROVIDER_ID,
        provider_name="deepseek",
    )
    assert result.usage["prompt_tokens"] == 120
    assert result.usage["calls"] == 1
    rows = await _rows()
    assert len(rows) == 1
    assert not rows[0].task_id


async def test_runner_attributes_the_child_to_the_session_provider(seeded, tmp_path):
    """线上入口是 `SubAgentRunner`：它得把会话的供应商一路带到子代理的用量行上。

    `run_subagent` 可以脱离会话被直接调用（v13 的形状），provider 由调用方给；
    这一条钉的是"派出去的活记在派它的那个账号上"，缺了它账单就对不上供应商。
    """
    parent = _parent(
        tmp_path,
        ScriptedAdapter("mock", [_response("子代理结论", Usage(prompt_tokens=210, completion_tokens=21, cached_tokens=200))]),
    )
    result = await SubAgentRunner().bind(parent)(SubAgentRequest(goal="复述 README", isolate=False, max_steps=2))
    assert result.ok, result.error
    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].provider_id == PROVIDER_ID, "子代理的钱挂错账号：账单与供应商对不上"
    assert rows[0].task_id == TASK_ID
    assert rows[0].cached_tokens == 200
    record = (await _records())[-1]
    assert record["call"]["provider"] == "deepseek"
    assert record["agent_path"] == "root/sub-1" and record["depth"] == 1


async def test_parallel_children_do_not_lose_events_to_the_seq_race(seeded, tmp_path):
    """并行子代理共用一个事件记录器：seq 是"读最大值再插一条"，两个协程会撞同一个号。

    这条不是为 #33 设计的，是 #33 把它撞出来的——跑变异时当场
    `IntegrityError: UNIQUE constraint failed: agent_events.task_id, agent_events.seq`。
    撞号时事件不是变乱序，是直接消失，而时间线是唯一能拿给用户看的证据。
    """
    factory = _factory()
    recorder = EventRecorder(factory, TASK_ID)
    seqs = await asyncio.gather(*(recorder.add("probe.event", "subagent", {"i": i}) for i in range(12)))
    assert sorted(seqs) == list(range(1, 13)), f"并发写入撞号：{sorted(seqs)}"
    async with factory() as session:
        rows = (
            await session.execute(AgentEvent.__table__.select().where(AgentEvent.task_id == TASK_ID))
        ).fetchall()
    assert len(rows) == 12, "撞号的那条事件是静默丢失的：一条都没少才算闭环"
    assert sorted(row.seq for row in rows) == list(range(1, 13))
    assert len({row.id for row in rows}) == 12
