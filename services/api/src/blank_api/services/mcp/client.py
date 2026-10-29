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
import uuid
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
from blank_api.services.sandbox import windows_job
from blank_api.services.sandbox.policy import SandboxPolicy

# 握手不该等满一次工具调用的时长：连不上就该早说，别让任务开个头就卡住
CONNECT_TIMEOUT = 15.0
STDERR_TAIL = 4096
KILL_GRACE = 2.0
DEFAULT_TIMEOUT = 30.0

# 边界事实的词表。stdio 服务端是我们亲手拉起的进程，能挂进 Job 就挂；
# http 服务端的进程在远端，本机 Job 摸不到。三种事实不许互相冒充。
BOUNDARY_JOB = "job_object"
BOUNDARY_UNBOUNDED = "host_process_unbounded"
BOUNDARY_REMOTE = "remote"


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
    # 这两条跟着沙箱策略走：空串 / 0 表示用 SandboxPolicy 的默认值（512m / 128）。
    # MCP 服务端是我们自己拉起的进程，没有理由让它比任务命令多一份不受限的内存与进程数。
    memory: str = ""
    pids: int = 0
    # 只给测试用：HTTP 传输要能塞一个 MockTransport 进来，否则验证协议就得真联网
    client_factory: Any = None


def _job_limits(config: ServerConfig) -> tuple[int, int]:
    """本次 stdio 服务端要设的 Job 限额。配置没写就用沙箱默认值，而不是"随便跑"。"""
    policy = SandboxPolicy()
    memory_bytes = windows_job.parse_size(config.memory or policy.memory)
    pids = int(config.pids or policy.pids or 0)
    return memory_bytes, max(0, pids)


def _resolve_command(command: str) -> tuple[str, tuple[str, ...]]:
    """定位可执行文件；批处理启动器要套一层 ``cmd /c``。

    Windows 上直接 CreateProcess("npx") 是找不到的——它只有 npx.cmd。
    """
    resolved = shutil.which(command) or command
    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        return os.environ.get("COMSPEC", "cmd.exe"), ("/c", resolved)
    return resolved, ()


