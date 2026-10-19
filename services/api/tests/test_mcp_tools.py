"""P1-3：接 MCP 客户端，把外部服务端的工具变成普通工具。

分五层验证，因为每一层失败的方式都不一样：

1. 协议层——名字要不要按模型的规则改写、结果里的图片会不会被悄悄丢掉；
2. 传输层——真的起一个子进程，验证握手、翻页、stderr 写满不卡死、进程挂了报什么；
3. HTTP 传输——会话 id 有没有回带，SSE 回包能不能解析；
4. 桥接层——同名的外部工具会不会顶掉内置工具，风险等级默认是多少；
5. 网关层——外部工具是不是真的走了同一套审批与审计，而不是绕过去。
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.mcp.bridge import MCPManager, bridge_tools, parse_servers
from blank_api.services.mcp.bridge import manager, reset_manager
from blank_api.services.models.base import AdapterConfig, ModelAdapter, ModelResponse, Usage
from blank_api.services.mcp.client import (
    HttpClient,
    MCPError,
    ServerConfig,
    StdioClient,
    build_client,
)
from blank_api.services.mcp.protocol import (
    NAME_LIMIT,
    flatten_result,
    normalize_schema,
    parse_message,
    parse_sse,
    qualify,
)
from blank_api.services.permissions import ASK, MODE_BYPASS, PermissionPolicy
from blank_api.services.tools.base import ToolDefinition
from blank_api.services.tools.gateway import ToolGateway
from blank_api.services.tools.registry import ToolRegistry
from tests.test_agent_v04 import (  # noqa: F401  (复用端到端夹具；workspace 来自 conftest)
    create_project,
    create_task,
    env,
)

SERVER = Path(__file__).with_name("fake_mcp_server.py")


def _stdio(name: str = "fake", mode: str = "normal", **overrides) -> ServerConfig:
    return ServerConfig(
        name=name,
        transport="stdio",
        command=sys.executable,
        args=(str(SERVER), mode),
        timeout=float(overrides.pop("timeout", 10.0)),
        **overrides,
    )


# --------------------------------------------------------------------------- #
# 1. 协议层
# --------------------------------------------------------------------------- #
def test_names_are_namespaced_and_model_safe():
    name = qualify("my.server", "list files")
    assert name == "mcp__my_server__list_files"
    # 空格、点号在 function name 里都是非法的，必须磨平
    assert " " not in name and "." not in name


def test_chinese_segments_still_produce_a_legal_name():
    """服务端叫「文件系统」、工具叫「读取」时不能算出空名字。

    磨不出可用字符就退成 x，至少还是个能调用的名字；空格或空串会让整个
    工具表在模型那边被拒。
    """
    name = qualify("文件系统", "读取")
    assert name.startswith("mcp__")
    assert len(name) > len("mcp__")
    assert all(char.isascii() for char in name)


def test_overlong_names_are_truncated_stably():
    tool = "a" * 80
    first = qualify("server", tool)
    second = qualify("server", tool)

    assert first == second, "同一个工具必须每次算出同一个名字，否则检索拉回来的名字对不上"
    assert len(first) <= NAME_LIMIT
    # 截断后仍要能区分不同工具
    assert first != qualify("server", tool + "b")


def test_empty_schema_becomes_a_legal_function_schema():
    """MCP 允许省掉 inputSchema，模型 API 不允许。"""
    assert normalize_schema(None) == {"type": "object", "properties": {}}
    assert normalize_schema({"type": "object"})["properties"] == {}
    kept = normalize_schema({"type": "object", "properties": {"a": {"type": "string"}}})
    assert kept["properties"]["a"]["type"] == "string"


def test_media_blocks_are_described_not_dropped():
    """图片/资源不能悄悄消失：模型看不到"这里有一张图"就会重复再调一次。"""
    is_error, text, data = flatten_result(
        {
            "content": [
                {"type": "text", "text": "截图如下"},
                {"type": "image", "data": "A" * 400, "mimeType": "image/png"},
                {"type": "resource", "resource": {"uri": "file:///a.bin"}},
            ]
        }
    )

    assert is_error is False
    assert "截图如下" in text
    assert "image/png" in text
    assert "file:///a.bin" in text
    assert [item["type"] for item in data["media"]] == ["image", "resource"]


def test_server_side_error_is_not_a_success():
    is_error, text, _ = flatten_result({"content": [{"type": "text", "text": "炸了"}], "isError": True})
    assert is_error is True
    assert text == "炸了"


def test_empty_result_says_so_instead_of_returning_nothing():
    _, text, _ = flatten_result({"content": []})
    # 空白会被模型当成"被截断了"，然后重试
    assert text.strip()


def test_structured_content_is_kept_when_there_is_no_text():
    _, text, data = flatten_result({"content": [], "structuredContent": {"count": 3}})
    assert "count" in text
    assert data["structured"] == {"count": 3}


def test_sse_payload_is_parsed_and_multiline_data_is_joined():
    body = 'event: message\ndata: {"jsonrpc":"2.0","id":1,\ndata: "result":{}}\n\n'
    messages = parse_sse(body)
    assert messages and messages[0]["id"] == 1


def test_non_json_banner_is_ignored():
    assert parse_message("fake-mcp-server 1.2.3 ready") is None
    assert parse_message('{"jsonrpc":"2.0","id":1,"result":{}}')["id"] == 1


# --------------------------------------------------------------------------- #
# 2. stdio 传输（真的起进程）
# --------------------------------------------------------------------------- #
async def test_handshake_and_list_tools():
    client = StdioClient(_stdio())
    try:
        await client.start()
        tools = await client.list_tools()
    finally:
        await client.close()

    assert client.server_info["name"] == "fake"
    assert client.protocol_version
    assert {item["name"] for item in tools} == {"echo", "boom", "run shell", "slow"}


async def test_call_returns_text():
    client = StdioClient(_stdio())
    try:
        await client.start()
        result = await client.call_tool("echo", {"text": "你好"})
    finally:
        await client.close()

    _, text, _ = flatten_result(result)
    assert text == "echo:你好"


async def test_noisy_stderr_does_not_deadlock():
    """服务端写 200 KiB 日志时，没人读 stderr 就会把进程卡死在自己的写操作上。

    症状是"调用挂住"，不是报错——所以这条测试的意义在于它会超时失败。
    """
    client = StdioClient(_stdio(mode="noisy", timeout=8.0))
    try:
        await client.start()
        tools = await client.list_tools()
    finally:
        await client.close()

    assert tools, "stderr 没被持续读走，服务端在写日志时卡住了"


async def test_banner_before_protocol_is_tolerated():
    client = StdioClient(_stdio(mode="banner"))
    try:
        await client.start()
        assert await client.list_tools()
    finally:
        await client.close()


async def test_crashed_server_reports_its_stderr():
    """服务端起不来时最有用的是它自己说的话，不是一句"连接失败"。"""
    client = StdioClient(_stdio(mode="crash"))
    with pytest.raises(MCPError) as error:
        await client.start()
    await client.close()

    assert error.value.code in ("mcp_closed", "mcp_transport", "mcp_handshake_failed")
    assert "MISSING_API_KEY" in error.value.message


async def test_silent_server_times_out_instead_of_hanging():
    client = StdioClient(_stdio(mode="silent", timeout=1.0))
    with pytest.raises(MCPError) as error:
        await client.start()
    await client.close()

    assert error.value.code == "mcp_timeout"


async def test_close_terminates_the_child_process():
    client = StdioClient(_stdio())
    await client.start()
    process = client._process
    await client.close()

    assert process is not None
    # 关闭之后子进程必须真的退出，否则退出应用会在系统里留下孤儿 npx 进程
    assert process.returncode is not None


@pytest.mark.skipif(sys.platform != "win32", reason=".cmd 启动器是 Windows 特有的坑")
async def test_batch_launcher_is_wrapped_in_cmd(tmp_path: Path):
    """npx / uvx 在 Windows 上是 .cmd，直接 spawn 会 FileNotFoundError。

    这条用真的 .cmd 文件起一遍，因为「解析出来看着对」和「真的能跑」是两回事。
    """
    launcher = tmp_path / "fake-mcp.cmd"
    launcher.write_text(
        f'@echo off\r\n"{sys.executable}" "{SERVER}" normal\r\n', encoding="ascii"
    )

    client = StdioClient(
        ServerConfig(name="shim", transport="stdio", command=str(launcher), timeout=15.0)
    )
    try:
        await client.start()
        assert await client.list_tools()
    finally:
        await client.close()


def test_unknown_transport_fails_loudly():
    with pytest.raises(MCPError) as error:
        build_client(ServerConfig(name="x", transport="sse", url="http://a"))
    assert error.value.code == "mcp_unsupported_transport"


# --------------------------------------------------------------------------- #
# 3. HTTP 传输
# --------------------------------------------------------------------------- #
def _http_config(handler, **overrides) -> ServerConfig:
    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return ServerConfig(
        name="remote",
        transport="http",
        url="https://example.test/mcp",
        timeout=5.0,
        client_factory=factory,
        **overrides,
    )


async def test_http_session_id_is_echoed_back():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        seen.append(dict(request.headers))
        if message.get("id") is None:
            return httpx.Response(202)
        if message["method"] == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "remote"}},
                },
                headers={"mcp-session-id": "sess-1"},
            )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": message["id"], "result": {"tools": [{"name": "ping"}]}},
        )

    client = HttpClient(_http_config(handler))
    try:
        await client.start()
        tools = await client.list_tools()
    finally:
        await client.close()

    assert tools[0]["name"] == "ping"
    # 第一个请求（initialize）还没有会话 id，后面的必须带上
    assert "mcp-session-id" not in seen[0]
    assert seen[-1]["mcp-session-id"] == "sess-1"


async def test_http_sse_response_is_accepted():
    def handler(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        if message.get("id") is None:
            return httpx.Response(202)
        if message["method"] == "initialize":
            payload = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "remote"}}
        else:
            payload = {"tools": [{"name": "ping"}]}
        body = 'event: message\ndata: {"jsonrpc":"2.0","id":%d,"result":%s}\n\n' % (
            message["id"],
            json.dumps(payload),
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = HttpClient(_http_config(handler))
    try:
        await client.start()
        assert [item["name"] for item in await client.list_tools()] == ["ping"]
    finally:
        await client.close()


async def test_http_error_status_becomes_a_clear_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad token")

    client = HttpClient(_http_config(handler))
    with pytest.raises(MCPError) as error:
        await client.start()
    await client.close()

    assert error.value.code == "mcp_http_error"
    assert "401" in error.value.message


# --------------------------------------------------------------------------- #
# 4. 桥接层
# --------------------------------------------------------------------------- #
CATALOG = [
    {
        "name": "run shell",
        "description": "外部服务的同名工具",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {"name": "echo", "description": "", "inputSchema": None},
]


def test_external_tool_cannot_shadow_a_builtin_one():
    server = ServerConfig(name="fake")
    tools = bridge_tools(server, client=None, catalog=CATALOG)
    names = {tool.name for tool in tools}

    assert "mcp__fake__run_shell" in names
    assert "run_shell" not in names


def test_tools_are_high_risk_by_default():
    tools = bridge_tools(ServerConfig(name="fake"), client=None, catalog=CATALOG)
    assert {tool.risk_level for tool in tools} == {"high"}
    assert all(tool.requires_approval for tool in tools)


def test_low_risk_must_be_declared_explicitly():
    tools = bridge_tools(ServerConfig(name="fake", risk="low"), client=None, catalog=CATALOG)
    assert {tool.risk_level for tool in tools} == {"low"}


def test_description_carries_the_server_name():
    """两家服务端可能有同名工具，描述里不写服务名模型分不清。"""
    tools = bridge_tools(ServerConfig(name="github"), client=None, catalog=CATALOG)
    assert all(tool.description.startswith("[github]") for tool in tools)


def test_same_sanitized_name_does_not_collapse_two_tools():
    """`a.b` 与 `a-b` 磨平后是同一个名字，不能因此少一个工具。"""
    catalog = [{"name": "a.b"}, {"name": "a-b"}]
    tools = bridge_tools(ServerConfig(name="s"), client=None, catalog=catalog)
    assert len({tool.name for tool in tools}) == 2


def test_config_rejects_unsupported_transport_with_a_reason():
    servers, statuses = parse_servers(
        {
            "mcpServers": {
                "legacy": {"type": "sse", "url": "http://x/sse"},
                "off": {"command": "x", "disabled": True},
                "ok": {"command": "npx", "args": ["-y", "server"]},
            }
        }
    )

    assert [server.name for server in servers] == ["ok"]
    reasons = {status.name: status.error for status in statuses}
    assert "sse" in reasons["legacy"]
    assert reasons["off"] == "已禁用"


def test_env_placeholders_are_expanded(monkeypatch):
    monkeypatch.setenv("MCP_TOKEN", "secret-1")
    servers, _ = parse_servers(
        {"mcpServers": {"remote": {"url": "https://x/mcp", "headers": {"Authorization": "Bearer ${MCP_TOKEN}"}}}}
    )
    assert servers[0].headers["Authorization"] == "Bearer secret-1"


# --------------------------------------------------------------------------- #
# 5. 管理器：连不上不能拖垮别人
# --------------------------------------------------------------------------- #
def _write_config(path: Path, servers: dict) -> None:
    path.write_text(json.dumps({"mcpServers": servers}, ensure_ascii=False), encoding="utf-8")


def _server_entry(mode: str) -> dict:
    return {"command": sys.executable, "args": [str(SERVER), mode], "timeout": 8}


async def test_missing_config_connects_nothing(tmp_path):
    manager = MCPManager(tmp_path / "mcp.json")
    bundle = await manager.bundle()
    await manager.close()

    assert bundle.tools == []
    assert bundle.statuses == []


async def test_broken_server_does_not_take_the_others_down(tmp_path):
    path = tmp_path / "mcp.json"
    _write_config(path, {"good": _server_entry("normal"), "bad": _server_entry("crash")})
    manager = MCPManager(path)
    try:
        bundle = await manager.bundle()
    finally:
        await manager.close()

    states = {status.name: status.state for status in bundle.statuses}
    assert states["good"] == "ready"
    assert states["bad"] == "failed"
    assert len(bundle.tools) == 4, "好的服务端必须照常提供工具"
    failed = next(status for status in bundle.statuses if status.name == "bad")
    assert "MISSING_API_KEY" in failed.error


async def test_manager_reconnects_when_the_file_changes(tmp_path):
    path = tmp_path / "mcp.json"
    _write_config(path, {})
    manager = MCPManager(path)
    try:
        assert (await manager.bundle()).tools == []
        _write_config(path, {"good": _server_entry("normal")})
        bundle = await manager.bundle()
        assert len(bundle.tools) == 4, "改完配置就该生效，不该要求重启进程"
    finally:
        await manager.close()


async def test_disable_env_wins_over_the_file(tmp_path, monkeypatch):
    path = tmp_path / "mcp.json"
    _write_config(path, {"good": _server_entry("normal")})
    monkeypatch.setenv("BLANK_MCP_DISABLE", "1")
    manager = MCPManager(path)
    bundle = await manager.bundle()
    await manager.close()

    assert bundle.tools == []


async def test_corrupt_config_is_reported_not_fatal(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text("{ 这不是 json", encoding="utf-8")
    manager = MCPManager(path)
    bundle = await manager.bundle()
    await manager.close()

    assert bundle.tools == []
    assert bundle.statuses and "解析失败" in bundle.statuses[0].error


# --------------------------------------------------------------------------- #
# 6. 注册表与网关：外部工具走同一套规则
# --------------------------------------------------------------------------- #
def _mcp_tool(name: str = "mcp__fake__echo", risk: str = "high") -> ToolDefinition:
    async def handler(_context, arguments):
        from blank_api.services.tools.base import ok

        return ok("ran:" + json.dumps(arguments, ensure_ascii=False))

    return ToolDefinition(
        name=name,
        description="[fake] 回显",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        risk_level=risk,
        handler=handler,
    )


def test_external_tools_are_deferred_by_default():
    """外部工具一接就常驻，会把提示词撑成"用户装了几个服务端"的函数。"""
    registry = ToolRegistry([_mcp_tool()])
    assert registry.deferred_names() == ["mcp__fake__echo"]
    assert registry.visible_names() == []


def test_tool_search_pulls_external_tools_by_server_name():
    registry = ToolRegistry([_mcp_tool(name="mcp__github__create_issue")])
    matched, added = registry.search("github")

    assert [tool.name for tool in matched] == ["mcp__github__create_issue"]
    assert added == ["mcp__github__create_issue"]
    assert registry.visible_names() == ["mcp__github__create_issue"]


def test_lean_profile_hides_external_tools():
    registry = ToolRegistry([_mcp_tool()])
    registry.set_profile("lean")
    assert "mcp__fake__echo" in registry.deferred_names()


async def _task_context(env, workspace: Path, mode: str):
    project_id = await create_project(env, workspace)
    task_id = await create_task(env, project_id, ["外部工具走审批"])
    context = replace(env.context(task_id, workspace), permissions=PermissionPolicy(mode=mode))
    return context, task_id


async def test_high_risk_external_tool_asks_before_running(env, workspace: Path):  # noqa: F811
    gateway = ToolGateway([_mcp_tool()])
    context, _ = await _task_context(env, workspace, "default")
    tool = gateway.get("mcp__fake__echo")

    decision = gateway.decide(tool, {"text": "x"}, context)
    assert decision.outcome == ASK, "外部工具默认必须走审批，不能当成内部只读工具"


class _StubAdapter(ModelAdapter):
    """只为装配而存在：这条用例验证的是工具表，不是模型。"""

    def __init__(self) -> None:
        super().__init__(
            AdapterConfig(
                name="stub",
                kind="openai",
                base_url="http://localhost",
                api_key="",
                model="stub",
                supports_tools=True,
            )
        )

    async def invoke(self, messages, **kwargs):  # pragma: no cover
        return ModelResponse(text="{}", usage=Usage(1, 1), model="stub")

    async def stream(self, messages, **kwargs):  # pragma: no cover
        yield ""

    async def bind_tools(self, specs, messages, **kwargs):  # pragma: no cover
        return ModelResponse(text="{}", usage=Usage(1, 1), model="stub")


async def test_runtime_prepares_external_tools_and_reports_them(
    tmp_path: Path, workspace: Path, monkeypatch
):  # noqa: F811
    """真正要验的是装配：`_prepare` 之后网关里有没有这些工具、状态有没有被记下来。

    这条覆盖的是"接了 MCP 但工具没进网关"这种最难发现的情况——代码全在，
    只是没接上。
    """
    config = tmp_path / "mcp.json"
    _write_config(config, {"fake": _server_entry("normal")})
    monkeypatch.setenv("BLANK_MCP_FILE", str(config))
    reset_manager()

    runtime = AgentRuntime(session_factory=None)
    request = RunRequest(
        task_id="task-mcp-wiring",
        provider_id="p",
        provider_name="stub",
        adapter=_StubAdapter(),
        root=workspace,
        goal="看看外部工具接上没有",
    )
    try:
        scope = await runtime._prepare(request, "task-mcp-wiring:1")
        registry = scope.session.gateway.registry
        assert "mcp__fake__echo" in scope.session.gateway.tools
        # 外部工具默认收起，但确实可被检索到
        assert "mcp__fake__echo" in registry.deferred_names()
        assert scope.mcp["tools"] == 4
        assert scope.mcp["servers"][0]["state"] == "ready"
    finally:
        await manager().close()
        reset_manager()


async def test_status_endpoint_explains_each_server(client, tmp_path: Path, monkeypatch):
    """排障靠这一条：哪个连上了、哪个为什么没连上，都要能直接看到。"""
    config = tmp_path / "mcp.json"
    _write_config(config, {"good": _server_entry("normal"), "bad": _server_entry("crash")})
    monkeypatch.setenv("BLANK_MCP_FILE", str(config))
    reset_manager()
    try:
        body = (await client.get("/api/v1/settings/mcp")).json()
    finally:
        await manager().close()
        reset_manager()

    assert body["tools"] == 4
    states = {item["name"]: item["state"] for item in body["servers"]}
    assert states == {"good": "ready", "bad": "failed"}
    assert body["config"].endswith("mcp.json")


async def test_approved_external_tool_runs_through_the_gateway(env, workspace: Path):  # noqa: F811
    gateway = ToolGateway([_mcp_tool()])
    context, _ = await _task_context(env, workspace, MODE_BYPASS)

    result = await gateway.execute("mcp__fake__echo", {"text": "hi"}, context, node="execute_action")

    assert result["ok"] is True
    assert result["output"] == 'ran:{"text": "hi"}'
    assert result["tool"] == "mcp__fake__echo"
    # 审计记录必落库：外部工具和内置工具在同一张表里，没有第二条账
    assert result["tool_call_id"]
