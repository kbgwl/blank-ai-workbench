"""用量三级累计 `model.usage`：对标 Codex rollout 里的 `token_usage_record`。

Codex 那条一次写三个口径（单次 / 回合 / 线程），为的是「这一轮花了多少」和
「这个任务累计花了多少」都是**查出来的事实**，而不是读出时求和推出来的猜测。
要把这件事证扎实，得同时管住六个方向：

1. 每次调用恰好一条事件，且 call/turn/task 三级齐全、数值单调（call ⊆ turn ⊆ task）；
2. 一次调用只落一行 —— 事件数必须等于表里的行数（双发会伪装成"调用了很多次"）；
3. 缓存与推理单独成字段并被搬运（否则"打开缓存"永远只是我们自述）；
4. 任务级跨会话：子代理、摘要、审查员写同一张表，也要进同一个任务口径；
   回合级不跨会话：换一个 AgentSession 就从零开始；
5. 失败的调用同样进账（一次请求根本没花 token，只有这条记录说得出）；
6. 证据包与事件对得上，且上线前的老任务是 None，不补一个像是算过的数。
"""

from types import SimpleNamespace

import pytest

from blank_api.db import get_session_factory
from blank_api.models.model_provider import ModelUsage
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import AgentSession
from blank_api.services.agent.evidence import build_evidence
from blank_api.services.models.base import (
    AdapterConfig,
    ModelAdapter,
    ModelError,
    ModelResponse,
    Usage,
)
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway

TASK_ID = "task-usage-1"
LEVEL_KEYS = {"prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens", "reasoning_tokens", "total_tokens", "calls"}


class ScriptedAdapter(ModelAdapter):
    """按脚本返回用量；None 表示这一次调用直接失败。"""

    def __init__(self, name: str, script: list[Usage | None]) -> None:
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
        return ModelResponse(text="好", usage=item, model=self.config.name)


async def _seed() -> None:
    from blank_api.db import Base, get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="project-usage", name="用量三级"))
        session.add(Task(id=TASK_ID, project_id="project-usage", title="用量", goal="用量", max_steps=10))
        await session.commit()


@pytest.fixture
async def seeded():
    await _seed()


def _session(tmp_path, adapter: ModelAdapter, *, provider_name: str = "mock") -> AgentSession:
    factory = get_session_factory()
    return AgentSession(
        adapter=adapter,
        gateway=ToolGateway([]),
        context=ToolContext(root=tmp_path, task_id=TASK_ID, session_factory=factory),
        recorder=EventRecorder(factory, TASK_ID),
        session_factory=factory,
        provider_id="provider-1",
        provider_name=provider_name,
        goal="用量",
        max_steps=10,
    )


async def _records() -> list[dict]:
    async with get_session_factory()() as session:
        rows = (await session.execute(AgentEvent.__table__.select())).fetchall()
    return [row.payload for row in rows if row.type == "model.usage"]


async def _rows() -> list[ModelUsage]:
    async with get_session_factory()() as session:
        return list((await session.execute(ModelUsage.__table__.select())).fetchall())


