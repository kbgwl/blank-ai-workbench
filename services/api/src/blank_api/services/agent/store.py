"""Serialized session access for a single Agent run.

LangGraph dispatches checkpoint writes from background tasks while nodes keep
recording events, so several transactions of one run can overlap. A local
single-connection SQLite backend cannot interleave them, and an overlapping
seq lookup could break the unique (task_id, seq) constraint. Holding one
transaction at a time per run keeps both backends consistent.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class SessionStore(Protocol):
    """Anything usable as `async with factory() as session`."""

    def __call__(self) -> contextlib.AbstractAsyncContextManager[AsyncSession]: ...


class SerializedSessionFactory:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._depth = 0

    def __call__(self) -> Any:
        return self._open()

    @contextlib.asynccontextmanager
    async def _open(self):
        current = asyncio.current_task()
        if self._owner is not current:
            await self._lock.acquire()
            self._owner, self._depth = current, 1
        else:
            self._depth += 1
        try:
            async with self._factory() as session:
                yield session
        finally:
            self._depth -= 1
            if self._depth == 0:
                self._owner = None
                self._lock.release()
