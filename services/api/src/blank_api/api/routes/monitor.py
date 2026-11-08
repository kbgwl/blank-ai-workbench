"""监控：把「它正在盯什么」变成一屏读数。

v0.10.0 的 Chat Supervisor 其实一直在工作——每收到一条消息都会判一次
（answer / execute / continue / approve…），只是那件事只落在事件流里，
用户在界面上看不见，于是「持续监控聊天」这条能力等于隐形。

这个接口把四件事摊到一屏：

1. ``watching`` —— 监督者最近把每句话判成了什么（**本产品内的**聊天）
2. ``running``  —— 正在跑、等审批、被暂停的任务
3. ``pending``  —— 登记为「等事件 / 等时间」但还没跑起来的 watch、schedule
4. ``sources``  —— 触发源到底通没通（IM 三条明确标未接通）

口径：全部按库里的状态算，不看 Agent 自述。``pending`` 里 fire_at 为空、
又没有任何事件源会放行的那种，显式标 ``never_fires``——「登记了却永远等不到」
比假装它正在盯更糟，用户会以为自己在被监控着。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from blank_api.api.auth import require_token
from blank_api.deps import DbSession
from blank_api.models.task import Task
from blank_api.models.trigger import AgentTrigger
from blank_api.services.agent import triggers as triggers_module
from blank_api.services.features import is_enabled

router = APIRouter(tags=["monitor"], dependencies=[Depends(require_token)])

FEATURE = "background_execution"

# 任务在「还没结束」的状态。draft 也要算：v0.10.0 的自动下发是建完 draft
# 由后端就地启动，漏掉 draft 会出现「界面说它在跑、监控页是空的」。
OPEN_STATUSES = ("draft", "queued", "running", "waiting_approval", "paused")

# 触发源清单。``connected=False`` 的不是"暂不支持"这种模糊说法：库里确实
# 只有这张枚举，IM 类只留了位置，接的时候要连凭据与回调一起做。
_SOURCES: tuple[dict, ...] = (
    {
        "id": "user_task",
        "label": "你发的消息",
        "kind": "builtin",
        "connected": True,
        "note": "监督者每收到一条就判一次：要不要动手、要不要问、要不要接着上一轮",
    },
    {
        "id": "task_failed",
        "label": "任务失败",
        "kind": "builtin",
        "connected": True,
        "note": "失败会记下来，可自动开一轮修复",
    },
    {
        "id": "approval_expired",
        "label": "审批过期",
        "kind": "builtin",
        "connected": True,
        "note": "超时没批的操作回到对话，不会偷偷执行",
    },
    {
        "id": "workspace_changed",
        "label": "工作区文件变化",
        "kind": "builtin",
        "connected": True,
        "note": "每拍巡检扫一次：登记之后有文件改动才动，跳过 node_modules 等依赖目录",
    },
    {
        "id": "schedule",
        "label": "定时到点",
        "kind": "builtin",
        "connected": True,
        "note": "带 fire_at 的到点自动执行；每天/每周的跑完自动排下一次",
    },
)

# IM 三家的兜底行：客户还没配的时候也要出现在这一栏。
# v0.10.3 之前这里是写死的 ``connected=False``——于是「监控页说没接通」永远是假话，
# 客户配好了也还是那一句。现在这三行只是**兜底**：真配了通道就用库里的状态替换。
_IM_DEFAULTS: tuple[dict, ...] = (
    {
        "id": "wecom",
        "label": "企业微信",
        "kind": "im",
        "connected": False,
        "note": "未配置：设置 → 消息通道，填企业 ID 与应用 Secret 即可",
    },
    {
        "id": "dingtalk",
        "label": "钉钉",
        "kind": "im",
        "connected": False,
        "note": "未配置：设置 → 消息通道，填 Client ID/Secret 即可（长连接不需要公网）",
    },
    {
        "id": "feishu",
        "label": "飞书",
        "kind": "im",
        "connected": False,
        "note": "未配置：设置 → 消息通道，填 App ID/Secret 即可",
    },
)

# 这些事件类型没有对应的外部生产者：登记进来了也没有人放行。
_WITHOUT_PRODUCER = ("schedule",)


def _channel_row(channel) -> dict:
    """一条真实存在的通道。连接与否按库里的事实说，不按"配了就算通"。"""
    from blank_api.services.channels import adapter_for

    adapter = adapter_for(channel.kind)
    label = adapter.label if adapter else channel.kind
    inbound_at = _iso(channel.last_inbound_at)
    connected = bool(channel.status == "ok" or inbound_at)
    parts: list[str] = []
    if channel.status == "ok":
        parts.append("凭据已验证")
    elif channel.status == "failed":
        parts.append("上次验证失败")
    else:
        parts.append("未验证")
    if channel.receive and channel.receive_mode == "stream":
        parts.append("长连接收消息")
    elif channel.receive and channel.receive_mode == "webhook":
        parts.append("回调收消息")
    if inbound_at:
        parts.append(f"最近收到消息 {inbound_at}")
    else:
        parts.append("还没收到过消息")
    if channel.last_inbound_error:
        parts.append(f"最近入站错误：{channel.last_inbound_error[:120]}")
    elif channel.status == "failed" and not inbound_at:
        # 有消息进来之后，"上次验证失败"就是旧读数了——回调通了比一次凭据探测更有说服力。
        parts.append(channel.status_note[:160])
    return {
        "id": f"channel:{channel.id}",
        "label": f"{label} · {channel.name}",
        "kind": "im",
        "connected": connected,
        "note": "｜".join(parts),
    }


async def _im_sources(db) -> list[dict]:
    """IM 一栏的真实读数。没配的仅保留兜底行（说清去哪儿配），不假装接通。"""
    from blank_api.models.channel import Channel

    rows = (await db.execute(select(Channel).order_by(Channel.created_at))).scalars().all()
    by_kind: dict[str, list] = {}
    for row in rows:
        by_kind.setdefault(row.kind, []).append(row)
    out: list[dict] = []
    for default in _IM_DEFAULTS:
        configured = by_kind.pop(default["id"], [])
        if not configured:
            out.append(dict(default))
            continue
        out.extend(_channel_row(item) for item in configured)
    # 通用 Webhook 之类没写进兜底表的类型，配了就照实列出来。
    for items in by_kind.values():
        out.extend(_channel_row(item) for item in items)
    return out


def _iso(moment: datetime | None) -> str:
    return moment.isoformat() if moment else ""


def _supervisor_of(task: Task) -> dict:
    spec = task.goal_spec or {}
    block = spec.get("supervisor")
    return block if isinstance(block, dict) else {}


def _waiting_for(row: AgentTrigger) -> tuple[str, bool]:
    """这条触发器在等什么，以及它是不是永远等不到。

    ``triggers.due()`` 只捞 ``fire_at <= now`` 的行，注释写得很清楚：
    「fire_at 为空 = 等外部事件，不是等时间」。所以 ``fire_at`` 为空的行到底
    有没有救，取决于**它等的是什么事件**：

    * ``intent=watch``：等工作区文件变化。v0.10.4 起 ``watch_scan`` 每拍巡检
      真的会扫并对上 mtime —— 它等得到。
    * ``intent=schedule`` 却没有 ``fire_at``：既不被时间扫到，也没有事件源唤醒。
      这是缺陷，必须如实标出来，而不是让人以为它在等。
    """
    intent = str((row.payload or {}).get("intent") or "")
    if row.fire_at is not None:
        return f"到点 {_iso(row.fire_at)} 自动执行", False
    if intent == "watch":
        since = str((row.payload or {}).get("watch_since") or "")
        return (
            f"盯着工作区：{since[11:16]} 之后有文件变化就动（不是定时）" if since else
            "盯着工作区：文件有变化就动（不是定时）"
        ), False
    if row.event_type in _WITHOUT_PRODUCER:
        return "等一个不会到来的事件：没有到点时间，也没有对应的事件源", True
    return f"等外部事件：{row.event_type}", False


@router.get("/monitor")
async def monitor(db: DbSession, limit: int = Query(default=30, ge=1, le=200)) -> dict:
    """监督者判定流 + 在跑的任务 + 待触发项 + 触发源是否接通。"""
    cap = max(1, min(int(limit), 200))
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # ── 1. 监督者最近判定 ────────────────────────────────────────────────
    # 只看带 supervisor 段的任务：v0.9.1 的旧轮次没有这段，混进来会让
    # 「判成什么」这一列大面积空着，看起来像是判定失败了。
    # created_at 只到秒，同一秒连开两轮时顺序随机（见 Task.turn_index 的注释），
    # 所以再挂一个 updated_at 兜底；这里只决定展示顺序，不影响判定结果。
    recent = (
        await db.execute(
            select(Task)
            .order_by(Task.created_at.desc(), Task.updated_at.desc())
            .limit(cap * 5)
        )
    ).scalars().all()
    watching: list[dict] = []
    for task in recent:
        block = _supervisor_of(task)
        if not block:
            continue
        watching.append(
            {
                "task_id": task.id,
                "thread_id": str(task.thread_id or ""),
                "goal": (task.goal or "")[:200],
                "title": task.title,
                "status": task.status,
                "intent": str(block.get("intent") or ""),
                "reason": str(block.get("reason") or ""),
                "control_action": str(block.get("control_action") or ""),
                "needs_execution": bool(block.get("needs_execution")),
                "continues_goal": bool(block.get("continues_goal")),
                "scenario": str(block.get("scenario") or ""),
                "automation_mode": str(block.get("automation_mode") or ""),
                "permission_mode": str(((task.goal_spec or {}).get("dispatch") or {}).get("permission_mode") or ""),
                "created_at": _iso(task.created_at),
            }
        )
        if len(watching) >= cap:
            break

    # ── 2. 正在跑 / 等你处理 ────────────────────────────────────────────
    open_tasks = (
        await db.execute(
            select(Task).where(Task.status.in_(OPEN_STATUSES)).order_by(Task.updated_at.desc()).limit(cap)
        )
    ).scalars().all()
    running = [
        {
            "task_id": t.id,
            "thread_id": str(t.thread_id or ""),
            "title": t.title,
            "goal": (t.goal or "")[:200],
            "status": t.status,
            "updated_at": _iso(t.updated_at),
        }
        for t in open_tasks
    ]

    # ── 3. 待触发的 watch / schedule ────────────────────────────────────
    # 不再按 ``background_execution`` 决定显不显示：定时/盯着是用户明确交代的，
    # 那条 flag 关不关，这些行都真实存在、也真的会醒（由 watch_scan / 到点扫描负责）。
    # 拿 flag 决定"显不显示"，只会让关着开关的用户看不到自己设的提醒。
    pending: list[dict] = []
    background_on = is_enabled(FEATURE)
    rows = (
        await db.execute(
            select(AgentTrigger)
            .where(
                AgentTrigger.status.in_(
                    (triggers_module.STATUS_PENDING, triggers_module.STATUS_PAUSED)
                )
            )
            .order_by(AgentTrigger.created_at.desc())
            .limit(cap)
        )
    ).scalars().all()
    for row in rows:
        waiting, never = _waiting_for(row)
        payload = dict(row.payload or {})
        pending.append(
            {
                "trigger_id": row.id,
                "project_id": row.project_id,
                "task_id": row.task_id or "",
                "event_type": row.event_type,
                "source": row.source,
                "goal": str(payload.get("goal") or "")[:200],
                "fire_at": _iso(row.fire_at),
                "created_at": _iso(row.created_at),
                "waiting_for": waiting,
                "never_fires": never,
                # v0.11.2 §8：暂停过的触发器也要留在页面上，否则用户没有地方点恢复。
                "status": row.status,
                "can_release": row.status == triggers_module.STATUS_PENDING,
            }
        )

    counts = {
        "watching": len(watching),
        "running": sum(1 for t in running if t["status"] == "running"),
        "waiting_approval": sum(1 for t in running if t["status"] == "waiting_approval"),
        "pending": len(pending),
        "never_fires": sum(1 for p in pending if p["never_fires"]),
    }

    return {
        "now": _iso(now),
        "watching": watching,
        "running": running,
        "pending": pending,
        "sources": [dict(item) for item in _SOURCES] + await _im_sources(db),
        "counts": counts,
        "background_execution": background_on,
    }
