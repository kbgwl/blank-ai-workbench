from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from blank_api.api.exception_handlers import register_error_handlers
from blank_api.api.routes import api_router
from blank_api.config import get_settings
from blank_api.services.agent.manager import run_manager


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0")
    register_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router, prefix="/api/v1")

    @app.on_event("shutdown")
    async def stop_runs() -> None:
        await run_manager.shutdown()

    @app.on_event("startup")
    async def recover_runs() -> None:
        from blank_api.api.routes.tasks import recover_pending_runs

        await recover_pending_runs()

    return app


app = create_app()