def _job_facts(config: ServerConfig, job: windows_job.Job, assigned: bool,
               assign_error: str) -> dict[str, Any]:
    """把 stdio 服务端的边界算成结构化事实。挂不上就写挂不上的原因，不许默认成真。"""
    applied = bool(job.ok and assigned)
    memory_bytes = int(job.enforced.get("memory_bytes", 0)) if applied else 0
    pids_limit = int(job.enforced.get("active_process_limit", 0)) if applied else 0
    if applied:
        limits = []
        if memory_bytes:
            limits.append(f"内存 {round(memory_bytes / 1024 / 1024, 1)}MB")
        if pids_limit:
            limits.append(f"活跃进程 {pids_limit} 个")
        limit_text = "、".join(limits) or "没有可设的限额"
        note = (
            f"stdio 服务端进程已挂进 Windows Job Object：{limit_text}由内核强制，"
            "关闭连接时整棵进程树一起被带走"
        )
        boundary = BOUNDARY_JOB
    else:
        reason = job.error or assign_error or "未知原因"
        note = (
            f"⚠ stdio 服务端没能挂进 Job（{reason}）：它按宿主权限运行，"
            "内存上限、活跃进程数与退出清理都未由内核强制"
        )
        boundary = BOUNDARY_UNBOUNDED
    return {
        "transport": "stdio",
        "boundary": boundary,
        "limits_applied": applied,
        "job_created": job.ok,
        "job_assigned": assigned,
        "job_error": job.error or assign_error or "",
        "memory_enforced": applied and memory_bytes > 0,
        "memory_bytes": memory_bytes,
        "pids_enforced": applied and pids_limit > 0,
        "pids_limit": pids_limit,
        "tree_kill_enforced": applied and "kill_on_close" in job.enforced,
        "note": note,
    }


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

    def boundary_facts(self) -> dict[str, Any]:
        """连接落点的结构化事实。没连上时不许猜成任何具体边界。"""
        return {
            "transport": self.transport,
            "boundary": "unknown",
            "limits_applied": False,
            "job_created": False,
            "job_assigned": False,
            "job_error": "",
            "memory_enforced": False,
            "memory_bytes": 0,
            "pids_enforced": False,
            "pids_limit": 0,
            "tree_kill_enforced": False,
            "note": "连接还没建立，没有边界事实可报",
        }

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
        self._job: windows_job.Job | None = None
        # 关 Job 会把 handle 置 0，之后再读就永远是"没建成"。事实必须在关之前算好并留底，
        # 否则一个真被内核限着的服务端会在账上变成"限额未强制"——反向说谎同样是谎。
        self._job_facts: dict[str, Any] = {}

    async def start(self) -> None:
        memory_bytes, pids = _job_limits(self.config)
        job = windows_job.create_job(
            "blank-mcp-" + uuid.uuid4().hex[:8],
            memory_bytes=memory_bytes,
            active_process_limit=pids,
        )
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
            windows_job.close_job(job)
            raise MCPError(
                "mcp_spawn_failed", f"{self.config.name} 启动失败：{error}"
            ) from error
        self._job = job
        # 与命令沙箱同一段窗口：进程起来之后才能挂 Job，中间它已经派生的子进程
        # 不在 active_process_limit 的账里（见 windows_job.assign_pid 的说明）。
        assigned, assign_error = (False, job.error) if not job.ok else windows_job.assign_pid(job, self._process.pid)
        self._job_facts = _job_facts(self.config, job, assigned, assign_error)
        self._stderr = asyncio.create_task(self._drain_stderr())
        self._reader = asyncio.create_task(self._read_stdout())
        try:
            await self.handshake()
        except BaseException:
            # 握手失败也要把进程与 Job 收干净：调用方可能只记住异常，不会再 close 一次。
            # 这里不 await 任何东西——正在被取消时，任何 await 都会立刻再抛一次。
            self._closed = True
            self._close_job()
            if self._process is not None and self._process.returncode is None:
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
            raise

    def boundary_facts(self) -> dict[str, Any]:
        if self._job_facts:
            return dict(self._job_facts)
        return super().boundary_facts()

    async def _send(self, message: dict) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise MCPError("mcp_closed", f"{self.config.name} 的进程不在运行")
        if process.returncode is not None:
            # 进程在握手之前就退了（缺配置、脚本第一行就崩）：这时最有用的是它自己打的那句
            # stderr，不是"进程不在运行"。跟下面写失败那条分支走同一个出口——先 settle 再拼原因。
            # 2026-10-29 Linux runner 上 `test_crashed_server_reports_its_stderr` 就是这么红的：
            # 子进程退得比 Windows 快，`returncode` 在第一次 send 前就非空了，那句 stderr 被跳过。
            raise MCPError("mcp_closed", await self._exit_reason_soon())
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

    def _close_job(self) -> None:
        """关句柄＝这条边界结束。KILL_ON_JOB_CLOSE 会把还活着的整棵进程树一起带走。"""
        job, self._job = self._job, None
        if job is not None:
            windows_job.close_job(job)

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
        try:
            # 先关 Job：KILL_ON_JOB_CLOSE 把整棵树带走。反过来先 await process.wait()
            # 会白等——wait() 要等所有 stdio 管道断开，而孙子进程可能正握着 stdout。
            self._close_job()
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
                        await asyncio.wait_for(process.wait(), timeout=KILL_GRACE)
            except ProcessLookupError:
                # 已经自己退了（Job 关闭也会让它退），这正是我们想要的结果
                pass
            except asyncio.TimeoutError:
                # 没进 Job 的服务端，它的孙子不归我们管，可能一直握着管道。
                # close 是收尾路径，不许因此把任务无限期挂住。
                pass
        finally:
            # 即便进程已经自己退出，句柄也必须关：孙子进程不一定跟着父进程走。
            self._close_job()


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

    def boundary_facts(self) -> dict[str, Any]:
        """HTTP 服务端的进程在远端：本机 Job 管不到，也不许写成"已纳入边界"。"""
        return {
            "transport": "http",
            "boundary": BOUNDARY_REMOTE,
            "limits_applied": False,
            "job_created": False,
            "job_assigned": False,
            "job_error": "",
            "memory_enforced": False,
            "memory_bytes": 0,
            "pids_enforced": False,
            "pids_limit": 0,
            "tree_kill_enforced": False,
            "note": (
                "HTTP 型 MCP 的进程在远端：本机 Job Object 管不到它的内存、进程数与退出清理；"
                "本机只保证每次出站请求走同一套审批与审计"
            ),
        }

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
