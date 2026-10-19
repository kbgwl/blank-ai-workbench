"""MCP clients: one process or one URL per configured server.

Two transports, both JSON-RPC 2.0:

* ``stdio`` — we spawn the server and talk over its stdin/stdout, one JSON
  message per line. This is how nearly every MCP server ships today.
* ``http``  — Streamable HTTP: one POST per request, answer either a plain
  JSON body or an SSE stream.

Four things here are easy to get wrong, and all four show up as "it hangs":

* **stderr must be drained continuously.** A server that logs fills the 64 KiB
  pipe buffer and then blocks forever on its next write. Nothing reports an
  error; the call just never returns. We keep the tail for diagnostics.
* **A dead server must fail its pending calls.** Every request has a timeout,
  but waiting 30s for nothing is worse than the real reason, so the reader
  loop fails everything the moment stdout closes.
* **Windows needs the ``.cmd`` shim.** ``npx`` and ``uvx`` are batch files;
  spawning them directly raises FileNotFoundError, and those two are the most
  common MCP launchers in existence.
* **Late replies must be dropped, not crash.** A response that arrives after
  its caller timed out has no future to resolve; writing to it would raise
  inside the reader loop and take down the whole transport.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

import httpx

from blank_api.services.mcp.protocol import (
    CLIENT_NAME,
    CLIENT_VERSION,
    MAX_LIST_PAGES,
    PROTOCOL_VERSIONS,
    notification,
    parse_message,
    parse_sse,
    request,
)

# 握手不该等满一次工具调用的时长：连不上就该早说，别让任务开个头就卡住
CONNECT_TIMEOUT = 15.0
STDERR_TAIL = 4096
KILL_GRACE = 2.0
DEFAULT_TIMEOUT = 30.0


class MCPError(Exception):
    """带机器可读 code 的失败：上层要把它写进审计，不能只剩一句中文。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ServerConfig:
    name: str
    transport: str = "stdio"
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = DEFAULT_TIMEOUT
    risk: str = "high"
    # 只给测试用：HTTP 传输要能塞一个 MockTransport 进来，否则验证协议就得真联网
    client_factory: Any = None


def _resolve_command(command: str) -> tuple[str, tuple[str, ...]]:
    """定位可执行文件；批处理启动器要套一层 ``cmd /c``。

    Windows 上直接 CreateProcess("npx") 是找不到的——它只有 npx.cmd。
    """
    resolved = shutil.which(command) or command
    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        return os.environ.get("COMSPEC", "cmd.exe"), ("/c", resolved)
    return resolved, ()


