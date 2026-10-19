"""按需选模型（门控路由）。

要证明四件事，缺一件这个特性就不该开：

1. 打分是纯函数、可解释 —— 同样的状态永远同一个结果，每一分都能说出从哪来；
2. 够用优先、便宜其次 —— 用最便宜的那个，但绝不是「先挑最便宜再祈祷够用」；
3. 失败会升级 —— 工具真跑失败才提档，被用户驳回、被钩子拦下不算；
4. 关掉就完全不变 —— 只有一个候选或没有候选时，行为和单模型一模一样。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.db import get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import AgentSession, execute_action_node
from blank_api.services.agent.router import (
    HEAVY,
    LIGHT,
    MAX_ESCALATION,
    STANDARD,
    Candidate,
    Router,
    assess,
    candidate_from_provider,
    cost_of,
    pick,
    tier_for_complexity,
    tier_for_params,
)
from blank_api.services.hooks import HookResult
from blank_api.services.models.base import AdapterConfig, ModelAdapter
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome
from blank_api.services.tools.gateway import ToolGateway


# --------------------------------------------------------------------------- 打分


def test_assess_is_deterministic_and_explains_itself():
    first = assess(node="execute_action", step=3, context_tokens=40_000)
    second = assess(node="execute_action", step=3, context_tokens=40_000)

    assert first == second
    assert first.complexity == second.complexity
    # 每一分都要有出处：出问题时用户看到的是事件流，不是源码
    assert first.notes
    assert any("节点" in note for note in first.notes)
    assert any("上下文" in note for note in first.notes)


def test_short_step_with_small_context_stays_light():
    demand = assess(node="execute_action", step=1, context_tokens=6_000)

    # 便宜的模型必须真的有机会被选中，否则这个特性只是装饰
    assert demand.tier == LIGHT
    assert demand.complexity < 40


def test_review_is_heavier_than_execution():
    review = assess(node="review_result", step=1, context_tokens=1_000)
    execute = assess(node="execute_action", step=1, context_tokens=1_000)

    assert TIER_rank(review.tier) > TIER_rank(execute.tier)


def TIER_rank(tier: str) -> int:
    return {LIGHT: 1, STANDARD: 2, HEAVY: 3}[tier]


def test_long_context_and_late_steps_raise_the_demand():
    easy = assess(node="execute_action", step=1, context_tokens=2_000)
    hard = assess(node="execute_action", step=25, context_tokens=120_000)

    assert hard.complexity > easy.complexity
    assert TIER_rank(hard.tier) > TIER_rank(easy.tier)


def test_pure_chat_needs_less_than_tool_work():
    with_tools = assess(node="execute_action", step=1, needs_tools=True)
    chat = assess(node="execute_action", step=1, needs_tools=False)

    assert chat.complexity < with_tools.complexity


def test_failure_bumps_exactly_one_tier():
    before = assess(node="execute_action", step=1)
    after = assess(node="execute_action", step=1, escalated=1)

    assert TIER_rank(after.tier) == TIER_rank(before.tier) + 1


def test_complexity_is_clamped():
    # 满级失败 + 超长上下文也不能溢出 100，否则日志里会出现 137 分这种没法解释的数
    demand = assess(node="review_result", step=99, context_tokens=10_000_000, escalated=MAX_ESCALATION)
    assert demand.complexity == 100


def test_tiers_have_hard_boundaries():
    assert tier_for_complexity(39) == LIGHT
    assert tier_for_complexity(40) == STANDARD
    assert tier_for_complexity(69) == STANDARD
    assert tier_for_complexity(70) == HEAVY


# ----------------------------------------------------------------- 从参数量推能力


def test_param_tiers():
    assert tier_for_params(8, 8) == LIGHT
    assert tier_for_params(671, 37) == STANDARD  # 激活 37B 的稀疏模型
    assert tier_for_params(405, 405) == HEAVY
    assert tier_for_params(700, 150) == HEAVY


def test_unknown_params_are_not_treated_as_cheap():
    """没填参数量不能被当成「很小很便宜」。

    否则路由会把难任务发给一个没人知道多大的模型，而且事后从事件里看不出原因。
    """
    assert tier_for_params(0, 0) == STANDARD
    assert cost_of(0, 0, STANDARD) > cost_of(8, 8, LIGHT)


def test_cost_prefers_active_params():
    # 单价与激活量近似成正比：671B 总参数但只激活 37B，比总参数便宜得多
    assert cost_of(671, 37, STANDARD) == 37
    assert cost_of(671, 0, STANDARD) == 671


# ------------------------------------------------------------------------- 选择


def _pool() -> list[Candidate]:
    return [
        Candidate("local", "本机 7B", LIGHT, 7.0),
        Candidate("cloud", "云端 671/37", STANDARD, 37.0),
        Candidate("giant", "巨型 405B", HEAVY, 405.0),
    ]


def test_pick_takes_the_cheapest_sufficient_one():
    demand = assess(node="execute_action", step=1)  # 轻量档
    decision = pick(demand, _pool())

    assert decision.provider_id == "local"
    assert decision.routed


def test_pick_never_sends_a_hard_step_to_a_weak_model():
    demand = assess(node="review_result", step=1, context_tokens=80_000)
    assert demand.tier == HEAVY

    decision = pick(demand, _pool())

    # 「路由错了比不路由更糟」：够用是第一约束，便宜是第二约束
    assert decision.tier == HEAVY
    assert decision.provider_id == "giant"


def test_pick_explains_why_each_loser_lost():
    demand = assess(node="review_result", step=1)
    decision = pick(demand, _pool())

    rejected = dict(decision.rejected)
    assert "本机 7B" in rejected
    assert "不够" in rejected["本机 7B"]
    assert "巨型 405B" in rejected
    assert "更贵" in rejected["巨型 405B"]


def test_pick_skips_models_without_tool_support():
    pool = [
        Candidate("chat-only", "纯聊天模型", LIGHT, 1.0, supports_tools=False),
        Candidate("coder", "会调工具的", LIGHT, 9.0),
    ]
    decision = pick(assess(node="execute_action", step=1, needs_tools=True), pool)

    assert decision.provider_id == "coder"
    assert "不支持工具调用" in dict(decision.rejected)["纯聊天模型"]


def test_pick_marks_shortfall_when_the_whole_pool_is_too_weak():
    demand = assess(node="review_result", step=25, context_tokens=200_000)
    assert demand.tier == HEAVY

    decision = pick(demand, [Candidate("local", "本机 7B", LIGHT, 7.0)])

    # 仍然要选一个（否则会退回默认模型，而默认未必是池子里最强的），
    # 但必须标明「不够用」，免得事后有人以为是路由判错
    assert decision.provider_id == "local"
    assert decision.shortfall is True
    assert "没有达到" in decision.reason


def test_pick_is_reproducible_on_ties():
    pool = [
        Candidate("b", "beta", LIGHT, 7.0),
        Candidate("a", "alpha", LIGHT, 7.0),
    ]
    decision = pick(assess(node="execute_action", step=1), pool)

    # 同档同价时按名字定序：不定序会让测试随机飘，也让线上行为不可复现
    assert decision.name == "alpha"


def test_empty_pool_keeps_the_default_model():
    decision = pick(assess(node="execute_action"), [])
    assert decision.routed is False
    assert decision.provider_id == ""


def test_decision_serializes_for_the_event_stream():
    decision = pick(assess(node="execute_action", step=2), _pool())
    payload = decision.as_dict()

    assert payload["complexity"] >= 0
    assert payload["rejected"]
    assert {"name", "why"} <= set(payload["rejected"][0])
    assert payload["shortfall"] is False
    # 成本当量要和 rejected 里那些数字用同一把尺子，否则"省了多少"根本没法比较
    assert payload["cost"] == 7


# --------------------------------------------------------------------- 路由器状态


def test_router_with_one_candidate_is_inactive():
    router = Router(pool=(Candidate("only", "唯一", STANDARD, 50.0),))
    assert router.active is False


def test_router_ignores_candidates_without_provider_id():
    router = Router(pool=(Candidate("", "", STANDARD, 1.0), Candidate("a", "甲", STANDARD, 1.0)))
    assert router.active is False


def test_escalation_is_sticky_and_capped():
    router = Router(pool=tuple(_pool()))
    for _ in range(10):
        router.note_failure("execute_action")

    # 粘性 + 封顶：来回抖动（升级又降回）比一直用强模型还贵，而且事件里看不出发生过什么
    assert router.escalation("execute_action") == MAX_ESCALATION


def test_escalation_changes_the_chosen_model():
    router = Router(pool=tuple(_pool()))
    cheap = router.choose(node="execute_action", step=1)
    assert cheap.tier == LIGHT

    router.note_failure("execute_action")
    upgraded = router.choose(node="execute_action", step=1)

    assert upgraded.tier == STANDARD
    assert upgraded.name == "云端 671/37"


def test_escalation_is_per_node():
    router = Router(pool=tuple(_pool()))
    router.note_failure("execute_action")

    assert router.choose(node="execute_action", step=1).tier == STANDARD
    # 别的节点不该被连带提档
    assert router.choose(node="review_result", step=1).tier == STANDARD


def test_history_is_bounded():
    router = Router(pool=tuple(_pool()))
    for _ in range(120):
        router.choose(node="execute_action", step=1)

    # 长任务跑几百步不该把内存吃掉
    assert len(router.history) == 50


def test_summary_measures_what_routing_saved():
    router = Router(pool=tuple(_pool()))
    decision = router.choose(node="execute_action", step=1)
    router.note_decision(decision, switched=True)

    summary = router.summary()

    assert summary["calls"] == 1
    assert summary["switched"] == 1
    # 对照值是池子里最贵的那个（405），实际花了 7 —— 不比这一个数，
    # 「省了多少」就只能靠嘴说
    assert summary["cost_units"] == 7
    assert summary["baseline_units"] == 405
    assert 0.9 < summary["saved_ratio"] < 1.0


def test_summary_is_empty_without_decisions():
    # 一次都没路由过就返回空：收尾里多一个全是 0 的字段只会让人以为路由跑过了
    assert Router(pool=tuple(_pool())).summary() == {}


def test_candidate_from_provider_tolerates_old_rows():
    row = SimpleNamespace(id="p1", name="老库", total_params_b=None, active_params_b=None, supports_tools=True)
    candidate = candidate_from_provider(row)

    assert candidate.tier == STANDARD
    assert candidate.supports_tools is True


# ----------------------------------------------------------- 接入执行链路（真事件）


TASK_ID = "task-router-1"


class FakeAdapter(ModelAdapter):
    def __init__(self, name: str) -> None:
        super().__init__(AdapterConfig(name=name, kind="openai_compatible", base_url="", api_key="", model=name, supports_tools=True))

    async def invoke(self, messages, **kwargs):  # pragma: no cover - 本用例不调模型
        raise AssertionError("路由不该触发真实模型调用")

    async def stream(self, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def bind_tools(self, tools, messages, **kwargs):  # pragma: no cover
        raise AssertionError("路由不该触发真实模型调用")


def _empty_schema() -> dict:
    return {"type": "object", "properties": {}, "additionalProperties": False}


def _failing_tool(name: str) -> ToolDefinition:
    async def handler(context: ToolContext, arguments: dict) -> ToolOutcome:
        return ToolOutcome(ok=False, status="error", message="磁盘上没有这个路径")

    return ToolDefinition(
        name=name,
        description="注定失败的测试工具",
        input_schema=_empty_schema(),
        risk_level="low",
        handler=handler,
        timeout_seconds=5.0,
    )


def _permission_tool(name: str) -> ToolDefinition:
    async def handler(context: ToolContext, arguments: dict) -> ToolOutcome:  # pragma: no cover
        raise AssertionError("被拦下的工具不该被执行")

    return ToolDefinition(
        name=name,
        # 风险等级必须是 low：high 会先走到审批门去 interrupt，在单测里没有
        # runnable context 可用。这里的重点是「refuse 分支不提档」，不是审批本身。
        description="会被钩子拦下的测试工具",
        input_schema=_empty_schema(),
        risk_level="low",
        handler=handler,
        timeout_seconds=5.0,
    )


class NoHooks:
    async def run(self, event: str, tool: str = "", payload: dict | None = None):
        return []


class DenyAllHooks(NoHooks):
    async def run(self, event: str, tool: str = "", payload: dict | None = None):
        if event == "pre_tool_use":
            return [HookResult(event=event, command=tool, status="ok", decision="deny", reason="测试拦截")]
        return []


async def _seed_task(workspace: Path) -> None:
    async with get_session_factory()() as session:
        session.add(Project(id="project-router", name="路由测试"))
        session.add(
            Task(id=TASK_ID, project_id="project-router", title="路由", goal="路由", max_steps=10)
        )
        await session.commit()


@pytest.fixture
async def seeded(tmp_path):
    from blank_api.db import Base, get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await _seed_task(tmp_path)


def _session(tmp_path: Path, tools: list[ToolDefinition], router: Router | None) -> AgentSession:
    # 主供应商选「云端」：这样「轻量档该用本机模型」才有可观察的切换，
    # 否则路由选中的就是默认模型本身，switched 永远是 False，测了个寂寞。
    primary = FakeAdapter("云端 671/37")
    session = AgentSession(
        adapter=primary,
        gateway=ToolGateway(tools),
        context=ToolContext(
            root=tmp_path,
            task_id=TASK_ID,
            session_factory=get_session_factory(),
            approval_policy=lambda *_: False,
        ),
        recorder=EventRecorder(get_session_factory(), TASK_ID),
        session_factory=get_session_factory(),
        provider_id="cloud",
        provider_name="云端 671/37",
        goal="路由",
        max_steps=10,
        router=router,
        routing_adapters={
            "local": FakeAdapter("本机 7B"),
            "cloud": primary,
            "giant": FakeAdapter("巨型 405B"),
        },
    )
    session.hooks = NoHooks()  # type: ignore[assignment]

    async def fake_ask(state, prompt, node, *, messages=None):
        # 本用例只验证「失败后有没有提档」，下一步的模型调用直接给个假回复
        return {
            "text": "完成",
            "tool_calls": [],
            "usage": {},
            "messages": list(messages or []) + [{"role": "assistant", "content": "完成"}],
        }

    session.ask = fake_ask  # type: ignore[method-assign]
    return session


async def _events(kind: str) -> list[dict]:
    async with get_session_factory()() as session:
        rows = (await session.execute(AgentEvent.__table__.select())).fetchall()
    return [row.payload for row in rows if row.type == kind]


def _call(name: str) -> dict:
    return {"id": f"call_{name}", "type": "function", "function": {"name": name, "arguments": "{}"}}


async def test_routing_switches_the_adapter_and_says_why(seeded, tmp_path):
    router = Router(pool=tuple(_pool()))
    session = _session(tmp_path, [], router)

    chosen = await session._routed_adapter("execute_action", 1, session.adapter)

    assert chosen.config.name == "本机 7B"
    payloads = await _events("model.routed")
    assert len(payloads) == 1
    assert payloads[0]["switched"] is True
    assert payloads[0]["tier"] == LIGHT
    # 成本当量要落到事件里：用户看到"换了模型"，也得能看出便宜了多少
    assert payloads[0]["cost"] == 7
    # 淘汰原因必须落到事件里，否则用户只能看到一个黑盒换模型
    assert payloads[0]["rejected"]


async def test_routing_without_an_adapter_falls_back_to_the_default(seeded, tmp_path):
    router = Router(
        pool=(Candidate("ghost", "未装配的供应商", LIGHT, 1.0), Candidate("cloud", "云端", STANDARD, 37.0))
    )
    session = _session(tmp_path, [], router)
    session.routing_adapters.pop("local", None)

    chosen = await session._routed_adapter("execute_action", 1, session.adapter)

    # 宁可放弃省钱，也不能把任务发给一个没有适配器的供应商
    assert chosen.config.name == "云端 671/37"
    assert await _events("model.routed") == []


async def test_tool_failure_escalates_the_next_call(seeded, tmp_path):
    router = Router(pool=tuple(_pool()))
    session = _session(tmp_path, [_failing_tool("read_file")], router)

    await execute_action_node(
        {"messages": [{"role": "user", "content": "读文件"}], "steps": 0, "pending": [_call("read_file")]},
        session,
    )

    assert router.escalation("execute_action") == 1
    payloads = await _events("model.escalation")
    assert payloads and payloads[0]["tools"] == ["read_file"]

    upgraded = await session._routed_adapter("execute_action", 1, session.adapter)
    assert upgraded.config.name == "云端 671/37"


async def test_refused_tool_does_not_escalate(seeded, tmp_path):
    """被拦下的调用不提档。

    「被驳回 / 被钩子拦下」是策略判断，不是模型不够强。为它换更强的模型，
    等于拿钱重试用户刚刚说「不」的事——而且更强模型重做一遍也还是会被拦。
    """
    router = Router(pool=tuple(_pool()))
    session = _session(tmp_path, [_permission_tool("write_file")], router)
    session.hooks = DenyAllHooks()  # type: ignore[assignment]

    await execute_action_node(
        {"messages": [{"role": "user", "content": "写文件"}], "steps": 0, "pending": [_call("write_file")]},
        session,
    )

    assert router.escalation("execute_action") == 0
    assert await _events("model.escalation") == []


async def test_no_router_means_no_routing_events(seeded, tmp_path):
    """关掉开关（router=None）时，链路里不该出现任何路由痕迹。"""
    session = _session(tmp_path, [_failing_tool("read_file")], None)

    await execute_action_node(
        {"messages": [{"role": "user", "content": "读文件"}], "steps": 0, "pending": [_call("read_file")]},
        session,
    )

    assert await _events("model.routed") == []
    assert await _events("model.escalation") == []
