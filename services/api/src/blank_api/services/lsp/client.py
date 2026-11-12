"""Speak LSP over a child process' stdin/stdout.

Three things are different from MCP's stdio transport and each one shows up as
"the call hangs" when done wrong:

* **Framing.** LSP is ``Content-Length: N\\r\\n\\r\\n<body>``, not one JSON
  object per line. Reading a line and parsing it yields nothing.
* **Both sides send requests.** Servers ask ``workspace/configuration`` and
  ``client/registerCapability`` during startup. If we only wait for replies, a
  well-behaved server waits for an answer that never comes and the first tool
  call times out instead of failing with a reason.
* **A file must be opened before it can be asked about.** Position-based
  requests against an unopened document are legal but empty on most servers.

A document is re-sent as a full-text ``didChange`` before every request. That
is how we stay correct without a file watcher: after an edit the server is
never queried against stale text.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blank_api.services.sandbox import windows_job
from blank_api.services.sandbox.policy import SandboxPolicy

INITIALIZE_TIMEOUT = 20.0
DEFAULT_TIMEOUT = 30.0
STDERR_TAIL = 4096
KILL_GRACE = 2.0
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
CLIENT_NAME = "blank"
CLIENT_VERSION = "1.0"

# 服务端在 initialize 里可能问这些；答不上来时它要么卡住，要么把能力关掉。
_SERVER_REQUEST_METHODS = frozenset(
    {
        "client/registerCapability",
        "client/unregisterCapability",
        "window/workDoneProgress/create",
        "window/showMessageRequest",
        "workspace/codeLens/refresh",
        "workspace/inlayHint/refresh",
        "workspace/diagnostic/refresh",
    }
)


class LSPError(Exception):
    """带机器可读 code 的失败：上层要把它写进审计，不能只剩一句中文。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LSPServerConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    # 扩展名（含点，小写）→ LSP languageId。一个服务端可以认领多个扩展名。
    extension_to_language: dict[str, str] = field(default_factory=dict)
    timeout: float = DEFAULT_TIMEOUT
    initialization_options: dict | None = None
    memory: str = ""
    pids: int = 0


def config_path() -> Path:
    """``lsp.json`` 的位置：跟 mcp.json 一样跟着设置文件走。"""
    override = str(os.environ.get("BLANK_LSP_FILE") or "").strip()
    if override:
        return Path(override)
    from blank_api.services.settings import SettingsStore

    return SettingsStore().path.parent / "lsp.json"


def _expand(value: str) -> str:
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


def _extensions(entry: dict) -> dict[str, str]:
    """认 ``extensionToLanguage``（Claude 插件写法）与 ``extensions`` 两种表。

    前者给 languageId，后者只给扩展名；没有 languageId 时用扩展名本身——
    比"猜一个语言名"诚实，服务端真需要特定 id 时配置里写清楚就行。
    """
    mapping: dict[str, str] = {}
    raw = entry.get("extensionToLanguage")
    if isinstance(raw, dict):
        for key, value in raw.items():
            ext = str(key or "").strip().lower()
            if not ext:
                continue
            if not ext.startswith("."):
                ext = "." + ext
            mapping[ext] = str(value or ext.lstrip(".")).strip() or ext.lstrip(".")
    raw = entry.get("extensions")
    if isinstance(raw, list):
        for item in raw:
            ext = str(item or "").strip().lower()
            if not ext:
                continue
            if not ext.startswith("."):
                ext = "." + ext
            mapping.setdefault(ext, ext.lstrip("."))
    return mapping


