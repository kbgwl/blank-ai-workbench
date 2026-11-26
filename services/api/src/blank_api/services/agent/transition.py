"""节点之间为什么跳（transition）。

一轮任务在图上走的是 ``create_plan → execute_action ⇄ run_validation → review_result
→ summarize_result`` 这张网。以前时间线上只能看到「进了哪个节点」（``node.started``），
看不到「为什么进」——于是同一段执行连续跑三次时，用户分不清是"还有步骤没做完"、
"校验没过在修"、还是"审查驳回又退回"，只能靠翻工具卡片猜。这一层补的就是那句话。

设计取舍：

* **只记「进入」，不记「离开」。** 一次跳转只该有一行；离开+进入会让时间线上同一件事
  出现两次，阅读成本翻倍，收益为零。
* **不记「进入 finish」的那一步。** 收尾已经有终态码事件（``task.completed`` /
  ``task.failed`` / ``task.cancelled``，见 ``services/agent/terminal.py``），
  "为什么停"已经说得比这里更准；再记一条只是把同一句话写两遍。
* **解释由事实推出，不由模型自报。** 与 ``verifier.py`` 同一口径：``rule`` 是机读码，
  ``why`` 是给人看的一句，两者都只吃 state 里的字段（校验状态、修复轮次、待办条数…），
  不调模型。模型自报的"我因为 X 继续"在时间线上不可核对，宁可不要。
* **判定顺序必须与 ``graph.route_after_*`` 逐条一致。** 这是一份重复实现，天然有漂移
  风险；``tests/test_transition_explain_v432.py`` 用一张对照表把两边钉在一起——改了路由
  不同步改这里，那张表会红。漂移没被认出来时退化成 ``unexplained``，仍然记账、仍然可见。

调用点是 ``graph.build_agent_graph`` 里的边：路由函数算完目标之后顺手记一条
``AgentSession.pending_transition``，进入的那个节点在入口把它落成 ``agent.transition``
事件。放在节点入口而不是路由函数里，是因为路由函数是同步的、而落事件要等数据库；
而且审批恢复时路由不会重跑，挂在入口才不会重复记账。
"""

from __future__ import annotations

from collections.abc import Mapping

# 校验没通过的三种状态都算"要修"：``error`` 是工具自己报错（run_tests 跑不起来），
# 与 ``failed``（跑了但没通过）在修复决策上是同一件事。与 route_after_validation 同口径。
FAILED_VALIDATION_STATES = ("failed", "timeout", "error")

# 认不出来的跳转用它。刻意不是异常：记账失败不该让任务挂掉，但也绝不能悄悄记成
# 一个看起来正常的码——时间线上写着 unexplained，读的人才知道"这句解释没跟上代码"。
UNEXPLAINED = "unexplained"


def _pending_count(state: Mapping) -> int:
    pending = state.get("pending") or []
    try:
        return len(pending)
    except TypeError:
        return 0


def _plan_count(state: Mapping) -> int:
    steps = state.get("plan_steps") or state.get("plan") or []
    try:
        return len(steps)
    except TypeError:
        return 0


def _result(frm: str, to: str, rule: str, why: str, **facts) -> dict:
    """一条跳转记录。``rule`` 机读、``why`` 给人看，``facts`` 是支撑这句话的数字。"""
    return {"from": frm, "to": to, "rule": rule, "why": why, **facts}


