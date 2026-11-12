"""MCP 资源四工具（对齐 Claude Code 2.1.271）。

Claude 把 MCP 服务端的资源读成四个独立工具，而不是塞进 ``mcp__*`` 命名空间：

* ``ListMcpResources`` —— 只列已连接、且在 initialize 里声明了 resources 的服务端；
* ``ReadMcpResource`` —— 按 URI 读一条资源，文本原样返回，二进制落盘给路径；
* ``ReadMcpResourceDir`` —— 非标准方法 ``resources/directory/read``，服务端必须声明
  ``extensions.*.directoryRead``；首页 InvalidParams＝不是目录，后续页坏了保留前几页；
* ``RefreshMcpTools`` —— 重查已连接服务端的工具表并热更新本地注册表，**绝不重拨**。

三个不变量：

* **能力先于调用。** 没声明 resources 的服务端不被 ``resources/list`` 打扰；没声明
  目录能力的服务端连非标准方法都不发。少发一条 RPC 换来的是"没这个能力"与"能力坏了"
  能分开说。
* **失败按服务端隔离。** 一家坏了不影响另一家；全部失败时不回 "No resources found"，
  因为那句话会把"没人给我资源"和"每家都炸了"说成同一件事。
* **刷新只动本地工具表。** ``RefreshMcpTools`` 不重连、不重建进程；同名工具就地换定义，
  模型用 tool_search 拉出来的激活状态跟着保留。
"""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
from pathlib import Path

from blank_api.services.mcp.client import (
    MCP_INVALID_PARAMS,
    MCP_METHOD_NOT_FOUND,
    MCP_RESOURCE_NOT_FOUND,
    MCPError,
    ServerConfig,
)
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok

_NO_SERVERS = "No MCP servers are connected."
_LIST_EMPTY = "No resources found. MCP servers may still provide tools even if they have no resources."


def _clients(context: ToolContext) -> dict:
    bundle = getattr(context, "mcp", None)
    clients = getattr(bundle, "clients", None)
    return dict(clients) if isinstance(clients, dict) else {}


def _known_servers(context: ToolContext) -> list[str]:
    """所有在配置里出现过的服务端名（连上的 + 连失败的），排障时比只列连上的有用。"""
    bundle = getattr(context, "mcp", None)
    names = set(_clients(context))
    for status in getattr(bundle, "statuses", None) or []:
        name = str(getattr(status, "name", "") or "")
        if name:
            names.add(name)
    return sorted(names)


def _server_not_found(context: ToolContext, server: str) -> ToolOutcome:
    available = ", ".join(sorted(_clients(context)))
    return fail("error", f'Server "{server}" not found. Available servers: {available}')


def _dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# ListMcpResources
# --------------------------------------------------------------------------- #
def _resource_row(server: str, item: dict) -> dict:
    row = {"server": server, "uri": str(item.get("uri") or "")}
    for key in ("name", "mimeType", "description"):
        value = item.get(key)
        if value:
            row[key] = str(value)
    return row


async def _list_resources(context: ToolContext, arguments: dict) -> ToolOutcome:
    clients = _clients(context)
    requested = str(arguments.get("server") or "").strip()
    if requested:
        if requested not in clients:
            return _server_not_found(context, requested)
        targets = [requested]
    else:
        if not clients:
            return fail("error", _NO_SERVERS)
        targets = sorted(clients)

    rows: list[dict] = []
    errors: list[dict] = []
    for name in targets:
        client = clients[name]
        if not getattr(client, "supports_resources", False):
            continue
        try:
            for item in await client.list_resources():
                rows.append(_resource_row(name, item))
        except MCPError as error:
            # 同一家失败不拖垮别家；但失败这件事必须留下（data.errors），
            # 不能像 Claude 那样只写日志——模型看不到日志。
            errors.append({"server": name, "error": error.message})
    if not rows and errors and len(errors) == sum(
        1 for name in targets if getattr(clients[name], "supports_resources", False)
    ):
        # 每家都失败时不说 "No resources found"：那是"没有资源"，不是"读不出来"。
        detail = "；".join(f"{item['server']}: {item['error']}" for item in errors)
        return fail("error", f"MCP 资源列举全部失败：{detail}", output=_dump({"errors": errors}))
    body = _dump({"resources": rows})
    if not rows:
        body = _LIST_EMPTY
    data = {"resources": rows, "count": len(rows)}
    if errors:
        data["errors"] = errors
    return ToolOutcome(ok=True, output=body, data=data)


