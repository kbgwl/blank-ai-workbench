import json

import httpx
import pytest

from blank_api.app import create_app
from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.model_provider import ModelProvider

SEEN: dict[str, str] = {}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    SEEN["auth"] = request.headers.get("Authorization", "") + "|" + request.headers.get("x-api-key", "")
    if path.endswith("/chat/completions"):
        body = json.loads(request.content)
        if body.get("stream"):
            chunks = b""
            for token in ["pong", " ", "streamed"]:
                chunks += b"data: " + json.dumps({"choices": [{"delta": {"content": token}}]}).encode() + b"\n\n"
            chunks += b"data: [DONE]\n\n"
            return httpx.Response(200, content=chunks)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                "model": "deepseek-chat",
            },
        )
    if path.endswith("/v1/messages"):
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "claude pong"}],
                "usage": {"input_tokens": 7, "output_tokens": 4},
                "model": "claude-3-5-sonnet",
            },
        )
    if path.endswith("/api/chat"):
        return httpx.Response(
            200,
            json={
                "message": {"content": "ollama pong"},
                "prompt_eval_count": 6,
                "eval_count": 2,
                "done": True,
                "model": "qwen2.5-coder",
            },
        )
    return httpx.Response(404, json={"error": "no route"})


@pytest.fixture
async def model_client():
    app = create_app()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    app.state.model_client_factory = lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


async def _create(model_client, name, kind, base_url, model, key, tools=True):
    resp = await model_client.post(
        "/api/v1/providers",
        json={
            "name": name,
            "kind": kind,
            "base_url": base_url,
            "model": model,
            "api_key": key,
            "supports_tools": tools,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_three_vendors_invoke_and_stream(model_client):
    openai = await _create(model_client, "deepseek", "openai_compatible", "https://api.deepseek.com/v1", "deepseek-chat", "sk-deepseek-secret")
    anthropic = await _create(model_client, "anthropic", "anthropic", "https://api.anthropic.com", "claude-3-5-sonnet", "ant-secret-key")
    ollama = await _create(model_client, "local-ollama", "ollama", "http://localhost:11434", "qwen2.5-coder", "")

    r1 = await model_client.post(f"/api/v1/providers/{openai['id']}/test")
    assert r1.json()["ok"] is True and r1.json()["model"] == "deepseek-chat"
    assert SEEN["auth"].startswith("Bearer sk-deepseek-secret")

    r2 = await model_client.post(f"/api/v1/providers/{anthropic['id']}/test")
    assert r2.json()["ok"] is True and r2.json()["model"] == "claude-3-5-sonnet"
    assert "ant-secret-key" in SEEN["auth"]

    r3 = await model_client.post(f"/api/v1/providers/{ollama['id']}/test")
    assert r3.json()["ok"] is True and r3.json()["model"] == "qwen2.5-coder"

    caps = r1.json()["capabilities"]
    assert caps["tools"] is True and caps["streaming"] is True

    chat = await model_client.post(
        f"/api/v1/providers/{openai['id']}/chat", json={"message": "hi", "stream": False}
    )
    assert chat.json()["text"] == "pong"
    assert chat.json()["usage"] == {"prompt_tokens": 5, "completion_tokens": 3}

    async with model_client.stream(
        "POST", f"/api/v1/providers/{openai['id']}/chat/stream", json={"message": "hi"}
    ) as stream:
        events = [line async for line in stream.aiter_lines() if line.startswith("data:")]
    assert any("pong" in e for e in events)
    assert events[-1].endswith("completed")


async def test_api_key_never_leaks_and_is_encrypted_at_rest(model_client):
    created = await _create(
        model_client, "openai", "openai_compatible", "https://api.openai.com/v1", "gpt-4o-mini", "sk-supersecret-value"
    )
    assert "sk-supersecret-value" not in json.dumps(created)
    assert created["api_key_hint"] == "****alue"

    listed = await model_client.get("/api/v1/providers")
    assert "sk-supersecret-value" not in listed.text

    async with get_session_factory()() as session:
        row = (await session.get(ModelProvider, created["id"]))
        assert row.api_key_encrypted != "sk-supersecret-value"
        assert "sk-supersecret-value" not in row.api_key_encrypted

    detail = await model_client.get(f"/api/v1/providers/{created['id']}")
    assert detail.status_code == 200
    assert "sk-supersecret-value" not in detail.text


async def test_tools_unsupported_gives_readable_error(model_client):
    provider = await _create(
        model_client,
        "weak-local",
        "ollama",
        "http://localhost:11434",
        "tiny-model",
        "",
        tools=False,
    )
    from blank_api.services.crypto import decrypt_secret
    from blank_api.services.models.base import ChatMessage, ToolSpecLite
    from blank_api.services.models.registry import build_adapter

    async with get_session_factory()() as session:
        row = await session.get(ModelProvider, provider["id"])
    adapter = build_adapter(row)
    with pytest.raises(Exception) as excinfo:
        await adapter.bind_tools([ToolSpecLite(name="read_file", description="d", input_schema={})], [ChatMessage("user", "x")])
    assert excinfo.value.code == "model_tools_unsupported"
    assert "工具" in excinfo.value.message


async def test_usage_summary_records_tokens(model_client):
    provider = await _create(
        model_client, "deepseek2", "openai_compatible", "https://api.deepseek.com/v1", "deepseek-chat", "sk-x123456789"
    )
    await model_client.post(f"/api/v1/providers/{provider['id']}/test")
    await model_client.post(f"/api/v1/providers/{provider['id']}/chat", json={"message": "hi"})
    summary = (await model_client.get("/api/v1/usage/summary")).json()
    assert summary["total_calls"] == 2
    entry = summary["providers"][0]
    assert entry["provider"] == "deepseek2"
    assert entry["prompt_tokens"] == 10 and entry["completion_tokens"] == 6


async def test_provider_switch_on_same_input(model_client):
    a = await _create(model_client, "vendor-a", "openai_compatible", "https://a.example/v1", "model-a", "sk-aaaaaaaa")
    b = await _create(model_client, "vendor-b", "anthropic", "https://b.example", "model-b", "sk-bbbbbbbb")
    ra = (await model_client.post(f"/api/v1/providers/{a['id']}/chat", json={"message": "same task"})).json()
    rb = (await model_client.post(f"/api/v1/providers/{b['id']}/chat", json={"message": "same task"})).json()
    assert ra["model"] == "deepseek-chat"  # mock echoes the openai-compatible shape
    assert rb["model"] == "claude-3-5-sonnet"
    assert ra["text"] != rb["text"]


async def test_presets_cover_four_vendors(model_client):
    data = (await model_client.get("/api/v1/providers/presets")).json()
    assert {"openai", "deepseek", "qwen", "anthropic", "ollama"} <= set(data["presets"])
    assert "openai_compatible" in data["kinds"]


async def test_delete_and_patch_provider(model_client):
    provider = await _create(model_client, "todelete", "ollama", "http://x", "m", "")
    patched = await model_client.patch(
        f"/api/v1/providers/{provider['id']}", json={"model": "new-model", "supports_tools": False}
    )
    assert patched.json()["model"] == "new-model" and patched.json()["supports_tools"] is False
    assert (await model_client.delete(f"/api/v1/providers/{provider['id']}")).status_code == 204
    assert (await model_client.get(f"/api/v1/providers/{provider['id']}")).status_code == 404
    assert (await model_client.post(f"/api/v1/providers/{provider['id']}/test")).status_code == 404
