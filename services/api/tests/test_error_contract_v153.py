import asyncio
import json

from blank_api.api.errors import RATE_LIMIT_CODE, error_body
from blank_api.helpers.middleware import RateLimitMiddleware


def test_error_body_uses_nested_contract():
    assert error_body(RATE_LIMIT_CODE, "请求过于频繁") == {
        "error": {"code": "rate_limit_exceeded", "message": "请求过于频繁"}
    }


def test_rate_limited_response_uses_nested_contract():
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "path": "/api/v1/tasks",
        "method": "GET",
        "client": ("127.0.0.1", 12345),
    }
    middleware = RateLimitMiddleware(inner, rpm=1)

    async def call():
        sent = []

        async def send(message):
            sent.append(message)

        await middleware(scope, receive, send)
        return sent

    asyncio.run(call())
    limited = asyncio.run(call())
    start = next(item for item in limited if item["type"] == "http.response.start")
    body = b"".join(item.get("body", b"") for item in limited if item["type"] == "http.response.body")

    assert start["status"] == 429
    assert json.loads(body) == {
        "error": {"code": RATE_LIMIT_CODE, "message": "请求过于频繁，请稍后再试"}
    }
