"""Process-local run manager.

This is the first step toward a durable worker queue: HTTP requests enqueue a
run and return immediately, while the runtime owns execution and persistence.
The database remains the source of truth for task state and events; the
process-local registry only prevents duplicate launches in one API process.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


class RunAlreadyActive(RuntimeError):
    pass


class RunManager:
    def __init__(self) -> None:
        self._runs: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def start(self, run_id: str, runner: Callable[[], Awaitable[object]]) -> None:
        async with self._lock:
            current = self._runs.get(run_id)
            if current is not None and not current.done():
                raise RunAlreadyActive(run_id)
            task = asyncio.create_task(runner(), name=f"blank-run-{run_id}")
            self._runs[run_id] = task
            task.add_done_callback(lambda _: self._runs.pop(run_id, None))

    def is_active(self, run_id: str) -> bool:
        task = self._runs.get(run_id)
        return task is not None and not task.done()

    async def shutdown(self) -> None:
        tasks = list(self._runs.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._runs.clear()

    async def recover(self, loader: Callable[[], Awaitable[list[tuple[str, Callable[[], Awaitable[object]]]]]]) -> int:
        """Enqueue database-backed runs after an API process starts."""
        recovered = 0
        for run_id, runner in await loader():
            if self.is_active(run_id):
                continue
            try:
                await self.start(run_id, runner)
            except RunAlreadyActive:
                continue
            recovered += 1
        return recovered


run_manager = RunManager()