def parse_servers(payload: dict) -> tuple[list[LSPServerConfig], list[str]]:
    """配置文件 → 服务端列表 + 跳过原因（写错一条不能拖垮整份配置）。"""
    if not isinstance(payload, dict):
        return [], ["lsp.json 顶层必须是对象"]
    raw_servers = payload.get("lspServers")
    if not isinstance(raw_servers, dict):
        raw_servers = {
            key: value
            for key, value in payload.items()
            if isinstance(value, dict) and not key.startswith("$")
        }
    servers: list[LSPServerConfig] = []
    errors: list[str] = []
    for name, entry in raw_servers.items():
        server_name = str(name or "").strip()
        if not server_name:
            continue
        if not isinstance(entry, dict):
            errors.append(f"{server_name}: 配置项必须是对象")
            continue
        if entry.get("disabled") is True or entry.get("enabled") is False:
            errors.append(f"{server_name}: 已禁用")
            continue
        command = _expand(str(entry.get("command") or "")).strip()
        if not command:
            errors.append(f"{server_name}: 缺少 command")
            continue
        extensions = _extensions(entry)
        if not extensions:
            errors.append(f"{server_name}: 缺少 extensionToLanguage（不知道它管哪些文件）")
            continue
        args = entry.get("args")
        env = entry.get("env")
        options = entry.get("initializationOptions")
        if options is None:
            options = entry.get("initialization_options")
        memory = str(entry.get("memory") or entry.get("memory_limit") or "").strip()
        if memory and windows_job.parse_size(memory) <= 0:
            memory = ""
        try:
            pids = int(entry.get("pids") or entry.get("pids_limit") or 0)
        except (TypeError, ValueError):
            pids = 0
        servers.append(
            LSPServerConfig(
                name=server_name,
                command=command,
                args=tuple(_expand(str(item)) for item in args) if isinstance(args, list) else (),
                env={str(k): _expand(str(v)) for k, v in env.items()} if isinstance(env, dict) else {},
                cwd=_expand(str(entry.get("cwd") or "")).strip(),
                extension_to_language=extensions,
                timeout=_as_timeout(entry.get("timeout")),
                initialization_options=options if isinstance(options, dict) else None,
                memory=memory,
                pids=max(0, min(65535, pids)),
            )
        )
    return servers, errors


# --------------------------------------------------------------------------- #
# 线上格式
# --------------------------------------------------------------------------- #
def encode_message(message: dict) -> bytes:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body


async def read_message(stream: asyncio.StreamReader) -> dict | None:
    """读一条完整的 LSP 消息；对端关闭时返回 None。"""
    length = -1
    while True:
        line = await stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            if length < 0:
                continue
            break
        if b":" not in line:
            continue
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            try:
                length = int(value.strip())
            except ValueError:
                return None
    if length < 0 or length > MAX_MESSAGE_BYTES:
        raise LSPError("lsp_protocol", f"LSP 消息长度不合法：{length}")
    body = await stream.readexactly(length)
    try:
        message = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return message if isinstance(message, dict) else {}


def path_to_uri(path: str | Path) -> str:
    return Path(path).resolve().as_uri()


_DRIVE = re.compile(r"^/[A-Za-z]:")


def uri_to_path(uri: str) -> str:
    """``file:///E:/a/b.py`` → ``E:/a/b.py``；非 file URI 原样返回（只做斜杠归一）。"""
    text = str(uri or "")
    if not text:
        return ""
    if text.startswith("file://"):
        text = text[len("file://"):]
        if _DRIVE.match(text):
            text = text[1:]
        try:
            text = urllib.parse.unquote(text)
        except ValueError:
            pass
        return text.replace("\\", "/")
    return text.replace("\\", "/")


def format_uri(uri: str, root: str | Path | None = None) -> str:
    """结果里的 URI → 给人看的路径：工作区内给相对路径，否则给绝对路径。"""
    raw = uri_to_path(uri)
    if not raw:
        return "<unknown location>"
    if root is None:
        return raw
    try:
        rel = os.path.relpath(raw, str(root))
    except ValueError:
        return raw
    rel = rel.replace("\\", "/")
    if len(rel) < len(raw) and not rel.startswith("../../"):
        return rel
    return raw


def _job_limits(config: LSPServerConfig) -> tuple[int, int]:
    policy = SandboxPolicy()
    memory_bytes = windows_job.parse_size(config.memory or policy.memory)
    pids = int(config.pids or policy.pids or 0)
    return memory_bytes, max(0, pids)


def _resolve_command(command: str) -> tuple[str, tuple[str, ...]]:
    resolved = shutil.which(command) or command
    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        return os.environ.get("COMSPEC", "cmd.exe"), ("/c", resolved)
    return resolved, ()


