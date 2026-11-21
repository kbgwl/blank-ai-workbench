"""跳转解释与路由的一致性：把采样换成穷举（v485）。

上一版（``test_transition_explain_v432.py``）用手写样本，一个分支一条，共 22 条。
它能挡住"改了路由忘了改解释"，但挡不住**没被采样的状态组合**：``explain`` 是
``route_after_*`` 的另一份实现，两边判定顺序一旦在某个角落不一致（例如
``budget_exhausted`` 与 ``readonly`` 同时为真、``repair_rounds`` 刚好压线），
时间线上只会出现一行 ``unexplained``，不会红。

这里改成按字段取值做笛卡尔积，并且**以真实路由函数为唯一真值**：对每一个组合
先问路由"你去哪"，再问解释"为什么去那儿"，要求

1. 解释的 ``to`` 就是路由给的终点（不是手写的期望值）；
2. 解释不返回 ``unexplained``（返回了说明这个组合没被覆盖）；
3. 解释里给人看的那句话不为空。

判据钉在"拿掉实现就红"的位置：删掉 ``explain`` 里任何一个分支 → 对应的组合
返回 ``unexplained``，这里立刻红；改路由多出一条新分支而解释没跟上 → 同样红。
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest

from blank_api.services.agent import transition
from blank_api.services.agent.graph import (
    MAX_REPAIR_ROUNDS,
    MAX_REVIEW_ROUNDS,
    route_after_act,
    route_after_plan,
    route_after_review,
    route_after_summarize,
    route_after_validation,
)

# ── 每个路由节点读哪些字段：取值就是代码里出现的那些分支条件 ────────────────

_VALIDATION_STATUSES = ("", "passed", "failed", "timeout", "error", "skipped")
_ROUNDS = (0, MAX_REPAIR_ROUNDS, MAX_REPAIR_ROUNDS + 1)
_MANIFESTS = ({}, {"exhausted": True}, {"no_new_evidence": True})

_SPACES: dict[str, tuple[list[str], dict[str, list]]] = {
    "create_plan": (
        ["direct_answer"],
        {"direct_answer": [True, False]},
    ),
    "execute_action": (
        ["status", "error", "gap_continue", "pending"],
        {
            "status": ["", "cancelled", "paused", "running"],
            "error": [None, {"type": "model_error"}],
            "gap_continue": [True, False],
            "pending": [[], [{"tool": "read_file"}]],
        },
    ),
    "run_validation": (
        [
            "readonly", "session_readonly", "budget_exhausted",
            "validation", "repair_rounds", "plan_manifest",
        ],
        {
            # 只读有两个来源：state 级（任务本身是只读）与会话级（用户切了只读）。
            # 路由读的是两者的"或"，只测其中一个会漏掉会话级那一半。
            "readonly": [True, False],
            "session_readonly": [True, False],
            "budget_exhausted": [True, False],
            "validation": [{"status": status} for status in _VALIDATION_STATUSES],
            "repair_rounds": list(_ROUNDS),
            "plan_manifest": list(_MANIFESTS),
        },
    ),
    "review_result": (
        ["budget_exhausted", "review_rounds", "review", "repair_rounds"],
        {
            "budget_exhausted": [True, False],
            "review_rounds": [0, MAX_REVIEW_ROUNDS - 1, MAX_REVIEW_ROUNDS],
            "review": [{"approved": True}, {"approved": False}, {"approved": False, "issues": ["a", "b"]}],
            "repair_rounds": list(_ROUNDS),
        },
    ),
    "summarize_result": (
        ["stop_blocked"],
        {"stop_blocked": [True, False]},
    ),
}

_ROUTES = {
    "create_plan": route_after_plan,
    "execute_action": route_after_act,
    "run_validation": route_after_validation,
    "review_result": route_after_review,
    "summarize_result": route_after_summarize,
}


def _combinations(node: str) -> list[dict]:
    fields, space = _SPACES[node]
    out: list[dict] = []
    for values in itertools.product(*(space[name] for name in fields)):
        raw = dict(zip(fields, values))
        session_readonly = bool(raw.pop("session_readonly", False))
        state = {key: value for key, value in raw.items() if key != "session_readonly"}
        state["__session_readonly"] = session_readonly
        out.append(state)
    return out


def _route(node: str, state: dict) -> str:
    session = SimpleNamespace(readonly=bool(state.pop("__session_readonly", False)))
    try:
        return _ROUTES[node](state, session)
    finally:
        state["__session_readonly"] = session.readonly


def _explain(node: str, to: str, state: dict) -> dict:
    return transition.explain(
        node,
        to,
        state,
        readonly=bool(state.get("readonly") or state.get("__session_readonly")),
        max_repair=MAX_REPAIR_ROUNDS,
        max_review=MAX_REVIEW_ROUNDS,
    )


@pytest.mark.parametrize("node", sorted(_SPACES))
def test_every_reachable_hop_has_a_real_explanation(node):
    """每个可达组合都必须有一句真解释，且终点与路由一致。

    断言的是"路由怎么走，解释就怎么说"，不是手写的期望表——期望表会跟实现一起
    漂移，而路由本身就是用户真正看到的那个行为。
    """
    seen: set[tuple[str, str]] = set()
    broken: list[tuple[dict, str, dict]] = []
    for state in _combinations(node):
        to = _route(node, state)
        seen.add((node, to))
        record = _explain(node, to, state)
        if record["to"] != to or record["rule"] == transition.UNEXPLAINED or not record["why"]:
            broken.append((state, to, record))

    assert not broken, "解释与路由在下列组合上不一致：\n" + "\n".join(
        f"  state={state} route->{to} explain->{record['rule']!r}" for state, to, record in broken[:8]
    )
    # 每个节点至少要走到两个不同的终点，否则这个穷举等于没测（字段名写错时
    # 全都落到同一个分支，断言会"全绿"）。
    assert len({target for _, target in seen}) >= 2, seen


def test_validation_that_never_ran_is_not_reported_as_passed():
    """校验没跑过就不能说"校验通过"。

    这是穷举测出来的真实缺陷：``validation`` 为空时路由照样把控制权交给审查
    （没有失败证据就不该再修），但旧解释落在 ``validation_passed`` 上，时间线上
    会写一句"校验通过（通过）"——用户会以为有人验过了。
    """
    record = _explain("run_validation", "review_result", {"validation": {}})
    assert record["rule"] != "validation_passed", record
    assert "通过" not in record["why"] or "没有" in record["why"], record


def test_a_state_where_readonly_wins_over_budget_is_explained_by_readonly():
    """两个条件同时为真时，说出来的必须是路由真正用的那一条。

    路由先看只读、再看预算：这里说成"预算耗尽"就会把用户引去加预算，而加了
    也不会好——只读任务本来就不进审查回路。
    """
    state = {"readonly": True, "budget_exhausted": True, "validation": {"status": "failed"}}
    to = route_after_validation(state, SimpleNamespace(readonly=False))
    assert to == "summarize_result"
    assert _explain("run_validation", to, state)["rule"] == "readonly_no_artifact"
