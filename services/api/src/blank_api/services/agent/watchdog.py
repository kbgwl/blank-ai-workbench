"""Watchdog：判断一条 run 是不是"在跑但没在动"，并把判断落成一次可读的诊断。

它**不提高权限、不扩大工作区、不换模型**（任务书 §5）。这一层能做的只有四件事：
记录诊断、通知一次、请求放行、请求终止。把"能不能继续跑"的判断放在这里、把
"怎么跑"留在运行时，是为了让卡死这件事有一个独立于执行者的观察者——执行者自己
报告"我还活着"是最不可信的信号。

三条口径：

1. **只看事实，不做猜测。** 心跳还在续、但事件流十分钟没有新增，和心跳已经停了
   是两件不同的事：前者可能是长测试，后者是进程没了。混成一句"卡住了"，用户就
   不知道该等还是该重试。
2. **同一条诊断只通知一次。** 五分钟扫一次、一次扫十条，不设门槛就是一晚上几百条
   通知，用户会关掉通知——那比不通知更糟。
3. **超预算不是失败。** 步数/时间用完时动作是"要求收尾"，不是"判死"：已经做过的
   改动还在，判死会让用户以为全部作废。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from blank_api.models.run import AgentRun
from blank_api.models.task import AgentEvent, Task, ToolCall

# 动作，从轻到重。调用方只需按顺序升级，不必自己组合。
ACTION_OK = "ok"
ACTION_DIAGNOSE = "diagnose"
ACTION_NOTIFY = "notify"
ACTION_RELEASE = "release"
ACTION_PAUSE = "pause"
ACTION_TERMINATE = "terminate"

ACTION_ORDER = (ACTION_OK, ACTION_DIAGNOSE, ACTION_NOTIFY, ACTION_RELEASE, ACTION_PAUSE, ACTION_TERMINATE)

# 事件流静默多久算"没在动"。5 分钟是长测试/大补丁的正常上界；再长用户就该知道。
SILENCE_SECONDS = 300
# 静默到这个程度就不是慢，而是卡住了：请求暂停。
STUCK_SECONDS = 900
# 审批挂太久：用户可能没看见，也可能是卡片被关掉了。15 分钟后提醒一次。
APPROVAL_STALE_SECONDS = 900


def _aware(value: datetime | None) -> datetime | None:
    """库里存的是无时区 UTC；比较前统一成 naive UTC，避免 aware/naive 相减报错。"""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def now_utc() -> datetime:
    """库里的时间是"无时区 UTC"；判断也统一用这个口径，避免本地时区混进来。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class Pulse:
    """一条 run 的体检数据。全部来自库，不看进程内存——观察者不能在执行者里面。"""

    run_id: str
    task_id: str
    status: str
    attempts: int
    max_recoveries: int = 2
    max_steps: int = 200
    # 耗时预算（秒）。0 表示这条 run 没声明时间上限（老数据/未派发），不参与判定。
    max_seconds: int = 0
    steps_used: int = 0
    lease_expires_at: datetime | None = None
    last_event_at: datetime | None = None
    started_at: datetime | None = None
    approval_waiting_since: datetime | None = None
    has_delivery: bool = False
    tokens_used: int = 0
    token_budget: int = 0
    now: datetime = field(default_factory=lambda: now_utc())

    # ── 单项判断 ────────────────────────────────────────────────────────────
    @property
    def lease_expired(self) -> bool:
        lease = _aware(self.lease_expires_at)
        return lease is not None and lease <= self.now

    @property
    def silent_seconds(self) -> int:
        last = _aware(self.last_event_at) or _aware(self.started_at)
        if last is None:
            return 0
        return max(0, int((self.now - last).total_seconds()))

    @property
    def approval_wait_seconds(self) -> int:
        since = _aware(self.approval_waiting_since)
        if since is None:
            return 0
        return max(0, int((self.now - since).total_seconds()))

    @property
    def steps_exhausted(self) -> bool:
        return self.max_steps > 0 and self.steps_used >= self.max_steps

    @property
    def elapsed_seconds(self) -> int:
        started = _aware(self.started_at)
        if started is None:
            return 0
        return max(0, int((self.now - started).total_seconds()))

    @property
    def time_exhausted(self) -> bool:
        """耗时预算用完。和步数/token 一样，动作是「收尾」不是「判死」。"""
        return self.max_seconds > 0 and self.elapsed_seconds >= self.max_seconds

    @property
    def tokens_exhausted(self) -> bool:
        return self.token_budget > 0 and self.tokens_used >= self.token_budget


