"""MCP 资源四工具对齐 Claude 的判据（R4）。

四层，每层都能单独失败：

1. **客户端协议**：resources 能力门控、目录声明的识别、目录翻页（第二页坏了保留
   第一页）、原文 JSON-RPC 错误码能被区分（-32601 / -32602 / -32002）。
2. **工具语义**：没声明 resources 的服务端不被列举；二进制落盘并给路径；不是目录时
   的文案；找不到资源时按"服务端有没有目录能力"决定要不要补提示；全部失败时不回
   "No resources found"。
3. **刷新**：只重查已连接的服务端、绝不重拨；新增/移除按名字列出；同名工具就地换
   定义；用 tool_search 拉出来的激活状态不被刷新清掉。
4. **注册与装配**：四工具默认收起、可检索、三读可并行；运行时把 MCPBundle 注入
   ToolContext，网关里能拿到四工具。

外部依赖只有 ``tests/fake_mcp_server.py`` 的子进程与 ``BLANK_MCP_FILE``；
每个用例自己收尾，不碰用户 home。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.mcp.bridge import MCPStatus, manager, reset_manager
from blank_api.services.mcp.client import (
    MAX_DIRECTORY_PAGES,
    MCP_INVALID_PARAMS,
    MCP_METHOD_NOT_FOUND,
    MCP_RESOURCE_NOT_FOUND,
    MCPError,
    ServerConfig,
    StdioClient,
)
from blank_api.services.models.base import AdapterConfig, ModelAdapter, ModelResponse, Usage
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.base import ToolContext, ToolDefinition
from blank_api.services.tools.gateway import ToolGateway
from blank_api.services.tools.registry import DEFERRED_TOOLS, PARALLEL_SAFE_TOOLS, ToolRegistry

SERVER = Path(__file__).with_name("fake_mcp_server.py")
TOOLS = {
    "list": "list_mcp_resources",
    "read": "read_mcp_resource",
    "dir": "read_mcp_resource_dir",
    "refresh": "refresh_mcp_tools",
}


def _stdio(name: str = "fake", mode: str = "resources") -> ServerConfig:
    return ServerConfig(
        name=name,
        transport="stdio",
        command=sys.executable,
        args=(str(SERVER), mode),
        timeout=10.0,
    )


async def _client(mode: str = "resources", name: str = "fake") -> StdioClient:
    client = StdioClient(_stdio(name, mode))
    await client.start()
    return client


def _bundle(clients: dict, statuses: list | None = None) -> SimpleNamespace:
    return SimpleNamespace(clients=clients, statuses=statuses or [])


def _context(mcp=None, gateway=None, root: Path | None = None, task_id: str = "task-mcp") -> ToolContext:
    return ToolContext(
        root=root or Path("."),
        task_id=task_id,
        session_factory=None,
        mcp=mcp,
        tool_gateway=gateway,
    )


def _tool(name: str) -> ToolDefinition:
    return {tool.name: tool for tool in build_default_tools()}[name]


async def _call(name: str, context: ToolContext, arguments: dict | None = None):
    return await _tool(name).handler(context, arguments or {})


# --------------------------------------------------------------------------- #
# 1. 客户端协议
# --------------------------------------------------------------------------- #
async def test_capabilities_and_directory_declaration_are_read_from_handshake():
    capable = await _client("resources")
    plain = await _client("normal")
    try:
        assert capable.supports_resources is True
        assert capable.declares_directory_read() is True
        assert plain.supports_resources is False
        assert plain.declares_directory_read() is False
    finally:
        await capable.close()
        await plain.close()


async def test_resource_listing_is_paginated_by_the_client():
    client = await _client("resources")
    try:
        resources = await client.list_resources()
    finally:
        await client.close()
    assert [item["uri"] for item in resources] == [
        "file:///notes/readme.txt",
        "file:///notes/data.bin",
        "dir:///docs",
    ]


async def test_directory_listing_follows_cursor_to_the_last_page():
    client = await _client("resources")
    try:
        entries, truncated = await client.read_resource_directory("dir:///docs")
    finally:
        await client.close()
    assert [item["name"] for item in entries] == ["a", "b", "c"]
    assert truncated is False


async def test_directory_listing_stops_at_the_page_cap_and_says_truncated():
    """服务端永远给 nextCursor 时必须在页数上限停下，并如实标出"被截断"。

    少了这条，`truncated` 恒为 False 也不会有人发现——而"翻到一半就停"正是
    模型最容易被误导的地方（它会以为目录里只有这些）。
    """
    client = await _client("resources")
    try:
        entries, truncated = await client.read_resource_directory("dir:///endless")
    finally:
        await client.close()
    assert len(entries) == MAX_DIRECTORY_PAGES
    assert truncated is True


async def test_directory_listing_keeps_previous_pages_when_a_later_page_breaks():
    """第二页回 InvalidParams 时，前几页是真的，不能整次判失败。"""
    client = await _client("resources")
    try:
        entries, truncated = await client.read_resource_directory("dir:///broken")
    finally:
        await client.close()
    assert [item["uri"] for item in entries] == ["file:///docs/x.md"]
    assert truncated is False


async def test_raw_rpc_error_codes_are_visible_to_callers():
    """工具要靠错误码分开"没实现 / 不是目录 / 找不到"，不能在文案里猜。"""
    client = await _client("resources")
    try:
        with pytest.raises(MCPError) as not_directory:
            await client.read_resource_directory("file:///notes/readme.txt")
        with pytest.raises(MCPError) as missing:
            await client.read_resource("file:///missing.txt")
        with pytest.raises(MCPError) as no_method:
            await client.request("resources/teleport", {})
    finally:
        await client.close()

    assert not_directory.value.rpc_code == MCP_INVALID_PARAMS
    assert missing.value.rpc_code == MCP_RESOURCE_NOT_FOUND
    assert no_method.value.rpc_code == MCP_METHOD_NOT_FOUND


# --------------------------------------------------------------------------- #
# 2. 工具语义
# --------------------------------------------------------------------------- #
async def test_list_skips_servers_that_never_declared_resources():
    """没声明 resources 的服务端连 resources/list 都不该发。"""
    client = await _client("normal")
    try:
        outcome = await _call(TOOLS["list"], _context(_bundle({"fake": client})))
    finally:
        await client.close()

    assert outcome.ok is True
    assert outcome.output == (
        "No resources found. MCP servers may still provide tools even if they have no resources."
    )
    assert outcome.data["count"] == 0
    # 没有能力就不该发 resources/list；发了会拿到 -32601，表现为 data.errors 非空。
    assert "errors" not in outcome.data


async def test_list_merges_servers_and_names_each_row():
    first = await _client("resources", "alpha")
    second = await _client("resources", "beta")
    try:
        outcome = await _call(
            TOOLS["list"], _context(_bundle({"alpha": first, "beta": second}))
        )
    finally:
        await first.close()
        await second.close()

    assert outcome.ok is True
    rows = outcome.data["resources"]
    assert len(rows) == 6
    assert {row["server"] for row in rows} == {"alpha", "beta"}
    assert {row["uri"] for row in rows} >= {"file:///notes/readme.txt", "dir:///docs"}


class _BrokenList:
    supports_resources = True
    declares_directory_read = lambda self: False  # noqa: E731

    async def list_resources(self):
        raise MCPError("mcp_transport", "连接断了")


class _EmptyList:
    """能列资源、但这会儿一条也没有：这不是失败。"""

    supports_resources = True
    declares_directory_read = lambda self: False  # noqa: E731

    async def list_resources(self):
        return []


async def test_one_broken_server_does_not_hide_the_healthy_ones():
    """一家坏了，另一家的资源照常返回；失败只作为附带信息出现。"""
    good = await _client("resources")
    try:
        outcome = await _call(
            TOOLS["list"], _context(_bundle({"good": good, "bad": _BrokenList()}))
        )
    finally:
        await good.close()

    assert outcome.ok is True
    assert outcome.data["count"] == 3
    assert [item["server"] for item in outcome.data["errors"]] == ["bad"]


async def test_all_failed_requires_every_capable_server_to_actually_fail():
    """一家失败、另一家只是没有资源：那是"一部分读不出来"，不是"全部失败"。"""
    outcome = await _call(
        TOOLS["list"], _context(_bundle({"empty": _EmptyList(), "bad": _BrokenList()}))
    )

    assert outcome.ok is True
    assert outcome.output == (
        "No resources found. MCP servers may still provide tools even if they have no resources."
    )
    assert [item["server"] for item in outcome.data["errors"]] == ["bad"]


async def test_list_all_failed_is_not_reported_as_no_resources():
    """每家都炸了时说 "No resources found" 会把"读不出来"说成"没有资源"。"""
    outcome = await _call(TOOLS["list"], _context(_bundle({"bad": _BrokenList()})))

    assert outcome.ok is False
    assert "全部失败" in outcome.message
    assert "bad" in outcome.message


async def test_read_returns_text_as_is():
    client = await _client("resources")
    try:
        outcome = await _call(
            TOOLS["read"],
            _context(_bundle({"fake": client})),
            {"server": "fake", "uri": "file:///notes/readme.txt"},
        )
    finally:
        await client.close()

    assert outcome.ok is True
    payload = json.loads(outcome.output)
    assert payload["contents"] == [
        {"uri": "file:///notes/readme.txt", "mimeType": "text/plain", "text": "# 说明\n第一行"}
    ]


async def test_read_saves_blob_outside_the_workspace(tmp_path):
    client = await _client("resources")
    try:
        outcome = await _call(
            TOOLS["read"],
            _context(_bundle({"fake": client}), root=tmp_path),
            {"server": "fake", "uri": "file:///notes/data.bin"},
        )
    finally:
        await client.close()

    assert outcome.ok is True
    entry = json.loads(outcome.output)["contents"][0]
    saved = Path(entry["blobSavedTo"])
    assert saved.is_file()
    assert saved.read_bytes() == b"\x00\x01\x02"
    assert tmp_path not in saved.parents, "读远端资源不该在用户工作区里留文件"
    assert entry["text"].startswith("[资源 file:///notes/data.bin]")


class _StubRead:
    """按需回包的读客户端：只测工具对回包形状的处理，不涉传输。"""

    declares_directory_read = lambda self: False  # noqa: E731

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def read_resource(self, uri: str) -> dict:
        return self.payload


async def test_read_with_no_content_is_an_error_not_an_empty_success():
    context = _context(_bundle({"fake": _StubRead({"contents": []})}))
    outcome = await _call(TOOLS["read"], context, {"server": "fake", "uri": "file:///x"})

    assert outcome.ok is False
    assert "returned no content" in outcome.message


async def test_invalid_base64_blob_is_reported_without_saving():
    context = _context(_bundle({"fake": _StubRead({"contents": [{"uri": "file:///x", "blob": "!!!"}]})}))
    outcome = await _call(TOOLS["read"], context, {"server": "fake", "uri": "file:///x"})

    assert outcome.ok is True
    entry = json.loads(outcome.output)["contents"][0]
    assert "blobSavedTo" not in entry
    assert "未保存" in entry["text"]


async def test_read_missing_resource_adds_directory_hint_when_declared():
    client = await _client("resources")
    try:
        outcome = await _call(
            TOOLS["read"],
            _context(_bundle({"fake": client})),
            {"server": "fake", "uri": "file:///missing.txt"},
        )
    finally:
        await client.close()

    assert outcome.ok is False
    assert outcome.message == (
        "Resource not found: file:///missing.txt — it may have been deleted or the URI is "
        "stale. Re-run ListMcpResources to refresh. "
        "If the URI is a directory resource, use ReadMcpResourceDir instead."
    )


async def test_read_missing_resource_has_no_directory_hint_without_the_capability():
    client = await _client("resources_nodir")
    try:
        outcome = await _call(
            TOOLS["read"],
            _context(_bundle({"fake": client})),
            {"server": "fake", "uri": "file:///missing.txt"},
        )
    finally:
        await client.close()

    assert outcome.ok is False
    assert "ReadMcpResourceDir" not in outcome.message
    assert outcome.message.startswith("Resource not found: file:///missing.txt")


async def test_read_on_server_without_resource_reads_says_exactly_that():
    client = await _client("normal")
    try:
        outcome = await _call(
            TOOLS["read"],
            _context(_bundle({"fake": client})),
            {"server": "fake", "uri": "file:///notes/readme.txt"},
        )
    finally:
        await client.close()

    assert outcome.ok is False
    assert outcome.message == (
        'Server "fake" advertises resource support but does not implement resource reads.'
    )


async def test_directory_tool_refuses_before_sending_the_nonstandard_method():
    """服务端没有声明目录能力时，工具必须自己拒绝。

    这条服务端在收到 resources/directory/read 时会"成功"返回一页——所以工具如果只是
    把服务端的拒绝转述出去，这里会拿到条目而不是这句拒绝。
    """
    client = await _client("resources_nodir")
    try:
        outcome = await _call(
            TOOLS["dir"],
            _context(_bundle({"fake": client})),
            {"server": "fake", "uri": "dir:///docs"},
        )
        direct, _ = await client.read_resource_directory("dir:///docs")
    finally:
        await client.close()

    assert outcome.ok is False
    assert outcome.message == 'Server "fake" does not support directory listing.'
    assert direct, "服务端本身能答；工具的结果必须是门控拒绝，而不是转发"


async def test_directory_tool_rejects_a_file_uri_with_the_file_tool_hint():
    client = await _client("resources")
    try:
        outcome = await _call(
            TOOLS["dir"],
            _context(_bundle({"fake": client})),
            {"server": "fake", "uri": "file:///notes/readme.txt"},
        )
    finally:
        await client.close()

    assert outcome.ok is False
    assert outcome.message == (
        "Not a directory resource: file:///notes/readme.txt. "
        "If it is a file resource, use ReadMcpResource instead."
    )


async def test_directory_tool_returns_entries_with_names():
    client = await _client("resources")
    try:
        outcome = await _call(
            TOOLS["dir"],
            _context(_bundle({"fake": client})),
            {"server": "fake", "uri": "dir:///docs"},
        )
    finally:
        await client.close()

    assert outcome.ok is True
    assert [row["name"] for row in outcome.data["resources"]] == ["a", "b", "c"]
    assert outcome.data["truncated"] is False


async def test_directory_tool_reports_truncation_to_the_model():
    client = await _client("resources")
    try:
        outcome = await _call(
            TOOLS["dir"],
            _context(_bundle({"fake": client})),
            {"server": "fake", "uri": "dir:///endless"},
        )
    finally:
        await client.close()

    assert outcome.ok is True
    assert outcome.data["count"] == MAX_DIRECTORY_PAGES
    assert outcome.data["truncated"] is True


async def test_unknown_server_reports_the_connected_ones():
    client = await _client("resources")
    try:
        outcome = await _call(
            TOOLS["list"], _context(_bundle({"fake": client})), {"server": "ghost"}
        )
    finally:
        await client.close()

    assert outcome.ok is False
    assert outcome.message == 'Server "ghost" not found. Available servers: fake'


# --------------------------------------------------------------------------- #
# 3. 刷新
# --------------------------------------------------------------------------- #
def _gateway_with_catalog(client: StdioClient, catalog: list[dict], name: str = "fake") -> ToolGateway:
    from blank_api.services.mcp.bridge import bridge_tools

    gateway = ToolGateway(build_default_tools())
    gateway.add_tools(bridge_tools(client.config, client, catalog))
    return gateway


async def test_refresh_lists_added_and_removed_tools_and_hot_updates_the_gateway():
    client = await _client("dynamic")
    try:
        catalog = await client.list_tools()
        gateway = _gateway_with_catalog(client, catalog)
        context = _context(_bundle({"fake": client}), gateway=gateway)

        await client.call_tool("add_late", {})
        added = await _call(TOOLS["refresh"], context)
        assert added.ok is True
        first = json.loads(added.output)[0]
        assert first["status"] == "refreshed"
        assert first["added"] == ["mcp__fake__late_tool"]
        assert first["removed"] == []
        assert gateway.get("mcp__fake__late_tool") is not None

        await client.call_tool("drop_echo", {})
        removed = await _call(TOOLS["refresh"], context)
        second = json.loads(removed.output)[0]
        assert second["removed"] == ["mcp__fake__echo"]
        assert gateway.get("mcp__fake__echo") is None
    finally:
        await client.close()


async def test_refresh_keeps_activation_for_tools_that_did_not_change():
    client = await _client("dynamic")
    try:
        catalog = await client.list_tools()
        gateway = _gateway_with_catalog(client, catalog)
        gateway.registry.activate(["mcp__fake__echo"])
        context = _context(_bundle({"fake": client}), gateway=gateway)

        await client.call_tool("add_late", {})
        await _call(TOOLS["refresh"], context)

        assert "mcp__fake__echo" in gateway.registry.visible_names(), (
            "同名工具被刷新时不该顺带丢掉 tool_search 拉出来的激活状态"
        )
    finally:
        await client.close()


def test_sync_replaces_definition_in_place_without_losing_visibility():
    gateway = ToolGateway(build_default_tools())
    original = ToolDefinition(
        name="mcp__fake__echo",
        description="旧描述",
        input_schema={"type": "object", "properties": {}},
        risk_level="high",
        handler=None,  # type: ignore[arg-type]
    )
    gateway.add_tools([original])
    gateway.registry.activate(["mcp__fake__echo"])

    replacement = ToolDefinition(
        name="mcp__fake__echo",
        description="新描述",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        risk_level="high",
        handler=None,  # type: ignore[arg-type]
    )
    added, removed = gateway.sync_mcp_tools("fake", [replacement])

    assert (added, removed) == ([], [])
    assert gateway.get("mcp__fake__echo").description == "新描述"
    # 注册表里的那份也要换：检索和下发用它，只换网关的 self.tools 会留下两份定义。
    stored = {tool.name: tool for tool in gateway.registry.all()}["mcp__fake__echo"]
    assert stored.description == "新描述"
    assert "mcp__fake__echo" in gateway.registry.visible_names()


def test_sync_only_touches_the_named_server():
    """刷新一家服务端不能把别家的工具一起摘掉（名字前缀必须带服务端名）。"""
    gateway = ToolGateway(build_default_tools())
    gateway.add_tools(
        [
            ToolDefinition(
                name="mcp__fake__echo",
                description="fake",
                input_schema={"type": "object", "properties": {}},
                risk_level="high",
                handler=None,  # type: ignore[arg-type]
            ),
            ToolDefinition(
                name="mcp__other__keep",
                description="other",
                input_schema={"type": "object", "properties": {}},
                risk_level="high",
                handler=None,  # type: ignore[arg-type]
            ),
        ]
    )
    gateway.sync_mcp_tools("fake", [])

    assert gateway.get("mcp__fake__echo") is None
    assert gateway.get("mcp__other__keep") is not None


class _BrokenTools:
    """已连接但 tools/list 失败：刷新必须报 error，不能当成刷新成功。"""

    config = None

    async def list_tools(self):
        raise MCPError("mcp_transport", "工具表读不出来")


async def test_refresh_reports_a_server_whose_tool_listing_failed():
    gateway = ToolGateway(build_default_tools())
    context = _context(_bundle({"bad": _BrokenTools()}), gateway=gateway)

    outcome = await _call(TOOLS["refresh"], context)
    assert outcome.ok is True
    entry = json.loads(outcome.output)[0]
    assert entry["status"] == "error"
    assert "工具表读不出来" in entry["error"]
    assert entry["added"] == [] and entry["removed"] == []


def test_refresh_cannot_bring_back_a_scenario_excluded_tool():
    """场景禁用的工具名在刷新后才出现时，也不能从此变得可用。"""
    gateway = ToolGateway([], excluded={"mcp__fake__late_tool"})
    # 构造期这个名字还不在工具表里，第一次 exclude 拦不住它。
    assert gateway.registry.exclude(gateway.excluded) == []

    gateway.add_tools(
        [
            ToolDefinition(
                name="mcp__fake__late_tool",
                description="late",
                input_schema={"type": "object", "properties": {}},
                risk_level="high",
                handler=None,  # type: ignore[arg-type]
            )
        ]
    )

    assert "mcp__fake__late_tool" in gateway.registry.excluded


async def test_refresh_never_redials_a_server_that_failed_to_connect():
    status = MCPStatus("dead", "stdio", "failed", error="启动失败")
    context = _context(_bundle({}, [status]))

    not_connected = await _call(TOOLS["refresh"], context, {"server": "dead"})
    assert not_connected.ok is True
    assert json.loads(not_connected.output)[0]["status"] == "not_connected"

    unknown = await _call(TOOLS["refresh"], context, {"server": "ghost"})
    assert unknown.ok is False
    assert unknown.message == 'Server "ghost" not found. Available servers: '

    empty = await _call(TOOLS["refresh"], context)
    assert empty.ok is True
    assert empty.output == "No MCP servers are connected."


# --------------------------------------------------------------------------- #
# 4. 注册与装配
# --------------------------------------------------------------------------- #
def test_four_tools_are_deferred_and_searchable():
    names = {tool.name for tool in build_default_tools()}
    assert set(TOOLS.values()) <= names
    assert set(TOOLS.values()) <= DEFERRED_TOOLS

    registry = ToolRegistry(build_default_tools())
    matched, _ = registry.search("资源")
    assert "list_mcp_resources" in {tool.name for tool in matched}
    matched, _ = registry.search("刷新工具")
    assert "refresh_mcp_tools" in {tool.name for tool in matched}


def test_three_reads_are_parallel_safe_but_refresh_is_not():
    assert {TOOLS["list"], TOOLS["read"], TOOLS["dir"]} <= PARALLEL_SAFE_TOOLS
    assert TOOLS["refresh"] not in PARALLEL_SAFE_TOOLS


def test_schemas_match_the_claude_contract_shape():
    tools = {tool.name: tool for tool in build_default_tools()}
    listing = tools[TOOLS["list"]].input_schema
    assert listing["additionalProperties"] is False
    assert "server" in listing["properties"]
    assert "required" not in listing

    for name in (TOOLS["read"], TOOLS["dir"]):
        schema = tools[name].input_schema
        assert schema["required"] == ["server", "uri"]
        assert schema["additionalProperties"] is False
    assert tools[TOOLS["refresh"]].input_schema["additionalProperties"] is False


class _StubAdapter(ModelAdapter):
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


async def test_runtime_injects_the_mcp_bundle_into_the_tool_context(
    tmp_path: Path, workspace: Path, monkeypatch
):  # noqa: F811
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fake": {
                        "command": sys.executable,
                        "args": [str(SERVER), "resources"],
                        "timeout": 8,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BLANK_MCP_FILE", str(config))
    reset_manager()

    runtime = AgentRuntime(session_factory=None)
    request = RunRequest(
        task_id="task-mcp-resources",
        provider_id="p",
        provider_name="stub",
        adapter=_StubAdapter(),
        root=workspace,
        goal="看看资源工具接上没有",
    )
    try:
        scope = await runtime._prepare(request, "task-mcp-resources:1")
        context = scope.session.context
        assert "fake" in context.mcp.clients
        assert context.mcp.clients["fake"].supports_resources is True
        assert scope.session.gateway.get(TOOLS["list"]) is not None
        assert TOOLS["list"] in scope.session.gateway.registry.deferred_names()
    finally:
        await manager().close()
        reset_manager()
