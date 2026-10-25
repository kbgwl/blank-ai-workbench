"""每一笔钱都要说得出"算在哪一轮、被谁引起的"。

REA 拆 Codex 的 rollout（docs/REA-WAVE4-CODEX-RUNTIME.md §3、§4.1）时，它的
`token_usage_record` 每行同时带 `turn_id` / `root_turn_id` / `session_id` /
`initiating_agent_path`，`resume_metadata.turn_attribution` 再补 `parent_turn_id`。
我们这边账本行只有 `task_id` + `trace_id`，于是两个问题答不出：

① 「这一轮花了多少」——同一个任务重跑三次，行里没有字段能把三轮分开；
② 「这条子分支是谁引起的」——父子关系只写在事件 payload 里，账本上只有一串看不出源头的 trace_id。

这一批把回合归属做成一等字段（`model_usage.run_id/root_trace_id/parent_trace_id/agent_path`
+ 事件 payload 里的 `attribution` 块），并且只留**一个真源**：轮 id 读事件记录器，
根线程在主会话上是"没有父的就是自己"，子会话的值由父会话一路传下来。

真模型第一次点验就撞出一条离线没覆盖的洞（见最后一条判定）：同步 `/run` 这个入口不建
run 台账，`request.run_id` 是空的，于是 7 行账全部落进"归属未记"、按轮汇总表一条都没有。
离线测的是「给了轮 id 之后搬得对不对」，没人测「入口没给的时候会不会整批留空」。

下面每一条判定都能失败；期望值全部来自表里的行与接口返回，不写死后比对。
"""

from types import SimpleNamespace

import pytest

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.model_provider import ModelProvider, ModelUsage
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.evidence import _sole, branch_rollup, build_evidence, turn_rollup
from blank_api.services.agent.graph import AgentSession
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.models.base import (
    AdapterConfig,
    ModelAdapter,
    ModelResponse,
    Usage,
)
from blank_api.services.subagent import SubAgentRequest, SubAgentRunner, run_subagent
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway

TASK_ID = "task-turnattr-1"
PROVIDER_ID = "provider-turnattr"
RUN_A = "run-alpha"
RUN_B = "run-beta"
ROOT_THREAD = "thread-root"
# #45 起多了 parallel_group：这一笔属于哪一次并行编排（空串=不属于任何一批）。
# 这个集合是 `model.usage` 事件 attribution 的**全集**，不是"v24 那六个键"——
# 加键要在这里一起改，否则守卫就退化成"至少这些键在"，多个没人认领的键没人发现。
ATTRIBUTION_KEYS = {
    "run_id",
    # #47 起的发起轮：整棵任务树共用一个值，跨任务的团队轮靠它合账。
    "origin_run_id",
    "thread_id",
    "parent_thread_id",
    "root_thread_id",
    "agent_path",
    "depth",
    "parallel_group",
    # M3 起多了 sandbox_mode：这一笔账当时按哪一档边界跑（空串=上线前的老行）。
    "sandbox_mode",
}


class _Silent(ModelAdapter):
    """这一批不需要模型真的说话：只要会话能落账。"""

    def __init__(self, name: str = "mock") -> None:
        super().__init__(
            AdapterConfig(name=name, kind="openai_compatible", base_url="", api_key="", model=name)
        )

    async def invoke(self, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def stream(self, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def bind_tools(self, tools, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError


class _OneShot(_Silent):
    """一次调用就给出终稿：子代理走一步、落一笔、结束。"""

    async def bind_tools(self, tools, messages, **kwargs):
        return ModelResponse(
            text="已完成", usage=Usage(prompt_tokens=200, completion_tokens=20), model=self.config.name
        )


async def _seed() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="project-turnattr", name="回合归属"))
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
            Task(id=TASK_ID, project_id="project-turnattr", title="回合归属", goal="回合归属", max_steps=10)
        )
        await session.commit()


@pytest.fixture
async def seeded():
    await _seed()


def _factory():
    return get_session_factory()


