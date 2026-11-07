"""v165 Phase 2 的三块纯函数内核：Planner / Verifier / Dispatcher。

这里全部不查库、不起服务、不调模型。接线之前先把判据钉死：执行链一改就是
4000 行图状态，届时再分不清「规则写错」还是「接错位置」会非常贵。
"""

from __future__ import annotations

from blank_api.services.agent.dispatcher import (
    decide_dispatch,
    dispatch_snapshot,
)
from blank_api.services.agent.planner import (
    MAX_PLAN_REVISIONS,
    build_plan,
    plan_signature,
    repair_hint,
    revise_plan,
    step_kind,
    step_risk,
)
from blank_api.services.agent.verifier import failure_reason, verify


# --------------------------------------------------------------------------- Planner


def test_build_plan_has_v165_structure_and_stable_id():
    plan = build_plan(["读取入口文件", "修改按钮颜色", "跑测试"], goal_id="g1", scenario="dev")
    assert plan["goal_id"] == "g1"
    assert plan["plan_id"] == build_plan(["读取入口文件", "修改按钮颜色", "跑测试"], goal_id="g1")["plan_id"]
    assert plan["max_steps"] == 30 and plan["token_budget"] == 150_000
    assert plan["rollback_strategy"] == "workspace_diff"
    assert [item["kind"] for item in plan["steps"]] == ["inspect", "edit", "verify"]
    assert plan["steps"][1]["depends_on"] == ["step-1"]
    assert plan["steps"][2]["depends_on"] == ["step-2"]
    assert all(item["success_condition"] for item in plan["steps"])
    assert plan["approval_points"] == []


def test_plan_accepts_structured_plan_steps_from_update_plan():
    steps = [{"id": "step-1", "step": "修复 average 的实现", "status": "completed"}]
    plan = build_plan(steps, goal_id="g2")
    assert plan["steps"][0]["kind"] == "repair"
    assert plan["steps"][0]["title"] == "修复 average 的实现"


def test_high_risk_step_becomes_an_approval_point():
    assert step_risk("删除旧目录", "edit") == "high"
    assert step_risk("安装依赖", "edit") == "high"
    plan = build_plan(["读取配置", "安装依赖并重新构建"], goal_id="g3")
    assert plan["approval_points"] == ["step-2"]
    assert step_kind("先看一眼 README") == "inspect"


def test_revise_plan_adds_a_repair_step_from_a_real_test_failure():
    plan = build_plan(["修改按钮颜色", "跑测试"], goal_id="g4")
    revised = revise_plan(plan, failure={"kind": "test_failed", "detail": "pytest 1 failed"}, repair_round=1)
    assert revised["revision"] == 1
    assert revised["steps"][-1]["kind"] == "repair"
    assert "run_tests" in revised["steps"][-1]["tools"]
    assert plan["revision"] == 0 and len(plan["steps"]) == 2  # 不原地改，checkpoint 才比得出来
    assert plan_signature(revised) != plan_signature(plan)


def test_revise_plan_stops_when_a_failure_repeats_without_new_evidence():
    plan = build_plan(["改代码"], goal_id="g5")
    once = revise_plan(plan, failure={"kind": "test_failed", "detail": "boom"}, repair_round=1)
    twice = revise_plan(once, failure={"kind": "test_failed", "detail": "boom"}, repair_round=2)
    assert len(twice["steps"]) == len(once["steps"])
    assert twice["no_new_evidence"] is True and twice["exhausted"] is True
    assert "没有新证据" in twice["revision_reason"]


def test_revise_plan_updates_instead_of_stacking_the_same_kind_of_repair():
    plan = build_plan(["改代码"], goal_id="g5b")
    once = revise_plan(plan, failure={"kind": "test_failed", "detail": "boom"}, repair_round=1)
    twice = revise_plan(once, failure={"kind": "test_failed", "detail": "another failure"}, repair_round=2)
    assert len(twice["steps"]) == len(once["steps"]) == 2
    assert "第 2 轮" in twice["steps"][-1]["title"]
    assert twice["revision"] == 2


