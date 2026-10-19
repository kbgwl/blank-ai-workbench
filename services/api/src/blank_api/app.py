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
    try:
        yield
    finally:
        recovery_task.cancel()
        await asyncio.gather(recovery_task, return_exceptions=True)
        await run_manager.shutdown()
        # 外部工具服务端是我们起的子进程：不在这里收掉，退出后会留下孤儿进程
        from blank_api.services.mcp import manager as mcp_manager

        await mcp_manager().close()


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