async def test_each_call_emits_exactly_one_record_with_three_levels(seeded, tmp_path):
    adapter = ScriptedAdapter(
        "mock",
        [
            Usage(prompt_tokens=1000, completion_tokens=100, cached_tokens=800, reasoning_tokens=40),
            Usage(prompt_tokens=500, completion_tokens=50),
        ],
    )
    session = _session(tmp_path, adapter)
    await session.call_model([{"role": "user", "content": "第一步"}], "execute_action", step=1)
    await session.call_model([{"role": "user", "content": "第二步"}], "review_result", step=2)

    records = await _records()
    assert len(records) == 2, "一次调用一条记录；两条以上就把同一笔账重复计了"
    assert all({"call", "turn", "task"} <= set(item) for item in records)
    first, second = records
    assert first["call"]["prompt_tokens"] == 1000
    assert first["call"]["cached_tokens"] == 800
    assert first["call"]["reasoning_tokens"] == 40
    assert first["call"]["total_tokens"] == 1100
    assert first["call"]["calls"] == 1
    assert first["call"]["status"] == "ok"
    assert first["call"]["model"] == "mock" and first["call"]["provider"] == "mock"
    # 第一次调用时回合级=单次级（本会话才跑了一步），第二次才看得出两者各自累计的边界
    assert {key: first["turn"][key] for key in LEVEL_KEYS} == {key: first["call"][key] for key in LEVEL_KEYS}
    assert second["call"]["prompt_tokens"] == 500
    assert second["turn"]["prompt_tokens"] == 1500
    assert second["turn"]["completion_tokens"] == 150
    assert second["turn"]["cached_tokens"] == 800
    assert second["turn"]["calls"] == 2
    assert second["task"]["prompt_tokens"] == 1500
    assert second["task"]["calls"] == 2


async def test_levels_are_monotonic_and_event_count_equals_row_count(seeded, tmp_path):
    """三级是包含关系，不是三套各自算的账；事件数必须等于落库行数。"""
    adapter = ScriptedAdapter(
        "mock",
        [
            Usage(prompt_tokens=100, completion_tokens=10, cached_tokens=60),
            Usage(prompt_tokens=200, completion_tokens=20, cached_tokens=200),
            Usage(prompt_tokens=300, completion_tokens=30),
        ],
    )
    session = _session(tmp_path, adapter)
    for step in range(3):
        await session.call_model([{"role": "user", "content": f"第 {step} 步"}], "execute_action", step=step)
    records = await _records()
    rows = await _rows()
    assert len(records) == len(rows) == 3, (
        f"事件 {len(records)} 条 vs 表 {len(rows)} 行：不相等就说明有一侧在双发或漏发"
    )
    for item in records:
        call, turn, task = item["call"], item["turn"], item["task"]
        for key in LEVEL_KEYS - {"calls"}:
            assert call[key] <= turn[key] <= task[key], f"{key} 三级不单调：{call[key]} / {turn[key]} / {task[key]}"
        assert call["calls"] == 1 and turn["calls"] <= task["calls"]
        assert task["total_tokens"] == task["prompt_tokens"] + task["completion_tokens"]
    assert records[-1]["task"]["cached_tokens"] == 260
    assert records[-1]["turn"]["cached_tokens"] == 260


async def test_task_level_spans_sessions_turn_level_does_not(seeded, tmp_path):
    """子代理往同一张表写，所以任务级必须把它算进来；
    回合级属于"这一个 AgentSession"，换会话就得从零开始。"""
    root_adapter = ScriptedAdapter("mock", [Usage(prompt_tokens=1000, completion_tokens=100)])
    root = _session(tmp_path, root_adapter)
    await root.call_model([{"role": "user", "content": "根"}], "execute_action", step=0)

    child_adapter = ScriptedAdapter("mock", [Usage(prompt_tokens=700, completion_tokens=70, cached_tokens=700)])
    child = _session(tmp_path, child_adapter)
    child.depth = 1
    child.agent_path = "root/researcher"
    await child.call_model([{"role": "user", "content": "子"}], "execute_action", step=0)

    child_record = (await _records())[-1]
    assert child_record["depth"] == 1
    assert child_record["agent_path"] == "root/researcher"
    # 回合级：子代理自己只跑了一次，不能把根代理那 1000 算成它的
    assert child_record["turn"]["prompt_tokens"] == 700
    assert child_record["turn"]["calls"] == 1
    # 任务级：同一个 task_id 的全部调用，跨会话合计
    assert child_record["task"]["prompt_tokens"] == 1700
    assert child_record["task"]["completion_tokens"] == 170
    assert child_record["task"]["cached_tokens"] == 700
    assert child_record["task"]["calls"] == 2

    root_next = ScriptedAdapter("mock", [Usage(prompt_tokens=1, completion_tokens=1)])
    root2 = _session(tmp_path, root_next)
    await root2.call_model([{"role": "user", "content": "根再一步"}], "execute_action", step=1)
    record = (await _records())[-1]
    assert record["turn"]["calls"] == 1, "回合级跨会话累计 = 把整个任务的账当成这一步的账"
    assert record["task"]["calls"] == 3