@dataclass(frozen=True)
class Verdict:
    action: str
    reasons: tuple[str, ...] = ()
    # 是否该发一条通知（同一条诊断只发一次，由调用方按 reason 去重）。
    notify: bool = False

    @property
    def needs_attention(self) -> bool:
        return self.action != ACTION_OK

    def as_dict(self) -> dict:
        return {"action": self.action, "reasons": list(self.reasons), "notify": self.notify}


def inspect(pulse: Pulse) -> Verdict:
    """纯判断：给定体检数据，给出该做什么。

    顺序是刻意的：先看"已经没人跑"，再看"还在跑但没动"，最后看预算。
    反过来会把一条已经死掉的 run 报成"预算超了"，而它其实一步都没再走。
    """
    reasons: list[str] = []
    action = ACTION_OK

    def bump(level: str, reason: str) -> None:
        nonlocal action
        reasons.append(reason)
        if ACTION_ORDER.index(level) > ACTION_ORDER.index(action):
            action = level

    if pulse.status in ("succeeded", "failed", "cancelled"):
        # 终态之后只剩一件事值得看：说要交付，却没有任何交付证据。
        if pulse.status == "succeeded" and not pulse.has_delivery:
            bump(ACTION_DIAGNOSE, "任务已成功但没有任何交付证据（无完成事件/无工具结果）")
        return Verdict(action, tuple(reasons), notify=action == ACTION_DIAGNOSE)

    if pulse.attempts > pulse.max_recoveries:
        bump(ACTION_TERMINATE, f"已恢复 {pulse.attempts} 次，超过上限 {pulse.max_recoveries}")
        return Verdict(action, tuple(reasons), notify=True)

    if pulse.lease_expired:
        # 租约过期 = 执行者没再续心跳。恢复次数还有余量就放行给下一个 worker，
        # 没有余量就判死：无限重排同一条 run 只会空转。
        if pulse.attempts >= pulse.max_recoveries:
            bump(ACTION_TERMINATE, f"租约已过期，且已恢复 {pulse.attempts} 次（上限 {pulse.max_recoveries}）")
        else:
            bump(ACTION_RELEASE, f"租约已过期，可交给下一个 worker 接管（第 {pulse.attempts} 次恢复）")

    if pulse.steps_exhausted:
        bump(ACTION_PAUSE, f"步数预算用完（{pulse.steps_used}/{pulse.max_steps}），应当收尾而不是继续开新工作面")
    if pulse.time_exhausted:
        bump(ACTION_PAUSE, f"耗时预算用完（{pulse.elapsed_seconds}s/{pulse.max_seconds}s），应当收尾")
    if pulse.tokens_exhausted:
        bump(ACTION_PAUSE, f"token 预算用完（{pulse.tokens_used}/{pulse.token_budget}），应当收尾")

    waited = pulse.approval_wait_seconds
    if waited >= APPROVAL_STALE_SECONDS:
        # 只在等审批时提醒，不等就判死：用户可能只是还没回来看。
        bump(ACTION_NOTIFY, f"审批已等待 {waited // 60} 分钟，任务停在这里")

    # 等审批时"没有新事件"是正常的——那是用户在看着卡片，不是卡死。
    # 这种情况交给上面的审批等待判断，不当成静默。
    silent = pulse.silent_seconds
    if not pulse.approval_waiting_since:
        if silent >= STUCK_SECONDS:
            bump(ACTION_PAUSE, f"已有 {silent // 60} 分钟没有新事件，疑似卡死")
        elif silent >= SILENCE_SECONDS:
            bump(ACTION_DIAGNOSE, f"已有 {silent // 60} 分钟没有新事件（可能在跑长任务）")

    notify = action in (ACTION_NOTIFY, ACTION_RELEASE, ACTION_PAUSE, ACTION_TERMINATE)
    return Verdict(action, tuple(reasons), notify=notify)


# ── 落库：把判断变成一次可读的诊断 ──────────────────────────────────────────


async def _last_event_at(db: AsyncSession, task_id: str, run_id: str) -> datetime | None:
    stmt = select(AgentEvent.created_at).where(AgentEvent.task_id == task_id)
    if run_id:
        stmt = stmt.where(AgentEvent.run_id == run_id)
    return (await db.execute(stmt.order_by(AgentEvent.seq.desc()).limit(1))).scalar_one_or_none()


