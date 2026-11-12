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
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blank_api.services.mcp.client import (
    BOUNDARY_JOB,
    BOUNDARY_REMOTE,
    BOUNDARY_UNBOUNDED,
    CONNECT_TIMEOUT,
    DEFAULT_TIMEOUT,
    MCPError,
    ServerConfig,
    build_client,
)
from blank_api.services.mcp.oauth import OAuthConfig
from blank_api.services.mcp.protocol import (
    flatten_result,
    is_mcp_name,
    normalize_schema,
    qualify,
    summarize_description,
)
from blank_api.services.sandbox import windows_job
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail

# 连接超时之外再套一层：单个服务端再怎么慢，也不能把整次任务启动拖住
TOTAL_CONNECT_TIMEOUT = 45.0
STATE_READY = "ready"
STATE_FAILED = "failed"
STATE_SKIPPED = "skipped"

# MCP 服务端的执行边界：stdio 是我们拉起来的**宿主**进程，但能挂进 Job 就必须挂；
# 挂不进去是一等事实，要在状态与证据里红字说清。#94 之前这里只有 host_process，
# 那句话在 stdio 真挂上 Job 之后就成了假话（"不受任何约束"并不成立）。
BOUNDARY_HOST = BOUNDARY_UNBOUNDED  # 旧名保留：调用方读到的仍是"没进 Job 的宿主进程"
BOUNDARY_UNKNOWN = "unknown"
BOUNDARY_MIXED = "mixed"
BOUNDARY_NOTE = "MCP 服务端按宿主权限运行，未纳入任务沙箱档位；stdio 服务端会尽量挂进 Job Object"
BOUNDARY_NOTE_UNBOUNDED = "⚠ 没有 Job 事实：MCP 服务端按宿主权限运行，内存/进程数/退出清理都未由内核强制"
BOUNDARY_NOTE_UNKNOWN = "没有已连接的 MCP 服务端，或连接失败，不知道工具在哪个边界执行"


def mcp_boundary(state: str, transport: str = "", boundary: str = "") -> str:
    """按连接状态给边界事实。

    连上时以客户端报来的事实为准：stdio 挂进 Job ＝ ``job_object``，挂不进 ＝
    ``host_process_unbounded``（红字），http ＝ ``remote``（进程在远端）。
    没有事实时不许默认成 ``job_object``——那会把"不知道"说成"已强制"。
    """
    if state == STATE_READY:
        if boundary:
            return boundary
        return BOUNDARY_REMOTE if transport == "http" else BOUNDARY_UNBOUNDED
    if state == STATE_SKIPPED:
        return "none"
    return BOUNDARY_UNKNOWN


def _ready_note(boundary: str) -> str:
    if boundary == BOUNDARY_JOB:
        return "stdio 服务端进程已挂进 Windows Job Object（内存/进程数/退出清理由内核强制）"
    if boundary == BOUNDARY_REMOTE:
        return "HTTP 型 MCP 的进程在远端，不归本机 Job 管；出站请求仍走审批与审计"
    return BOUNDARY_NOTE_UNBOUNDED


@dataclass
class MCPStatus:
    name: str
    transport: str
    state: str
    tools: int = 0
    error: str = ""
    server_info: str = ""
    # 来自客户端的事实（``boundary_facts()``）。空串＝没有事实，按状态保守推断。
    boundary: str = ""
    boundary_note: str = ""
    enforcement: dict[str, Any] = field(default_factory=dict)
    # 配置展开告警：变量没配就把占位符原样留着，得让用户在状态里看见
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        boundary = self.boundary or mcp_boundary(self.state, self.transport)
        body = {
            "name": self.name,
            "transport": self.transport,
            "state": self.state,
            "tools": self.tools,
            "boundary": boundary,
        }
        if self.warnings:
            body["warnings"] = list(self.warnings)
        if self.error:
            body["error"] = self.error
        if self.server_info:
            body["server"] = self.server_info
        if self.state == STATE_READY:
            body["boundary_note"] = self.boundary_note or _ready_note(boundary)
            if self.enforcement:
                body["enforcement"] = dict(self.enforcement)
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

    def _boundary_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.ready:
            kind = item.boundary or mcp_boundary(item.state, item.transport)
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    def as_dict(self) -> dict:
        counts = self._boundary_counts()
        unbounded = [
            item.name
            for item in self.ready
            if (item.boundary or mcp_boundary(item.state, item.transport)) == BOUNDARY_UNBOUNDED
        ]
        if not counts:
            boundary = "none"
            note = "没有已连接的 MCP 服务端"
        elif unbounded:
            # 最坏的一条决定汇总口径：只要有服务端没进 Job，就不能把整包说成"已纳入边界"。
            boundary = BOUNDARY_UNBOUNDED
            note = (
                f"⚠ {len(unbounded)} 个服务端（{'、'.join(unbounded)}）没能挂进 Job："
                "按宿主权限运行，内存上限、进程数与退出清理都未由内核强制"
            )
        elif set(counts) == {BOUNDARY_JOB}:
            boundary = BOUNDARY_JOB
            note = "stdio 服务端进程已挂进 Windows Job Object（内存/进程数/退出清理由内核强制）"
        elif set(counts) == {BOUNDARY_REMOTE}:
            boundary = BOUNDARY_REMOTE
            note = "HTTP 型 MCP 的进程在远端，不归本机 Job 管；出站请求仍走审批与审计"
        else:
            boundary = BOUNDARY_MIXED
            note = (
                "stdio 服务端已挂 Job、http 服务端在远端："
                f"job_object {counts.get(BOUNDARY_JOB, 0)} 个 / remote {counts.get(BOUNDARY_REMOTE, 0)} 个"
            )
        return {
            "tools": len(self.tools),
            "servers": [item.as_dict() for item in self.statuses],
            "boundary": boundary,
            "boundary_note": note,
            # 结构化账：界面与证据不用再按文案反推每种边界各有几个。
            "by_boundary": counts,
            "unbounded_servers": unbounded,
        }