def _parent(tmp_path, *, run_id: str = RUN_A, trace_id: str = ROOT_THREAD) -> AgentSession:
    factory = _factory()
    return AgentSession(
        adapter=_Silent(),
        gateway=ToolGateway([]),
        context=ToolContext(root=tmp_path, task_id=TASK_ID, session_factory=factory),
        recorder=EventRecorder(factory, TASK_ID, trace_id=trace_id, run_id=run_id),
        session_factory=factory,
        provider_id=PROVIDER_ID,
        provider_name="deepseek",
        goal="回合归属",
        trace_id=trace_id,
        max_steps=10,
    )


async def _rows() -> list:
    async with _factory()() as session:
        return list((await session.execute(ModelUsage.__table__.select().order_by(ModelUsage.id))).fetchall())


async def _usage_records() -> list[dict]:
    async with _factory()() as session:
        rows = (await session.execute(AgentEvent.__table__.select().order_by(AgentEvent.seq))).fetchall()
    return [row.payload for row in rows if row.type == "model.usage"]


async def _all_events() -> list[dict]:
    async with _factory()() as session:
        rows = (await session.execute(AgentEvent.__table__.select().order_by(AgentEvent.seq))).fetchall()
    return [{"type": row.type, "run_id": row.run_id, "payload": row.payload} for row in rows]


async def _add(session: AgentSession, prompt: int, completion: int) -> None:
    await session._record_usage("execute_action", "mock", Usage(prompt_tokens=prompt, completion_tokens=completion), 10, "ok")


async def _child(
    tmp_path,
    *,
    agent_path: str,
    depth: int,
    parent_thread_id: str,
    root_thread_id: str,
    run_id: str = RUN_A,
    adapter: ModelAdapter | None = None,
):
    factory = _factory()
    return await run_subagent(
        adapter=adapter or _OneShot("kid"),
        tools=[],
        root=tmp_path,
        task_id=TASK_ID,
        session_factory=factory,
        recorder=EventRecorder(factory, TASK_ID, trace_id=parent_thread_id, run_id=run_id),
        request=SubAgentRequest(goal="读 README 并复述", max_steps=2),
        agent_path=agent_path,
        depth=depth,
        parent_thread_id=parent_thread_id,
        root_thread_id=root_thread_id,
        provider_id=PROVIDER_ID,
        provider_name="deepseek",
    )


# --- 落账：一次调用，行与事件说的是同一件事 ---------------------------------------------------


async def test_main_call_carries_attribution_on_both_row_and_event(seeded, tmp_path):
    """主代理的根就是它自己、父为空；轮 id 来自事件记录器，两处必须同源。"""
    session = _parent(tmp_path)
    await _add(session, 500, 50)

    row = (await _rows())[0]
    record = (await _usage_records())[0]
    attribution = record["attribution"]
    assert attribution["run_id"] == RUN_A, "轮 id 不进事件，「这一轮花了多少」就仍然答不出"
    assert row.run_id == RUN_A
    # 逐字段比对，不是「都有值」：事件与行各推一次早晚对不上，这里当场钉死同源。
    assert row.root_trace_id == attribution["root_thread_id"] == ROOT_THREAD
    assert row.parent_trace_id == attribution["parent_thread_id"] == ""
    assert row.agent_path == attribution["agent_path"] == "root"
    assert attribution["thread_id"] == row.trace_id == ROOT_THREAD
    assert attribution["depth"] == record["depth"] == 0


async def test_attribution_key_set_is_the_cross_language_contract(seeded, tmp_path):
    """字段名就是契约：取值方按 `ATTRIBUTION_KEYS` 这一组键读，改名不会报错，只会安静显示「暂无」。"""
    session = _parent(tmp_path)
    await _add(session, 100, 10)
    record = (await _usage_records())[0]
    assert set(record["attribution"]) == ATTRIBUTION_KEYS
    assert {"role", "trace_id", "depth", "agent_path", "call", "turn", "task", "attribution"} <= set(record)


async def test_missing_run_is_recorded_as_empty_not_backfilled_with_task_id(seeded, tmp_path):
    """没有轮 id 时不许拿 task_id 顶上：那是「没说」，不是「说了别的」。"""
    session = _parent(tmp_path, run_id="")
    await _add(session, 100, 10)
    row = (await _rows())[0]
    record = (await _usage_records())[0]
    assert row.run_id == "" and record["attribution"]["run_id"] == ""
    assert TASK_ID not in (row.run_id, record["attribution"]["run_id"])


