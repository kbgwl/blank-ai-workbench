"""低打扰通知门：一个后台事件到底该不该打扰用户。

四种策略来自 ``dispatch.py``：silent / milestone / approval_only / always。
产品默认 milestone 或 approval_only——后台任务刷屏比不通知更糟，用户会关掉通知，
那之后连"审批等你点"也收不到了。

这一层只回答"要不要打扰"，不负责怎么发。判断与投递分开，测试才不用起一个
邮件/推送服务器；接新通道时也只需要实现投递，不必重写判断。
"""

from __future__ import annotations

from blank_api.services.agent.dispatch import (
    NOTIFY_ALWAYS,
    NOTIFY_APPROVAL_ONLY,
    NOTIFY_MILESTONE,
    NOTIFY_SILENT,
)

# 事件类型。少而固定：加一种要先回答"它比审批更急吗"。
EVENT_APPROVAL = "approval"  # 需要用户点批准，任务停在这里
EVENT_FAILURE = "failure"  # 无法自动恢复的失败
EVENT_DELIVERABLE = "deliverable"  # 完成、有交付物
EVENT_MILESTONE = "milestone"  # 阶段性节点（长任务的中间进度）
EVENT_PROGRESS = "progress"  # 普通进度

EVENT_KINDS = (
    EVENT_APPROVAL,
    EVENT_FAILURE,
    EVENT_DELIVERABLE,
    EVENT_MILESTONE,
    EVENT_PROGRESS,
)


def should_disturb(policy: str, event_kind: str, *, quiet_hours: bool = False) -> bool:
    """这一个事件现在该不该打扰用户。

    - 审批永远打扰：任务停在等批准，压到第二天早上等于白跑一晚。这也是 silent
      策略下唯一会发出的事件——"静默"不等于"让任务停死"。
    - 免打扰时段只放审批过去，其余等到时段结束。
    - 认不出的策略按 approval_only 处理：宁可少说，不要多说。
    """
    kind = str(event_kind or "").strip().lower()
    if kind not in EVENT_KINDS:
        return False
    if kind == EVENT_APPROVAL:
        # 审批是唯一"压着就动不了"的事件，静默策略下也放行——否则任务会停到天亮。
        return True
    if quiet_hours:
        return False
    if policy == NOTIFY_ALWAYS:
        return True
    if policy == NOTIFY_MILESTONE:
        return kind in (EVENT_FAILURE, EVENT_DELIVERABLE, EVENT_MILESTONE)
    if policy == NOTIFY_APPROVAL_ONLY:
        return False
    # silent 与未知值：都不打扰。审批在上面已经单独放行。
    return False


def explain(policy: str, event_kind: str, *, quiet_hours: bool = False) -> dict:
    """给证据页看的判定结果：要不要打扰、为什么。"""
    disturb = should_disturb(policy, event_kind, quiet_hours=quiet_hours)
    if disturb:
        reason = "审批等待" if event_kind == EVENT_APPROVAL else f"命中 {policy} 策略"
    elif quiet_hours:
        reason = "免打扰时段，等时段结束"
    else:
        reason = f"{policy} 策略下不打扰"
    return {"policy": policy, "event": event_kind, "quiet_hours": quiet_hours, "disturb": disturb, "reason": reason}


__all__ = [
    "EVENT_APPROVAL",
    "EVENT_DELIVERABLE",
    "EVENT_FAILURE",
    "EVENT_KINDS",
    "EVENT_MILESTONE",
    "EVENT_PROGRESS",
    "explain",
    "should_disturb",
]
