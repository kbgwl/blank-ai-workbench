"""Thread fork — branch an existing run without disturbing it.

The checkpoint store is already per-thread, so a fork is a copy operation with
one hard rule: the copy must be self-contained. We copy exactly one checkpoint
(the latest) and its pending writes, and deliberately drop
``parent_checkpoint_id`` — pointing at a row that only exists under the source
thread would make the fork unreadable once the source is pruned.

Two trunk strategies, matching what the mainstream agents expose:

* ``trunk_reuse`` — carry the state over, so the fork resumes mid-task. Used
  when the operator wants "try this approval differently" without redoing work.
* ``trunk_new``   — keep only the lineage, start from an empty state. Used when
  the branch is a genuinely different attempt.

Lineage is recorded as a ``thread.forked`` event, which is also what thread-id
allocation reads, so a fresh trunk cannot be handed out twice.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from blank_api.api.errors import ApiError
from blank_api.models.checkpoint import AgentCheckpoint, AgentCheckpointWrite
from blank_api.services.agent.checkpoints import task_id_from_thread, thread_id_for

TRUNK_REUSE = "trunk_reuse"
TRUNK_NEW = "trunk_new"
STRATEGIES = (TRUNK_REUSE, TRUNK_NEW)
FORK_EVENT = "thread.forked"


@dataclass(frozen=True)
class ForkResult:
    task_id: str
    thread_id: str
    forked_from_thread_id: str
    strategy: str
    checkpoints_copied: int = 0
    writes_copied: int = 0

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "thread_id": self.thread_id,
            "forked_from_thread_id": self.forked_from_thread_id,
            "strategy": self.strategy,
            "checkpoints_copied": self.checkpoints_copied,
            "writes_copied": self.writes_copied,
        }


async def _checkpoint_threads(session, task_id: str) -> list[str]:
    rows = (
        (
            await session.execute(
                select(AgentCheckpoint.thread_id).where(AgentCheckpoint.task_id == task_id)
            )
        )
        .scalars()
        .all()
    )
    return [thread for thread in set(rows) if task_id_from_thread(thread) == task_id]


async def fork_threads(session, task_id: str) -> list[str]:
    """Threads that exist only as forks (no checkpoints yet)."""
    from blank_api.models.task import AgentEvent

    rows = (
        (
            await session.execute(
                select(AgentEvent.payload).where(
                    AgentEvent.task_id == task_id, AgentEvent.type == FORK_EVENT
                )
            )
        )
        .scalars()
        .all()
    )
    found: list[str] = []
    for payload in rows or []:
        thread = str((payload or {}).get("thread_id") or "")
        if thread and task_id_from_thread(thread) == task_id:
            found.append(thread)
    return found


def _attempt(thread_id: str) -> int:
    try:
        return int(str(thread_id).rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return 0


async def allocate_thread(session, task_id: str) -> str:
    """Next free attempt number, counting both checkpoints and recorded forks."""
    threads = [*await _checkpoint_threads(session, task_id), *await fork_threads(session, task_id)]
    attempts = [_attempt(thread) for thread in threads]
    return thread_id_for(task_id, max(attempts or {0}) + 1)


async def fork_thread(
    session_factory,
    task_id: str,
    *,
    from_thread_id: str = "",
    strategy: str = TRUNK_REUSE,
) -> ForkResult:
    """Copy the source thread's latest checkpoint into a brand-new thread."""
    if strategy not in STRATEGIES:
        raise ApiError(
            code="fork_strategy_invalid",
            message=f"未知的分叉策略：{strategy}",
            status_code=422,
        )

    async with session_factory() as session:
        thread_ids = await _checkpoint_threads(session, task_id)
        if not thread_ids:
            raise ApiError(
                code="task_not_runnable",
                message="该任务还没有检查点，无法分叉",
                status_code=409,
            )
        source = from_thread_id or max(thread_ids, key=_attempt)
        if source not in thread_ids:
            raise ApiError(
                code="thread_not_found", message="指定的线程不属于该任务或已被清理", status_code=404
            )

        target = await allocate_thread(session, task_id)
        latest = (
            await session.execute(
                select(AgentCheckpoint)
                .where(AgentCheckpoint.thread_id == source)
                .order_by(AgentCheckpoint.created_at.desc(), AgentCheckpoint.checkpoint_id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        copied, writes = 0, 0
        if strategy == TRUNK_REUSE and latest is not None:
            session.add(
                AgentCheckpoint(
                    thread_id=target,
                    checkpoint_ns=latest.checkpoint_ns,
                    checkpoint_id=latest.checkpoint_id,
                    task_id=task_id,
                    # 悬空父指针会让分叉在源线程被清理后无法反序列化
                    parent_checkpoint_id=None,
                    serde_type=latest.serde_type,
                    checkpoint=latest.checkpoint,
                    metadata_json={**(latest.metadata_json or {}), "forked_from": source},
                )
            )
            copied = 1
            rows = (
                (
                    await session.execute(
                        select(AgentCheckpointWrite).where(
                            AgentCheckpointWrite.thread_id == source,
                            AgentCheckpointWrite.checkpoint_id == latest.checkpoint_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                session.add(
                    AgentCheckpointWrite(
                        thread_id=target,
                        checkpoint_ns=row.checkpoint_ns,
                        checkpoint_id=row.checkpoint_id,
                        write_task_id=row.write_task_id,
                        idx=row.idx,
                        task_path=row.task_path,
                        channel=row.channel,
                        serde_type=row.serde_type,
                        value=row.value,
                    )
                )
                writes += 1
        await session.commit()

    return ForkResult(
        task_id=task_id,
        thread_id=target,
        forked_from_thread_id=source,
        strategy=strategy,
        checkpoints_copied=copied,
        writes_copied=writes,
    )