async def test_failed_call_is_recorded_and_counted(seeded, tmp_path):
    """一次请求根本没花 token，只有这条记录说得出「发生过、失败了」。"""
    adapter = ScriptedAdapter("mock", [None])
    session = _session(tmp_path, adapter)
    with pytest.raises(ModelError):
        await session.call_model([{"role": "user", "content": "炸"}], "execute_action", step=0)
    records = await _records()
    assert len(records) == 1
    assert records[0]["call"]["status"] == "error"
    assert records[0]["call"]["prompt_tokens"] == 0
    assert records[0]["call"]["latency_ms"] >= 0
    assert records[0]["turn"]["calls"] == 1
    assert records[0]["task"]["calls"] == 1
    assert records[0]["task"]["total_tokens"] == 0
    assert len(await _rows()) == 1, "失败调用只该有事件没有行：那笔请求根本没发生成功，但账还是要落"


async def test_summarizer_and_reviewer_paths_emit_their_own_record(seeded, tmp_path):
    """非模型主路径（摘要、审查员）走的是 public record_usage，也必须进同一条账。"""
    session = _session(tmp_path, ScriptedAdapter("mock", []))
    await session.record_usage(
        "context_compact", "summarizer-model", Usage(prompt_tokens=400, completion_tokens=80, cached_tokens=200), 12, "ok", role="summarizer"
    )
    await session.record_usage(
        "review_result", "guardian-model", Usage(prompt_tokens=300, completion_tokens=20, reasoning_tokens=15), 8, "ok", role="guardian"
    )
    records = await _records()
    assert [item["role"] for item in records] == ["summarizer", "guardian"]
    assert records[0]["call"]["model"] == "summarizer-model"
    assert records[1]["call"]["reasoning_tokens"] == 15
    assert records[1]["turn"]["cached_tokens"] == 200
    assert records[1]["turn"]["calls"] == 2
    assert records[1]["task"]["prompt_tokens"] == 700


async def test_evidence_carries_cache_reasoning_and_levels(seeded, tmp_path):
    adapter = ScriptedAdapter(
        "mock",
        [
            Usage(prompt_tokens=1000, completion_tokens=100, cached_tokens=750, reasoning_tokens=40),
            Usage(prompt_tokens=500, completion_tokens=50, cached_tokens=500),
        ],
    )
    session = _session(tmp_path, adapter)
    await session.call_model([{"role": "user", "content": "一"}], "execute_action", step=0)
    await session.call_model([{"role": "user", "content": "二"}], "review_result", step=1)
    usage = (await build_evidence(get_session_factory(), TASK_ID))["usage"]
    assert usage["prompt_tokens"] == 1500
    assert usage["completion_tokens"] == 150
    assert usage["total_tokens"] == 1650
    assert usage["cached_tokens"] == 1250
    assert usage["reasoning_tokens"] == 40
    assert usage["cache_hit_ratio"] == round(1250 / 1500, 4)
    assert usage["calls"] == 2
    assert usage["records"] == 2
    levels = usage["levels"]
    assert set(levels) == {"call", "turn", "task"}
    last = (await _records())[-1]
    assert levels["call"] == last["call"]
    assert levels["turn"] == last["turn"]
    assert levels["task"] == last["task"]
    # 证据包求和（DB 行）与事件里的任务级必须指向同一批行，不能各算各的
    assert levels["task"]["prompt_tokens"] == usage["prompt_tokens"]
    assert levels["task"]["calls"] == usage["calls"]


