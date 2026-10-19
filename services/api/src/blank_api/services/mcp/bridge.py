"""Bridge MCP servers into the tool layer.

The agent should not know the difference between ``read_file`` and
``mcp__github__create_issue``: same ``ToolDefinition``, same gateway, same
approval path, same audit record. Three rules make that safe:

* **Namespaced names.** ``mcp__<server>__<tool>``. A server that happens to
  ship a tool called ``run_shell`` must not shadow ours, and a name collision
  between two servers must not become a coin flip.
* **High risk by default.** Every bridged tool is high risk unless the user
  lowers it in the config. We can see the tool's schema but not what the
  server does with it, so "assume harmless" is how a read-only integration
  ends up deleting something.
* **Failure is contained.** A server that hangs, crashes, or speaks the wrong
  protocol costs that server's tools and nothing else. Task startup must still
  succeed — a broken MCP server is not a reason to refuse to work.

The config lives next to ``settings.json`` as ``mcp.json``, in the same
``mcpServers`` shape every other MCP client uses. **The file is the switch**:
no file means no processes are spawned and no code path changes, which is the
same gate ``BLANK_HOOKS`` uses. ``BLANK_MCP_DISABLE=1`` turns it off without
deleting the file.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blank_api.services.mcp.client import (
    CONNECT_TIMEOUT,
    DEFAULT_TIMEOUT,
    MCPError,
    ServerConfig,
    build_client,
)
from blank_api.services.mcp.protocol import (
    flatten_result,
    is_mcp_name,
    normalize_schema,
    qualify,
    summarize_description,
)
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail

# 连接超时之外再套一层：单个服务端再怎么慢，也不能把整次任务启动拖住
TOTAL_CONNECT_TIMEOUT = 45.0
STATE_READY = "ready"
STATE_FAILED = "failed"
STATE_SKIPPED = "skipped"


@dataclass
class MCPStatus:
    name: str
    transport: str
    state: str
    tools: int = 0
    error: str = ""
    server_info: str = ""

    def as_dict(self) -> dict:
        body = {"name": self.name, "transport": self.transport, "state": self.state, "tools": self.tools}
        if self.error:
            body["error"] = self.error
        if self.server_info:
            body["server"] = self.server_info
        return body


@dataclass
class MCPBundle:
    tools: list[ToolDefinition] = field(default_factory=list)
    statuses: list[MCPStatus] = field(default_factory=list)
    # 名字 → 客户端，供关闭与排障；工具本身已经把调用点闭包进去了
    clients: dict[str, Any] = field(default_factory=dict)

    @property
    def ready(self) -> list[MCPStatus]:
        return [item for item in self.statuses if item.state == STATE_READY]

    def as_dict(self) -> dict:
        return {
            "tools": len(self.tools),
            "servers": [item.as_dict() for item in self.statuses],
        }


def config_path() -> Path:
    """``mcp.json`` 的位置：跟着设置文件走，这样桌面壳换数据目录时它一起搬。"""
    override = str(os.environ.get("BLANK_MCP_FILE") or "").strip()
    if override:
        return Path(override)
    from blank_api.services.settings import SettingsStore

    return SettingsStore().path.parent / "mcp.json"


def _expand(value: str) -> str:
    """``${VAR}`` 展开。token 写进配置文件能少一个明文，但别指望它能加密。"""
    text = str(value or "")
    if "${" not in text:
        return text
    for key in {part.split("}")[0] for part in text.split("${")[1:]}:
        text = text.replace("${" + key + "}", os.environ.get(key, ""))
    return text


def _as_timeout(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return max(1.0, min(300.0, value))


def parse_servers(payload: dict) -> tuple[list[ServerConfig], list[MCPStatus]]:
    """配置文件 → 服务端列表 + 一眼能看懂的跳过原因。

    接受两种写法：标准的 ``{"mcpServers": {...}}``，以及直接一份 ``{name: {...}}``。
    配置写错时必须说清楚是哪一条错在哪，否则用户只能靠猜。
    """
    if not isinstance(payload, dict):
        return [], [MCPStatus("config", "", STATE_FAILED, error="mcp.json 顶层必须是对象")]

    raw_servers = payload.get("mcpServers")
    if not isinstance(raw_servers, dict):
        # 没有 mcpServers 时把顶层当服务端表，但排除明显的元数据键
        raw_servers = {
            key: value
            for key, value in payload.items()
            if isinstance(value, dict) and not key.startswith("$")
        }
    if not isinstance(raw_servers, dict):
        return [], [MCPStatus("config", "", STATE_FAILED, error="mcpServers 必须是对象")]

    servers: list[ServerConfig] = []
    statuses: list[MCPStatus] = []
    for name, entry in raw_servers.items():
        server_name = str(name or "").strip()
        if not server_name:
            continue
        if not isinstance(entry, dict):
            statuses.append(MCPStatus(server_name, "", STATE_SKIPPED, error="配置项必须是对象"))
            continue
        if entry.get("disabled") is True or entry.get("enabled") is False:
            # 关掉不等于删掉：下次想用时把这一行改回来就行
            statuses.append(MCPStatus(server_name, "", STATE_SKIPPED, error="已禁用"))
            continue
        declared = str(entry.get("type") or entry.get("transport") or "").strip().lower()
        url = _expand(str(entry.get("url") or entry.get("endpoint") or ""))
        command = _expand(str(entry.get("command") or ""))
        transport = declared or ("http" if url else "stdio" if command else "")
        if transport not in ("stdio", "http"):
            statuses.append(
                MCPStatus(
                    server_name,
                    transport or "?",
                    STATE_SKIPPED,
                    error=f"不支持的传输方式：{transport or '未声明'}（只支持 stdio 与 http）",
                )
            )
            continue
        if transport == "stdio" and not command:
            statuses.append(MCPStatus(server_name, transport, STATE_SKIPPED, error="缺少 command"))
            continue
        if transport == "http" and not url:
            statuses.append(MCPStatus(server_name, transport, STATE_SKIPPED, error="缺少 url"))
            continue

        args = entry.get("args")
        env = entry.get("env")
        headers = entry.get("headers")
        risk = str(entry.get("risk") or entry.get("riskLevel") or "high").strip().lower()
        if risk not in ("low", "medium", "high", "critical"):
            risk = "high"
        servers.append(
            ServerConfig(
                name=server_name,
                transport=transport,
                command=command,
                args=tuple(_expand(str(item)) for item in args) if isinstance(args, list) else (),
                env={str(k): _expand(str(v)) for k, v in env.items()} if isinstance(env, dict) else {},
                cwd=_expand(str(entry.get("cwd") or "")),
                url=url,
                headers={str(k): _expand(str(v)) for k, v in headers.items()} if isinstance(headers, dict) else {},
                timeout=_as_timeout(entry.get("timeout")),
                risk=risk,
            )
        )
    return servers, statuses


def _handler(client: Any, server: ServerConfig, tool_name: str):
    async def run(_context: ToolContext, arguments: dict) -> ToolOutcome:
        try:
            result = await client.call_tool(tool_name, arguments)
        except MCPError as error:
            # 用 message 而不是 code 作为对模型的说明：模型看不懂 code，
            # 而 code 已经进了审计字段
            return fail("error", f"MCP 工具调用失败（{server.name}）：{error.message}")
        is_error, text, data = flatten_result(result)
        if is_error:
            return ToolOutcome(ok=False, status="error", message=text, data=data)
        return ToolOutcome(ok=True, output=text, data=data)

    return run


def bridge_tools(server: ServerConfig, client: Any, catalog: list[dict]) -> list[ToolDefinition]:
    """一个服务端的 tools/list → ToolDefinition 列表。

    名字冲突在这里就地解决：先到先得，后来的补稳定哈希。按名字排序后再分配，
    所以同一份配置每次算出来的名字都一样——不需要靠服务器返回顺序保持稳定。
    """
    tools: list[ToolDefinition] = []
    used: set[str] = set()
    for item in sorted(catalog, key=lambda row: str(row.get("name") or "")):
        original = str(item.get("name") or "").strip()
        if not original:
            continue
        name = qualify(server.name, original)
        if name in used:
            suffix = qualify(server.name, f"{original}-{len(used)}")
            name = suffix
        used.add(name)
        tools.append(
            ToolDefinition(
                name=name,
                description=summarize_description(original, str(item.get("description") or ""), server.name),
                input_schema=normalize_schema(item.get("inputSchema")),
                risk_level=server.risk,
                handler=_handler(client, server, original),
                timeout_seconds=server.timeout,
            )
        )
    return tools


class MCPManager:
    """进程级连接池：连一次，所有任务共用。

    每个任务各连一次是行不通的——stdio 服务端启动要几百毫秒到几秒，
    一个 `npx` 服务器还能顺手拉一次包。连接结果（包括失败）都会被缓存，
    所以坏掉的服务端只让第一个任务慢一点，后面的任务直接拿到失败原因。
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._bundle: MCPBundle | None = None
        self._lock = asyncio.Lock()
        self._stamp: tuple[int, int] | None = None

    @property
    def path(self) -> Path:
        return self._path or config_path()

    def _stamp_of(self, path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _read(self, path: Path) -> tuple[list[ServerConfig], list[MCPStatus]]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return [], []
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            # 配置文件坏了不能拦住任务：说清楚哪里坏了，然后当作没配
            return [], [MCPStatus("config", "", STATE_FAILED, error=f"mcp.json 解析失败：{error}")]
        return parse_servers(payload)

    async def bundle(self) -> MCPBundle:
        if str(os.environ.get("BLANK_MCP_DISABLE") or "").strip() in ("1", "true", "yes"):
            return MCPBundle()
        path = self.path
        stamp = self._stamp_of(path)
        async with self._lock:
            # 文件被改过就重连：用户改完配置不该还需要重启进程
            if self._bundle is not None and stamp == self._stamp:
                return self._bundle
            if self._bundle is not None:
                await self._close_bundle(self._bundle)
            self._bundle = await self._connect(path)
            self._stamp = stamp
            return self._bundle

    async def reload(self) -> MCPBundle:
        self._stamp = None
        self._bundle = None
        return await self.bundle()

    async def _connect(self, path: Path) -> MCPBundle:
        servers, statuses = self._read(path)
        if not servers:
            return MCPBundle(statuses=statuses)

        async def one(server: ServerConfig) -> tuple[ServerConfig, Any, list[dict], MCPStatus]:
            status = MCPStatus(server.name, server.transport, STATE_FAILED)
            client = None
            try:
                client = build_client(server)
                await asyncio.wait_for(client.start(), timeout=CONNECT_TIMEOUT + 5)
                catalog = await asyncio.wait_for(client.list_tools(), timeout=CONNECT_TIMEOUT + 5)
                status.state = STATE_READY
                status.tools = len(catalog)
                info = client.server_info or {}
                status.server_info = str(info.get("name") or "")
                return server, client, catalog, status
            except MCPError as error:
                status.error = error.message
            except asyncio.TimeoutError:
                status.error = f"连接超时（{CONNECT_TIMEOUT:.0f}s）"
            except Exception as error:  # noqa: BLE001 - 服务端失联的方式无法穷举
                status.error = f"{error.__class__.__name__}：{error}"
            if client is not None:
                try:
                    await client.close()
                except Exception:  # noqa: BLE001 - 关不掉的连接不影响其它服务端
                    pass
            return server, None, [], status

        results = await asyncio.wait_for(
            asyncio.gather(*(one(server) for server in servers)), timeout=TOTAL_CONNECT_TIMEOUT
        )

        bundle = MCPBundle(statuses=[*statuses])
        for server, client, catalog, status in results:
            bundle.statuses.append(status)
            if client is None:
                continue
            bundle.clients[server.name] = client
            bundle.tools.extend(bridge_tools(server, client, catalog))
        return bundle

    async def _close_bundle(self, bundle: MCPBundle) -> None:
        for client in bundle.clients.values():
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - 关闭失败没有补救手段
                pass
        bundle.clients.clear()

    async def close(self) -> None:
        async with self._lock:
            if self._bundle is not None:
                await self._close_bundle(self._bundle)
                self._bundle = None
                self._stamp = None


_MANAGER: MCPManager | None = None


def manager() -> MCPManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = MCPManager()
    return _MANAGER


def reset_manager() -> None:
    """测试用：丢掉进程级单例，让下一轮重新读配置。"""
    global _MANAGER
    _MANAGER = None


__all__ = [
    "MCPBundle",
    "MCPManager",
    "MCPStatus",
    "bridge_tools",
    "config_path",
    "is_mcp_name",
    "manager",
    "parse_servers",
    "reset_manager",
]
