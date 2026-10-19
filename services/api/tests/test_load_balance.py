"""负载均衡：同档同价时按近期表现轮换，而不是永远压在名字最靠前的那个身上。

要证明四件事，缺一件这个特性就不该开：

1. **不改「选哪一档」和「花多少钱」。** 轮换只发生在可以互换的候选之间——
   同档位、同成本。一旦它能影响档位或价格，它就在偷偷改路由的核心判断。
2. **近期在失败的先让开。** 3 次里错 2 次的端点再便宜也不叫「够用」。
3. **可复现。** 同样的输入永远同样的输出，不掷骰子——否则测试会随机飘，
   线上出问题也没法重现。
4. **关掉就完全不变。** 并列仍按名字固定，与本特性不存在时逐字节一致。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from blank_api.db import get_session_factory
from blank_api.models.model_provider import ModelProvider, ModelUsage
from blank_api.services.agent.loadstats import recent_load
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.agent.router import (
    HEAVY,
    LIGHT,
    LOAD_MIN_CALLS,
    LOAD_SEED_CAP,
    STANDARD,
    Candidate,
    LoadStat,
    Router,
    assess,
    pick,
)
from blank_api.services.features import FEATURES, describe, is_enabled
from blank_api.services.models.base import AdapterConfig, ModelAdapter, ModelResponse, Usage
from blank_api.services.settings import SettingsStore


def _ties() -> tuple[Candidate, ...]:
    """同档同价的两个候选。它们在旧逻辑里永远按名字定序，第一名恒为 A。"""
    return (Candidate("a", "A", LIGHT, 7.0), Candidate("b", "B", LIGHT, 7.0))


def _light():
    return assess(node="execute_action", step=1)


# ------------------------------------------------------------------ 关掉时不变


def test_ties_fall_back_to_name_when_load_balance_is_off():
    # 不给 loads = 本特性不存在时的那条路径：并列永远落在名字最靠前的那个
    assert pick(_light(), _ties()).name == "A"
    assert pick(_light(), _ties(), loads=None).name == "A"


def test_router_without_seed_ignores_loads():
    router = Router(pool=_ties(), loads={"b": LoadStat(calls=0), "a": LoadStat(calls=99)})
    assert router.load_balance is False

    assert router.choose(node="execute_action", step=1).name == "A"


# ------------------------------------------------------------------ 轮换与可复现


def test_rotation_goes_to_the_least_used_tie():
    decision = pick(_light(), _ties(), loads={"a": LoadStat(calls=2), "b": LoadStat(calls=0)})

    assert decision.name == "B"
    # 换人要说得出来为什么，否则这就是个黑盒
    assert "近期调用" in decision.reason
    assert "近期调用更多" in dict(decision.rejected)["A"]


def test_rotation_is_reproducible():
    loads = {"a": LoadStat(calls=1), "b": LoadStat(calls=1)}
    first = pick(_light(), _ties(), loads=loads)
    second = pick(_light(), _ties(), loads=dict(loads))

    # 不掷骰子：一次 run 里同样的状态必须给出同样的结论，否则用户重跑一次就换了个模型
    assert first == second


def test_rotation_alternates_within_one_run():
    router = Router(pool=_ties())
    router.seed_load({})

    picked = []
    for _ in range(4):
        decision = router.choose(node="execute_action", step=1)
        router.note_decision(decision, switched=False)
        picked.append(decision.provider_id)

    # 记完一次决策就要 +1，否则同一个任务里永远选同一个——任务通常跑不到第二次读库
    assert picked == ["a", "b", "a", "b"]


def test_seed_is_capped_so_a_skewed_window_does_not_sprint():
    router = Router(pool=_ties())
    router.seed_load({"a": LoadStat(calls=100), "b": LoadStat(calls=0)})

    picked = []
    for _ in range(LOAD_SEED_CAP + 2):
        decision = router.choose(node="execute_action", step=1)
        router.note_decision(decision, switched=False)
        picked.append(decision.provider_id)

    # 头几步补到 B 是对的；连着补 100 步就是纠偏过头——那不是均衡，是另一种偏心
    assert picked[0] == "b"
    assert "a" in picked


# ------------------------------------------------------------------ 失败先让开


def test_a_failing_tie_steps_aside():
    decision = pick(_light(), _ties(), loads={"a": LoadStat(calls=5, failures=3), "b": LoadStat(calls=0)})

    assert decision.name == "B"
    assert "失败 3 次" in dict(decision.rejected)["A"]
    assert "先让开" in dict(decision.rejected)["A"]


def test_a_thin_sample_is_not_a_verdict():
    thin = LoadStat(calls=LOAD_MIN_CALLS - 1, failures=LOAD_MIN_CALLS - 1)
    assert thin.suspect is False

    # B 明显用得多，但 A 那一两次失败样本量太小，不构成「让开」的理由
    decision = pick(_light(), _ties(), loads={"a": thin, "b": LoadStat(calls=5)})
    assert decision.name == "A"


def test_everyone_failing_still_picks_one():
    bad = LoadStat(calls=9, failures=9)
    decision = pick(_light(), _ties(), loads={"a": bad, "b": bad})

    # 全都不健康也必须有结论：返回空会退回默认模型，而默认模型可能正是连不上的那个
    assert decision.routed is True
    assert decision.provider_id in {"a", "b"}
    assert "先让开" not in dict(decision.rejected).get("A", "") + dict(decision.rejected).get("B", "")


# ------------------------------------------------- 轮换不许越界（档位与价格）


def test_rotation_never_buys_a_more_expensive_model():
    pool = (
        Candidate("a", "A", LIGHT, 7.0),
        Candidate("b", "B", LIGHT, 7.0),
        Candidate("big", "大型", STANDARD, 50.0),
    )
    both_bad = LoadStat(calls=9, failures=9)
    decision = pick(_light(), pool, loads={"a": both_bad, "b": both_bad})

    # 省钱是路由存在的理由：都"不健康"时也只在同价的两个里挑，不许跳去贵七倍的
    assert decision.cost == 7.0
    assert decision.tier == LIGHT
    assert "超出这一步所需" in dict(decision.rejected)["大型"]


def test_rotation_does_not_change_which_tier_is_needed():
    pool = (Candidate("light", "轻量", LIGHT, 7.0), Candidate("std", "标准", STANDARD, 50.0))
    # 轻量那个"很忙"，标准那个"很闲"——档位仍然是第一位
    decision = pick(_light(), pool, loads={"light": LoadStat(calls=80), "std": LoadStat(calls=0)})

    assert decision.name == "轻量"


def test_rotation_still_prefers_the_cheap_tier_when_it_is_the_heavy_step():
    demand = assess(node="review_result", step=25)
    assert demand.tier == HEAVY

    pool = (Candidate("mid", "中", STANDARD, 50.0), Candidate("top", "顶", HEAVY, 200.0))
    decision = pick(demand, pool, loads={"top": LoadStat(calls=0), "mid": LoadStat(calls=99)})

    assert decision.name == "顶"


# ------------------------------------------------------------------ 读库那一侧


def _usage(provider_id: str, *, status: str, minutes_ago: int, tokens: int = 100, latency: int = 500):
    stamp = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=minutes_ago)
    return ModelUsage(
        id=f"{provider_id}-{status}-{minutes_ago}-{tokens}-{latency}",
        provider_id=provider_id,
        trace_id="t",
        node="execute_action",
        model="m",
        prompt_tokens=tokens,
        completion_tokens=0,
        latency_ms=latency,
        status=status,
        created_at=stamp,
    )


async def _providers(*ids: str) -> None:
    async with get_session_factory()() as session:
        for provider_id in ids:
            session.add(
                ModelProvider(
                    id=provider_id,
                    name=f"供应商 {provider_id}",
                    kind="openai_compatible",
                    base_url="http://example.test/v1",
                    api_key_encrypted="x",
                    default_model="m",
                )
            )
        await session.commit()


async def _seed(*rows: ModelUsage) -> None:
    async with get_session_factory()() as session:
        session.add_all(rows)
        await session.commit()


async def test_recent_load_reads_counts_failures_and_window(client):
    await _providers("p1", "p2")
    await _seed(
        _usage("p1", status="ok", minutes_ago=1),
        _usage("p1", status="ok", minutes_ago=2),
        _usage("p1", status="error", minutes_ago=3),
        # 窗口外的：不能算进来，否则一次早就恢复的故障会一直记着
        _usage("p1", status="error", minutes_ago=90),
        _usage("p2", status="ok", minutes_ago=1, tokens=40, latency=100),
    )

    stats = await recent_load(get_session_factory(), minutes=15)

    assert stats["p1"].calls == 3
    assert stats["p1"].failures == 1
    assert stats["p1"].tokens == 300
    assert stats["p1"].window == 15
    assert stats["p2"].calls == 1
    assert stats["p2"].latency_ms == 100


async def test_recent_load_returns_nothing_when_the_query_fails():
    class Broken:
        def __call__(self):
            raise RuntimeError("库没了")

        async def __aenter__(self):  # pragma: no cover - 不会走到
            raise AssertionError

        async def __aexit__(self, *args):  # pragma: no cover
            return False

    # 负载是锦上添花：读不到就当没有，绝不能成为任务起不来的理由
    assert await recent_load(Broken()) == {}


async def test_recent_load_can_be_limited_to_the_candidates(client):
    await _providers("p1", "p2")
    await _seed(_usage("p1", status="ok", minutes_ago=1), _usage("p2", status="ok", minutes_ago=1))

    stats = await recent_load(get_session_factory(), providers={"p2"})

    assert set(stats) == {"p2"}


# ------------------------------------------------------------------ 注册表


def test_load_balance_is_registered_as_beta_and_off_by_default():
    flag = FEATURES["load_balance"]

    assert flag.stage == "beta"
    assert flag.default is False
    # 关着就完全不变：注册表里写着默认值，而不是靠调用点自己兜底
    assert is_enabled("load_balance", {}) is False
    assert "load_balance" in {item["name"] for item in describe({})["features"]}


# ------------------------------------------------------------------ 装配


class _TinyAdapter(ModelAdapter):
    """只为把运行时装配起来：这些用例断言的是"开关有没有传到位"，不是模型行为。"""

    def __init__(self) -> None:
        super().__init__(
            AdapterConfig(
                name="tiny",
                kind="openai",
                base_url="http://localhost",
                api_key="",
                model="tiny",
                context_window=32_000,
                supports_tools=True,
            )
        )

    async def invoke(self, messages, **kwargs) -> ModelResponse:  # pragma: no cover
        return ModelResponse(text="{}", usage=Usage(1, 1), model="tiny")

    async def stream(self, messages, **kwargs):  # pragma: no cover
        yield ""

    async def bind_tools(self, specs, messages, **kwargs) -> ModelResponse:  # pragma: no cover
        return ModelResponse(text="ok", usage=Usage(1, 1), model="tiny")


async def test_prepare_only_seeds_load_when_the_flag_is_on(client, workspace, tmp_path, monkeypatch):
    await _providers("a")
    await _seed(_usage("a", status="error", minutes_ago=2))
    path = tmp_path / "settings.json"
    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(path))
    runtime = AgentRuntime(session_factory=get_session_factory())
    request = RunRequest(
        task_id="task-load",
        provider_id="a",
        provider_name="A",
        adapter=_TinyAdapter(),
        root=workspace,
        goal="看看开关有没有传到路由上",
        max_steps=3,
        routing_pool=_ties(),
    )

    SettingsStore(path).write({"features": {"model_routing": True}})
    off = await runtime._prepare(request, "task-load:1")
    assert off.session.router is not None
    # 光有候选还不够：没开这个开关时它必须一眼看出来是关的
    assert off.session.router.load_balance is False
    assert off.session.router.loads == {}

    SettingsStore(path).write({"features": {"model_routing": True, "load_balance": True}})
    on = await runtime._prepare(request, "task-load:2")
    assert on.session.router is not None
    # 库里没有任何用量也要开：本次任务内部的轮换不依赖历史
    assert on.session.router.load_balance is True
    # 而库里有的那一条必须真的被读进来——否则这里就是"代码全在，只是没接上"
    assert on.session.router.loads["a"].failures == 1


async def test_prepare_skips_load_lookup_for_a_single_candidate(workspace, tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(path))
    SettingsStore(path).write({"features": {"model_routing": True, "load_balance": True}})
    runtime = AgentRuntime(session_factory=None)
    request = RunRequest(
        task_id="task-solo",
        provider_id="a",
        provider_name="A",
        adapter=_TinyAdapter(),
        root=workspace,
        goal="只有一个候选",
        max_steps=3,
        routing_pool=(Candidate("a", "A", LIGHT, 7.0),),
    )

    # session_factory 是 None：真去查库这里会直接炸。只有候选不足两个时才选这条路，
    # 而那时候连"选"都没有，查库纯属浪费
    scope = await runtime._prepare(request, "task-solo:1")
    assert scope.session.router is None
