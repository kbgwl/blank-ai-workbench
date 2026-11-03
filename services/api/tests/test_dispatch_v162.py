# -*- coding: utf-8 -*-
"""v162 Router 执行快照：派发决策、安全默认值、不可变性与预算。

这一组只钉纯函数（不碰库、不调模型）。集成侧「快照真的拦住了写操作」在
``test_dispatch_runtime_v162.py``。
"""

from __future__ import annotations

import pytest

from blank_api.limits import DEFAULT_MAX_STEPS
from blank_api.services.agent.dispatch import (
    DISPATCH_ASK,
    DISPATCH_DEFER,
    DISPATCH_IMMEDIATE,
    DISPATCH_QUEUE,
    INTENT_ANSWER,
    INTENT_ASK_USER,
    INTENT_BRAINSTORM,
    INTENT_EXECUTE,
    INTENT_SCHEDULE,
    INTENT_WATCH,
    MEMORY_OFF,
    MEMORY_OPT_IN_WRITE,
    MEMORY_READ_ONLY,
    NOTIFY_MILESTONE,
    NOTIFY_SILENT,
    TOOL_READ_ONLY,
    TOOL_WORKSPACE_EDIT,
    Budget,
    ExecutionSnapshot,
    UnsafeSnapshot,
    classify,
    explain,
    route,
)


# ── 派发方式 ────────────────────────────────────────────────────────────────


def test_chitchat_is_answered_immediately_and_silently():
    snapshot = route("你好")
    assert snapshot.intent == INTENT_ANSWER
    assert snapshot.dispatch == DISPATCH_IMMEDIATE
    assert snapshot.notify_policy == NOTIFY_SILENT
    assert snapshot.tool_policy == TOOL_READ_ONLY
    # 纯问答给 1 步：多给一步就允许它把「你好」跑成一整轮工具循环。
    assert snapshot.budget.max_steps == 1


def test_coding_request_goes_to_worker_queue():
    snapshot = route("把登录令牌改成 30 天有效")
    assert snapshot.intent == INTENT_EXECUTE
    assert snapshot.dispatch == DISPATCH_QUEUE
    assert snapshot.tool_policy == TOOL_WORKSPACE_EDIT
    assert snapshot.notify_policy == NOTIFY_MILESTONE
    assert snapshot.budget.max_steps == DEFAULT_MAX_STEPS


def test_opinion_request_is_read_only_even_though_it_runs():
    # 「你觉得哪儿能优化」会真去看几处再答，但不许改。
    snapshot = route("你觉得这个界面有什么可以优化的")
    assert snapshot.intent == INTENT_EXECUTE
    assert snapshot.tool_policy == TOOL_READ_ONLY


def test_daily_scenario_never_writes_workspace():
    snapshot = route("帮我改一下这句话的措辞", scenario="daily")
    assert snapshot.tool_policy == TOOL_READ_ONLY
    # 日常场景允许读记忆，但没选「记住」就不能写。
    assert snapshot.memory_policy == MEMORY_READ_ONLY


def test_only_daily_with_opt_in_can_write_memory():
    assert route("改一下配置", scenario="daily", memory_opt_in=True).memory_policy == MEMORY_OPT_IN_WRITE
    assert route("改一下配置", scenario="dev", memory_opt_in=True).memory_policy != MEMORY_OPT_IN_WRITE
    # 纯问答即使选了记住也不写：那类请求里"记住"的常常是一句临时信息。
    assert route("你好", scenario="daily", memory_opt_in=True).memory_policy != MEMORY_OPT_IN_WRITE


def test_brainstorm_is_read_only_and_does_not_queue():
    snapshot = route("给我几个方案，怎么重构这个模块", scenario="brainstorm")
    assert snapshot.intent == INTENT_BRAINSTORM
    assert snapshot.tool_policy == TOOL_READ_ONLY
    assert snapshot.dispatch == DISPATCH_IMMEDIATE


def test_schedule_request_waits_for_trigger_instead_of_running_now():
    snapshot = route("每天帮我跑一次测试")
    assert snapshot.intent == INTENT_SCHEDULE
    assert snapshot.dispatch == DISPATCH_DEFER
    # 不能当场跑：那等于把「每天」执行成「现在」。
    assert snapshot.budget.max_steps == 1


def test_watch_request_waits_for_event():
    snapshot = route("盯着 src 目录，一旦有变化就告诉我")
    assert snapshot.intent == INTENT_WATCH
    assert snapshot.dispatch == DISPATCH_DEFER


def test_empty_goal_asks_instead_of_running():
    snapshot = route("   ")
    assert snapshot.intent == INTENT_ASK_USER
    assert snapshot.dispatch == DISPATCH_ASK
    assert snapshot.needs_user_decision is True


def test_unknown_rule_falls_back_to_execute_not_chitchat():
    # 判不准按执行派发：反过来会把一次真改动变成一句「好的」，用户以为做完了。
    intent, notes = classify("那个东西你处理一下", scenario="dev")
    assert intent == INTENT_EXECUTE
    assert notes and "判不准" in notes[0]


# ── 高风险与审批 ────────────────────────────────────────────────────────────


def test_high_risk_words_do_not_raise_priority_to_bypass_approval():
    snapshot = route("删除 data 下所有临时文件")
    assert snapshot.high_risk is True
    assert snapshot.risk_summary
    # 审批是硬约束：快照里没有能关掉它的取值。
    assert snapshot.approval_required is True
    assert snapshot.priority == "normal"


