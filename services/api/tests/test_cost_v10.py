"""阶段7 成本统计：Token 换算成估算成本，并给出成功率与平均耗时。"""

import json

import httpx

from blank_api.app import create_app
from blank_api.db import Base, get_engine
from blank_api.services.models.pricing import estimate_cost, price_for


def test_pricing_lookup_and_unknown_model():
    assert price_for("deepseek-chat") is not None
    priced = estimate_cost("deepseek-chat", 1_000_000, 1_000_000)
    assert priced.priced is True and priced.currency == "USD"
    assert round(priced.amount, 2) == 1.37
    unknown = estimate_cost("some-internal-llm", 500, 500)
    assert unknown.priced is False and unknown.amount == 0.0
    assert estimate_cost("", 10, 10).priced is False


def test_prefix_match_for_versioned_model_names():
    assert estimate_cost("gpt-4o-2024-11-20", 0, 1_000_000).priced is True


def _mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("stream"):
            return httpx.Response(200, content=b"data: [DONE]\n\n")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
                "model": "deepseek-chat",
            },
        )

    return httpx.MockTransport(handler)


async def test_usage_summary_reports_cost_and_rates():
    app = create_app()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    app.state.model_client_factory = lambda **kw: httpx.AsyncClient(transport=_mock_transport())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as ac:
        created = (
            await ac.post(
                "/api/v1/providers",
                json={
                    "name": "cost-check",
                    "kind": "openai_compatible",
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-chat",
                    "api_key": "sk-costcheck-secret",
                },
            )
        ).json()
        await ac.post(f"/api/v1/providers/{created['id']}/test")
        await ac.post(f"/api/v1/providers/{created['id']}/chat", json={"message": "hi"})
        summary = (await ac.get("/api/v1/usage/summary")).json()

    assert summary["total_calls"] == 2
    assert summary["success_rate"] == 1.0
    bucket = summary["providers"][0]
    assert bucket["calls"] == 2 and bucket["failed"] == 0
    assert bucket["prompt_tokens"] == 2000 and bucket["completion_tokens"] == 1000
    assert bucket["cost"]["priced"] is True
    assert round(bucket["cost"]["amount"], 8) == round((2000 * 0.27 + 1000 * 1.10) / 1_000_000, 8)
    assert bucket["success_rate"] == 1.0
    assert bucket["avg_latency_ms"] >= 0
    assert summary["total_cost"]["amount"] > 0
