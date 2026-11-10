"""远程 MCP OAuth（v0.11.7 G 批）。

这里刻意用**真的本地 HTTP 服务端**（127.0.0.1 随机端口）而不是只打 MockTransport：
发现路径、表单编码、Basic 认证头、401→刷新→重试这些环节，只有真发一遍请求才算数。
令牌是合成 JWT（签名段为空），因为本机只解包 aud/exp、不验签——这正是被测语义。

四条不变量：
1. 非 loopback 的 http 在发请求之前就被拒绝；
2. JWT 的 aud 与 exp 必须在用之前核对，缺失/不符/过期都拒绝；
3. opaque 令牌可以接，但状态里必须承认"受众未在本机核对"；
4. 令牌与 client_secret 不进错误、状态、证据。
"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from blank_api.services.mcp.bridge import MCPManager, parse_servers
from blank_api.services.mcp.client import HttpClient, MCPError, ServerConfig
from blank_api.services.mcp.oauth import (
    OAuthConfig,
    decode_jwt_claims,
    require_secure_url,
)

CLIENT_ID = "blank-client"
CLIENT_SECRET = "synthetic-secret-do-not-leak"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def make_jwt(claims: dict) -> str:
    header = _b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64(json.dumps(claims).encode())
    return f"{header}.{payload}."


class _FakeServerState:
    def __init__(self, **overrides) -> None:
        self.discovery = True
        self.methods = ("client_secret_basic",)
        self.opaque = False
        self.aud = None
        self.include_aud = True
        self.exp_delta = 3600.0
        self.token_error: dict | None = None
        self.always_401 = False
        self.reject_first_token = False
        self.calls = {"token": 0, "mcp": 0, "wellknown": 0}
        self.seen_auth: list[str] = []
        self.token_forms: list[dict] = []
        self.basic_headers: list[str] = []
        self.issued: list[str] = []
        for key, value in overrides.items():
            setattr(self, key, value)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # pragma: no cover - 测试服务端不需要访问日志
        pass

    @property
    def fake(self) -> _FakeServerState:
        return self.server.fake  # type: ignore[attr-defined]

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        fake = self.fake
        path = urllib.parse.urlsplit(self.path).path
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        if not fake.discovery:
            return self._json(404, {"error": "not_found"})
        fake.calls["wellknown"] += 1
        if path.endswith("/oauth-protected-resource"):
            return self._json(
                200,
                {"resource": f"{base}/mcp", "authorization_servers": [base]},
            )
        if path.endswith("/oauth-authorization-server"):
            return self._json(
                200,
                {
                    "issuer": base,
                    "token_endpoint": f"{base}/token",
                    "token_endpoint_auth_methods_supported": list(fake.methods),
                },
            )
        return self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        fake = self.fake
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        path = urllib.parse.urlsplit(self.path).path
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        if path == "/token":
            return self._token(base, raw)
        if path == "/mcp":
            return self._mcp(raw)
        return self._json(404, {"error": "not_found"})

    def _token(self, base: str, raw: bytes) -> None:
        fake = self.fake
        fake.calls["token"] += 1
        form = {key: values[0] for key, values in urllib.parse.parse_qs(raw.decode()).items()}
        fake.token_forms.append(form)
        fake.basic_headers.append(self.headers.get("authorization") or "")
        if fake.token_error is not None:
            return self._json(400, fake.token_error)
        if form.get("grant_type") not in ("client_credentials", "refresh_token"):
            return self._json(400, {"error": "unsupported_grant_type"})
        claims = {
            "sub": "blank-agent",
            # 每次签发都要不同：同一秒内 exp/aud/sub 完全一样，JWT 会逐字节相同，
            # "拒绝第一枚"就会把刷新后的新令牌一起拒掉。
            "jti": f"token-{fake.calls['token']}",
            "exp": int(time.time() + fake.exp_delta),
        }
        if fake.include_aud:
            claims["aud"] = fake.aud or f"{base}/mcp"
        token = f"opaque-{fake.calls['token']}" if fake.opaque else make_jwt(claims)
        fake.issued.append(token)
        self._json(
            200,
            {"access_token": token, "token_type": "Bearer", "expires_in": 3600},
        )

    def _mcp(self, raw: bytes) -> None:
        fake = self.fake
        fake.calls["mcp"] += 1
        auth = self.headers.get("authorization") or ""
        fake.seen_auth.append(auth)
        if fake.always_401:
            return self._json(401, {"error": "invalid_token"})
        if fake.reject_first_token and fake.issued:
            # 握手用第一枚令牌时服务端已经把它吊销；客户端必须刷新后重试一次
            if auth == f"Bearer {fake.issued[0]}":
                return self._json(401, {"error": "invalid_token"})
        message = json.loads(raw)
        if message.get("id") is None:
            return self._empty(202)
        method = message.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fake"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "ping"}]}
        else:
            result = {}
        self._json(200, {"jsonrpc": "2.0", "id": message["id"], "result": result})


class FakeServer:
    def __init__(self, **overrides) -> None:
        self.state = _FakeServerState(**overrides)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.fake = self.state  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


@pytest.fixture()
def fake_server():
    server = FakeServer()
    try:
        yield server
    finally:
        server.close()


def _config(server: FakeServer, **overrides) -> ServerConfig:
    oauth = OAuthConfig(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        **overrides,
    )
    return ServerConfig(name="remote", transport="http", url=f"{server.base}/mcp", oauth=oauth)


# --------------------------------------------------------------------------- #
# 1. 纯函数：JWT 解包与 URL 安全规则
# --------------------------------------------------------------------------- #
def test_jwt_claims_are_decoded_without_signature():
    token = make_jwt({"aud": "https://example.test/mcp", "exp": 9999999999})
    assert decode_jwt_claims(token) == {"aud": "https://example.test/mcp", "exp": 9999999999}


def test_opaque_or_malformed_tokens_are_not_guessed():
    assert decode_jwt_claims("opaque-token") is None
    assert decode_jwt_claims("a.!!!.c") is None
    assert decode_jwt_claims("a.bm90LWpzb24.c") is None  # payload 不是 JSON
    assert decode_jwt_claims("") is None


def test_insecure_remote_http_is_refused_but_loopback_is_allowed():
    for url in (
        "http://127.0.0.1:9/x",
        "http://localhost:9/x",
        "http://[::1]:9/x",
        "https://example.test/x",
    ):
        require_secure_url(url, "测试地址")  # 不抛就是通过
    with pytest.raises(Exception) as error:
        require_secure_url("http://example.test/x", "测试地址")
    assert "https" in str(error.value)


def test_error_text_strips_userinfo_from_urls():
    with pytest.raises(Exception) as error:
        require_secure_url("http://user:topsecret@example.test/x", "测试地址")
    assert "topsecret" not in str(error.value)


# --------------------------------------------------------------------------- #
# 2. 发现 + client_credentials
# --------------------------------------------------------------------------- #
async def test_client_credentials_discovers_token_endpoint_and_attaches_bearer(fake_server):
    client = HttpClient(_config(fake_server))
    try:
        await client.start()
        tools = await client.list_tools()
        # 连接关闭后 provider 会被抹掉；事实要在关之前取（这与"令牌只在内存里"一致）。
        facts = client.boundary_facts()
    finally:
        await client.close()

    assert [item["name"] for item in tools] == ["ping"]
    assert fake_server.state.calls["token"] == 1
    assert fake_server.state.token_forms[0]["grant_type"] == "client_credentials"
    assert fake_server.state.token_forms[0]["resource"] == f"{fake_server.base}/mcp"
    assert fake_server.state.basic_headers[0].startswith("Basic ")
    token = fake_server.state.issued[0]
    assert fake_server.state.seen_auth[-1] == f"Bearer {token}"

    auth = facts["auth"]
    assert auth["audience_checked"] is True
    assert auth["expiry_checked"] is True
    assert auth["metadata_source"] == "discovery"
    serialized = json.dumps(facts, ensure_ascii=False)
    assert token not in serialized and CLIENT_SECRET not in serialized


async def test_configured_token_endpoint_skips_discovery(fake_server):
    client = HttpClient(
        _config(fake_server, token_url=f"{fake_server.base}/token", audience=f"{fake_server.base}/mcp")
    )
    try:
        await client.start()
    finally:
        await client.close()
    assert fake_server.state.calls["wellknown"] == 0
    assert fake_server.state.calls["token"] == 1


async def test_configured_token_endpoint_honours_post_auth_method(fake_server):
    # 显式配 token_url 时不能丢 auth_method：post 必须把凭据放进表单，
    # 不能悄悄退回 Basic 头（真实授权服务器在两种方式间是严格区分的）。
    client = HttpClient(
        _config(
            fake_server,
            token_url=f"{fake_server.base}/token",
            audience=f"{fake_server.base}/mcp",
            auth_method="post",
        )
    )
    try:
        await client.start()
    finally:
        await client.close()
    form = fake_server.state.token_forms[0]
    assert form["client_id"] == CLIENT_ID
    assert form["client_secret"] == CLIENT_SECRET
    assert fake_server.state.basic_headers[0] == ""


async def test_client_secret_post_is_used_when_metadata_prefers_it(fake_server):
    fake_server.state.methods = ("client_secret_post",)
    client = HttpClient(_config(fake_server))
    try:
        await client.start()
    finally:
        await client.close()
    form = fake_server.state.token_forms[0]
    assert form["client_id"] == CLIENT_ID
    assert form["client_secret"] == CLIENT_SECRET
    assert fake_server.state.basic_headers[0] == ""


# --------------------------------------------------------------------------- #
# 3. 401 → 刷新 → 只重试一次
# --------------------------------------------------------------------------- #
async def test_401_refreshes_and_retries_once():
    server = FakeServer(reject_first_token=True)
    client = HttpClient(_config(server))
    try:
        await client.start()
        tools = await client.list_tools()
    finally:
        await client.close()
        server.close()

    assert [item["name"] for item in tools] == ["ping"]
    assert server.state.calls["token"] == 2, "看到 401 必须换一枚令牌，且只换一次"
    assert server.state.seen_auth[-1] == f"Bearer {server.state.issued[1]}"


async def test_401_again_fails_without_looping(fake_server):
    fake_server.state.always_401 = True
    client = HttpClient(_config(fake_server))
    with pytest.raises(MCPError) as error:
        await client.start()
    await client.close()
    assert error.value.code == "mcp_http_error"
    # 握手会按两个协议版本各试一次；每次 401 只换一枚令牌（1 枚初始 + 2 次刷新）。
    # 若刷新会递归/风暴，这里会明显大于 3。
    assert fake_server.state.calls["token"] == 3, "每次 401 只许刷新一次，不能刷新风暴"
    assert fake_server.state.calls["mcp"] == 4, "两个协议版本各两次请求，没有额外重试"


# --------------------------------------------------------------------------- #
# 4. 受众 / 过期 / opaque：核对结论必须如实
# --------------------------------------------------------------------------- #
async def test_expired_jwt_is_rejected(fake_server):
    fake_server.state.exp_delta = -60
    client = HttpClient(_config(fake_server))
    with pytest.raises(MCPError) as error:
        await client.start()
    await client.close()
    assert error.value.code == "mcp_auth_expired"
    assert CLIENT_SECRET not in error.value.message


async def test_audience_mismatch_is_rejected(fake_server):
    fake_server.state.aud = "https://someone-else.test/mcp"
    client = HttpClient(_config(fake_server))
    with pytest.raises(MCPError) as error:
        await client.start()
    await client.close()
    assert error.value.code == "mcp_auth_audience"
    assert "someone-else" in error.value.message


async def test_jwt_without_audience_is_rejected(fake_server):
    fake_server.state.include_aud = False
    client = HttpClient(_config(fake_server, audience=f"{fake_server.base}/mcp"))
    with pytest.raises(MCPError) as error:
        await client.start()
    await client.close()
    assert error.value.code == "mcp_auth_audience"


async def test_opaque_token_is_used_but_audience_check_is_reported_as_unavailable(fake_server):
    fake_server.state.opaque = True
    client = HttpClient(_config(fake_server, audience=f"{fake_server.base}/mcp"))
    try:
        await client.start()
        facts = client.boundary_facts()["auth"]
        assert facts["audience_checked"] is False
        assert facts["expiry_checked"] is False
        assert "不是 JWT" in facts["note"]
    finally:
        await client.close()
    assert fake_server.state.seen_auth[-1] == f"Bearer {fake_server.state.issued[0]}"


# --------------------------------------------------------------------------- #
# 5. 凭证不进错误、不发请求就拒绝
# --------------------------------------------------------------------------- #
async def test_token_endpoint_error_does_not_echo_the_secret(fake_server):
    fake_server.state.token_error = {
        "error": "invalid_client",
        "error_description": f"secret {CLIENT_SECRET} is wrong",
    }
    client = HttpClient(_config(fake_server))
    with pytest.raises(MCPError) as error:
        await client.start()
    await client.close()
    assert error.value.code == "mcp_auth_token_endpoint"
    assert CLIENT_SECRET not in error.value.message
    assert "***" in error.value.message


async def test_remote_http_is_refused_before_any_request_is_sent():
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(500)

    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    client = HttpClient(
        ServerConfig(
            name="remote",
            transport="http",
            url="http://mcp.example.test/mcp",
            oauth=OAuthConfig(client_id=CLIENT_ID, client_secret=CLIENT_SECRET),
            client_factory=factory,
        )
    )
    with pytest.raises(MCPError) as error:
        await client.start()
    assert error.value.code == "mcp_auth_insecure"
    assert sent == [], "不安全地址必须在任何令牌请求之前就被拒绝"


# --------------------------------------------------------------------------- #
# 6. 配置解析：auth 段说清楚每条错误
# --------------------------------------------------------------------------- #
def test_parse_servers_builds_oauth_config_and_never_reprs_the_secret(monkeypatch):
    monkeypatch.setenv("MCP_OAUTH_SECRET", CLIENT_SECRET)
    servers, statuses = parse_servers(
        {
            "mcpServers": {
                "remote": {
                    "url": "https://mcp.example.test/mcp",
                    "auth": {
                        "client_id": "cid",
                        "client_secret": "${MCP_OAUTH_SECRET}",
                        "scope": "mcp.read",
                        "audience": "https://mcp.example.test/mcp",
                        "auth_method": "post",
                    },
                }
            }
        }
    )
    assert statuses == []
    oauth = servers[0].oauth
    assert oauth is not None
    assert oauth.client_secret == CLIENT_SECRET
    assert oauth.scope == "mcp.read"
    assert oauth.auth_method == "post"
    assert CLIENT_SECRET not in repr(oauth)
    assert CLIENT_SECRET not in repr(servers[0])


@pytest.mark.parametrize(
    ("entry", "pattern"),
    [
        ({"command": "node", "auth": {"client_id": "c", "client_secret": "s"}}, "stdio"),
        ({"url": "https://x/mcp", "auth": {"client_id": "c"}}, "client_secret"),
        ({"url": "https://x/mcp", "auth": {"client_id": "c", "client_secret": "s", "type": "authorization_code"}}, "交互式"),
        ({"url": "https://x/mcp", "auth": {"client_id": "c", "client_secret": "s", "auth_method": "private_key_jwt"}}, "auth_method"),
        ({"url": "https://x/mcp", "auth": "Bearer abc"}, "对象"),
    ],
)
def test_parse_servers_rejects_broken_auth_with_a_reason(entry, pattern):
    servers, statuses = parse_servers({"mcpServers": {"bad": entry}})
    assert servers == []
    assert len(statuses) == 1
    assert statuses[0].state == "skipped"
    assert pattern in statuses[0].error


def test_parse_servers_without_auth_still_works():
    servers, statuses = parse_servers(
        {"mcpServers": {"plain": {"url": "https://x/mcp"}}}
    )
    assert statuses == []
    assert servers[0].oauth is None


# --------------------------------------------------------------------------- #
# 7. 桥接层端到端：状态带 auth 事实，不带令牌
# --------------------------------------------------------------------------- #
async def test_bridge_status_reports_auth_facts_without_the_token(fake_server, tmp_path):
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {
                        "url": f"{fake_server.base}/mcp",
                        "auth": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    manager = MCPManager(path=config_path)
    try:
        bundle = await manager.bundle()
        body = bundle.as_dict()
    finally:
        await manager.close()

    assert body["servers"][0]["state"] == "ready"
    assert body["servers"][0]["enforcement"]["auth"]["audience_checked"] is True
    serialized = json.dumps(body, ensure_ascii=False)
    token = fake_server.state.issued[0]
    assert token not in serialized
    assert CLIENT_SECRET not in serialized