def test_urgent_priority_still_keeps_approval():
    snapshot = route("清空测试数据库", priority="urgent")
    assert snapshot.priority == "urgent"
    assert snapshot.approval_required is True
    assert snapshot.notify_policy != NOTIFY_SILENT


def test_unknown_priority_falls_back_to_normal():
    assert route("改一下", priority="fire").priority == "normal"


# ── 序列化：缺字段与脏值一律收紧 ────────────────────────────────────────────


def test_empty_payload_is_read_only_and_asks_user():
    snapshot = ExecutionSnapshot.from_dict({})
    assert snapshot.intent == INTENT_ASK_USER
    assert snapshot.tool_policy == TOOL_READ_ONLY
    assert snapshot.memory_policy == MEMORY_OFF
    assert snapshot.notify_policy == "approval_only"
    assert snapshot.approval_required is True
    assert snapshot.dispatch == DISPATCH_ASK


def test_garbage_values_are_clamped_not_trusted():
    snapshot = ExecutionSnapshot.from_dict(
        {
            "intent": "rm -rf",
            "scenario": "production",
            "tool_policy": "everything",
            "notify_policy": "spam",
            "memory_policy": "write_all",
            "priority": "asap",
        }
    )
    assert snapshot.intent == INTENT_ASK_USER
    assert snapshot.scenario == "dev"
    assert snapshot.tool_policy == TOOL_READ_ONLY
    assert snapshot.memory_policy == MEMORY_OFF
    assert snapshot.priority == "normal"
    assert any("不认识" in note for note in snapshot.notes)


def test_readonly_scenario_cannot_be_widened_by_data():
    # 一份声称「日常场景、可写工作区」的快照，读回来必须是只读。
    snapshot = ExecutionSnapshot.from_dict(
        {"intent": "execute", "scenario": "daily", "tool_policy": TOOL_WORKSPACE_EDIT}
    )
    assert snapshot.tool_policy == TOOL_READ_ONLY
    assert any("收紧为只读" in note for note in snapshot.notes)


def test_memory_write_outside_daily_is_downgraded():
    snapshot = ExecutionSnapshot.from_dict(
        {"intent": "execute", "scenario": "dev", "memory_policy": MEMORY_OPT_IN_WRITE}
    )
    assert snapshot.memory_policy != MEMORY_OPT_IN_WRITE


def test_approval_required_cannot_be_switched_off_by_data():
    snapshot = ExecutionSnapshot.from_dict({"intent": "execute", "scenario": "dev", "approval_required": False})
    assert snapshot.approval_required is True


def test_budget_is_clamped_to_hard_limits():
    snapshot = ExecutionSnapshot.from_dict(
        {"intent": "execute", "scenario": "dev", "budget": {"max_steps": 9999, "max_seconds": 10**9, "max_recoveries": 99}}
    )
    assert snapshot.budget.max_steps == DEFAULT_MAX_STEPS
    assert snapshot.budget.max_seconds <= 6 * 3600
    assert snapshot.budget.max_recoveries <= 3


def test_budget_missing_falls_back_to_intent_default():
    budget = Budget.from_dict(None, intent=INTENT_ANSWER)
    assert budget.max_steps == 1 and budget.max_recoveries == 0


def test_broken_budget_does_not_raise():
    snapshot = ExecutionSnapshot.from_dict(
        {"intent": "execute", "scenario": "dev", "budget": {"max_steps": "many"}}
    )
    assert snapshot.budget.max_steps == DEFAULT_MAX_STEPS


# ── 不可变与收紧 ────────────────────────────────────────────────────────────


def test_snapshot_id_is_deterministic_and_changes_with_policy():
    first = route("改一下配置")
    second = route("改一下配置")
    assert first.snapshot_id == second.snapshot_id
    assert first.snapshot_id.startswith("dsp-")
    # 只读与可写是两份不同的决定，id 必须不同——否则执行侧对不上账。
    assert route("改一下配置", scenario="daily").snapshot_id != first.snapshot_id


def test_tightened_only_goes_stricter():
    snapshot = route("改一下配置")
    tighter = snapshot.tightened(readonly=True, max_steps=3)
    assert tighter.tool_policy == TOOL_READ_ONLY
    assert tighter.budget.max_steps == 3
    # 已经在只读上，再放宽不会发生（这个 API 没有放宽的方向）。
    assert tighter.tightened(max_steps=99).budget.max_steps == 3


def test_route_never_exceeds_requested_step_budget():
    snapshot = route("改一下配置", max_steps=5)
    assert snapshot.budget.max_steps == 5


def test_guard_rejects_handcrafted_unsafe_snapshot():
    unsafe = route("改一下配置")
    with pytest.raises(UnsafeSnapshot):
        ExecutionSnapshot(**{**unsafe.__dict__, "approval_required": False}).guard()
    with pytest.raises(UnsafeSnapshot):
        ExecutionSnapshot(
            **{**unsafe.__dict__, "scenario": "daily", "memory_policy": MEMORY_OPT_IN_WRITE}
        ).guard()


def test_explain_is_one_line_and_mentions_tools_and_budget():
    text = explain(route("改一下配置"))
    assert "排队" in text and "工具" in text and "步数上限" in text
    assert "\n" not in text
