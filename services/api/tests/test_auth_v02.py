import httpx

from blank_api.app import create_app
from blank_api.config import get_settings
from blank_api.db import Base, get_engine


async def test_token_required_when_configured(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("BLANK_API_TOKEN", "secret-token")
    try:
        app = create_app()
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            anonymous = await ac.get("/api/v1/projects")
            assert anonymous.status_code == 401
            assert anonymous.json()["error"]["code"] == "unauthorized"

            wrong = await ac.get("/api/v1/projects", headers={"Authorization": "Bearer nope"})
            assert wrong.status_code == 401

            ok = await ac.get("/api/v1/projects", headers={"Authorization": "Bearer secret-token"})
            assert ok.status_code == 200
            assert ok.json() == []

            health = await ac.get("/api/v1/health")
            assert health.status_code == 200
    finally:
        monkeypatch.delenv("BLANK_API_TOKEN", raising=False)
        get_settings.cache_clear()