class _JSONRPCClient:
    """请求复用、按 id 派发的公共部分。子类只需要负责"怎么把消息发出去"。"""

    transport = ""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.protocol_version = ""
        self.server_info: dict = {}
        self.capabilities: dict = {}
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._closed = False

    # -- 生命周期 -----------------------------------------------------------
    async def start(self) -> None:  # pragma: no cover - 由子类实现
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover - 由子类实现
        raise NotImplementedError

    # -- 请求 ---------------------------------------------------------------
    def _track(self, method: str, params: dict | None) -> tuple[int, dict, asyncio.Future]:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        return request_id, request(request_id, method, params), future

    async def _wait(self, request_id: int, method: str, future: asyncio.Future):
        timeout = float(self.config.timeout or DEFAULT_TIMEOUT)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            raise MCPError(
                "mcp_timeout", f"{self.config.name} 在 {timeout:.0f}s 内没有回应 {method}"
            ) from None
        finally:
            self._pending.pop(request_id, None)

    async def request(self, method: str, params: dict | None = None) -> dict:
        request_id, message, future = self._track(method, params)
        try:
            await self._send(message)
            return await self._wait(request_id, method, future)
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self._send(notification(method, params))

    async def _send(self, message: dict) -> None:  # pragma: no cover - 由子类实现
        raise NotImplementedError

    def _dispatch(self, message: dict) -> None:
        """把一条回包交给等它的调用。没有 id 的是通知，这里不关心。"""
        request_id = message.get("id")
        if request_id is None:
            return
        future = self._pending.get(int(request_id)) if isinstance(request_id, int) else None
        if future is None or future.done():
            # 调用方已经超时走人了。迟到的结果没有意义，写进去反而会炸掉读循环。
            return
        error = message.get("error")
        if isinstance(error, dict):
            future.set_exception(
                MCPError(
                    f"mcp_error_{error.get('code')}",
                    str(error.get("message") or "服务端返回错误"),
                )
            )
        else:
            future.set_result(message.get("result") or {})

    def _fail_all(self, error: MCPError) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    # -- 协议动作 -----------------------------------------------------------
    async def handshake(self) -> None:
        """initialize → notifications/initialized。

        声明 capabilities 时只填 ``{}``：我们不支持 roots 与 sampling，
        声明了却不应答，服务端会等一个永远不来的回复。
        """
        last: MCPError | None = None
        for version in PROTOCOL_VERSIONS:
            try:
                result = await asyncio.wait_for(
                    self.request(
                        "initialize",
                        {
                            "protocolVersion": version,
                            "capabilities": {},
                            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                        },
                    ),
                    timeout=CONNECT_TIMEOUT,
                )
            except MCPError as error:
                last = error
                continue
            except asyncio.TimeoutError:
                last = MCPError(
                    "mcp_timeout", f"{self.config.name} 握手超时（{CONNECT_TIMEOUT:.0f}s）"
                )
                continue
            self.protocol_version = str(result.get("protocolVersion") or version)
            info = result.get("serverInfo")
            self.server_info = info if isinstance(info, dict) else {}
            caps = result.get("capabilities")
            self.capabilities = caps if isinstance(caps, dict) else {}
            # 严格意义上这条通知是必须的：缺了它，部分服务端会拒绝后续请求
            await self.notify("notifications/initialized")
            return
        raise last or MCPError("mcp_handshake_failed", f"{self.config.name} 初始化失败")

    async def list_tools(self) -> list[dict]:
        """拉全量工具，跟着 nextCursor 翻页。

        只读第一页就等于"能力少了一半"，而少的那部分用户完全看不出来——
        所以宁可多翻几页，也不能默认一页就够。
        """
        tools: list[dict] = []
        cursor = ""
        for _ in range(MAX_LIST_PAGES):
            params = {"cursor": cursor} if cursor else {}
            result = await self.request("tools/list", params)
            for item in result.get("tools") or []:
                if isinstance(item, dict) and item.get("name"):
                    tools.append(item)
            cursor = str(result.get("nextCursor") or "")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict) -> dict:
        return await self.request("tools/call", {"name": name, "arguments": arguments or {}})