# --------------------------------------------------------------------------- #
# ReadMcpResource
# --------------------------------------------------------------------------- #
def _save_blob(uri: str, mime: str, encoded: str, task_id: str) -> tuple[str, int]:
    """二进制内容落到系统临时目录，返回 (路径, 字节数)。

    不写进工作区：读一条远端资源不该在用户的仓库里留下文件。这跟 Claude 的
    内部存储位置不同（见 CHANGELOG 偏离项），但"给路径、不展开正文"的契约一致。
    """
    body = base64.b64decode(encoded or "", validate=True)
    folder = Path(tempfile.gettempdir()) / "blank-mcp-resources" / (task_id or "task")
    folder.mkdir(parents=True, exist_ok=True)
    suffix = ""
    if "/" in mime:
        suffix = "." + mime.split("/", 1)[1].split(";")[0].strip().replace("+", "")
    name = hashlib.sha1((uri or "").encode("utf-8")).hexdigest()[:12] + suffix
    path = folder / name
    path.write_bytes(body)
    return str(path), len(body)


async def _read_resource(context: ToolContext, arguments: dict) -> ToolOutcome:
    clients = _clients(context)
    server = str(arguments.get("server") or "").strip()
    uri = str(arguments.get("uri") or "").strip()
    if server not in clients:
        return _server_not_found(context, server)
    client = clients[server]
    try:
        result = await client.read_resource(uri)
    except MCPError as error:
        code = error.rpc_code
        if code == MCP_METHOD_NOT_FOUND:
            return fail(
                "error",
                f'Server "{server}" advertises resource support but does not implement resource reads.',
            )
        if code == MCP_RESOURCE_NOT_FOUND:
            message = (
                f"Resource not found: {uri} — it may have been deleted or the URI is stale. "
                "Re-run ListMcpResources to refresh."
            )
            if getattr(client, "declares_directory_read", lambda: False)():
                message += " If the URI is a directory resource, use ReadMcpResourceDir instead."
            return fail("error", message)
        return fail("error", f'MCP resource read failed on server "{server}": {error.message}')

    contents: list[dict] = []
    for item in result.get("contents") or []:
        if not isinstance(item, dict):
            continue
        entry = {"uri": str(item.get("uri") or uri)}
        mime = str(item.get("mimeType") or "application/octet-stream")
        entry["mimeType"] = mime
        if "text" in item and item.get("text") is not None:
            entry["text"] = str(item.get("text"))
        elif isinstance(item.get("blob"), str):
            try:
                path, size = _save_blob(entry["uri"], mime, str(item["blob"]), context.task_id)
            except ValueError:
                entry["text"] = f"[资源 {entry['uri']}] 二进制内容不是合法的 base64，未保存。"
            else:
                entry["blobSavedTo"] = path
                entry["text"] = (
                    f"[资源 {entry['uri']}] 二进制内容（{mime}，{size} 字节）"
                    f"已保存到 {path}，未在文本中展开。"
                )
        contents.append(entry)
    if not contents:
        return fail("error", f'Server "{server}" returned no content for {uri}.')
    return ToolOutcome(ok=True, output=_dump({"contents": contents}), data={"server": server, "uri": uri, "contents": contents})


# --------------------------------------------------------------------------- #
# ReadMcpResourceDir
# --------------------------------------------------------------------------- #
async def _read_resource_dir(context: ToolContext, arguments: dict) -> ToolOutcome:
    clients = _clients(context)
    server = str(arguments.get("server") or "").strip()
    uri = str(arguments.get("uri") or "").strip()
    if server not in clients:
        return _server_not_found(context, server)
    client = clients[server]
    if not getattr(client, "declares_directory_read", lambda: False)():
        return fail("error", f'Server "{server}" does not support directory listing.')
    try:
        entries, truncated = await client.read_resource_directory(uri)
    except MCPError as error:
        if error.rpc_code == MCP_INVALID_PARAMS:
            return fail(
                "error",
                f"Not a directory resource: {uri}. If it is a file resource, use ReadMcpResource instead.",
            )
        return fail("error", f'MCP directory listing failed on server "{server}": {error.message}')
    rows = []
    for item in entries:
        row = {"uri": str(item.get("uri") or "")}
        for key in ("name", "mimeType", "description"):
            value = item.get(key)
            if value:
                row[key] = str(value)
        rows.append(row)
    return ToolOutcome(
        ok=True,
        output=_dump({"resources": rows}),
        data={"server": server, "uri": uri, "resources": rows, "count": len(rows), "truncated": truncated},
    )


