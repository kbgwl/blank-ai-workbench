from fastapi import APIRouter, Depends
from sqlalchemy import text

from blank_api.api.auth import require_token
from blank_api.api.routes import (
    files,
    maintenance,
    observability,
    plan_templates,
    projects,
    providers,
    reverse_lab,
    settings,
    skills,
    tasks,
    team,
    threads,
    triggers,
    uploads,
)
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


@api_router.get("/scenarios", dependencies=[Depends(require_token)])
async def scenarios() -> list[dict]:
    """场景清单（scenarios.py 五选一）。纯配置，无状态——给前端选择器用。

    挂鉴权与全站口径一致：auth surface 测试要求"非公共接口匿名一律 401"，
    这里没有豁免的理由（清单本身不含敏感数据，但漏挂一个就多一个先例）。
    """
    from blank_api.services.agent.scenarios import list_scenarios

    return list_scenarios()


api_router.include_router(projects.router)
api_router.include_router(uploads.router)
api_router.include_router(files.router)
api_router.include_router(providers.router)
api_router.include_router(tasks.router)
api_router.include_router(plan_templates.router)
api_router.include_router(skills.router)
api_router.include_router(observability.router)
api_router.include_router(maintenance.router)
api_router.include_router(settings.router)
api_router.include_router(threads.router)
# 事件触发（v162 §5）：上报/查询/点火，受 background_execution 开关控制
api_router.include_router(triggers.router)
api_router.include_router(team.router)
# REA 逆向线（#108）：/reverse-lab 的能力入口，走 MCP Adapter，不走模型
api_router.include_router(reverse_lab.router)