async def test_run_id_is_read_from_the_recorder_not_a_second_copy(seeded, tmp_path):
    """轮 id 只有一个真源：改记录器，行与事件都跟着变。

    如果会话里另存一份 run_id，这条会红——两处副本迟早分家，到那时按轮汇总就不知道信谁。
    """
    session = _parent(tmp_path)
    session.recorder.run_id = RUN_B
    await _add(session, 100, 10)
    assert (await _rows())[0].run_id == RUN_B
    assert (await _usage_records())[0]["attribution"]["run_id"] == RUN_B


async def test_failed_call_still_carries_attribution(seeded, tmp_path):
    """失败那笔没花 token，但归属一个字都不能少——不然重试的花费永远归不了轮。"""
    session = _parent(tmp_path)
    await session._record_usage("execute_action", "", Usage(), 7, "error", role="coder")
    row = (await _rows())[0]
    record = (await _usage_records())[0]
    assert row.status == "error" and row.prompt_tokens == 0
    assert row.run_id == RUN_A and row.root_trace_id == ROOT_THREAD
    assert record["attribution"]["run_id"] == RUN_A


# --- 子代理：谁引起的要说得清 -----------------------------------------------------------------


async def test_subagent_row_keeps_parent_and_root_apart(seeded, tmp_path):
    """子线程的行上 root 是最初那条、parent 是派它那条；借用父线程 id 或漏传根都会被这条抓住。"""
    result = await _child(
        tmp_path, agent_path="root/sub-1", depth=1, parent_thread_id=ROOT_THREAD, root_thread_id=ROOT_THREAD
    )
    assert result.ok, result.error
    rows = [row for row in await _rows() if row.node == "subagent"]
    assert len(rows) == 1, "子代理不落账的话这一批就白做了"
    row = rows[0]
    assert row.parent_trace_id == ROOT_THREAD
    assert row.root_trace_id == ROOT_THREAD
    assert row.agent_path == "root/sub-1"
    assert row.run_id == RUN_A
    assert row.trace_id != ROOT_THREAD, "子代理是一条独立线程：借用父线程 id 就把两个 agent 的账混成一个人"
    record = (await _usage_records())[0]
    # 事件侧的归属由 _Recorder 注入，与行上那份是两条路：只钉行不钉事件，事件时间线就会自己漂。
    assert record["root_thread_id"] == ROOT_THREAD and record["parent_thread_id"] == ROOT_THREAD


async def test_runner_passes_root_down_and_labels_the_spawned_branch(seeded, tmp_path):
    """经 SubAgentRunner 派下去的那条：事件与行上的根都是最初那条线程，父是派它的这条。"""
    session = _parent(tmp_path, trace_id="thread-main")
    session.adapter = _OneShot("main")
    await _add(session, 900, 90)

    result = await SubAgentRunner().bind(session)(SubAgentRequest(goal="查一下 README", max_steps=2))
    assert result.agent_path == "root/sub-1", "分支路径由父会话一路拼出来"

    events = await _all_events()
    started = [item for item in events if item["type"] == "subagent.started"]
    assert started, "派子代理必须留一条 started 事件"
    assert started[0]["payload"]["root_thread_id"] == "thread-main"
    assert started[0]["payload"]["parent_thread_id"] == "thread-main"
    assert started[0]["run_id"] == RUN_A, "子代理与主代理同一轮：run_id 来自同一个记录器"

    kid_rows = [row for row in await _rows() if row.node == "subagent"]
    assert kid_rows, "子代理这一路也要落账（#33 定的口径），不然归属只存在于事件里"
    assert kid_rows[0].root_trace_id == "thread-main"
    assert kid_rows[0].parent_trace_id == "thread-main"
    assert kid_rows[0].run_id == RUN_A


