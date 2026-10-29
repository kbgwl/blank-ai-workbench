"""Database-backed run enqueue and lease operations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from blank_api.api.errors import ApiError
from blank_api.models.run import AgentRun


TERMINAL_RUN_STATES = {"succeeded", "failed", "cancelled", "paused", "waiting_approval"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def enqueue_run(db: AsyncSession, task_id: str, request_payload: dict | None = None) -> AgentRun:
    existing = (await db.execute(select(AgentRun).where(AgentRun.task_id == task_id))).scalar_one_or_none()
    if existing is not None:
        if existing.status not in TERMINAL_RUN_STATES:
            raise ApiError(code="task_running", message="任务已经有运行中的 Run", status_code=409)
        existing.status = "queued"
        existing.worker_id = ""
        existing.lease_expires_at = None
        existing.attempts = 0
        existing.request_payload = dict(request_payload or existing.request_payload or {})
        await db.flush()
        return existing
    run = AgentRun(
        id=str(uuid4()), task_id=task_id, status="queued", request_payload=dict(request_payload or {})
    )
    db.add(run)
    await db.flush()
    return run


async def claim_run(db: AsyncSession, run_id: str, worker_id: str, lease_seconds: int = 90) -> bool:
    now = utc_now()
    result = await db.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run_id,
            or_(
                AgentRun.status == "queued",
                # 到期这一拍就算别人的了：所有权边界只有一条，lease_expires_at 严格大于 now
                # 才算持约（renew_run 用的正是 >）。写成 < 时，本机同一拍两次 utc_now()
                # 200/200 相等（E:/tmp/kbg144/clock_probe.py），A 认领 lease_seconds=0 后
                # B 在同一拍永远接不了手，租约成了永久锁。MySQL 的 DATETIME 只到秒，
                # 边界差一拍就是整整一秒。
                (AgentRun.status == "running")
                & ((AgentRun.lease_expires_at.is_(None)) | (AgentRun.lease_expires_at <= now)),
            ),
        )
        .values(
            status="running",
            worker_id=worker_id,
            attempts=AgentRun.attempts + 1,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
        )
    )
    await db.commit()
    return result.rowcount == 1


async def finish_run(db: AsyncSession, run_id: str, status: str, worker_id: str) -> bool:
    """写终态，但只允许仍然持有这条租约的 worker 写。

    只按 run_id 更新会出这种事：A 的租约过期、B 接管（attempts 已经加到 2），
    A 手上的活儿跑完照样把这条 run 写成 succeeded——界面上看到的是"B 在跑、
    结果已就绪"，而 B 其实一步都还没走完。实测见 E:/tmp/m90/lease2-readings.txt 的 S3。
    """
    result = await db.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run_id,
            AgentRun.status == "running",
            AgentRun.worker_id == worker_id,
        )
        .values(status=status, worker_id="", lease_expires_at=None)
    )
    await db.commit()
    return result.rowcount == 1


async def abandon_run(db: AsyncSession, run_id: str) -> bool:
    """启动恢复"不再重试"那一路：把没人跑的 run 判死，但不得踩住一条正被跑着的。

    这条路径没有 worker 身份可验（认领它的进程早没了），所以验租约：还新鲜就说明
    有人正在跑，判死必须失败。边界与 claim_run 同一条：等于 now 就算过期。
    """
    result = await db.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run_id,
            or_(
                AgentRun.status == "queued",
                (AgentRun.status == "running")
                & ((AgentRun.lease_expires_at.is_(None)) | (AgentRun.lease_expires_at <= utc_now())),
            ),
        )
        .values(status="failed", worker_id="", lease_expires_at=None)
    )
    await db.commit()
    return result.rowcount == 1


async def renew_run(db: AsyncSession, run_id: str, worker_id: str, lease_seconds: int = 90) -> bool:
    """Extend a lease only for its current owner; stale workers cannot revive a run."""
    now = utc_now()
    result = await db.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run_id,
            AgentRun.status == "running",
            AgentRun.worker_id == worker_id,
            # 过期即失去身份：否则旧 worker 抢在接管者之前把租约续上，接管窗口永远打开
            AgentRun.lease_expires_at.is_not(None),
            AgentRun.lease_expires_at > now,
        )
        .values(lease_expires_at=now + timedelta(seconds=lease_seconds))
    )
    await db.commit()
    return result.rowcount == 1
