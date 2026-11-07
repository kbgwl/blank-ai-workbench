"""v165 §5 Task Dispatcher：自动下发条件判定与下发快照。

下发不是「收到消息就开跑」。v165 §5.1 的七个条件任一不成立，就必须停在
等待/询问，而不是先跑起来再失败——用户实测里「同意了却说没同意」，
根子就是把环境/授权/澄清问题混进了执行结果。

模块只做**判定**（纯函数），真正创建 task / run / 事件的动作仍在
``threads.create_turn`` 和 ``tasks.start_task``，避免再造一套状态机。
"""

from __future__ import annotations

from typing import Any

AUTOMATION_MODES = ("manual", "assist", "autonomous")
DECISIONS = ("immediate", "queue", "wait_approval", "wait_user", "blocked")
PRIORITIES = ("urgent", "high", "normal", "low")

# 与 v165 §8 对齐。manual 必须由用户点执行，autonomous 也不能跳过审批。
_MODE_ALLOWS_AUTO = {"manual": False, "assist": True, "autonomous": True}


def _judgement_dict(judgement: Any) -> dict[str, Any]:
    if judgement is None:
        return {}
    if hasattr(judgement, "as_dict"):
        return dict(judgement.as_dict())
    return dict(judgement) if isinstance(judgement, dict) else {}


def decide_dispatch(
    judgement: Any,
    *,
    project_id: str = "",
    scenario: str = "",
    automation_mode: str = "assist",
    permission_mode: str = "default",
    has_workspace: bool = True,
    risk_known: bool = True,
    has_clarification: bool = False,
    has_active_task: bool = False,
    budget_left: bool = True,
) -> dict[str, Any]:
    """按 §5.1 七条件判定这一轮该怎么下发。

    ``auto_start`` 直接决定前端要不要替用户点「运行」：这是「不用一直催」的关键，
    但只在不违反任何一条前置条件时才为 True。
    """
    j = _judgement_dict(judgement)
    mode = automation_mode if automation_mode in AUTOMATION_MODES else "assist"
    priority = str(j.get("priority") or "normal")
    if priority not in PRIORITIES:
        priority = "normal"

    conditions = {
        "goal_clear": bool(j.get("needs_task", True))
        and (bool(j.get("intent")) or bool(j.get("reason"))),
        "workspace_bound": bool(project_id) and has_workspace,
        "scenario_set": bool(scenario),
        "permission_known": bool(permission_mode),
        "risk_assessable": risk_known,
        "mode_allows": _MODE_ALLOWS_AUTO[mode],
        "no_clarification": not has_clarification and not j.get("needs_clarification", False),
        "budget_left": budget_left,
    }
    reasons: list[str] = []

    if not conditions["goal_clear"]:
        return _result("blocked", False, priority, conditions, reasons + ["消息没有形成可下发的目标"], j, scenario, mode, permission_mode)
    if not conditions["workspace_bound"]:
        return _result("wait_user", False, priority, conditions, reasons + ["当前会话没有绑定工作区"], j, scenario, mode, permission_mode)
    if not conditions["scenario_set"]:
        return _result("wait_user", False, priority, conditions, reasons + ["场景未确定，请先在界面选择"], j, scenario, mode, permission_mode)
    if not conditions["permission_known"]:
        return _result("wait_user", False, priority, conditions, reasons + ["工具权限档位无法确定"], j, scenario, mode, permission_mode)
    if not conditions["budget_left"]:
        return _result("blocked", False, priority, conditions, reasons + ["目标预算已用尽，停止自动下发"], j, scenario, mode, permission_mode)
    if not conditions["risk_assessable"]:
        return _result("wait_approval", False, priority, conditions, reasons + ["风险无法评估，需要人工确认"], j, scenario, mode, permission_mode)
    if not conditions["no_clarification"]:
        return _result("wait_user", False, priority, conditions, reasons + ["有必须先让用户补充的信息"], j, scenario, mode, permission_mode)
    if j.get("needs_approval", False):
        return _result("wait_approval", False, priority, conditions, reasons + ["该目标含高风险动作，需先审批"], j, scenario, mode, permission_mode)
    if not conditions["mode_allows"]:
        return _result("queue", False, priority, conditions, reasons + ["手动模式：任务已排队，等待你点击执行"], j, scenario, mode, permission_mode)
    if has_active_task:
        return _result("queue", False, priority, conditions, reasons + ["已有进行中的任务，本轮排队"], j, scenario, mode, permission_mode)

    return _result(
        "immediate", True, priority, conditions,
        reasons + [f"{mode} 模式：条件齐备，自动下发"], j, scenario, mode, permission_mode,
    )


def _result(
    decision: str,
    auto_start: bool,
    priority: str,
    conditions: dict[str, bool],
    reasons: list[str],
    j: dict[str, Any],
    scenario: str,
    mode: str,
    permission_mode: str,
) -> dict[str, Any]:
    return {
        "decision": decision,
        "auto_start": auto_start,
        "priority": priority,
        "conditions": conditions,
        "reasons": reasons,
        "goal_id": str(j.get("goal_id") or ""),
        "related_task_id": str(j.get("related_task_id") or ""),
        "scenario": scenario,
        "automation_mode": mode,
        "permission_mode": permission_mode,
        "intent": str(j.get("intent") or ""),
    }


def dispatch_snapshot(
    judgement: Any,
    decision: dict[str, Any],
    *,
    task_id: str = "",
    run_id: str = "",
    conversation_id: str = "",
    goal_id: str = "",
    created_at: str = "",
) -> dict[str, Any]:
    """v165 §5.2：一次下发必须能靠 goal_id / task_id / conversation_id 找回。

    ``goal_id`` 必须由调用方传落库后的那个：首句时目标是在 observe() 之后才建的，
    判定里还没有 id，只从 judgement 取会让第一条下发的快照永远查不回目标。
    时间同样由调用方给（纯函数），这样快照可重放、可比对。
    """
    j = _judgement_dict(judgement)
    return {
        "task_id": task_id,
        "run_id": run_id,
        "conversation_id": conversation_id,
        "goal_id": goal_id or decision.get("goal_id") or str(j.get("goal_id") or ""),
        "intent": decision.get("intent") or str(j.get("intent") or ""),
        "decision": decision.get("decision"),
        "auto_start": bool(decision.get("auto_start")),
        "scenario": decision.get("scenario"),
        "automation_mode": decision.get("automation_mode"),
        "permission_mode": decision.get("permission_mode"),
        "priority": decision.get("priority"),
        "conditions": dict(decision.get("conditions") or {}),
        "reasons": list(decision.get("reasons") or []),
        "created_at": created_at,
    }
