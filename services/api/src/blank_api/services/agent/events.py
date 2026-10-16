"""Persistent Agent event timeline (one increasing seq per task)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from blank_api.models.task import AgentEvent


class EventRecorder:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_id: str,
        trace_id: str = "",
        run_id: str = "",
    ) -> None:
        self.session_factory = session_factory
        self.task_id = task_id
        self.trace_id = trace_id or task_id
        self.run_id = run_id

    async def add(self, event_type: str, node: str = "", payload: dict[str, Any] | None = None) -> int:
        event_payload = dict(payload or {})
        event_payload.setdefault("trace_id", self.trace_id)
        async with self.session_factory() as session:
            previous = (
                await session.execute(
                    select(AgentEvent)
                    .where(AgentEvent.task_id == self.task_id)
                    .order_by(AgentEvent.seq.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            last = previous.seq if previous is not None else 0
            event_id = str(uuid.uuid4())
            seq = int(last or 0) + 1
            session.add(
                AgentEvent(
                    id=event_id,
                    task_id=self.task_id,
                    run_id=self.run_id,
                    step_id=event_id,
                    parent_event_id=previous.id if previous is not None else "",
                    seq=seq,
                    node=node,
                    type=event_type,
                    payload=event_payload,
                )
            )
            await session.commit()
        return seq
