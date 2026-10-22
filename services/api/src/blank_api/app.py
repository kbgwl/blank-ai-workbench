import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from blank_api.api.exception_handlers import register_error_handlers
from blank_api.api.routes import api_router
from blank_api.config import get_settings
from blank_api.services.agent.manager import run_manager


@asynccontextmanager
async def app_lifespan(_: FastAPI) -> AsyncIterator[None]:
    from blank_api.api.routes.tasks import recover_pending_runs

    await recover_pending_runs()
    async def recovery_loop() -> None:
        while True:
            await asyncio.sleep(15)
            try:
                await recover_pending_runs()
            except Exception:
                # A transient database outage must not kill the API lifecycle;
                # the next tick will retry the durable queue scan.
                continue

    recovery_task = asyncio.create_task(recovery_loop(), name="blank-run-recovery")
    # 附件清理单独一个任务、而且不阻断启动：它要扫盘 + 查一遍引用，
    # 让 /health 等着它没有道理。
    sweep_task = asyncio.create_task(sweep_orphan_uploads(), name="blank-upload-sweep")
    try:
        yield
    finally:
        for task in (recovery_task, sweep_task):
            task.cancel()
        await asyncio.gather(recovery_task, sweep_task, return_exceptions=True)
        await run_manager.shutdown()
        # 外部工具服务端是我们起的子进程：不在这里收掉，退出后会留下孤儿进程
        from blank_api.services.mcp import manager as mcp_manager

        await mcp_manager().close()


async def sweep_orphan_uploads() -> None:
    """启动时清一次孤儿附件。

    三处会留下没人认领的文件：删掉的任务、选了图又没发出去的草稿、以及
    早期版本删项目时没清干净的残留。规则是"够老 + 没有任何任务引用"，
    这样刚选完还在写字的草稿不会被误删。
    """
    from sqlalchemy import select

    from blank_api.db import get_session_factory
    from blank_api.models.task import Task
    from blank_api.services.uploads import UploadStore

    async def collect() -> set[str]:
        async with get_session_factory()() as session:
            rows = (await session.execute(select(Task.attachments))).scalars().all()
        keep: set[str] = set()
        for row in rows:
            for item in row or []:
                keep.add(str(item.get("id") or ""))
        return keep

    try:
        keep = await collect()
        removed = await asyncio.to_thread(UploadStore().sweep, keep)
        if removed:
            print(f"[uploads] 清理孤儿附件 {removed} 个")
    except Exception:
        # 清理失败不该影响启动：磁盘上多几个文件，远好过服务起不来。
        return


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=app_lifespan)
    register_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router, prefix="/api/v1")

    return app


app = create_app()