class LSPClient:
    """一个语言服务器进程。请求按 id 派发，通知不等回包。"""

    def __init__(self, config: LSPServerConfig, root: str | Path) -> None:
        self.config = config
        self.root = Path(root)
        self.server_capabilities: dict = {}
        self.server_info: dict = {}
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._stderr: asyncio.Task | None = None
        self._stderr_tail = ""
        self._job: windows_job.Job | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._open: set[str] = set()
        self._versions: dict[str, int] = {}
        self._closed = False

    @property
    def alive(self) -> bool:
        return (
            self._process is not None
            and self._process.returncode is None
            and not self._closed
        )

    async def start(self) -> None:
        memory_bytes, pids = _job_limits(self.config)
        job = windows_job.create_job(
            "blank-lsp-" + uuid.uuid4().hex[:8],
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
                cwd=self.config.cwd or str(self.root),
                env=env,
            )
        except (OSError, ValueError) as error:
            windows_job.close_job(job)
            raise LSPError("lsp_spawn_failed", f"{self.config.name} 启动失败：{error}") from error
        self._job = job
        if job.ok:
            windows_job.assign_pid(job, self._process.pid)
        self._stderr = asyncio.create_task(self._drain_stderr())
        self._reader = asyncio.create_task(self._read_stdout())
        try:
            result = await self.request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                    "rootUri": path_to_uri(self.root),
                    "capabilities": {
                        "textDocument": {
                            "synchronization": {"didSave": False, "dynamicRegistration": False},
                            "definition": {},
                            "references": {},
                            "hover": {"contentFormat": ["markdown", "plaintext"]},
                            "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                            "implementation": {},
                            "callHierarchy": {},
                        },
                        "workspace": {"symbol": {}},
                        "window": {"workDoneProgress": False},
                    },
                    "initializationOptions": self.config.initialization_options,
                },
                timeout=INITIALIZE_TIMEOUT,
            )
        except BaseException:
            self._closed = True
            self._close_job()
            if self._process is not None and self._process.returncode is None:
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
            raise
        if isinstance(result, dict):
            caps = result.get("capabilities")
            self.server_capabilities = caps if isinstance(caps, dict) else {}
            info = result.get("serverInfo")
            self.server_info = info if isinstance(info, dict) else {}
        await self.notify("initialized", {})

    # -- 收发 ---------------------------------------------------------------
    async def _send(self, message: dict) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise LSPError("lsp_closed", f"{self.config.name} 的进程不在运行")
        if process.returncode is not None:
            raise LSPError("lsp_closed", await self._exit_reason_soon())
        try:
            process.stdin.write(encode_message(message))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as error:
            raise LSPError("lsp_closed", await self._exit_reason_soon()) from error

    async def request(
        self, method: str, params: dict | None = None, timeout: float | None = None
    ) -> Any:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
            )
            limit = float(timeout or self.config.timeout or DEFAULT_TIMEOUT)
            try:
                return await asyncio.wait_for(future, timeout=limit)
            except asyncio.TimeoutError:
                raise LSPError(
                    "lsp_timeout", f"{self.config.name} 在 {limit:.0f}s 内没有回应 {method}"
                ) from None
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        failure: LSPError | None = None
        try:
            while True:
                message = await read_message(process.stdout)
                if message is None:
                    break
                if not message:
                    continue
                if message.get("method"):
                    await self._answer_server(message)
                    continue
                self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 读循环死掉要转成待决调用的失败
            failure = LSPError("lsp_transport", f"{self.config.name} 读取输出失败：{error}")
        finally:
            if not self._closed:
                await self._settle_stderr()
                if failure is None:
                    failure = LSPError("lsp_closed", self._exit_reason())
            if failure is not None:
                self._fail_all(failure)

    async def _answer_server(self, message: dict) -> None:
        """服务端反向请求必须回包，否则它等的就是我们。"""
        request_id = message.get("id")
        if request_id is None:
            return
        method = str(message.get("method") or "")
        result: Any = None
        if method == "workspace/configuration":
            items = (message.get("params") or {}).get("items") or []
            result = [None for _ in items] if isinstance(items, list) else []
        elif method not in _SERVER_REQUEST_METHODS:
            # 未知方法回 MethodNotFound 比回 null 诚实：服务端不会以为我们支持它。
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Unsupported client method: {method}"},
                }
            )
            return
        await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _dispatch(self, message: dict) -> None:
        request_id = message.get("id")
        if request_id is None:
            return
        try:
            key = int(request_id)
        except (TypeError, ValueError):
            return
        future = self._pending.get(key)
        if future is None or future.done():
            return
        error = message.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            future.set_exception(
                LSPError(
                    f"lsp_error_{code}",
                    str(error.get("message") or "服务端返回错误"),
                )
            )
        else:
            future.set_result(message.get("result"))

    def _fail_all(self, error: LSPError) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    # -- 文档 ---------------------------------------------------------------
    async def open_document(self, path: str | Path, language_id: str, text: str) -> str:
        """打开或同步文档，返回 URI。每次请求前都同步全文，避免读到旧内容。"""
        uri = path_to_uri(path)
        version = self._versions.get(uri, 0) + 1
        if uri in self._open:
            await self.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": text}],
                },
            )
        else:
            await self.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": language_id,
                        "version": 1,
                        "text": text,
                    }
                },
            )
            self._open.add(uri)
            version = 1
        self._versions[uri] = version
        return uri

    # -- 收尾 ---------------------------------------------------------------
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
        code = None if process is None else process.returncode
        tail = self._stderr_tail.strip()
        reason = f"{self.config.name} 进程已退出（code={code}）"
        if tail:
            reason += f"：{tail}"
        return reason

    def _close_job(self) -> None:
        job, self._job = self._job, None
        if job is not None:
            windows_job.close_job(job)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is not None and process.returncode is None:
            try:
                await self.request("shutdown", {}, timeout=2.0)
            except Exception:  # noqa: BLE001 - 服务端不答 shutdown 也要能关
                pass
            try:
                await self.notify("exit", {})
            except Exception:  # noqa: BLE001
                pass
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=KILL_GRACE)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=KILL_GRACE)
                except asyncio.TimeoutError:
                    pass
        self._close_job()
        for task in (self._reader, self._stderr):
            if task is not None and not task.done():
                task.cancel()


