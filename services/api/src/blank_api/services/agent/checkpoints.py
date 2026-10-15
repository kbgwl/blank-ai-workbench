"""SQLAlchemy-backed LangGraph checkpointer so runs survive restarts on SQLite and Postgres."""

from __future__ import annotations

import random
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    PendingWrite,
    SerializerProtocol,
    get_checkpoint_id,
    get_checkpoint_metadata,
    writes_sort_key,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from blank_api.models.checkpoint import AgentCheckpoint, AgentCheckpointWrite


def thread_id_for(task_id: str, attempt: int = 1) -> str:
    return f"task:{task_id}:attempt:{attempt}"


def task_id_from_thread(thread_id: str) -> str:
    parts = str(thread_id).split(":")
    return parts[1] if len(parts) > 1 else thread_id


class SqlAlchemyCheckpointSaver(BaseCheckpointSaver[str]):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        serde: SerializerProtocol | None = None,
    ) -> None:
        super().__init__(serde=serde or JsonPlusSerializer())
        self.session_factory = session_factory

    def get_next_version(self, current: str | None, channel: Any = None) -> str:
        if current is None:
            current_v = 0
        elif isinstance(current, int):
            current_v = current
        else:
            current_v = int(str(current).split(".")[0])
        return f"{current_v + 1:032}.{random.random():016}"

    async def aget_tuple(self, config: dict) -> CheckpointTuple | None:
        thread_id = str(config["configurable"]["thread_id"])
        checkpoint_ns = str(config["configurable"].get("checkpoint_ns", ""))
        async with self.session_factory() as session:
            stmt = select(AgentCheckpoint).where(
                AgentCheckpoint.thread_id == thread_id,
                AgentCheckpoint.checkpoint_ns == checkpoint_ns,
            )
            if checkpoint_id := get_checkpoint_id(config):
                stmt = stmt.where(AgentCheckpoint.checkpoint_id == checkpoint_id)
            else:
                stmt = stmt.order_by(AgentCheckpoint.checkpoint_id.desc()).limit(1)
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            writes = (
                (
                    await session.execute(
                        select(AgentCheckpointWrite).where(
                            AgentCheckpointWrite.thread_id == thread_id,
                            AgentCheckpointWrite.checkpoint_ns == checkpoint_ns,
                            AgentCheckpointWrite.checkpoint_id == row.checkpoint_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            checkpoint = self.serde.loads_typed((row.serde_type, row.checkpoint))
            metadata = dict(row.metadata_json or {})
            checkpoint_id_ = row.checkpoint_id
            parent_checkpoint_id = row.parent_checkpoint_id
        resolved = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id_,
            }
        }
        pending: list[PendingWrite] = sorted(
            [
                (
                    write.write_task_id,
                    write.channel,
                    self.serde.loads_typed((write.serde_type, write.value)),
                )
                for write in writes
            ],
            key=writes_sort_key,
        )
        return CheckpointTuple(
            config=resolved,
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": parent_checkpoint_id,
                    }
                }
                if parent_checkpoint_id
                else None
            ),
            pending_writes=pending,
        )

    async def alist(
        self,
        config: dict | None,
        *,
        filter: dict[str, Any] | None = None,
        before: dict | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None or "thread_id" not in config.get("configurable", {}):
            return
        thread_id = str(config["configurable"]["thread_id"])
        checkpoint_ns = str(config["configurable"].get("checkpoint_ns", ""))
        before_id = get_checkpoint_id(before) if before else None
        async with self.session_factory() as session:
            stmt = select(AgentCheckpoint).where(
                AgentCheckpoint.thread_id == thread_id,
                AgentCheckpoint.checkpoint_ns == checkpoint_ns,
            )
            if before_id:
                stmt = stmt.where(AgentCheckpoint.checkpoint_id < before_id)
            stmt = stmt.order_by(AgentCheckpoint.checkpoint_id.desc())
            if limit:
                stmt = stmt.limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
        for row in rows:
            metadata = dict(row.metadata_json or {})
            if filter and not all(metadata.get(key) == value for key, value in filter.items()):
                continue
            yield CheckpointTuple(
                config={
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": row.checkpoint_id,
                    }
                },
                checkpoint=self.serde.loads_typed((row.serde_type, row.checkpoint)),
                metadata=metadata,
                parent_config=(
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": row.parent_checkpoint_id,
                        }
                    }
                    if row.parent_checkpoint_id
                    else None
                ),
                pending_writes=[],
            )

    async def aput(
        self,
        config: dict,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> dict:
        thread_id = str(config["configurable"]["thread_id"])
        checkpoint_ns = str(config["configurable"]["checkpoint_ns"])
        serde_type, serialized = self.serde.dumps_typed(checkpoint)
        payload = get_checkpoint_metadata(config, metadata)
        async with self.session_factory() as session:
            await session.execute(
                delete(AgentCheckpoint).where(
                    AgentCheckpoint.thread_id == thread_id,
                    AgentCheckpoint.checkpoint_ns == checkpoint_ns,
                    AgentCheckpoint.checkpoint_id == checkpoint["id"],
                )
            )
            session.add(
                AgentCheckpoint(
                    thread_id=thread_id,
                    checkpoint_ns=checkpoint_ns,
                    checkpoint_id=checkpoint["id"],
                    task_id=task_id_from_thread(thread_id),
                    parent_checkpoint_id=config["configurable"].get("checkpoint_id"),
                    serde_type=serde_type,
                    checkpoint=serialized,
                    metadata_json=dict(payload),
                )
            )
            await session.commit()
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    async def aput_writes(
        self,
        config: dict,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = str(config["configurable"]["thread_id"])
        checkpoint_ns = str(config["configurable"]["checkpoint_ns"])
        checkpoint_id = str(config["configurable"]["checkpoint_id"])
        replace = all(channel in WRITES_IDX_MAP for channel, _ in writes)
        async with self.session_factory() as session:
            for idx, (channel, value) in enumerate(writes):
                row_idx = WRITES_IDX_MAP.get(channel, idx)
                serde_type, serialized = self.serde.dumps_typed(value)
                existing = (
                    await session.execute(
                        select(AgentCheckpointWrite).where(
                            AgentCheckpointWrite.thread_id == thread_id,
                            AgentCheckpointWrite.checkpoint_ns == checkpoint_ns,
                            AgentCheckpointWrite.checkpoint_id == checkpoint_id,
                            AgentCheckpointWrite.write_task_id == task_id,
                            AgentCheckpointWrite.idx == row_idx,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if not replace:
                        continue
                    existing.serde_type = serde_type
                    existing.value = serialized
                    existing.channel = channel
                    continue
                session.add(
                    AgentCheckpointWrite(
                        thread_id=thread_id,
                        checkpoint_ns=checkpoint_ns,
                        checkpoint_id=checkpoint_id,
                        write_task_id=task_id,
                        idx=row_idx,
                        task_path=task_path,
                        channel=channel,
                        serde_type=serde_type,
                        value=serialized,
                    )
                )
            await session.commit()

    async def adelete_thread(self, thread_id: str) -> None:
        async with self.session_factory() as session:
            await session.execute(
                delete(AgentCheckpoint).where(AgentCheckpoint.thread_id == thread_id)
            )
            await session.execute(
                delete(AgentCheckpointWrite).where(
                    AgentCheckpointWrite.thread_id == thread_id
                )
            )
            await session.commit()