def config_path() -> Path:
    """``mcp.json`` 的位置：跟着设置文件走，这样桌面壳换数据目录时它一起搬。"""
    override = str(os.environ.get("BLANK_MCP_FILE") or "").strip()
    if override:
        return Path(override)
    from blank_api.services.settings import SettingsStore

    return SettingsStore().path.parent / "mcp.json"


_PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")
_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _expand(
    value: str,
    env: Mapping[str, str] | None = None,
    warnings: list[str] | None = None,
) -> str:
    """``${VAR}`` / ``${VAR:-default}`` 展开，口径对齐 Claude 的 ``.mcp.json``。

    只作用于服务端条目里的字面配置（command/args/env/url/headers 等）；``settings.json``
    本身不展开变量。三条规则：

    * 变量已设（哪怕是空串）→ 取环境值；
    * ``${VAR:-default}`` 在变量未设**或为空**时用 default（bash 的 ``:-`` 语义），
      默认值不递归展开；
    * ``${VAR}`` 变量未设 → **保留原文**并记一条告警。这和 shell 不同：替成空串会把
      「忘了配」变成「配了个空密码」，症状要到服务端 401 才显形。告警只说变量名，
      不带任何值——默认值里也可能藏着密钥。
    """
    text = str(value or "")
    if "${" not in text:
        return text
    source = os.environ if env is None else env
    seen: set[str] = set()

    def one(match: re.Match[str]) -> str:
        spec = match.group(1)
        name, sep, default = spec.partition(":-")
        if not _VAR_NAME.match(name):
            if name not in seen:
                seen.add(name)
                if warnings is not None:
                    # 不回显 spec：默认值可能是密钥
                    warnings.append("占位符的变量名不合法，已原样保留")
            return match.group(0)
        if name in source:
            resolved = source[name]
            if resolved or not sep:
                return resolved
            return default
        if sep:
            return default
        if name not in seen:
            seen.add(name)
            if warnings is not None:
                warnings.append(f"环境变量 {name} 未设置，占位符已原样保留")
        return match.group(0)

    return _PLACEHOLDER.sub(one, text)