async def test_nested_branch_root_stays_at_the_original_thread(seeded, tmp_path):
    """孙代理的根仍然是最初那条线程，父才是它的直接派生者——传一层就丢根，这条就红。"""
    await _child(
        tmp_path, agent_path="root/sub-1", depth=1, parent_thread_id=ROOT_THREAD, root_thread_id=ROOT_THREAD
    )
    await _child(
        tmp_path,
        agent_path="root/sub-1/sub-1",
        depth=2,
        parent_thread_id="thread-kid",
        root_thread_id=ROOT_THREAD,
    )
    rows = await _rows()
    assert len(rows) == 2
    assert {row.root_trace_id for row in rows} == {ROOT_THREAD}, "根必须一路传到底：写成自己的父等于把树切成一堆孤儿"
    assert {row.parent_trace_id for row in rows} == {ROOT_THREAD, "thread-kid"}, (
        "父必须是各自的直接派生者：把父也写成根，父子关系就没了"
    )


# --- 汇总：按轮与按分支 ----------------------------------------------------------------------


async def test_turn_rollup_separates_two_runs_of_the_same_task(seeded, tmp_path):
    """本轮解决的核心问题：同一任务重跑两次，两轮的钱要能分开算，也能合起来对得上总数。"""
    await _add(_parent(tmp_path, run_id=RUN_A), 500, 50)
    await _add(_parent(tmp_path, run_id=RUN_B, trace_id="thread-second"), 300, 30)

    rows = await _rows()
    turns = {item["run_id"]: item for item in turn_rollup(rows)}
    assert set(turns) == {RUN_A, RUN_B}
    assert turns[RUN_A]["calls"] == 1 and turns[RUN_A]["total_tokens"] == 550
    assert turns[RUN_B]["total_tokens"] == 330
    # 装置自检：两轮合计必须等于任务级求和，否则「分开算」只是把数切碎。
    assert sum(item["total_tokens"] for item in turns.values()) == sum(
        row.prompt_tokens + row.completion_tokens for row in rows
    )
    assert turn_rollup([]) == []


async def test_turn_rollup_leaves_unattributed_rows_out_and_says_so():
    """老账（没有轮 id）不许并进「第 0 轮」：它们只以条数出现，说清有多少笔归属未知。"""
    legacy = SimpleNamespace(
        run_id="",
        trace_id="thread-legacy",
        node="execute_action",
        prompt_tokens=700,
        completion_tokens=70,
        cached_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
    )
    modern = SimpleNamespace(
        run_id=RUN_A,
        trace_id=ROOT_THREAD,
        node="execute_action",
        prompt_tokens=100,
        completion_tokens=10,
        cached_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
    )
    turns = turn_rollup([legacy, modern])
    assert [item["run_id"] for item in turns] == [RUN_A]
    assert turns[0]["total_tokens"] == 110, "把老账混进来就是把「没说」算成了「说了」"
    assert sum(1 for row in (legacy, modern) if not str(row.run_id or "")) == 1


async def test_branch_rollup_names_the_cause_of_each_branch(seeded, tmp_path):
    """按线程汇总：主线程 parent 为空，子线程 parent/root/agent_path 都在行上，不必走事件流。"""
    session = _parent(tmp_path)
    await _add(session, 500, 50)
    await _child(
        tmp_path, agent_path="root/sub-1", depth=1, parent_thread_id=ROOT_THREAD, root_thread_id=ROOT_THREAD
    )
    branches = {item["trace_id"]: item for item in branch_rollup(await _rows())}
    assert len(branches) == 2
    main = branches[ROOT_THREAD]
    assert main["parent_thread_id"] == "" and main["root_thread_id"] == ROOT_THREAD
    assert main["agent_path"] == "root"
    kid = next(item for key, item in branches.items() if key != ROOT_THREAD)
    assert kid["parent_thread_id"] == ROOT_THREAD
    assert kid["root_thread_id"] == ROOT_THREAD
    assert kid["agent_path"] == "root/sub-1"
    assert main["calls"] + kid["calls"] == len(await _rows())


async def test_sole_refuses_to_pick_when_the_value_drifts():
    """同一条线程出现两个父/根/路径时给空串，不随便挑一个冒充整条分支的出身。"""
    assert _sole(["a", "a", ""]) == "a"
    assert _sole(["", ""]) == ""
    assert _sole(["a", "b"]) == ""


