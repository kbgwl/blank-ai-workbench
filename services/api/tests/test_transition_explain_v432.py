"""节点之间"为什么跳"的判据（v432，可移植清单 qwen #2 的 transition 那一半）。

两个面都要断言，少一个都不算接上：

1. **解释算得对**：``transition.explain`` 的判定必须与 ``graph.route_after_*`` 逐条一致。
   这是一份刻意的重复实现（路由要返回字符串、解释要返回一句话），对照表把两边钉在一起：
   以后改了路由不同步改解释，这张表会红，而不是让时间线上悄悄出现一行错的理由。
2. **接线接得上**：边算完之后 ``AgentSession.pending_transition`` 有值，
   目标节点入口把它落成 ``agent.transition`` 事件。只测纯函数会漏掉这一类——
   函数算得对但没人调用，交付物看起来完全正常。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from blank_api.services.agent import transition
from blank_api.services.agent.graph import (
    MAX_REPAIR_ROUNDS,
    MAX_REVIEW_ROUNDS,
    edge,
    emit_transition,
    route_after_act,
    route_after_plan,
    route_after_review,
    route_after_summarize,
    route_after_validation,
    run_validation_node,
)


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def add(self, kind: str, node: str, payload: dict | None = None) -> None:
        self.events.append((kind, node, payload or {}))

    def of(self, kind: str) -> list[tuple[str, str, dict]]:
        return [item for item in self.events if item[0] == kind]


def _explain(frm: str, to: str, state: dict) -> dict:
    return transition.explain(
        frm,
        to,
        state,
        readonly=bool(state.get("readonly")),
        max_repair=MAX_REPAIR_ROUNDS,
        max_review=MAX_REVIEW_ROUNDS,
    )


_SESSION = SimpleNamespace(readonly=False)

# (起点, 期望终点, 状态, 路由函数, 期望的机读码)
# 每条覆盖 route_after_* 里的一个分支。期望码是给人看的第二重口径：
# 光断言"和路由一致"会漏掉"两边都算错同一个东西"（比如把驳回说成通过）。
CASES = [
    ("create_plan", "summarize_result", {"direct_answer": True}, route_after_plan, "direct_answer"),
    (
        "create_plan", "execute_action",
        {"plan_steps": [{"id": "a"}, {"id": "b"}]}, route_after_plan, "plan_ready",
    ),
    ("execute_action", "finish", {"status": "cancelled"}, route_after_act, "cancelled"),
    ("execute_action", "finish", {"status": "paused"}, route_after_act, "paused"),
    ("execute_action", "finish", {"error": {"type": "model_error"}}, route_after_act, "errored"),
    (
        "execute_action", "execute_action",
        {"gap_continue": True}, route_after_act, "environment_gap",
    ),
    (
        "execute_action", "execute_action",
        {"pending": [{"tool": "read_file"}]}, route_after_act, "steps_remaining",
    ),
    ("execute_action", "run_validation", {"pending": []}, route_after_act, "steps_done"),
    (
        "run_validation", "summarize_result",
        {"readonly": True}, route_after_validation, "readonly_no_artifact",
    ),
    (
        "run_validation", "summarize_result",
        {"validation": {"status": "skipped"}}, route_after_validation, "validation_skipped",
    ),
    (
        "run_validation", "review_result",
        {"budget_exhausted": True}, route_after_validation, "budget_exhausted",
    ),
    (
        "run_validation", "execute_action",
        {"validation": {"status": "failed"}, "repair_rounds": 1},
        route_after_validation, "validation_failed_retry",
    ),
    (
        "run_validation", "review_result",
        {"validation": {"status": "failed"}, "repair_rounds": 1, "plan_manifest": {"exhausted": True}},
        route_after_validation, "repair_exhausted",
    ),
    (
        "run_validation", "review_result",
        {"validation": {"status": "failed"}, "repair_rounds": MAX_REPAIR_ROUNDS + 1},
        route_after_validation, "repair_budget_spent",
    ),
    (
        "run_validation", "review_result",
        {"validation": {"status": "passed"}}, route_after_validation, "validation_passed",
    ),
    (
        "review_result", "execute_action",
        {"review": {"approved": False, "issues": ["没用上验收条件"]}, "review_rounds": 1, "repair_rounds": 1},
        route_after_review, "review_rejected_retry",
    ),
    (
        "review_result", "summarize_result",
        {"review": {"approved": True}}, route_after_review, "review_approved",
    ),
    (
        "review_result", "summarize_result",
        {"review": {"approved": False}, "review_rounds": MAX_REVIEW_ROUNDS, "repair_rounds": 9},
        route_after_review, "review_round_limit",
    ),
    (
        "review_result", "summarize_result",
        {"budget_exhausted": True}, route_after_review, "budget_exhausted",
    ),
    (
        "review_result", "summarize_result",
        {"review": {"approved": False}, "repair_rounds": MAX_REPAIR_ROUNDS + 1},
        route_after_review, "repair_budget_spent",
    ),
    (
        "summarize_result", "execute_action",
        {"stop_blocked": True}, route_after_summarize, "stop_blocked",
    ),
    ("summarize_result", "finish", {}, route_after_summarize, "delivered"),
]


@pytest.mark.parametrize("frm,to,state,route,expected_rule", CASES)
def test_the_explanation_agrees_with_the_route(frm, to, state, route, expected_rule):
    """解释的终点与机读码都要与路由同口径。

    只断言"算得出一个码"没意义：真正会出事的是两边漂移——路由把人送进修复回路，
    解释却说"审查通过"，用户照着时间线排查就会走错方向。
    """
    assert route(state, _SESSION) == to
    record = _explain(frm, to, state)
    assert record["rule"] == expected_rule
    assert record["why"], "机读码有了，但给人看的那句话是空的"
    assert (record["from"], record["to"]) == (frm, to)


def test_an_unknown_hop_is_labelled_instead_of_invented():
    """没登记过的跳转如实说"没解释"，不编一句话。

    编出来的理由比没有理由更糟：时间线上看起来合理，排查的人就不会再去查代码。
    """
    record = _explain("review_result", "run_validation", {})
    assert record["rule"] == transition.UNEXPLAINED
    assert record["why"] == ""


# ── 接线：边记、节点落 ────────────────────────────────────────────────────


def test_the_edge_records_why_before_the_node_runs():
    session = SimpleNamespace(recorder=_Recorder(), readonly=False, pending_transition={})
    wrapped = edge("execute_action", lambda state: route_after_act(state, session), session)
    state = {"pending": [{"tool": "read_file"}, {"tool": "write_file"}]}

    target = wrapped(state)

    assert target == route_after_act(state, session), "包装改变了路由结果"
    assert session.pending_transition["to"] == target
    assert session.pending_transition["rule"] == "steps_remaining"
    assert session.pending_transition["pending"] == 2


def test_the_arriving_node_writes_the_transition_and_clears_it():
    session = SimpleNamespace(recorder=_Recorder(), readonly=False, pending_transition={})
    session.pending_transition = {
        "from": "execute_action", "to": "run_validation", "rule": "steps_done", "why": "没有待办步骤了"
    }

    asyncio.run(emit_transition(session, "run_validation"))

    events = session.recorder.of("agent.transition")
    assert len(events) == 1
    _, node, payload = events[0]
    assert node == "run_validation"
    assert payload["from"] == "execute_action"
    assert payload["rule"] == "steps_done"
    # 握手用的槽要清空：留着的话同一条解释会在下一个节点再落一次。
    assert session.pending_transition == {}


def test_a_transition_aimed_at_another_node_is_dropped():
    """目标不是自己就不记：图会重算边，留一行 to 对不上的解释只会自相矛盾。"""
    session = SimpleNamespace(recorder=_Recorder(), readonly=False, pending_transition={})
    session.pending_transition = {"from": "execute_action", "to": "review_result", "rule": "x", "why": ""}

    asyncio.run(emit_transition(session, "run_validation"))

    assert session.recorder.of("agent.transition") == []
    assert session.pending_transition == {}


def test_the_validation_node_actually_emits_it_end_to_end():
    """走真实节点，不只手搓调用。

    这一条防的是"函数写对了但没人调用"——那种交付物打开来看什么都对。
    """

    class _Gateway:
        calls = 0

        async def execute(self, tool, args, context, node):  # noqa: ANN001, ANN201
            raise AssertionError("已有 run_tests 结果时不该再执行一次")

    session = SimpleNamespace(
        recorder=_Recorder(),
        gateway=_Gateway(),
        readonly=False,
        pending_transition={
            "from": "execute_action", "to": "run_validation", "rule": "steps_done", "why": "没有待办步骤了"
        },
    )
    state = {
        "tool_results": [
            {
                "tool": "run_tests",
                "status": "ok",
                "ok": True,
                "output": "17 passed",
                "data": {"command": "pytest -q", "exit_code": 0},
            }
        ]
    }

    asyncio.run(run_validation_node(state, session))

    kinds = [item[0] for item in session.recorder.events]
    assert "agent.transition" in kinds, kinds
    # 解释要排在 node.started 之后：先让人看到"进了哪个节点"，再说"为什么进来"。
    assert kinds.index("agent.transition") > kinds.index("node.started")