def _as_timeout(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return max(1.0, min(300.0, value))


def _as_pids(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, min(65535, value))


def _as_memory(raw: Any) -> str:
    """认不出来的内存写法退回空串（＝用沙箱默认值），不把垃圾值悄悄当成"不限"。"""
    text = str(raw or "").strip()
    return text if text and windows_job.parse_size(text) > 0 else ""


def _as_oauth(
    entry: dict,
    transport: str,
    env: Mapping[str, str] | None = None,
    warnings: list[str] | None = None,
) -> tuple[OAuthConfig | None, str]:
    """``auth``/``oauth`` 段 → OAuthConfig；配置错误返回可读原因（服务端会被跳过）。

    client_credentials 需要机密客户端，所以 client_secret 必填；交互式授权
    （authorization_code/PKCE）没有人工点击的场所，明确拒绝而不是假装支持。
    """
    raw = entry.get("auth") if entry.get("auth") is not None else entry.get("oauth")
    if raw is None:
        return None, ""
    if transport != "http":
        return None, "auth 只用于远程 http 服务端；stdio 服务端不需要 OAuth"
    if not isinstance(raw, dict):
        return None, "auth 必须是对象"
    kind = str(raw.get("type") or "oauth").strip().lower()
    if kind in ("none", "off"):
        return None, ""
    if kind not in ("oauth", "client_credentials"):
        return None, f"auth.type 只支持 oauth/client_credentials（实际 {kind}；交互式授权没有人工点击的场所）"
    client_id = _expand(str(raw.get("client_id") or ""), env, warnings).strip()
    if not client_id:
        return None, "auth 缺少 client_id"
    client_secret = _expand(str(raw.get("client_secret") or ""), env, warnings).strip()
    if not client_secret:
        return None, "auth 的 client_credentials 缺少 client_secret"
    method = str(raw.get("auth_method") or raw.get("token_endpoint_auth_method") or "").strip().lower()
    if method in ("client_secret_basic", "basic"):
        method = "basic"
    elif method in ("client_secret_post", "post"):
        method = "post"
    elif method:
        return None, f"auth_method 只支持 basic 或 post（实际 {method}）"
    return (
        OAuthConfig(
            client_id=client_id,
            client_secret=client_secret,
            token_url=_expand(str(raw.get("token_url") or raw.get("tokenEndpoint") or ""), env, warnings).strip(),
            scope=_expand(str(raw.get("scope") or ""), env, warnings).strip(),
            audience=_expand(str(raw.get("audience") or raw.get("resource") or ""), env, warnings).strip(),
            resource_metadata_url=_expand(str(raw.get("resource_metadata_url") or ""), env, warnings).strip(),
            auth_method=method,
        ),
        "",
    )


def parse_servers(
    payload: dict,
    env: Mapping[str, str] | None = None,
) -> tuple[list[ServerConfig], list[MCPStatus]]:
    """配置文件 → 服务端列表 + 一眼能看懂的跳过原因。

    接受两种写法：标准的 ``{"mcpServers": {...}}``，以及直接一份 ``{name: {...}}``。
    配置写错时必须说清楚是哪一条错在哪，否则用户只能靠猜。``env`` 只给测试注入
    用；生产走 ``os.environ``。
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
        # 这一条展开时攒下的告警：跳过与连上两种结局都要带出去
        warn: list[str] = []

        def skipped(error: str, transport: str = "") -> None:
            statuses.append(
                MCPStatus(
                    server_name,
                    transport,
                    STATE_SKIPPED,
                    error=error,
                    warnings=list(dict.fromkeys(warn)),
                )
            )

        if not isinstance(entry, dict):
            skipped("配置项必须是对象")
            continue
        if entry.get("disabled") is True or entry.get("enabled") is False:
            # 关掉不等于删掉：下次想用时把这一行改回来就行
            skipped("已禁用")
            continue
        declared = str(entry.get("type") or entry.get("transport") or "").strip().lower()
        url = _expand(str(entry.get("url") or entry.get("endpoint") or ""), env, warn)
        command = _expand(str(entry.get("command") or ""), env, warn)
        transport = declared or ("http" if url else "stdio" if command else "")
        if transport not in ("stdio", "http"):
            skipped(f"不支持的传输方式：{transport or '未声明'}（只支持 stdio 与 http）", transport or "?")
            continue
        if transport == "stdio" and not command:
            skipped("缺少 command", transport)
            continue
        if transport == "http" and not url:
            skipped("缺少 url", transport)
            continue

        args = entry.get("args")
        env_raw = entry.get("env")
        headers = entry.get("headers")
        risk = str(entry.get("risk") or entry.get("riskLevel") or "high").strip().lower()
        if risk not in ("low", "medium", "high", "critical"):
            risk = "high"
        oauth, auth_error = _as_oauth(entry, transport, env, warn)
        if auth_error:
            skipped(auth_error, transport)
            continue
        servers.append(
            ServerConfig(
                name=server_name,
                transport=transport,
                command=command,
                args=tuple(_expand(str(item), env, warn) for item in args) if isinstance(args, list) else (),
                env={str(k): _expand(str(v), env, warn) for k, v in env_raw.items()} if isinstance(env_raw, dict) else {},
                cwd=_expand(str(entry.get("cwd") or ""), env, warn),
                url=url,
                headers={str(k): _expand(str(v), env, warn) for k, v in headers.items()} if isinstance(headers, dict) else {},
                timeout=_as_timeout(entry.get("timeout")),
                risk=risk,
                memory=_as_memory(entry.get("memory") or entry.get("memory_limit")),
                pids=_as_pids(entry.get("pids") or entry.get("pids_limit")),
                oauth=oauth,
                warnings=tuple(dict.fromkeys(warn)),
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
            status = MCPStatus(
                server.name, server.transport, STATE_FAILED, warnings=list(server.warnings)
            )
            client = None
            try:
                client = build_client(server)
                await asyncio.wait_for(client.start(), timeout=CONNECT_TIMEOUT + 5)
                catalog = await asyncio.wait_for(client.list_tools(), timeout=CONNECT_TIMEOUT + 5)
                status.state = STATE_READY
                status.tools = len(catalog)
                # 边界事实必须从客户端手里拿：只有它知道 Job 建没建成、进程挂没挂上。
                facts = client.boundary_facts()
                status.boundary = str(facts.get("boundary") or "")
                status.boundary_note = str(facts.get("note") or "")
                status.enforcement = facts
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
