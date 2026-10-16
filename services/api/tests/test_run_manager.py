import asyncio

import pytest

from blank_api.services.agent.manager import RunAlreadyActive, RunManager


@pytest.mark.asyncio
async def test_run_manager_prevents_duplicate_and_cleans_up():
    manager = RunManager()
    gate = asyncio.Event()

    async def runner():
        await gate.wait()

    await manager.start("run-1", runner)
    assert manager.is_active("run-1")
    with pytest.raises(RunAlreadyActive):
        await manager.start("run-1", runner)
    gate.set()
    for _ in range(20):
        if not manager.is_active("run-1"):
            break
        await asyncio.sleep(0)
    assert not manager.is_active("run-1")