def test_revise_plan_has_a_hard_limit_and_can_early_verify():
    plan = build_plan(["改代码"], goal_id="g6")
    current = plan
    for round_no in range(1, MAX_PLAN_REVISIONS + 3):
        current = revise_plan(
            current,
            failure={"kind": f"failure_{round_no}", "detail": f"第 {round_no} 类失败"},
            repair_round=round_no,
        )
    assert current["revision"] == MAX_PLAN_REVISIONS
    assert current["exhausted"] is True
    assert "上限" in current["revision_reason"]
    early = revise_plan(plan, goal_already_met=True)
    assert early["early_verify"] is True


def test_repair_hint_never_rewrites_the_user_message():
    hint = repair_hint({"detail": "pytest: 1 failed"}, 2)
    assert "第 2 轮自动修复" in hint and "pytest: 1 failed" in hint
    assert "重新跑一次验证" in hint
    assert not hint.startswith("执行规则")  # v164 删掉的前缀拼接不能借修复回路回来


# --------------------------------------------------------------------------- Verifier


def _ok_validation() -> dict:
    return {"status": "ok", "passed": True, "exit_code": 0, "command": "pytest -q"}


def test_verifier_accepts_a_real_change_with_real_tests():
    verdict = verify(
        goal_spec={"objective": "把按钮改成蓝色", "done_when": ["按钮颜色变蓝"]},
        tool_results=[{"tool": "apply_patch", "status": "ok"}],
        validation=_ok_validation(),
        changed_paths=["web/src/button.css"],
        report="已把按钮改成蓝色，测试通过。",
    )
    assert verdict["verdict"] == "passed" and verdict["passed"] is True
    assert verdict["blocking"] is False
    assert verdict["checks"]["files_changed"] is True
    assert verdict["checks"]["tests_executed"] is True


def test_verifier_blocks_zero_change_success():
    verdict = verify(
        goal_spec={"objective": "把按钮改成蓝色", "done_when": ["按钮颜色变蓝"]},
        tool_results=[{"tool": "apply_patch", "status": "ok"}],
        validation={"status": "skipped"},
        changed_paths=[],
        report="已经改好了。",
    )
    assert verdict["blocking"] is True and verdict["passed"] is False
    assert verdict["checks"]["not_zero_change"] is False
    assert any("零改动" in item for item in verdict["findings"])
    assert failure_reason(verdict) == "目标要求改动，但工作区没有任何文件变化"


def test_verifier_does_not_punish_a_readonly_task_whose_goal_mentions_edits():
    """回归：验收条件明写「不修改任何文件」的只读任务不能被判成伪成功。

    伪成功指「写工具成功返回却没落盘」，不是「目标里出现了『修改』二字」。
    """
    verdict = verify(
        goal_spec={
            "objective": "任务目标：修改 main.py 并运行测试\n验收条件：\n- 读一次 main.py\n- 不修改任何文件",
            "done_when": [],
        },
        tool_results=[{"tool": "read_file", "status": "ok", "ok": True}],
        validation={"status": "skipped"},
        changed_paths=[],
        report="已读取 main.py",
    )
    assert verdict["blocking"] is False and verdict["passed"] is True
    assert verdict["checks"]["not_zero_change"] is True


def test_run_shell_is_not_a_file_write():
    """回归：一条只读命令跑成功但没改文件，不能算零改动伪成功。

    团队 Bot 的「发一条要审批的命令」就走这条路；误判会把整轮群聊提前收口。
    """
    verdict = verify(
        goal_spec={"objective": "看一下当前状态"},
        tool_results=[{"tool": "run_shell", "status": "ok", "ok": True, "data": {"command": "git status"}}],
        validation={"status": "skipped"},
        changed_paths=[],
        report="当前工作区是干净的。",
    )
    assert verdict["blocking"] is False and verdict["checks"]["not_zero_change"] is True


def test_verifier_blocks_a_false_claim_about_tests():
    verdict = verify(
        goal_spec={"objective": "修复计算"},
        tool_results=[{"tool": "apply_patch", "status": "ok"}],
        validation={"status": "skipped"},
        changed_paths=["src/calc.py"],
        report="已修复，测试通过。",
    )
    assert verdict["blocking"] is True
    assert verdict["checks"]["no_false_claim"] is False


def test_verifier_does_not_punish_environment_or_approval_problems():
    """v0.9.1 实测教训：没有测试配置 / 审批未获批不是代码失败，不能改判整轮。"""
    for status in ("skipped", "awaiting_approval", "denied"):
        verdict = verify(
            goal_spec={"objective": "改样式"},
            tool_results=[{"tool": "apply_patch", "status": "ok"}],
            validation={"status": status},
            changed_paths=["a.css"],
            report="样式已调整。",
        )
        assert verdict["blocking"] is False, status
        assert verdict["checks"]["no_false_claim"] is True
        assert any("没有可采信" in item or "测试未通过" in item for item in verdict["findings"])


