from fastapi import APIRouter
from sqlalchemy import text

from blank_api.api.routes import files, observability, projects, providers, settings, skills, tasks
from blank_api.db import get_session_factory

api_router = APIRouter()


@api_router.get("/health")
async def health() -> dict:
    db_ok = False
    try:
        async with get_session_factory()() as session:
            await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "database": "connected" if db_ok else "unreachable",
    }


api_router.include_router(projects.router)
api_router.include_router(files.router)
api_router.include_router(providers.router)
api_router.include_router(tasks.router)
api_router.include_router(skills.router)
api_router.include_router(observability.router)
api_router.include_router(settings.router)