async def test_evidence_on_an_old_task_does_not_invent_levels(seeded, tmp_path):
    """三级记录上线前的任务：表里有行（求和照旧），但没有事件，levels 只能是 None。"""
    async with get_session_factory()() as session:
        session.add(
            ModelUsage(
                id="legacy-row-1",
                provider_id="provider-1",
                task_id=TASK_ID,
                node="execute_action",
                model="mock",
                prompt_tokens=100,
                completion_tokens=10,
                latency_ms=5,
                status="ok",
            )
        )
        await session.commit()
    usage = (await build_evidence(get_session_factory(), TASK_ID))["usage"]
    assert usage["levels"] is None
    assert usage["records"] == 0
    assert usage["prompt_tokens"] == 100
    assert usage["cached_tokens"] == 0
    assert usage["cache_hit_ratio"] == 0.0


async def test_cache_fields_are_carried_into_the_table(seeded, tmp_path):
    """§3 的落库那半：缓存/推理字段没进表，命中率就永远是 0，「缓存生效」无从证明。"""
    adapter = ScriptedAdapter(
        "mock", [Usage(prompt_tokens=2000, completion_tokens=200, cached_tokens=1900, cache_write_tokens=100, reasoning_tokens=90)]
    )
    session = _session(tmp_path, adapter)
    await session.call_model([{"role": "user", "content": "缓存"}], "execute_action", step=0)
    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].cached_tokens == 1900
    assert rows[0].cache_write_tokens == 100
    assert rows[0].reasoning_tokens == 90


async def test_the_wire_contract_is_what_the_panel_reads(seeded, tmp_path):
    """字段名就是跨语言契约：前端 `web/src/components/tasks/api.ts` 的 normalizeUsage
    按这些键取值，改名不会报错，只会安静地显示「暂无」。所以钉在这里。"""
    adapter = ScriptedAdapter("mock", [Usage(prompt_tokens=10, completion_tokens=1, cached_tokens=4, reasoning_tokens=2)])
    session = _session(tmp_path, adapter, provider_name="deepseek")
    await session.call_model([{"role": "user", "content": "契约"}], "execute_action", step=0)
    # 不走路由时 provider 取自适配器（真正的调用方），显式不传时退回会话声明的供应商名
    await session.record_usage("context_compact", "summarizer-model", Usage(prompt_tokens=1, completion_tokens=1), 3, "ok")
    records = await _records()
    assert {"role", "trace_id", "depth", "agent_path", "call", "turn", "task"} <= set(records[0])
    assert {"model", "provider", "latency_ms", "status"} <= set(records[0]["call"])
    assert records[0]["call"]["provider"] == "mock"
    assert records[0]["call"]["model"] == "mock"
    assert records[1]["call"]["provider"] == "deepseek", "退回会话供应商名：不然摘要那一笔挂在哪个账号上都说不清"
    assert LEVEL_KEYS <= set(records[0]["call"])
    assert LEVEL_KEYS <= set(records[0]["task"])
    usage = (await build_evidence(get_session_factory(), TASK_ID))["usage"]
    assert {
        "providers",
        "models",
        "trace_ids",
        "calls",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "cache_hit_ratio",
        "records",
        "levels",
        "latency_ms",
    } <= set(usage)


async def test_task_totals_fall_back_to_turn_when_task_is_unbound(seeded, tmp_path):
    """没有 task_id 的会话（离线跑图、评测适配器）不能查出一张空表就算总数：
    那时唯一诚实的任务级口径就是本回合自己。"""
    adapter = ScriptedAdapter("mock", [Usage(prompt_tokens=100, completion_tokens=10)])
    session = _session(tmp_path, adapter)
    session.context = SimpleNamespace(root=session.context.root, task_id="", session_factory=session.session_factory)
    await session.call_model([{"role": "user", "content": "无任务"}], "execute_action", step=0)
    record = (await _records())[0]
    assert record["task"] == record["turn"]
    assert record["task"]["prompt_tokens"] == 100