class LSPServerManager:
    """按扩展名懒启动语言服务器；启动失败会被记住，不反复重试。"""

    def __init__(self, path: Path | None = None, root: str | Path | None = None) -> None:
        self._path = path
        self.root = Path(root) if root is not None else Path.cwd()
        self.clients: dict[str, LSPClient] = {}
        self.errors: list[str] = []
        self._configs: list[LSPServerConfig] | None = None
        self._failures: dict[str, str] = {}
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path or config_path()

    def _load(self) -> list[LSPServerConfig]:
        if self._configs is not None:
            return self._configs
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._configs = []
            return self._configs
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            self.errors.append(f"lsp.json 解析失败：{error}")
            self._configs = []
            return self._configs
        servers, errors = parse_servers(payload)
        self.errors.extend(errors)
        self._configs = servers
        return self._configs

    def config_for(self, path: str | Path) -> tuple[LSPServerConfig, str] | None:
        suffix = Path(path).suffix.lower()
        if not suffix:
            return None
        for config in self._load():
            language = config.extension_to_language.get(suffix)
            if language:
                return config, language
        return None

    async def client_for(self, path: str | Path) -> tuple[LSPClient, str] | None:
        if str(os.environ.get("BLANK_LSP_DISABLE") or "").strip() in ("1", "true", "yes"):
            return None
        found = self.config_for(path)
        if found is None:
            return None
        config, language = found
        async with self._lock:
            if config.name in self._failures:
                raise LSPError("lsp_unavailable", self._failures[config.name])
            client = self.clients.get(config.name)
            if client is not None and client.alive:
                return client, language
            client = LSPClient(config, self.root)
            try:
                await client.start()
            except LSPError as error:
                self._failures[config.name] = error.message
                raise
            self.clients[config.name] = client
            return client, language

    async def close(self) -> None:
        clients = list(self.clients.values())
        self.clients.clear()
        for client in clients:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - 关不掉的进程不影响其它服务端
                pass


__all__ = [
    "DEFAULT_TIMEOUT",
    "LSPClient",
    "LSPError",
    "LSPServerConfig",
    "LSPServerManager",
    "config_path",
    "encode_message",
    "format_uri",
    "parse_servers",
    "path_to_uri",
    "read_message",
    "uri_to_path",
]