async def test_evidence_pack_exposes_turns_branches_and_honest_gap(seeded, tmp_path):
    """证据包的三个新键在册，且与账本行同源：漏算、重算、倒推都会被跨账自检抓住。"""
    session = _parent(tmp_path)
    await _add(session, 500, 50)
    await _add(session, 300, 30)
    usage = (await build_evidence(_factory(), TASK_ID))["usage"]
    assert {"turns", "branches", "unattributed_calls"} <= set(usage)
    assert usage["unattributed_calls"] == 0
    assert len(usage["turns"]) == 1 and usage["turns"][0]["run_id"] == RUN_A
    assert usage["turns"][0]["calls"] == 2
    assert usage["turns"][0]["total_tokens"] == usage["total_tokens"], (
        "按轮合计与任务总量必须相等：不等就是有一头算漏了"
    )
    assert usage["turns"][0]["kind"] == "run"
    assert usage["branches"][0]["trace_id"] == ROOT_THREAD
    assert set(usage["turns"][0]) == {
        "run_id",
        "kind",
        "threads",
        "subagent_calls",
        "calls",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
    }


async def test_legacy_rows_show_as_a_count_and_stay_in_the_task_total(seeded, tmp_path):
    """老账进证据包只贡献一个条数：任务总量里有它的钱，按轮的表里却一格都没有。

    这两处对不上是**对的**，`unattributed_calls` 就是那句解释。把老账合成一轮、或者干脆
    不算进任务总量，都会让「按轮合计 = 任务总量」这句自检变成谎话——前者把没记过的说成
    记过，后者少报钱。
    """
    await _add(_parent(tmp_path), 500, 50)
    async with _factory()() as db:
        db.add(
            ModelUsage(
                id="usage-legacy-1",
                provider_id=PROVIDER_ID,
                task_id=TASK_ID,
                trace_id="thread-legacy",
                node="execute_action",
                model="mock",
                prompt_tokens=700,
                completion_tokens=70,
            )
        )
        await db.commit()

    usage = (await build_evidence(_factory(), TASK_ID))["usage"]
    assert usage["unattributed_calls"] == 1
    assert [item["run_id"] for item in usage["turns"]] == [RUN_A], "老账不许冒充一轮"
    assert usage["turns"][0]["total_tokens"] == 550
    assert usage["total_tokens"] == 1320, "任务总量必须连老账一起算：漏了它就是少报钱"
    assert usage["total_tokens"] - usage["turns"][0]["total_tokens"] == 770


async def test_a_run_without_a_run_ledger_still_gets_a_turn_id(seeded, tmp_path, monkeypatch):
    """入口没带 run 台账（同步 /run 就是这样）时，轮 id 退回这条线程的 attempt 键。

    真模型点验第一次跑出来的实物：7 行账的 run_id 全空、证据包的 turns 表 0 条——
    「这一轮花了多少」在真链路上仍然答不出。留空不是"少一个字段"，是整批钱筛不出来。
    """
    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    thread_id = f"task:{TASK_ID}:attempt:3"
    runtime = AgentRuntime(session_factory=_factory())

    def request(run_id: str = "") -> RunRequest:
        return RunRequest(
            task_id=TASK_ID,
            provider_id=PROVIDER_ID,
            provider_name="deepseek",
            adapter=_OneShot("mock"),
            root=tmp_path,
            goal="回合归属",
            max_steps=4,
            run_id=run_id,
        )

    scope = await runtime._prepare(request(), thread_id)
    assert scope.recorder.run_id == thread_id, "入口没给轮 id 就必须现取一个能筛出来的，不许留空"
    assert scope.session.run_id == thread_id, "会话读的必须还是记录器那一份，不许另造"

    await scope.session._record_usage("execute_action", "mock", Usage(prompt_tokens=120, completion_tokens=24), 9, "ok")
    row = (await _rows())[0]
    record = (await _usage_records())[0]
    assert row.run_id == thread_id and record["attribution"]["run_id"] == thread_id
    assert turn_rollup([row])[0]["total_tokens"] == 144, "有轮 id 才谈得上按轮汇总"

    given = await runtime._prepare(request(run_id=RUN_B), thread_id)
    assert given.recorder.run_id == RUN_B, "入口给了就用入口的：那是 run 台账里那一行，能和 AgentRun 对上"
