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


async def enqueue_run(db: AsyncSession, task_id: str) -> AgentRun:
    existing = (await db.execute(select(AgentRun).where(AgentRun.task_id == task_id))).scalar_one_or_none()
    if existing is not None:
        if existing.status not in TERMINAL_RUN_STATES:
            raise ApiError(code="task_running", message="任务已经有运行中的 Run", status_code=409)
        existing.status = "queued"
        existing.worker_id = ""
        existing.lease_expires_at = None
        existing.attempts = 0
        await db.flush()
        return existing
    run = AgentRun(id=str(uuid4()), task_id=task_id, status="queued")
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
                (AgentRun.status == "running")
                & ((AgentRun.lease_expires_at.is_(None)) | (AgentRun.lease_expires_at < now)),
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


async def finish_run(db: AsyncSession, run_id: str, status: str) -> None:
    await db.execute(
        update(AgentRun)
        .where(AgentRun.id == run_id)
        .values(status=status, lease_expires_at=None)
    )
    await db.commit()
