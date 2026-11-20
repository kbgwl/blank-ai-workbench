"""客户端断开不该等于工作作废。

现场（2026-11-20 08:50）：夜班用 curl 跑一个长任务，客户端 600 秒超时退出。
服务端抛 CancelledError、回 500，已经跑掉的十分钟连同 token 一起作废，任务永远
停在 running——再点运行就是 409「任务正在执行中」，谁也捡不回来。

`await task` 在调用方被取消时会连带取消被等待的任务，所以"断开"和"停止干活"
默认是同一件事。`_run_detached` 用 shield 把两者拆开，这里钉住它别再合上。
"""

from __future__ import annotations

import asyncio

import pytest

from blank_api.api.routes.tasks import _run_detached


@pytest.mark.anyio
async def test_the_work_survives_the_waiter_being_cancelled():
    finished = asyncio.Event()

    async def work() -> dict:
        await asyncio.sleep(0.15)
        finished.set()
        return {"ok": True}

    running = asyncio.create_task(work())
    waiter = asyncio.create_task(_run_detached(running, "task-1"))
    await asyncio.sleep(0.02)
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter
    # 等的人没了，工作还在；过一会儿它自己跑完。
    assert not finished.is_set(), "取消等待的瞬间，工作不该已经结束（否则这条测了个寂寞）"
    await asyncio.wait_for(finished.wait(), timeout=2)
    assert running.done() and running.exception() is None


@pytest.mark.anyio
async def test_a_normal_run_still_returns_the_evidence():
    async def work() -> dict:
        return {"task": {"status": "succeeded"}}

    running = asyncio.create_task(work())
    assert await _run_detached(running, "task-2") == {"task": {"status": "succeeded"}}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