async def _approval_since(db: AsyncSession, task_id: str) -> datetime | None:
    return (
        await db.execute(
            select(ToolCall.created_at)
            .where(ToolCall.task_id == task_id, ToolCall.status == "awaiting_approval")
            .order_by(ToolCall.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _has_delivery(db: AsyncSession, task_id: str) -> bool:
    row = (
        await db.execute(
            select(AgentEvent.id)
            .where(
                AgentEvent.task_id == task_id,
                AgentEvent.type.in_(("task.completed", "validation.completed", "delivery.recorded")),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


def _budget_int(snapshot: Any, key: str) -> int:
    """从派发快照里取一项预算上限。读不出来按 0（= 不参与判定），绝不猜一个默认值。"""
    if not isinstance(snapshot, dict):
        return 0
    budget = snapshot.get("budget")
    if not isinstance(budget, dict):
        return 0
    try:
        return max(0, int(budget.get(key) or 0))
    except (TypeError, ValueError):
        return 0


async def pulses(db: AsyncSession, *, now: datetime | None = None, limit: int = 50) -> list[Pulse]:
    """把当前所有 running 的 run 读成体检数据。"""
    moment = now or now_utc()
    runs = (
        await db.execute(
            select(AgentRun).where(AgentRun.status == "running").limit(limit)
        )
    ).scalars().all()
    result: list[Pulse] = []
    for run in runs:
        task = await db.get(Task, run.task_id)
        if task is None:
            continue
        result.append(
            Pulse(
                run_id=run.id,
                task_id=run.task_id,
                status=run.status,
                attempts=int(run.attempts or 0),
                max_steps=int(task.max_steps or 0),
                max_seconds=_budget_int(task.dispatch_snapshot, "max_seconds"),
                steps_used=int(task.steps_used or 0),
                lease_expires_at=run.lease_expires_at,
                last_event_at=await _last_event_at(db, run.task_id, run.id),
                started_at=run.created_at,
                approval_waiting_since=await _approval_since(db, run.task_id),
                has_delivery=await _has_delivery(db, run.task_id),
                now=moment,
            )
        )
    return result


async def already_alerted(db: AsyncSession, run_id: str, reason: str) -> bool:
    """同一条诊断只发一次：扫描是周期性的，不设门槛就是一晚上几百条通知。"""
    rows = (
        await db.execute(
            select(AgentEvent.payload)
            .where(AgentEvent.type == "watchdog.alert")
            .order_by(AgentEvent.seq.desc())
            .limit(200)
        )
    ).scalars().all()
    for payload in rows:
        if not isinstance(payload, dict):
            continue
        if payload.get("run_id") == run_id and payload.get("reason") == reason:
            return True
    return False


async def sweep(db: AsyncSession, *, now: datetime | None = None) -> list[dict]:
    """扫一轮、落诊断。返回这一轮真正记下来的告警（已去重的不再返回）。

    只写事件，不改 run 状态：判死/放行是 worker 的活。让观察者同时当执行者，
    出问题时就没有第三方能说"当时它看到的是什么"。
    """
    from blank_api.services.agent.events import EventRecorder
    from blank_api.db import get_session_factory

    recorded: list[dict] = []
    for pulse in await pulses(db, now=now):
        verdict = inspect(pulse)
        if not verdict.needs_attention:
            continue
        for reason in verdict.reasons:
            if await already_alerted(db, pulse.run_id, reason):
                continue
            await EventRecorder(get_session_factory(), pulse.task_id, run_id=pulse.run_id).add(
                "watchdog.alert",
                "watchdog",
                {
                    "run_id": pulse.run_id,
                    "action": verdict.action,
                    "reason": reason,
                    "silent_seconds": pulse.silent_seconds,
                    "approval_wait_seconds": pulse.approval_wait_seconds,
                    "attempts": pulse.attempts,
                    "max_recoveries": pulse.max_recoveries,
                    "steps_used": pulse.steps_used,
                    "max_steps": pulse.max_steps,
                    # 只报"建议动作"，不假装已经执行：改了 run 状态而不改执行者，
                    # 会造出"库里说暂停、进程还在跑"的假象。
                    "enforced": False,
                },
            )
            recorded.append(
                {"run_id": pulse.run_id, "task_id": pulse.task_id, "action": verdict.action, "reason": reason}
            )
    return recorded


def explain(verdict: Verdict) -> str:
    if not verdict.reasons:
        return "正常"
    return "；".join(verdict.reasons)


__all__ = [
    "ACTION_DIAGNOSE",
    "ACTION_NOTIFY",
    "ACTION_OK",
    "ACTION_PAUSE",
    "ACTION_RELEASE",
    "ACTION_TERMINATE",
    "Pulse",
    "Verdict",
    "already_alerted",
    "explain",
    "inspect",
    "now_utc",
    "pulses",
    "sweep",
]
