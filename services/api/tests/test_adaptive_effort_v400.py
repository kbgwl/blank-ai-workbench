"""按阶段收紧思考档位（``adaptive_effort``）与审查器档位。

这两条改动的共同依据是一份实测取证：同一条 20 分钟的编程任务里 44 次模型调用、
52.7k 输出 token 中有 40.4k 是推理 token（76.6%），而其中 5 次超长思考
（8838 / 9384 / 9793 / 4892 / 2863）合计吃掉 771 秒；按阶段看，这 5 次全在
「执行节点整文件重写」，收尾与规划加起来不到 1k。

所以钉三件事：

1. 收紧只降不升——兜底出来的 high/max 可以被压到 medium，用户没理由因此掉档位。
2. 用户显式升档（ultrathink/ultracode）时整轮不降——他要的是深想。
3. 审查器自己发 low：审查的输出是 allow/deny，不是推理结论。
"""

from __future__ import annotations

import asyncio

from blank_api.services import features as registry
from blank_api.services.effort import STEP_EFFORT, rank, tighten_for_step
from blank_api.services.guardian import GUARDIAN_EFFORT, Guardian, _accepts_effort
from blank_api.services.models.base import ModelResponse, Usage


# --------------------------------------------------------------------------- #
# 按阶段收紧
# --------------------------------------------------------------------------- #


def test_the_flag_is_on_by_default() -> None:
    """提速是默认行为。默认关掉的开关等于没做——没有人会去找它。"""
    described = registry.FEATURES
    assert described["adaptive_effort"].default is True


def test_each_stage_has_a_level() -> None:
    assert STEP_EFFORT["create_plan"] == "high"
    assert STEP_EFFORT["execute_action"] == "medium"
    assert STEP_EFFORT["complete"] == "low"


def test_tightening_never_raises_the_level() -> None:
    for base in ("low", "medium", "high", "xhigh", "max"):
        for node in ("create_plan", "execute_action", "complete"):
            got = tighten_for_step(base, node)
            assert rank(got) <= rank(base), (base, node, got)


def test_a_low_turn_stays_low_everywhere() -> None:
    """用户把档位调到 low，规划阶段也不许自己抬回 high。"""
    assert tighten_for_step("low", "create_plan") == "low"
    assert tighten_for_step("low", "execute_action") == "low"
    assert tighten_for_step("low", "complete") == "low"


def test_an_unknown_node_keeps_the_turn_level() -> None:
    """没列进表里的节点（子代理、校验通道）保持原样，不做没依据的收紧。"""
    assert tighten_for_step("high", "run_validation") == "high"
    assert tighten_for_step("high", "") == "high"


def test_max_collapses_to_the_stage_level() -> None:
    """兜底档可能是 max；执行阶段只该要 medium。"""
    assert tighten_for_step("max", "execute_action") == "medium"


# --------------------------------------------------------------------------- #
# 审查器档位
# --------------------------------------------------------------------------- #


class _EffortAwareAdapter:
    """收 effort 的假适配器：记下发出去的那一档。"""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.config = type("C", (), {"model": "fake"})()

    async def invoke(self, messages, *, effort: str = "", **_):
        self.seen.append(effort)
        return ModelResponse(
            text='{"verdict": "allow", "reasons": ["只读命令"]}',
            tool_calls=[],
            usage=Usage(),
            model="fake",
        )


class _PlainAdapter:
    """不收 effort 的假适配器：多传一个 kwarg 会 TypeError。"""

    def __init__(self) -> None:
        self.calls = 0
        self.config = type("C", (), {"model": "fake"})()

    async def invoke(self, messages, **_):
        self.calls += 1
        return ModelResponse(
            text='{"verdict": "allow", "reasons": []}', tool_calls=[], usage=Usage(), model="fake"
        )


def test_the_guardian_asks_for_a_cheap_pass() -> None:
    adapter = _EffortAwareAdapter()
    guardian = Guardian(adapter, mode="auto")

    verdict = asyncio.run(guardian.review("run_shell", {"command": "git status"}))

    assert verdict.verdict == "allow"
    assert adapter.seen == [GUARDIAN_EFFORT] == ["low"]


def test_an_adapter_without_effort_still_works() -> None:
    """签名里没有 effort 的适配器不能被多传参数打死——那是运行时才炸的 TypeError。"""
    adapter = _PlainAdapter()
    guardian = Guardian(adapter, mode="auto")

    verdict = asyncio.run(guardian.review("run_shell", {"command": "ls"}))

    assert verdict.verdict == "allow"
    assert adapter.calls == 1
    assert _accepts_effort(adapter) is False
    assert _accepts_effort(_EffortAwareAdapter()) is True


def test_no_adapter_means_no_review() -> None:
    guardian = Guardian(None, mode="auto")

    verdict = asyncio.run(guardian.review("run_shell", {"command": "ls"}))

    assert verdict.verdict == "abstain"
    assert _accepts_effort(None) is False


# --------------------------------------------------------------------------- #
# 想太久就催动手
# --------------------------------------------------------------------------- #


def test_a_normal_step_gets_no_nudge() -> None:
    from blank_api.services.agent.graph import think_overrun_note

    # 实测正常步是 7-400 token：这些都不该被催。
    for tokens in (0, 7, 41, 355, 1200):
        assert think_overrun_note(tokens) == ""


def test_a_runaway_thought_is_named_with_its_cost() -> None:
    from blank_api.services.agent.graph import think_overrun_note

    note = think_overrun_note(16851)

    assert "16,851" in note
    # 催的话必须指向一个具体动作，否则模型只会"更努力地想"。
    assert "改一处" in note and "不要在一次思考里把整个改造方案想完" in note


def test_the_threshold_sits_between_normal_and_runaway() -> None:
    from blank_api.services.agent.graph import DEEP_THINK_TOKENS

    assert 400 < DEEP_THINK_TOKENS < 2863  # 上界取实测最小的一次长考


def test_overrun_nudge_reaches_the_model_as_visible_text() -> None:
    # 只测 think_overrun_note 不够：真接线是 append 到 session.hook_context，
    # 再由 drain_hook_context() 渲染成 system 消息。哪一环漏了 text，模型都收不到。
    # 这里把三环串起来走一遍，证明"事件记了、模型也真看得见"。
    from blank_api.services.agent.graph import (
        render_hook_context,
        think_overrun_note,
    )

    note = think_overrun_note(16851)
    rendered = render_hook_context([{"event": "think.overrun", "tool": "", "text": note}])

    assert "16,851" in rendered  # 具体的思考量进去了
    assert "改一处" in rendered  # 催的具体动作进去了
    assert "think.overrun" in rendered  # 来源标注进去了
