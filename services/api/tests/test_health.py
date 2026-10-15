import httpx
import pytest

from blank_api.api.errors import ApiError
from blank_api.app import create_app


@pytest.fixture
async def demo_client():
    app = create_app()

    @app.get("/api/v1/_demo/api-error")
    async def _demo_api_error() -> None:
        raise ApiError(code="demo_error", message="demo failure", status_code=418)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_health_ok(demo_client):
    resp = await demo_client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "connected"


async def test_api_error_shape(demo_client):
    resp = await demo_client.get("/api/v1/_demo/api-error")
    assert resp.status_code == 418
    assert resp.json() == {"error": {"code": "demo_error", "message": "demo failure"}}


async def test_http_error_shape(demo_client):
    resp = await demo_client.get("/api/v1/nope")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "http_404"
    assert "message" in body["error"]
