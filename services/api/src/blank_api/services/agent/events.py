"""Persistent Agent event timeline (one increasing seq per task)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from blank_api.models.task import AgentEvent


class EventRecorder:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], task_id: str, trace_id: str = "") -> None:
        self.session_factory = session_factory
        self.task_id = task_id
        self.trace_id = trace_id or task_id

    async def add(self, event_type: str, node: str = "", payload: dict[str, Any] | None = None) -> int:
        event_payload = dict(payload or {})
        event_payload.setdefault("trace_id", self.trace_id)
        async with self.session_factory() as session:
            last = (
                await session.execute(
                    select(func.max(AgentEvent.seq)).where(AgentEvent.task_id == self.task_id)
                )
            ).scalar()
            seq = int(last or 0) + 1
            session.add(
                AgentEvent(
                    id=str(uuid.uuid4()),
                    task_id=self.task_id,
                    seq=seq,
                    node=node,
                    type=event_type,
                    payload=event_payload,
                )
            )
            await session.commit()
        return seq