# --------------------------------------------------------------------------- #
# RefreshMcpTools
# --------------------------------------------------------------------------- #
def _status_names(context: ToolContext) -> set[str]:
    bundle = getattr(context, "mcp", None)
    return {
        str(getattr(status, "name", "") or "")
        for status in getattr(bundle, "statuses", None) or []
    }


async def _refresh_tools(context: ToolContext, arguments: dict) -> ToolOutcome:
    clients = _clients(context)
    requested = str(arguments.get("server") or "").strip()
    if requested:
        if requested not in clients:
            if requested in _status_names(context):
                # 连过但失败了：这不是"没这个服务端"，而是"没连上，不重拨"。
                result = {
                    "server": requested,
                    "status": "not_connected",
                    "toolCount": 0,
                    "added": [],
                    "removed": [],
                }
                return ok(_dump([result]), servers=[result])
            return _server_not_found(context, requested)
        targets = [requested]
    else:
        if not clients:
            return ok(_NO_SERVERS, servers=[])
        targets = sorted(clients)

    gateway = getattr(context, "tool_gateway", None)
    if gateway is None:
        return fail("error", "当前入口拿不到工具网关，无法刷新工具表。")

    results: list[dict] = []
    for name in targets:
        client = clients[name]
        try:
            catalog = await client.list_tools()
            config = getattr(client, "config", None)
            if not isinstance(config, ServerConfig):
                config = ServerConfig(name=name)
            # 延迟导入：bridge 依赖 tools.base，模块顶层互相导入会成环。
            from blank_api.services.mcp.bridge import bridge_tools

            tools = bridge_tools(config, client, catalog)
            added, removed = gateway.sync_mcp_tools(name, tools)
        except MCPError as error:
            results.append(
                {
                    "server": name,
                    "status": "error",
                    "toolCount": 0,
                    "added": [],
                    "removed": [],
                    "error": error.message,
                }
            )
            continue
        results.append(
            {
                "server": name,
                "status": "refreshed",
                "toolCount": len(tools),
                "added": added,
                "removed": removed,
            }
        )
    return ok(_dump(results), servers=results)


# --------------------------------------------------------------------------- #
# 工具定义
# --------------------------------------------------------------------------- #
def mcp_resource_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="list_mcp_resources",
            description=(
                "列出 MCP 服务端提供的数据资源（文件、文档、数据库条目等）。"
                "只列已连接、且声明了 resources 能力的服务端；不填 server 就列全部。"
                "要读其中一条，用 read_mcp_resource(server, uri)；目录资源用 read_mcp_resource_dir。"
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "server": {
                        "type": "string",
                        "description": "只列这个服务端的资源；不填＝所有已连接服务端",
                    }
                },
            },
            risk_level="low",
            handler=_list_resources,
            timeout_seconds=30.0,
        ),
        ToolDefinition(
            name="read_mcp_resource",
            description=(
                "按 URI 读取一条 MCP 资源。文本原样返回；二进制内容保存到本地文件并给出路径"
                "（不在正文里展开）。URI 从 list_mcp_resources 的结果里取。"
            ),
            input_schema={
                "type": "object",
                "required": ["server", "uri"],
                "additionalProperties": False,
                "properties": {
                    "server": {"type": "string", "description": "服务端名"},
                    "uri": {"type": "string", "description": "资源 URI"},
                },
            },
            risk_level="low",
            handler=_read_resource,
            timeout_seconds=30.0,
        ),
        ToolDefinition(
            name="read_mcp_resource_dir",
            description=(
                "列举一个目录型 MCP 资源（服务端必须声明 directoryRead 扩展）。"
                "URI 指向普通文件时会被拒绝，并提示改用 read_mcp_resource。"
            ),
            input_schema={
                "type": "object",
                "required": ["server", "uri"],
                "additionalProperties": False,
                "properties": {
                    "server": {"type": "string", "description": "服务端名"},
                    "uri": {"type": "string", "description": "目录资源 URI"},
                },
            },
            risk_level="low",
            handler=_read_resource_dir,
            timeout_seconds=30.0,
        ),
        ToolDefinition(
            name="refresh_mcp_tools",
            description=(
                "重新向已连接的 MCP 服务端要一次工具清单，并把本地工具表对齐"
                "（新增/消失的工具都会列出）。只重查已连接的服务端，绝不重拨；"
                "连不上的服务端会报 not_connected，而不是在这里重新连接。"
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "server": {
                        "type": "string",
                        "description": "只刷新这个服务端；不填＝所有已连接服务端",
                    }
                },
            },
            risk_level="low",
            handler=_refresh_tools,
            timeout_seconds=30.0,
        ),
    ]