class StdioClient(_JSONRPCClient):
    transport = "stdio"

    def __init__(self, config: ServerConfig) -> None:
        super().__init__(config)
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._stderr: asyncio.Task | None = None
        self._stderr_tail = ""

    async def start(self) -> None:
        executable, prefix = _resolve_command(self.config.command)
        env = {**os.environ, **self.config.env}
        try:
            self._process = await asyncio.create_subprocess_exec(
                executable,
                *prefix,
                *self.config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.config.cwd or None,
                env=env,
            )
        except (OSError, ValueError) as error:
            raise MCPError(
                "mcp_spawn_failed", f"{self.config.name} 启动失败：{error}"
            ) from error
        self._stderr = asyncio.create_task(self._drain_stderr())
        self._reader = asyncio.create_task(self._read_stdout())
        await self.handshake()

    async def _send(self, message: dict) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise MCPError("mcp_closed", f"{self.config.name} 的进程不在运行")
        line = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            process.stdin.write(line)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as error:
            # 写不进去说明进程已经没了。此时最该给用户看的是它退出前说的话，
            # 而不是 Errno 10054 —— 后者不提供任何下一步。
            raise MCPError("mcp_closed", await self._exit_reason_soon()) from error

    async def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        failure: MCPError | None = None
        try:
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                message = parse_message(raw)
                if message is not None:
                    self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 读循环死掉必须把它转成待决调用的失败
            failure = MCPError("mcp_transport", f"{self.config.name} 读取输出失败：{error}")
        finally:
            if not self._closed:
                # stdout 关掉的这一刻 stderr 往往还没读完，不等一下那句关键的话就丢了
                await self._settle_stderr()
                if failure is None:
                    failure = MCPError("mcp_closed", self._exit_reason())
            if failure is not None:
                self._fail_all(failure)

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        buffer = bytearray()
        try:
            while True:
                chunk = await process.stderr.read(4096)
                if not chunk:
                    break
                buffer.extend(chunk)
                if len(buffer) > STDERR_TAIL:
                    del buffer[: len(buffer) - STDERR_TAIL]
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - stderr 读失败不影响主流程
            pass
        finally:
            self._stderr_tail = buffer.decode("utf-8", "replace")

    async def _settle_stderr(self, seconds: float = 0.5) -> None:
        """等 stderr 把最后一段读完。

        它几乎总是比 stdout 的关闭晚一瞬，不等的话报出来的原因就只剩
        "进程退出了（code=3）"，而真正有用的那句话还躺在管道里。
        ``shield`` 是为了让超时的那次等待不要把读任务一起取消掉。
        """
        task = self._stderr
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=seconds)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    async def _exit_reason_soon(self, seconds: float = 0.5) -> str:
        process = self._process
        if process is not None:
            try:
                await asyncio.wait_for(process.wait(), timeout=seconds)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        await self._settle_stderr(seconds)
        return self._exit_reason()

    def _exit_reason(self) -> str:
        process = self._process
        code = process.returncode if process is not None else None
        reason = f"{self.config.name} 的进程已退出（code={code}）"
        detail = self._stderr_tail.strip()
        if detail:
            reason = f"{reason}：{detail[-400:]}"
        return reason

    async def close(self) -> None:
        self._closed = True
        for task in (self._reader, self._stderr):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._reader, self._stderr) if task is not None),
            return_exceptions=True,
        )
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=KILL_GRACE)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        except ProcessLookupError:
            # 已经自己退了，这正是我们想要的结果
            pass


class HttpClient(_JSONRPCClient):
    """Streamable HTTP：一问一答的 POST，回包可能是 JSON 也可能是 SSE。"""

    transport = "http"

    def __init__(self, config: ServerConfig) -> None:
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None
        self._session_id = ""

    async def start(self) -> None:
        factory = self.config.client_factory or httpx.AsyncClient
        self._client = factory(timeout=float(self.config.timeout or DEFAULT_TIMEOUT))
        await self.handshake()

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.config.headers,
        }
        if self._session_id:
            # 服务端给了会话 id 就必须回带，否则每个请求都被当成一次新会话
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def _post(self, message: dict) -> list[dict]:
        client = self._client
        if client is None or self._closed:
            raise MCPError("mcp_closed", f"{self.config.name} 的连接已关闭")
        try:
            response = await client.post(self.config.url, json=message, headers=self._headers())
        except httpx.HTTPError as error:
            raise MCPError(
                "mcp_transport", f"{self.config.name} 请求失败：{error.__class__.__name__}：{error}"
            ) from error
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        if response.status_code >= 400:
            raise MCPError(
                "mcp_http_error",
                f"{self.config.name} 返回 {response.status_code}：{response.text[:200]}",
            )
        if response.status_code in (202, 204) or not response.content:
            return []
        if "text/event-stream" in (response.headers.get("content-type") or ""):
            return parse_sse(response.text)
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return []
        return payload if isinstance(payload, list) else [payload]

    async def request(self, method: str, params: dict | None = None) -> dict:
        request_id, message, future = self._track(method, params)
        try:
            for reply in await self._post(message):
                self._dispatch(reply)
            return await self._wait(request_id, method, future)
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self._post(notification(method, params))

    async def close(self) -> None:
        self._closed = True
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - 关闭失败不影响退出
                pass


def build_client(config: ServerConfig) -> _JSONRPCClient:
    if config.transport == "http":
        return HttpClient(config)
    if config.transport == "stdio":
        return StdioClient(config)
    # 明确报错胜过静默挂起：sse 是已废弃的旧传输，说清楚比让它连不上更好查
    raise MCPError(
        "mcp_unsupported_transport",
        f"{config.name} 的传输方式 {config.transport} 不支持（只支持 stdio 与 http）",
    )
