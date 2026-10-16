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
    try:
        yield
    finally:
        await run_manager.shutdown()


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
