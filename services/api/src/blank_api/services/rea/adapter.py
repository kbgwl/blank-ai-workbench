"""REA 的 MCP Adapter：`/reverse-lab` 唯一的能力入口。

这一层存在的理由只有一句：**把"看实物"这件事变成一次有账可查的工具调用**。
桥接层（``services/mcp/bridge.py``）已经规定了外部工具必须走同一个网关、同一套审批、
同一份审计，REA 是它的第一个真实用户，所以这里不重新发明连接管理，只做三件事：

1. **一条属于 REA 自己的连接**。全局 ``manager()`` 的单例读的是 ``mcp.json``，
   那是用户配置的外部服务端；REA 是随产品分发的内置能力，混进同一份工具表会污染
   设置页的账（``test_mcp_tools.py`` 断言的就是那张表的条数）。所以这里单独起
   ``StdioClient``，但仍然复用 ``bridge_tools``——工具形状与调用路径完全一致。
2. **工具表必须完整才叫 ready**。服务端少报一个工具（版本不匹配、被裁剪）就是
   ``partial``，界面拿到的是一句"缺哪几个"，不是半份表配一个成功状态。
3. **失败必须是失败**。连不上是 ``failed`` 并带服务端自己说的话；解析读不下去是
   ``parse_failed`` 并带第几步、被解析对象是谁。两者都不会被写成"没有发现"。

风险档位：REA 工具的 ``risk`` 显式声明为 ``low``（见 ``_server_config`` 里的理由），
这是本条链路唯一一次对桥接层默认值（high）的主动下调。
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.store import SerializedSessionFactory
from blank_api.services.mcp.bridge import (
    BOUNDARY_NOTE_UNBOUNDED,
    BOUNDARY_UNBOUNDED,
    BOUNDARY_UNKNOWN,
    bridge_tools,
)
from blank_api.services.mcp.client import CONNECT_TIMEOUT, MCPError, ServerConfig, build_client
from blank_api.services.mcp.protocol import qualify
from blank_api.services.permissions import MODE_DEFAULT, PermissionPolicy
from blank_api.services.safety import SafetyPolicy
from blank_api.services.sandbox.tiers import normalize_tier
from blank_api.services.tools.base import ToolDefinition
from blank_api.services.workspace import mask_path

SERVER_NAME = "rea"
NODE = "reverse_lab"
# 这个服务端**必须**提供的工具。少了任何一个都不能算 ready：链路是靠这份名单
# 判断"对面是不是我认识的那个 REA 服务端"，而不是靠版本号猜。
EXPECTED_TOOLS = ("inspect_target", "scan_targets", "find_strings")

STATE_READY = "ready"
STATE_PARTIAL = "partial"
STATE_FAILED = "failed"
STATE_DISABLED = "disabled"

# 子进程启动 + 握手 + 列工具都要等得起：Python 解释器冷启动本身就几百毫秒，
# 加上 import blank_api 可能到秒级。比桥接层的 CONNECT_TIMEOUT 宽一点，
# 但绝不放开到"无限等"。
CONNECT_BUDGET = 25.0
CALL_TIMEOUT = 45.0

# 调查结果的种类：界面与证据包只认这几个值，不接受"空结果"这种含糊状态
STATE_OK = "ok"
STATE_PARSE_FAILED = "parse_failed"
STATE_UNSUPPORTED = "unsupported"
STATE_TARGET_UNAVAILABLE = "target_unavailable"
STATE_ADAPTER_UNAVAILABLE = "adapter_unavailable"


def _env_disabled() -> bool:
    return str(os.environ.get("BLANK_REA_DISABLE") or "").strip().lower() in ("1", "true", "yes")


def _server_config(root: Path) -> ServerConfig:
    """REA 服务端配置：用当前解释器起自己的模块，不依赖 ``mcp.json``。

    ``risk=low`` 的理由要写在这里，否则下一个读代码的人只会看到"有人把默认值改了"：
    这个服务端只做一件事——在 ``--root`` 圈定的目录内**读字节**，不写文件、不起命令、
    不联网（``mcp_server.py`` 里三个工具都是只读的）。把它判成 high，
    `/reverse-lab` 每次点"分析"都要弹一张审批卡，而卡片上没有任何用户能判断的风险。
    """
    return ServerConfig(
        name=SERVER_NAME,
        transport="stdio",
        command=sys.executable,
        args=("-m", "blank_api.services.rea.mcp_server", "--root", str(root)),
        env={"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        timeout=CALL_TIMEOUT,
        risk="low",
    )


@dataclass
class ReaStatus:
    """适配器的自述。``as_dict()`` 就是 `/reverse-lab/status` 的返回体。"""

    state: str
    root: str = ""
    server: str = ""
    # 服务端真正声明的原始工具名 / 桥接后可调用的名字
    declared: tuple[str, ...] = ()
    callable_tools: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    error: str = ""
    connect_ms: int = 0
    # 边界事实来自客户端（stdio 是否真的挂进 Job），不在这里按传输方式猜。
    boundary: str = ""
    boundary_note: str = ""

    @property
    def usable(self) -> bool:
        return self.state == STATE_READY

    def as_dict(self) -> dict[str, Any]:
        boundary = self.boundary or (
            BOUNDARY_UNBOUNDED if self.state in (STATE_READY, STATE_PARTIAL) else BOUNDARY_UNKNOWN
        )
        note = self.boundary_note
        if not note:
            note = (
                BOUNDARY_NOTE_UNBOUNDED
                if boundary == BOUNDARY_UNBOUNDED
                else "REA 服务端进程没有起来（或没连上），本次没有宿主进程执行工具"
            )
        body: dict[str, Any] = {
            "state": self.state,
            "server": self.server or "rea-artifacts",
            "transport": "stdio",
            "root": self.root,
            "expected": list(EXPECTED_TOOLS),
            "declared": list(self.declared),
            "tools": list(self.callable_tools),
            "tools_count": len(self.callable_tools),
            "missing": list(self.missing),
            "boundary": boundary,
            "connect_ms": self.connect_ms,
            "boundary_note": note,
        }
        if self.error:
            body["error"] = self.error
        if self.state == STATE_PARTIAL:
            body["hint"] = (
                "工具表不全（缺 " + "、".join(self.missing) + "），已拒绝按成功处理；"
                "请确认 REA 服务端版本与适配器期望一致"
            )
        return body


@dataclass
class ReaConnection:
    """一个授权根目录对应一条连接。连不上也缓存，坏服务端只让第一次请求慢一点。"""

    root: Path
    _client: Any = None
    _tools: list[ToolDefinition] = field(default_factory=list)
    _status: ReaStatus = field(default_factory=lambda: ReaStatus(STATE_FAILED))
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _loop: asyncio.AbstractEventLoop | None = None
    _attempted: bool = False

    @property
    def status(self) -> ReaStatus:
        return self._status

    @property
    def tools(self) -> list[ToolDefinition]:
        return list(self._tools)

    def _belongs_to_current_loop(self) -> bool:
        try:
            return self._loop is asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 没有循环时谈不上归属
            return False

    async def ensure(self, *, refresh: bool = False) -> ReaStatus:
        async with self._lock:
            if _env_disabled():
                self._drop()
                self._status = ReaStatus(
                    STATE_DISABLED,
                    root=mask_path(self.root),
                    error="REA MCP Adapter 已按 BLANK_REA_DISABLE 关闭",
                )
                self._attempted = True
                return self._status

            # 换事件循环（测试里每个用例一个循环）必须重连：子进程管道绑在旧循环上，
            # 拿过来只会得到一个永远不返回的 await。
            if not self._belongs_to_current_loop():
                self._drop()
            if refresh:
                await self._close_locked()
            # ready 复用；failed/partial/disabled 也复用——坏服务端不该让每个请求
            # 都重连一次，而"再试一次"的正确入口是带 refresh 的调用。
            if self._attempted and not refresh:
                return self._status

            started = asyncio.get_running_loop().time()
            status = ReaStatus(STATE_FAILED, root=mask_path(self.root))
            client = None
            try:
                client = build_client(_server_config(self.root))
                await asyncio.wait_for(client.start(), timeout=CONNECT_BUDGET)
                catalog = await asyncio.wait_for(client.list_tools(), timeout=CONNECT_BUDGET)
                info = client.server_info or {}
                status.server = " ".join(
                    part for part in (str(info.get("name") or ""), str(info.get("version") or "")) if part
                )
                declared = tuple(sorted(str(item.get("name") or "") for item in catalog))
                status.declared = declared
                status.missing = tuple(name for name in EXPECTED_TOOLS if name not in declared)
                status.callable_tools = tuple(
                    sorted(qualify(SERVER_NAME, name) for name in declared)
                )
                status.connect_ms = int((asyncio.get_running_loop().time() - started) * 1000)
                facts = client.boundary_facts()
                status.boundary = str(facts.get("boundary") or "")
                status.boundary_note = str(facts.get("note") or "")
                if status.missing:
                    # 半份工具表不能当成功用：把连接收掉，界面上一个 REA 工具都不出现
                    status.state = STATE_PARTIAL
                    status.error = (
                        f"工具表不全：服务端只声明了 {'、'.join(declared) or '（空）'}，"
                        f"缺少 {'、'.join(status.missing)}"
                    )
                    await _safe_close(client)
                    client = None
                else:
                    status.state = STATE_READY
                    self._tools = bridge_tools(_server_config(self.root), client, catalog)
                    self._client = client
                    self._loop = asyncio.get_running_loop()
            except MCPError as error:
                status.error = error.message
                status.connect_ms = int((asyncio.get_running_loop().time() - started) * 1000)
                if client is not None:
                    await _safe_close(client)
            except asyncio.TimeoutError:
                status.error = f"连接 REA 服务端超时（{CONNECT_BUDGET:.0f}s）"
                if client is not None:
                    await _safe_close(client)
            except Exception as error:  # noqa: BLE001 - 服务端失联的方式无法穷举，但必须留下名字
                status.error = f"{error.__class__.__name__}：{error}"
                if client is not None:
                    await _safe_close(client)
            self._status = status
            self._attempted = True
            return status

    def _drop(self) -> None:
        """丢掉一条不属于当前循环的连接（管道在别的循环上，close() 只会报错或挂住）。"""
        self._client = None
        self._tools = []
        self._loop = None
        self._attempted = False

    async def _close_locked(self) -> None:
        client, self._client = self._client, None
        self._tools = []
        self._loop = None
        self._attempted = False
        self._status = ReaStatus(STATE_FAILED, root=mask_path(self.root))
        if client is not None:
            await _safe_close(client)

    async def close(self) -> None:
        async with self._lock:
            if not self._belongs_to_current_loop():
                self._drop()
                return
            await self._close_locked()


async def _safe_close(client: Any) -> None:
    try:
        await client.close()
    except Exception:  # noqa: BLE001 - 关不掉的子进程没有补救手段，但不能把异常抛给请求
        pass


# --------------------------------------------------------------------------- #
# 进程级连接表（按授权根目录分条）
# --------------------------------------------------------------------------- #
_CONNECTIONS: dict[str, ReaConnection] = {}


def connection_for(root: str | Path) -> ReaConnection:
    key = str(Path(root).resolve())
    existing = _CONNECTIONS.get(key)
    if existing is None:
        existing = ReaConnection(root=Path(key))
        _CONNECTIONS[key] = existing
    return existing


async def ready_connection(root: str | Path) -> tuple[ReaConnection, ReaStatus]:
    """拿连接；上次连失败了就再真连一次。

    失败是缓存的（坏服务端不该让每个请求都重连），但缓存的失败也不该永久挡住
    "运维刚把服务端修好"那一次点击——所以不可用时重试一次，用重试后的状态说话。
    """
    connection = connection_for(root)
    status = await connection.ensure()
    if not status.usable:
        status = await connection.ensure(refresh=True)
    return connection, status


async def close_all() -> None:
    for connection in list(_CONNECTIONS.values()):
        await connection.close()
    _CONNECTIONS.clear()


def reset_connections() -> None:
    """测试用：丢掉引用（调用方负责先 await close_all()）。"""
    _CONNECTIONS.clear()


# --------------------------------------------------------------------------- #
# 一次调查：工具发现 → 网关调用 → 事件与证据
# --------------------------------------------------------------------------- #
async def scan_targets(root: str, *, path: str = "", limit: int = 60) -> dict[str, Any]:
    """列候选目标（只读列举，与 ``/projects/{id}/tree`` 同一档动作）。

    不建 Task、不进事件：它没有"结论"，只是把服务端认得的实物列出来。
    但失败照样要明确报出来——适配器不可用时这里抛错，绝不回一份空清单。
    """
    connection, status = await ready_connection(root)
    if status.state != STATE_READY:
        return {"ok": False, "state": STATE_ADAPTER_UNAVAILABLE, "adapter": status.as_dict()}
    tool = _tool_by_original(connection, "scan_targets")
    if tool is None:  # pragma: no cover - ensure() 已经按名单校验过
        return {
            "ok": False,
            "state": STATE_PARTIAL,
            "adapter": status.as_dict(),
            "error": "服务端报告 ready 却没有 scan_targets，适配器状态自相矛盾",
        }
    outcome = await tool.handler(_bare_context(root), {"path": path, "limit": limit})
    structured = (outcome.data.get("structured") or {}) if isinstance(outcome.data, dict) else {}
    if not outcome.ok:
        # 失败的种类由服务端给（target_unavailable / bad_arguments），界面才知道
        # 是"这个目录不对"还是"上游根本不在"
        return {
            "ok": False,
            "state": str(structured.get("kind") or "call_failed"),
            "error": outcome.message or outcome.output,
            "adapter": status.as_dict(),
        }
    return {"ok": True, "state": STATE_OK, "adapter": status.as_dict(), "result": structured}


async def find_strings(
    root: str, target: str, *, min_length: int = 4, limit: int = 80
) -> dict[str, Any]:
    """字符串线索：与 scan_targets 同为只读取证动作，不产出结论。"""
    connection, status = await ready_connection(root)
    if status.state != STATE_READY:
        return {"ok": False, "state": STATE_ADAPTER_UNAVAILABLE, "adapter": status.as_dict()}
    tool = _tool_by_original(connection, "find_strings")
    if tool is None:  # pragma: no cover
        return {"ok": False, "state": STATE_PARTIAL, "adapter": status.as_dict()}
    outcome = await tool.handler(
        _bare_context(root), {"target": target, "min_length": min_length, "limit": limit}
    )
    structured = (outcome.data.get("structured") or {}) if isinstance(outcome.data, dict) else {}
    if not outcome.ok:
        return {
            "ok": False,
            "state": str(structured.get("kind") or "call_failed"),
            "error": outcome.message or outcome.output,
            "adapter": status.as_dict(),
        }
    return {"ok": True, "state": STATE_OK, "adapter": status.as_dict(), "result": structured}


def _tool_by_original(connection: ReaConnection, original: str) -> ToolDefinition | None:
    wanted = qualify(SERVER_NAME, original)
    return next((tool for tool in connection.tools if tool.name == wanted), None)


def _bare_context(root: str) -> Any:
    """只读取证动作的上下文：没有任务、不写库，只需要 root 与档位标注。

    这里**不经网关**，所以不会伪造 ToolCall 行。真正进台账的是 investigate。
    """
    from blank_api.services.tools.base import ToolContext

    return ToolContext(
        root=Path(root),
        task_id="",
        session_factory=None,  # 只读工具不碰数据库
        sandbox_tier=_tier_label(),
    )


def _tier_label() -> str:
    from blank_api.services.settings import SettingsStore

    return normalize_tier(str((SettingsStore().read().get("sandbox") or {}).get("default_tier") or ""))


async def investigate(
    *,
    session_factory,
    task_id: str,
    root: str,
    target: str,
    project_id: str = "",
) -> dict[str, Any]:
    """把一条实物送进 REA 工具，并把结果落成事件与证据。

    返回值里的 ``state`` 是这条链路唯一的诚实口径：
    ``ok`` / ``parse_failed`` / ``unsupported`` / ``target_unavailable`` /
    ``adapter_unavailable`` / ``awaiting_approval`` / ``denied`` / ``error``。
    """
    from blank_api.services.tools.gateway import ToolGateway, sandbox_backend_event_field

    connection, status = await ready_connection(root)
    recorder = EventRecorder(SerializedSessionFactory(session_factory), task_id, trace_id=task_id)

    if status.state != STATE_READY:
        # 明确报状态：不落一份"跑了但什么都没发现"的时间线
        await recorder.add(
            "rea.adapter.unavailable",
            NODE,
            {"target": target, "state": status.state, "error": status.error, "missing": list(status.missing)},
        )
        return {
            "state": STATE_ADAPTER_UNAVAILABLE,
            "ok": False,
            "adapter": status.as_dict(),
            "reason": status.error or f"REA MCP Adapter 状态为 {status.state}",
        }

    gateway = ToolGateway(connection.tools, defer=False)
    name = qualify(SERVER_NAME, "inspect_target")
    permissions = PermissionPolicy.build(MODE_DEFAULT, sandbox_tier=_tier_label())
    from blank_api.services.tools.base import ToolContext

    context = ToolContext(
        root=Path(root),
        task_id=task_id,
        session_factory=SerializedSessionFactory(session_factory),
        trace_id=task_id,
        approval_policy=permissions.decide,
        permissions=permissions,
        safety=SafetyPolicy.load(),
        team_scope={"project_id": project_id} if project_id else {},
        sandbox_tier=_tier_label(),
    )

    await recorder.add(
        "rea.investigation.started",
        NODE,
        {
            "target": target,
            "root": mask_path(root),
            "server": status.server,
            "tool": name,
            "tools": list(status.callable_tools),
        },
    )
    payload = await gateway.execute(name, {"target": target}, context, node=NODE)

    await recorder.add(
        "tool.completed",
        NODE,
        {
            "tool": name,
            "status": payload.get("status"),
            "ok": payload.get("ok"),
            "risk_level": payload.get("risk_level"),
            "permission": payload.get("permission") or {},
            "duration_ms": payload.get("duration_ms"),
            "tool_call_id": payload.get("tool_call_id"),
            "summary": str(payload.get("output") or payload.get("message") or "")[:500],
            **sandbox_backend_event_field(payload),
        },
    )

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    structured = data.get("structured") if isinstance(data.get("structured"), dict) else {}
    failure = structured.get("failure") if isinstance(structured.get("failure"), dict) else None
    message = str(payload.get("message") or payload.get("output") or "")

    if payload.get("ok"):
        result = structured
        findings = list(result.get("findings") or [])
        state = STATE_OK
        await recorder.add(
            "rea.findings.recorded",
            NODE,
            {
                "target": result.get("target") or target,
                "format": result.get("format"),
                "count": len(findings),
                "evidence_ids": [item.get("evidence_id") for item in findings if isinstance(item, dict)],
                "findings": findings[:20],
                "limitations": list(result.get("limitations") or []),
            },
        )
    elif failure:
        state = STATE_UNSUPPORTED if failure.get("kind") == "unsupported_format" else STATE_PARSE_FAILED
        result = {}
        findings = []
        await recorder.add(
            "rea.parse.failed",
            NODE,
            {
                "target": target,
                "state": state,
                "kind": failure.get("kind"),
                "step": failure.get("step"),
                "steps_total": failure.get("steps_total"),
                "step_name": failure.get("step_name"),
                "parsed_object": failure.get("target"),
                "reason": message,
            },
        )
    elif structured.get("kind") == "target_unavailable":
        state = STATE_TARGET_UNAVAILABLE
        result = {}
        findings = []
        await recorder.add("rea.target.unavailable", NODE, {"target": target, "reason": message})
    else:
        # awaiting_approval / denied / error / timeout：网关给的口径原样传出去
        state = str(payload.get("status") or "error")
        result = {}
        findings = []
        await recorder.add(
            "rea.investigation.blocked",
            NODE,
            {"target": target, "state": state, "reason": message, "permission": payload.get("permission") or {}},
        )

    return {
        "state": state,
        "ok": state == STATE_OK,
        "adapter": status.as_dict(),
        "tool": name,
        "tool_call_id": payload.get("tool_call_id"),
        "artifact_path": payload.get("artifact_path"),
        "duration_ms": payload.get("duration_ms"),
        "boundary_kind": payload.get("boundary_kind"),
        "risk_level": payload.get("risk_level"),
        "permission": payload.get("permission") or {},
        "result": result,
        "findings": findings,
        "failure": failure or {},
        "reason": "" if state == STATE_OK else message,
        "message": message,
    }


__all__ = [
    "EXPECTED_TOOLS",
    "NODE",
    "ReaConnection",
    "ReaStatus",
    "STATE_ADAPTER_UNAVAILABLE",
    "STATE_DISABLED",
    "STATE_FAILED",
    "STATE_OK",
    "STATE_PARSE_FAILED",
    "STATE_PARTIAL",
    "STATE_TARGET_UNAVAILABLE",
    "STATE_UNSUPPORTED",
    "SERVER_NAME",
    "close_all",
    "connection_for",
    "find_strings",
    "investigate",
    "ready_connection",
    "reset_connections",
    "scan_targets",
]