def explain(
    frm: str,
    to: str,
    state: Mapping,
    *,
    readonly: bool = False,
    max_repair: int | None = None,
    max_review: int | None = None,
) -> dict:
    """``frm`` 节点为什么把控制权交给了 ``to``。

    ``readonly`` 由调用方把 ``state["readonly"]`` 与会话级只读合并后传入：
    ``route_after_validation`` 读的是两者的"或"，只看 state 会在会话级只读时解释错。
    """
    state = state or {}
    key = (frm, to)

    if key == ("create_plan", "summarize_result"):
        return _result(frm, to, "direct_answer", "判定这次不用动工作区（闲聊/概念提问），直接作答")

    if key == ("create_plan", "execute_action"):
        return _result(
            frm, to, "plan_ready", f"拆出 {_plan_count(state)} 步计划，进入执行",
            steps=_plan_count(state),
        )

    if key == ("execute_action", "finish"):
        status = str(state.get("status") or "")
        if status == "cancelled":
            return _result(frm, to, "cancelled", "这一轮被取消，直接收尾")
        if status == "paused":
            return _result(frm, to, "paused", "这一轮被暂停，先收尾，恢复后接着干")
        error = state.get("error") or {}
        kind = str(error.get("type") or error.get("code") or "error") if isinstance(error, dict) else "error"
        return _result(frm, to, "errored", f"执行出错（{kind}），直接收尾，不再进修复回路", error=kind)

    if key == ("execute_action", "execute_action"):
        if state.get("gap_continue"):
            return _result(frm, to, "environment_gap", "环境缺口还没补上，继续执行去装/去找，不把「需要你装」当交付")
        if state.get("act_continue"):
            return _result(
                frm, to, "plan_incomplete",
                "计划里有要做的事却只看不动手，推回执行要求直接完成",
                round=int(state.get("act_nudges") or 0),
            )
        count = _pending_count(state)
        return _result(frm, to, "steps_remaining", f"还有 {count} 项待办，继续执行", pending=count)

    if key == ("execute_action", "run_validation"):
        return _result(frm, to, "steps_done", "没有待办步骤了，进入校验")

    if key == ("execute_action", "summarize_result"):
        return _result(
            frm, to, "list_only",
            "只要文件/目录清单：已拿到 list_files/glob 回执，直接按回执出列表",
        )

    if key == ("run_validation", "summarize_result"):
        if readonly:
            return _result(frm, to, "readonly_no_artifact", "只读任务（征求意见/排查），没有交付物可校验，直接总结")
        validation = state.get("validation") or {}
        status = str(validation.get("status") or "") if isinstance(validation, dict) else ""
        if status == "skipped":
            return _result(frm, to, "validation_skipped", "这一步没有可跑的校验命令，跳过校验直接总结")
        return _result(frm, to, UNEXPLAINED, "")

    if key == ("run_validation", "execute_action"):
        validation = state.get("validation") or {}
        status = str(validation.get("status") or "") if isinstance(validation, dict) else ""
        if (
            state.get("budget_exhausted")
            and str(validation.get("command") or "") == "ui_check"
            and status == "failed"
        ):
            return _result(
                frm, to, "ui_repair_reserve",
                "前端自检未过、预算已尽，保留一次 UI 修复",
                validation=status,
            )
        rounds = int(state.get("repair_rounds") or 0)
        return _result(
            frm, to, "validation_failed_retry",
            f"校验没通过（{status or '失败'}），回执行修（已用 {rounds}/{max_repair} 轮修复）",
            validation=status, repair_rounds=rounds, max_repair=max_repair,
        )

    if key == ("run_validation", "review_result"):
        validation = state.get("validation") or {}
        status = str(validation.get("status") or "") if isinstance(validation, dict) else ""
        if state.get("budget_exhausted"):
            return _result(frm, to, "budget_exhausted", "步数预算耗尽，修不动了，直接进审查定性")
        rounds = int(state.get("repair_rounds") or 0)
        if status in FAILED_VALIDATION_STATES:
            # 两种"不再修"的原因要分开说：轮次用尽（预算账）与同一处反复失败（没新证据）。
            # 混成一句会让用户以为是预算不够，于是去加预算——加了也不会好。
            if max_repair is not None and rounds > max_repair:
                return _result(
                    frm, to, "repair_budget_spent",
                    f"校验没通过（{status}），修复轮次已用尽（{rounds}/{max_repair}），交给审查定性",
                    validation=status, repair_rounds=rounds, max_repair=max_repair,
                )
            return _result(
                frm, to, "repair_exhausted",
                f"校验没通过（{status}），但修复已经没有新证据可依（同一处反复失败），交给审查定性",
                validation=status, repair_rounds=rounds, max_repair=max_repair,
            )
        if status != "passed":
            # 没跑过校验（validation 为空）也会走到这里：路由没有失败证据就不该再修，
            # 但这不等于"验过了所以通过"。旧版本在这里落了 validation_passed，
            # 时间线上写"校验通过（通过）"——用户会以为有人验过。
            return _result(
                frm, to, "validation_not_observed",
                "这一步没有校验结果（没跑过，或结果没回来），没有失败证据，直接进审查",
                validation=status,
            )
        return _result(
            frm, to, "validation_passed", f"校验通过（{status or '通过'}），进入审查",
            validation=status,
        )

    if key == ("review_result", "execute_action"):
        review = state.get("review") or {}
        rounds = int(state.get("review_rounds") or 0)
        issues = review.get("issues") if isinstance(review, dict) else None
        count = len(issues) if isinstance(issues, list) else 0
        why = f"审查驳回（{count} 条问题），退回执行修" if count else "审查驳回，退回执行修"
        return _result(
            frm, to, "review_rejected_retry", f"{why}（已用 {rounds}/{max_review} 轮审查）",
            review_rounds=rounds, max_review=max_review, issues=count,
        )

    if key == ("review_result", "summarize_result"):
        review = state.get("review") or {}
        approved = bool(review.get("approved")) if isinstance(review, dict) else False
        rounds = int(state.get("review_rounds") or 0)
        if state.get("budget_exhausted"):
            return _result(frm, to, "budget_exhausted", "步数预算耗尽，不再修，按现状收尾")
        if max_review is not None and rounds >= max_review:
            return _result(
                frm, to, "review_round_limit",
                f"审查轮次用尽（{rounds}/{max_review}），按现状收尾", review_rounds=rounds,
            )
        if approved:
            return _result(frm, to, "review_approved", "审查通过，进入收尾")
        rounds_used = int(state.get("repair_rounds") or 0)
        return _result(
            frm, to, "repair_budget_spent",
            f"审查没通过，但修复预算已用尽（{rounds_used}/{max_repair}），按现状收尾",
            repair_rounds=rounds_used, max_repair=max_repair,
        )

    if key == ("summarize_result", "execute_action"):
        return _result(frm, to, "stop_blocked", "交付前的 Stop 钩子把这次收尾拦下了，带着它的理由继续干")

    if key == ("summarize_result", "finish"):
        return _result(frm, to, "delivered", "收尾完成，本轮结束")

    return _result(frm, to, UNEXPLAINED, "")
