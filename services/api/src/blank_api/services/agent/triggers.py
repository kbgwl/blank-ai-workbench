"""事件触发器：把「发生了什么」变成「要不要开一条任务」，并且只变一次。

三条口径，缺一条就会变成刷屏机器：

1. **同一件事只开一条任务。** 去重靠 ``dedupe_key``：同项目同 key 的触发器在
   有效期内复用原行，不会又建一条任务。窗口由 ``expires_at`` 表达——过期之后
   同样的 key 允许再来一次（"每天备份失败"应该每天都能报一次，而不是永远只报
   第一次）。唯一约束落在库上，进程重启/多进程并发都不会绕过它。
2. **过期的事件不补做。** ``expires_at`` 已经过去的事件直接跳过：一个半夜的
   失败通知在早上补跑，用户看到的是"现在失败了"，而事实不是。
3. **没有可执行目标就不建任务。** 只存事件、不建空任务：``goal`` 为空时行照落，
   状态记 ``skipped``，理由写清楚。空任务会污染"最近对话"，也让成本台账多出
   一条零产出的记录。

这里只管事件与登记，**不执行**。真正开跑仍然要过 Router（``dispatch.py``）与
Worker：触发器只是把"该跑了"写进既有的 run 队列，不新开一条执行路径。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from blank_api.models.trigger import AgentTrigger

# P0 支持的触发源。邮件/日历/IM 只留接口：没有授权与真实测试前不声称支持。
EVENT_USER_TASK = "user_task"
EVENT_TASK_FAILED = "task_failed"
EVENT_APPROVAL_EXPIRED = "approval_expired"
EVENT_WORKSPACE_CHANGED = "workspace_changed"
EVENT_SCHEDULE = "schedule"
EVENT_TYPES = (
    EVENT_USER_TASK,
    EVENT_TASK_FAILED,
    EVENT_APPROVAL_EXPIRED,
    EVENT_WORKSPACE_CHANGED,
    EVENT_SCHEDULE,
)

STATUS_PENDING = "pending"
STATUS_FIRED = "fired"
STATUS_SKIPPED = "skipped"
STATUS_EXPIRED = "expired"

# 默认有效期：一次失败的提醒留一天。太短会让用户第二天才看到时打不开，
# 太长会让"同一个 key 又发生一次"被当成重复而吞掉。
DEFAULT_WINDOW = timedelta(hours=24)
# 去重键的载荷摘要只取这几个字段：整份 payload 进哈希会让"同一件事多了一个
# 时间戳"变成两件事，而去重的全部意义就是不受无关字段影响。
_DIGEST_FIELDS = ("task_id", "run_id", "path", "reason", "command", "status")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_naive_utc(value: Any) -> datetime | None:
    """把可能带时区的时间统一成库里那种无时区 UTC。"""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


@dataclass(frozen=True)
class TriggerEvent:
    """统一事件字段（任务书 §5）。字段少而固定，接新触发源时不用改表。"""

    event_id: str
    event_type: str
    source: str
    payload: dict
    dedupe_key: str
    created_at: datetime
    expires_at: datetime | None = None

    def as_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "source": self.source,
            "payload": self.payload,
            "dedupe_key": self.dedupe_key,
            "created_at": _iso(self.created_at),
            "expires_at": _iso(self.expires_at),
        }

    @property
    def expired(self) -> bool:
        return bool(self.expires_at and self.expires_at <= utc_now())


def _iso(moment: datetime | None) -> str:
    return moment.isoformat() if isinstance(moment, datetime) else ""


def dedupe_key_of(event_type: str, source: str, payload: Mapping[str, Any]) -> str:
    """稳定摘要：同类型、同来源、同关键事实 → 同一个 key。

    不含 ``payload`` 里的时间戳/计数器，也不含整段正文：正文一改就换 key 的
    去重等于没去重。
    """
    facts = {name: payload.get(name) for name in _DIGEST_FIELDS if payload.get(name) not in (None, "")}
    raw = json.dumps(
        {"type": event_type, "source": source, "facts": facts},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def normalize(
    *,
    event_type: str,
    source: str = "",
    payload: Mapping[str, Any] | None = None,
    dedupe_key: str = "",
    created_at: datetime | str | None = None,
    expires_at: datetime | str | None = None,
    window: timedelta | None = None,
) -> TriggerEvent:
    """把外部传进来的事件规整成一条可信记录。

    未知 ``event_type`` 直接拒绝（返回前抛 ValueError），因为"随手写个类型名"
    最终会变成一堆没人消费的触发器，而它们看起来像坏掉的功能。
    """
    kind = str(event_type or "").strip()
    if kind not in EVENT_TYPES:
        raise ValueError(f"未知事件类型 {kind!r}，可用：{', '.join(EVENT_TYPES)}")
    body = dict(payload or {})
    origin = str(source or "").strip()[:120]
    created = _as_naive_utc(created_at) or utc_now()
    expires = _as_naive_utc(expires_at)
    if expires is None:
        expires = created + (window or DEFAULT_WINDOW)
    key = str(dedupe_key or "").strip()[:120] or dedupe_key_of(kind, origin, body)
    return TriggerEvent(
        event_id=str(uuid4()),
        event_type=kind,
        source=origin,
        payload=body,
        dedupe_key=key,
        created_at=created,
        expires_at=expires,
    )


def _active_clause(now: datetime):
    """「还没过期」的判定。空 expires_at 读作永久有效（显式登记的常驻触发器）。"""
    return or_(AgentTrigger.expires_at.is_(None), AgentTrigger.expires_at > now)


async def find_active(
    db: AsyncSession, project_id: str, dedupe_key: str
) -> AgentTrigger | None:
    return (
        await db.execute(
            select(AgentTrigger).where(
                AgentTrigger.project_id == project_id,
                AgentTrigger.dedupe_key == dedupe_key,
                _active_clause(utc_now()),
            )
        )
    ).scalar_one_or_none()


async def ingest(
    db: AsyncSession,
    project_id: str,
    event: TriggerEvent,
    *,
    task_id: str = "",
    provider_id: str = "",
    run_payload: Mapping[str, Any] | None = None,
    fire_at: datetime | str | None = None,
    status: str = STATUS_PENDING,
) -> tuple[AgentTrigger, bool]:
    """登记一条触发器。返回 (行, 是否新建/重置)。

    同 key 且未过期 → 原样返回旧行（``created=False``），调用方据此**不再建任务**。
    同 key 但已过期 → 复用这一行（唯一约束下只能这样），把窗口与载荷换成新的。
    """
    existing = (
        await db.execute(
            select(AgentTrigger).where(
                AgentTrigger.project_id == project_id,
                AgentTrigger.dedupe_key == event.dedupe_key,
            )
        )
    ).scalar_one_or_none()

    moment = _as_naive_utc(fire_at)
    if existing is not None and (
        existing.expires_at is None or existing.expires_at > utc_now()
    ):
        return existing, False

    if existing is not None:
        # 过期行复用：换掉全部内容，语义上等于"这件事又发生了一次"。
        existing.event_type = event.event_type
        existing.source = event.source
        existing.payload = dict(event.payload)
        existing.task_id = task_id or ""
        existing.provider_id = provider_id or ""
        existing.run_payload = dict(run_payload or {})
        existing.fire_at = moment
        existing.expires_at = event.expires_at
        existing.fired_at = None
        existing.status = status
        await db.flush()
        return existing, True

    row = AgentTrigger(
        id=event.event_id,
        project_id=project_id,
        event_type=event.event_type,
        source=event.source,
        payload=dict(event.payload),
        dedupe_key=event.dedupe_key,
        task_id=task_id or "",
        provider_id=provider_id or "",
        run_payload=dict(run_payload or {}),
        fire_at=moment,
        expires_at=event.expires_at,
        status=status,
    )
    db.add(row)
    await db.flush()
    return row, True


async def due(db: AsyncSession, now: datetime | None = None, *, limit: int = 20) -> list[AgentTrigger]:
    """到点该放的触发器：pending 且 fire_at <= now（fire_at 为空的不算到点）。

    fire_at 为空 = 等外部事件，不是等时间。把它当"现在就到点"会在每一轮扫描里
    反复触发同一条——那正是"后台任务凭空开跑"的来源。
    """
    moment = now or utc_now()
    rows = (
        await db.execute(
            select(AgentTrigger)
            .where(
                AgentTrigger.status == STATUS_PENDING,
                AgentTrigger.fire_at.is_not(None),
                AgentTrigger.fire_at <= moment,
                _active_clause(moment),
            )
            .order_by(AgentTrigger.fire_at)
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def mark_fired(db: AsyncSession, row: AgentTrigger, *, now: datetime | None = None) -> None:
    row.status = STATUS_FIRED
    row.fired_at = now or utc_now()
    await db.flush()


async def expire_stale(db: AsyncSession, now: datetime | None = None) -> int:
    """把过期的 pending 行标成 expired，返回条数。

    只标状态、不删行：过期本身是事实（"这条提醒没人在窗口内处理"），删掉之后
    用户问"我那件事后来怎样了"就查不到了。
    """
    moment = now or utc_now()
    rows = (
        await db.execute(
            select(AgentTrigger).where(
                AgentTrigger.status == STATUS_PENDING,
                AgentTrigger.expires_at.is_not(None),
                AgentTrigger.expires_at <= moment,
            )
        )
    ).scalars().all()
    for row in rows:
        row.status = STATUS_EXPIRED
    if rows:
        await db.flush()
    return len(rows)


def as_dict(row: AgentTrigger) -> dict:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "event_type": row.event_type,
        "source": row.source,
        "dedupe_key": row.dedupe_key,
        "payload": dict(row.payload or {}),
        "task_id": row.task_id or None,
        "provider_id": row.provider_id or None,
        "fire_at": _iso(row.fire_at),
        "expires_at": _iso(row.expires_at),
        "fired_at": _iso(row.fired_at),
        "status": row.status,
        "created_at": _iso(row.created_at),
    }


def filter_types(values: Iterable[str] | None) -> tuple[str, ...]:
    """校验一批事件类型（接口层用）。未知值不静默丢弃，交给调用方报错。"""
    if not values:
        return ()
    cleaned = tuple(str(item).strip() for item in values if str(item).strip())
    unknown = [item for item in cleaned if item not in EVENT_TYPES]
    if unknown:
        raise ValueError(f"未知事件类型：{', '.join(unknown)}")
    return cleaned


__all__ = [
    "DEFAULT_WINDOW",
    "EVENT_TYPES",
    "STATUS_EXPIRED",
    "STATUS_FIRED",
    "STATUS_PENDING",
    "STATUS_SKIPPED",
    "TriggerEvent",
    "as_dict",
    "dedupe_key_of",
    "due",
    "expire_stale",
    "filter_types",
    "find_active",
    "ingest",
    "mark_fired",
    "normalize",
    "utc_now",
]