def test_verifier_is_independent_and_readonly_safe():
    readonly = verify(goal_spec={}, validation=None, readonly=True, direct_answer=True)
    assert readonly["verdict"] == "not_applicable" and readonly["passed"] is True
    real_failure = verify(
        goal_spec={},
        validation={"status": "failed", "exit_code": 1},
        changed_paths=["a.py"],
        report="改了一处。",
    )
    assert real_failure["checks"]["exit_code_zero"] is None
    assert real_failure["checks"]["tests_executed"] is True


def test_verifier_flags_refused_calls_and_missing_delivery():
    verdict = verify(
        goal_spec={"objective": "改代码"},
        tool_results=[{"tool": "apply_patch", "status": "denied"}],
        validation={"status": "skipped"},
        changed_paths=["a.py"],
        report="",
    )
    assert verdict["checks"]["no_overreach"] is False
    assert verdict["checks"]["delivery_evidence"] is False
    assert verdict["blocking"] is False  # 事实不足只记 finding，不硬改判


# --------------------------------------------------------------------------- Dispatcher


def _judgement(**overrides) -> dict:
    base = {
        "intent": "execute",
        "needs_task": True,
        "needs_approval": False,
        "needs_clarification": False,
        "priority": "normal",
        "reason": "用户要求修改代码",
        "goal_id": "g-1",
    }
    return {**base, **overrides}


def test_dispatcher_auto_starts_when_all_seven_conditions_hold():
    d = decide_dispatch(
        _judgement(), project_id="p1", scenario="dev",
        automation_mode="assist", permission_mode="default",
    )
    assert d["decision"] == "immediate" and d["auto_start"] is True
    assert all(d["conditions"].values())
    assert d["goal_id"] == "g-1"


def test_each_dispatch_condition_can_stop_the_auto_start():
    cases = [
        ({"project_id": "", "has_workspace": False}, "wait_user"),
        ({"scenario": ""}, "wait_user"),
        ({"permission_mode": ""}, "wait_user"),
        ({"risk_known": False}, "wait_approval"),
        ({"has_clarification": True}, "wait_user"),
        ({"budget_left": False}, "blocked"),
        ({"automation_mode": "manual"}, "queue"),
        ({"has_active_task": True}, "queue"),
    ]
    base = {"project_id": "p1", "scenario": "dev", "permission_mode": "default"}
    for kwargs, expected in cases:
        d = decide_dispatch(_judgement(), **{**base, **kwargs})
        assert d["decision"] == expected, (kwargs, d)
        assert d["auto_start"] is False, kwargs
        assert d["reasons"]


def test_autonomous_mode_still_cannot_skip_approval_or_clarification():
    d = decide_dispatch(
        _judgement(needs_approval=True), project_id="p1", scenario="dev",
        automation_mode="autonomous", permission_mode="bypass", risk_known=True,
    )
    assert d["decision"] == "wait_approval" and d["auto_start"] is False
    assert "需先审批" in d["reasons"][0]


def test_dispatcher_refuses_a_message_that_is_not_a_goal():
    d = decide_dispatch(_judgement(needs_task=False), project_id="p1", scenario="dev")
    assert d["decision"] == "blocked" and d["auto_start"] is False


def test_dispatch_snapshot_can_find_the_task_three_ways():
    d = decide_dispatch(_judgement(), project_id="p1", scenario="dev")
    snap = dispatch_snapshot(
        _judgement(), d, task_id="t1", run_id="r1", conversation_id="c1", created_at="2026-11-06T18:00:00",
    )
    assert snap["task_id"] == "t1" and snap["run_id"] == "r1"
    assert snap["conversation_id"] == "c1" and snap["goal_id"] == "g-1"
    assert snap["auto_start"] is True and snap["decision"] == "immediate"
    # 首句时目标刚建好，judgement 里还没有 id；落库的 goal_id 必须优先。
    fresh = dispatch_snapshot({}, d, task_id="t2", goal_id="g-real")
    assert fresh["goal_id"] == "g-real"
