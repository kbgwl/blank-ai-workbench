"""fs-broker：把文件类工具放进一个子进程边界里执行。

设计稿（`docs/SANDBOX-WINDOWS-DESIGN.md` §4 / §9.2）里这条一直挂着：文件工具今天全部
跑在 API 主进程里（`boundary_kind=in_process`），路径边界只靠 `resolve_for_write` 的代码检查。
本模块把"读/写/补丁/列目录/搜索"这五个动作挪进一个**专职子进程**：

* 子进程经 stdio 长度前缀 JSON 协议调用，语义不在这里重造——broker 内部直接复用
  `workspace.list_tree / read_text_file`、`filesystem._search_blocking`、`patch._patch_on_disk`
  这些**现函数**，主进程与 broker 走的是同一份实现，只换执行体。
* 子进程挂进 Job Object（`windows_job.create_job` + `assign_pid` + `KILL_ON_JOB_CLOSE`），
  与 `LocalSandbox` 同一套机制：取消/闲置回收/主进程退出都会把它整棵带走。
* **降级必须说出来**：起不来、超时、被杀、协议不匹配，任何一层失败都让本次调用直接失败，
  并在结果 data 里写 `fs_broker_error.layer`（spawn/timeout/killed/protocol/cancelled/...）
  与可读原因；**绝不回退到宿主进程执行**——这是红线，与 `workspace-net` 拒绝而不是降级成
  `off` 是同一原则（设计稿 §9.5）。
  失败还带第三格 `side_effects`（`none`/`unknown`）：请求根本没进 broker 的失败（起不来、
  协议不匹配、调用到达时 broker 已死）说 `none`——本次动作确定没发生；请求已经进管道的失败
  （超时、取消、写到一半断管、broker 在响应前就死）说 `unknown`——磁盘动作可能已经落盘，
  把它念成"没执行"就是说假话（口径见下面 `SIDE_EFFECTS_*` 的注释，那条是被实测逼出来的）。
* 每次成功调用带 `fs_broker_latency`（samples/p50_ms/p95_ms/last_ms，单位毫秒）：
  IPC 往返延迟不是"感觉不卡"，是进事件、进证据包的数字。

开关：`features.fs_broker`（beta，默认关）。关着时文件工具走原宿主路径，行为完全不变。

测试故障注入（仅供判据能红，不是运行选项）：
* `BLANK_FS_BROKER_TEST_DELAY_MS`：broker 每个**业务**响应前固定睡眠（握手不睡，
  免得"起不来"与"业务慢"两件事搅在一起），用来确定性触发超时/取消层；
* `BLANK_FS_BROKER_TEST_PROTOCOL`：篡改 hello 应答的协议号，用来确定性触发协议不匹配层；
* `BLANK_FS_BROKER_PYTHON`：替换 broker 解释器路径，用来确定性触发 spawn 层。
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import os
import sys
import time
import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from blank_api.api.errors import ApiError
from blank_api.services import features
from blank_api.services.sandbox import windows_job
from blank_api.services.sandbox.policy import SandboxPolicy

PROTOCOL_VERSION = 1
FEATURE_NAME = "fs_broker"
# 帧格式：10 位十进制字节数 + UTF-8 JSON 载荷。定长头让两端都能 readexactly。
LENGTH_HEADER_BYTES = 10

DEFAULT_CALL_TIMEOUT_SECONDS = 20.0
DEFAULT_HANDSHAKE_TIMEOUT_SECONDS = 15.0
DEFAULT_IDLE_REAP_SECONDS = 300.0
LATENCY_WINDOW = 512

# 失败层：进事件/证据包，界面与测试读它，不读文案。
LAYER_SPAWN = "spawn"
LAYER_HANDSHAKE = "handshake"
LAYER_PROTOCOL = "protocol"
LAYER_TIMEOUT = "timeout"
LAYER_KILLED = "killed"
LAYER_BROKEN_PIPE = "broken_pipe"
LAYER_CANCELLED = "cancelled"

# 副作用口径（2026-10-26 被实测逼出来的，不是设计时想的）：
# 请求已经写进 broker 之后才坏的那些调用（超时/取消），**响应**丢了，动作却可能已经落盘——
# 注入 800ms 延迟、0.2s 超时那一次点验里，`main.py` 真的被 broker 改成了新内容（见
# `E:/tmp/m105/v93-1.log` 第 26–30 行的失败读数），而调用方只看得见"超时"。
# 把这种失败念成「没执行」（证据包里的 blocked）就是说假话。
# 因此失败要分两种说：`none`（请求根本没进去，动作确定没发生）与 `unknown`（结果不可知）。
SIDE_EFFECTS_NONE = "none"
SIDE_EFFECTS_UNKNOWN = "unknown"


class FsBrokerError(RuntimeError):
    """broker 侧的失败。layer 回答"是哪一层坏了"，reason 是人读得懂的原文，
    side_effects 回答"这次动作到底发生了没有"。"""

    def __init__(self, layer: str, reason: str, *, side_effects: str = SIDE_EFFECTS_NONE) -> None:
        super().__init__(f"[{layer}] {reason}")
        self.layer = layer
        self.reason = reason
        self.side_effects = side_effects


def broker_enabled() -> bool:
    return features.is_enabled(FEATURE_NAME)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# --------------------------------------------------------------------------- 协议帧


def _encode_frame(payload: dict) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return f"{len(body):0{LENGTH_HEADER_BYTES}d}".encode("ascii") + body


def _read_exact_buffer(stream, count: int) -> bytes | None:
    """从同步缓冲流读满 count 字节；EOF 返回 None（broker 侧用）。"""
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """最近秩法：n=1 时 p50 与 p95 都是那一个值，不对外推。"""
    if not sorted_values:
        raise ValueError("空样本集没有分位数")
    index = max(0, min(len(sorted_values) - 1, int(round(q * (len(sorted_values) - 1)))))
    return float(sorted_values[index])


# --------------------------------------------------------------------------- broker 服务端
# 这些操作就是现有文件工具的语义本体：主进程与 broker 分派调用同一批函数，
# 不存在"第二套文件语义"。异常按类型回编码错误，客户端原样重建（ApiError 不降级为字符串）。


def _op_dispatch(root: Path, request: dict) -> dict:
    op = request.get("op")
    args = request.get("args") or {}
    if op == "hello":
        advertised = os.environ.get("BLANK_FS_BROKER_TEST_PROTOCOL", "").strip()
        protocol = int(advertised) if advertised.isdigit() else PROTOCOL_VERSION
        return {"protocol": protocol, "pid": os.getpid(), "root": str(root)}
    if op == "list_tree":
        from blank_api.services.workspace import list_tree

        entries = list_tree(
            root,
            str(args.get("path", "")),
            max_depth=int(args.get("depth", 3)),
            limit=int(args.get("limit", 500)),
        )
        return {"entries": [dict(vars(entry)) for entry in entries]}
    if op == "read_text_file":
        from blank_api.services.workspace import read_text_file

        return {"payload": read_text_file(root, str(args["path"]))}
    if op == "search":
        from blank_api.services.tools.filesystem import _search_blocking

        return {"matches": _search_blocking(
            root,
            args.get("pattern"),
            _compile_regex(args) if args.get("mode") == "regex" else None,
            str(args.get("path", "")),
            int(args.get("max_matches", 50)),
        )}
    if op == "patch":
        from blank_api.services.tools.patch import _patch_on_disk

        return {"result": _patch_on_disk(
            root, str(args.get("task_id", "")), {"path": args.get("path"), "patch": args.get("patch")}
        )}
    raise ValueError(f"fs-broker 不认识的操作：{op!r}")


def _compile_regex(args: dict):
    import re

    from blank_api.services.tools.filesystem import _INVALID_REGEX_TEMPLATE

    try:
        return re.compile(str(args["pattern"]))
    except re.error as exc:
        raise ValueError(_INVALID_REGEX_TEMPLATE.format(exc=exc)) from exc


def serve(root: str) -> int:
    """broker 主循环：stdin 收帧 → 分派 → stdout 回帧。崩了就退出，由主进程读数留痕。"""
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    workspace_root = Path(root).resolve()
    delay_ms = _env_float("BLANK_FS_BROKER_TEST_DELAY_MS", 0.0)
    while True:
        header = _read_exact_buffer(stdin, LENGTH_HEADER_BYTES)
        if header is None:
            return 0
        try:
            size = int(header.decode("ascii"))
        except ValueError:
            return 1
        body = _read_exact_buffer(stdin, size)
        if body is None:
            return 0
        request = json.loads(body.decode("utf-8"))
        request_id = request.get("id")
        try:
            response = {"id": request_id, "ok": True, "result": _op_dispatch(workspace_root, request)}
        except ApiError as exc:
            response = {
                "id": request_id,
                "ok": False,
                "error": {
                    "type": "api_error", "code": exc.code, "message": exc.message,
                    "status_code": exc.status_code,
                },
            }
        except ValueError as exc:
            response = {"id": request_id, "ok": False, "error": {"type": "value", "message": str(exc)}}
        except Exception as exc:  # noqa: BLE001 - 任何异常都要回帧，别让主进程干等到超时
            response = {
                "id": request_id,
                "ok": False,
                "error": {"type": "internal", "message": f"{exc.__class__.__name__}: {exc}"},
            }
        if delay_ms > 0 and request.get("op") != "hello":
            # 注入的睡眠只加在业务请求上：握手是被计时的一次成型交换，
            # 让它也睡觉会把"起不来/慢启动"和"业务调用慢"两件事搅在一起。
            time.sleep(delay_ms / 1000.0)
        stdout.write(_encode_frame(response))
        stdout.flush()


# --------------------------------------------------------------------------- 客户端
# 一个 (工作区根, 任务) 一枚 broker：取消与任务终止的边界就落在"这一任务的 broker"上，
# 不跨任务共享。stdio 管道是串行的，一次调用在途时取消会让响应无处安放，
# 所以取消/超时/管道断都按"broker 中毒"处理：关 Job（内核带走整棵树）→ 下次调用重生。


@dataclass
class FsBrokerClient:
    root: Path
    task_id: str
    _call_timeout: float = field(default_factory=lambda: _env_float(
        "BLANK_FS_BROKER_TIMEOUT", DEFAULT_CALL_TIMEOUT_SECONDS))
    _handshake_timeout: float = field(default_factory=lambda: _env_float(
        "BLANK_FS_BROKER_HANDSHAKE_TIMEOUT", DEFAULT_HANDSHAKE_TIMEOUT_SECONDS))

    proc: asyncio.subprocess.Process | None = None
    job: windows_job.Job | None = None
    job_facts: dict = field(default_factory=dict)
    pid: int | None = None
    latency: deque = field(default_factory=lambda: deque(maxlen=LATENCY_WINDOW))
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _pending: dict = field(default_factory=dict)  # id -> Future
    _reader_task: asyncio.Task | None = None
    _closed: bool = False

    def status(self) -> dict:
        """成功/失败都要带上的边界事实：哪层执行、Job 挂没挂上。字段风格对齐 enforcement 账。"""
        return {
            "executed_by": "fs_broker_subprocess",
            "broker_pid": self.pid or 0,
            "protocol": PROTOCOL_VERSION,
            **self.job_facts,
        }

    def latency_summary(self, last_ms: float) -> dict:
        ordered = sorted(self.latency)
        return {
            "samples": len(ordered),
            "p50_ms": round(_percentile(ordered, 0.50), 3),
            "p95_ms": round(_percentile(ordered, 0.95), 3),
            "last_ms": round(last_ms, 3),
        }

    async def start(self) -> None:
        python = os.environ.get("BLANK_FS_BROKER_PYTHON", "").strip() or sys.executable
        package_root = str(Path(blank_api_dir()).parent)
        env = {**os.environ, "PYTHONPATH": package_root, "PYTHONIOENCODING": "utf-8"}
        memory_bytes = windows_job.parse_size(SandboxPolicy().memory)
        self.job = windows_job.create_job(
            "blank-fs-broker-" + uuid.uuid4().hex[:8],
            memory_bytes=memory_bytes,
            active_process_limit=SandboxPolicy().pids or 0,
        )
        try:
            self.proc = await asyncio.create_subprocess_exec(
                python, "-X", "utf8", "-m", "blank_api.services.tools.fs_broker",
                "--root", os.fspath(self.root),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=os.fspath(self.root),
                env=env,
                creationflags=0x00000200 if sys.platform == "win32" else 0,
            )
        except (OSError, ValueError) as exc:
            windows_job.close_job(self.job)
            self.job = None
            raise FsBrokerError(
                LAYER_SPAWN, f"broker 子进程起不来（解释器={python!r}）：{exc.__class__.__name__}: {exc}"
            ) from exc
        assigned, assign_error = windows_job.assign_pid(self.job, self.proc.pid)
        # 与 LocalSandbox 同一处已记录的竞态口径：起进程与挂 Job 之间派生的进程不算进限额。
        self.pid = self.proc.pid
        self.job_facts = {
            "job_created": self.job.ok,
            "job_assigned": bool(assigned),
            "job_error": self.job.error or assign_error or "",
            "job_memory_bytes": int(self.job.enforced.get("memory_bytes", 0)),
            "job_pids_limit": int(self.job.enforced.get("active_process_limit", 0)),
            "job_tree_kill": "kill_on_close" in self.job.enforced,
        }
        self._reader_task = asyncio.create_task(self._read_loop())
        try:
            # 握手这一趟没有文件动作可谈：超时/断管都确定没落盘（side_effects=none）。
            result = await self._roundtrip("hello", {}, timeout=self._handshake_timeout,
                                           side_effects_after_request=SIDE_EFFECTS_NONE)
        except FsBrokerError:
            await self.hard_stop()
            raise
        if result.get("protocol") != PROTOCOL_VERSION:
            await self.hard_stop()
            raise FsBrokerError(
                LAYER_PROTOCOL,
                f"协议不匹配：主进程要求 v{PROTOCOL_VERSION}，broker 应答 v{result.get('protocol')!r}，"
                "调用已失败，未回退宿主执行",
            )

    async def call(self, op: str, args: dict) -> dict:
        if self.proc is None:
            # 连进程都没有：这一层就是起不来，谈不上执行。
            raise FsBrokerError(LAYER_SPAWN, "broker 子进程不存在（起不来或已随任务收尸），本次调用未执行")
        if self._closed or self.proc.returncode is not None:
            # 调用到达时 broker 已经死了：请求根本没进管道，动作确定没发生（side_effects=none）。
            # 层名要说"被杀/已退出"而不是含糊的"已关闭"——读的人要知道是哪一层坏的。
            raise await self._dead_error("broker 在本次调用开始前已经退出",
                                        side_effects=SIDE_EFFECTS_NONE)
        started = time.perf_counter()
        try:
            async with self._lock:
                result = await self._roundtrip(op, args, timeout=self._call_timeout)
        except FsBrokerError as exc:
            exc.op = op  # type: ignore[attr-defined]
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.latency.append(elapsed_ms)
        return {"result": result, "latency": self.latency_summary(elapsed_ms), "status": self.status()}

    async def _roundtrip(
        self,
        op: str,
        args: dict,
        timeout: float,
        side_effects_after_request: str = SIDE_EFFECTS_UNKNOWN,
    ) -> dict:
        """一趟请求-响应。`side_effects_after_request` 回答"请求进管道之后才坏的这次，动作算不算发生过"：
        业务请求是 `unknown`（可能已落盘），握手是 `none`（它不碰磁盘）。"""
        request_id = uuid.uuid4().hex
        request = {"id": request_id, "op": op, "args": args}
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[request_id] = future
        assert self.proc is not None and self.proc.stdin is not None
        try:
            self.proc.stdin.write(_encode_frame(request))
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError, AssertionError) as exc:
            self._pending.pop(request_id, None)
            # 请求没进去：动作确定没发生（none），但 broker 依然按中毒收尸——它的管道已经不在了。
            raise await self._dead_error(
                f"请求写不进 broker（{exc.__class__.__name__}）", side_effects=SIDE_EFFECTS_NONE
            ) from exc
        try:
            # 同时等"响应到达"和"进程死亡"：只等前者的话，broker 被 taskkill 之后
            # 请求可能静静躺在管道缓冲里，20 秒超时才把"被杀"读成"超时"——
            # 层的名字错了，指认就废了（bench 实测过这条，见 #105 交付记录）。
            death = asyncio.ensure_future(self.proc.wait())
            response_future = asyncio.ensure_future(future)
            try:
                done, _ = await asyncio.wait(
                    {response_future, death}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                response_future.cancel()
                death.cancel()
            if response_future in done:
                response = response_future.result()
            elif death in done:
                raise await self._dead_error(
                    "broker 进程在本次调用期间退出", returncode=death.result(),
                    side_effects=side_effects_after_request,
                )
            else:
                # 超时：响应可能稍后才到，流已经不可信——中毒处理，不许"再试一次就当没事"。
                raise FsBrokerError(
                    LAYER_TIMEOUT,
                    f"broker 调用超时（op={op}，{timeout:g}s），子进程已被终止并随 Job 带走，未回退宿主执行",
                    side_effects=side_effects_after_request,
                )
        except TimeoutError as exc:  # pragma: no cover - 上面已消化超时
            raise FsBrokerError(LAYER_TIMEOUT, f"broker 调用超时（op={op}）",
                                side_effects=side_effects_after_request) from exc
        except (asyncio.CancelledError, FsBrokerError) as exc:
            self._pending.pop(request_id, None)
            if isinstance(exc, asyncio.CancelledError):
                raise FsBrokerError(
                    LAYER_CANCELLED, f"调用被取消（op={op}），broker 随本任务一并终止",
                    side_effects=side_effects_after_request,
                ) from exc
            raise
        if not response.get("ok"):
            error = response.get("error") or {}
            kind = error.get("type")
            message = str(error.get("message", ""))
            if kind == "api_error":
                raise ApiError(code=str(error.get("code", "fs_broker_error")),
                               message=message, status_code=int(error.get("status_code", 400)))
            if kind == "value":
                raise ValueError(message)
            # internal：流是完好的（响应正常到达），不该把 broker 收尸——它报的是业务异常，
            # 与宿主路径"handler 抛非受管异常 → 网关记 工具执行失败"同形。
            raise RuntimeError(f"fs-broker 内部错误：{error.get('type')}: {message}")
        return response.get("result") or {}

    async def _dead_error(
        self, prefix: str, returncode: int | None = ..., side_effects: str = SIDE_EFFECTS_UNKNOWN
    ) -> FsBrokerError:
        """管道断了先问内核它怎么死的，把 returncode 写进原因，不留"不知道为什么"的失败。"""
        if returncode is Ellipsis:
            returncode = None
            if self.proc is not None:
                try:
                    returncode = await asyncio.wait_for(self.proc.wait(), timeout=2.0)
                except (TimeoutError, OSError):
                    returncode = self.proc.returncode
        return FsBrokerError(
            LAYER_KILLED if returncode is not None else LAYER_BROKEN_PIPE,
            f"{prefix}；broker 退出码={returncode!r}，该调用已失败，未回退宿主执行",
            side_effects=side_effects,
        )

    async def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        stdout = self.proc.stdout
        try:
            while True:
                header = await stdout.readexactly(LENGTH_HEADER_BYTES)
                body = await stdout.readexactly(int(header.decode("ascii")))
                response = json.loads(body.decode("utf-8"))
                future = self._pending.pop(str(response.get("id")), None)
                if future is not None and not future.done():
                    future.set_result(response)
        except (asyncio.IncompleteReadError, ConnectionResetError, ValueError, OSError):
            return
        finally:
            # 流到头 = broker 生命周期到头。把"为什么没等到回话"原样交给在途调用。
            self._closed = True

    async def hard_stop(self) -> None:
        """关 Job 句柄 → 内核带走 broker（M0 P6.7 实测生效）；残留进程再补一刀 kill。

        顺序是**先关 Job 再等进程**：Job 的 KILL_ON_JOB_CLOSE 一生效，broker 就被内核点名，
        `wait()` 立刻回；反过来先等再关，正在睡 800ms 的 broker 会先把这次收尸拖满 2 秒。
        """
        if self._closed and self.proc is None:
            return
        self._closed = True
        if self.job is not None:
            windows_job.close_job(self.job)
            self.job = None
            self.job_facts = dict(self.job_facts)
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 收尸路径上的任何异常都不该外溢
                pass
            self._reader_task = None
        proc = self.proc
        if proc is not None:
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except (TimeoutError, OSError):
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
            # 显式关掉 transport（三条管道一起）：留给 GC 的话，解释器退出时会刷出一堆
            # "I/O operation on closed pipe" 的 ResourceWarning，把真正的读数埋进噪声里。
            try:
                proc.close()
            except Exception:  # noqa: BLE001 - 已经中毒的进程关不上也不改变判定
                pass
            self.proc = None


def blank_api_dir() -> str:
    """blank_api 包自己的目录：`-m` 启动子进程时 PYTHONPATH 要给它的父目录。"""
    import blank_api

    return str(Path(blank_api.__file__).parent.resolve())


class FsBrokerManager:
    """按 (工作区根, 任务) 管 broker 的出生与收尸。失败过的 broker 绝不复用。"""

    def __init__(self) -> None:
        self._clients: dict[tuple[str, str], FsBrokerClient] = {}
        self._lock = asyncio.Lock()
        self._last_used: dict[tuple[str, str], float] = {}
        self.idle_seconds = _env_float("BLANK_FS_BROKER_IDLE_SECONDS", DEFAULT_IDLE_REAP_SECONDS)

    async def call(self, root: Path, task_id: str, op: str, args: dict) -> dict:
        key = (str(Path(root).resolve()), str(task_id))
        async with self._lock:
            self._reap_idle()
            client = self._clients.get(key)
            if client is None:
                client = FsBrokerClient(root=Path(root).resolve(), task_id=task_id)
                await client.start()
                self._clients[key] = client
            self._last_used[key] = time.monotonic()
            try:
                return await client.call(op, args)
            except FsBrokerError:
                # 中毒的 broker 不收进"还能用"：立刻收尸，下一次调用重生（重生也是 broker，不是宿主）。
                # ApiError（越界路径被拒）走不到这里——broker 是健康的，拒的是参数。
                await client.hard_stop()
                self._clients.pop(key, None)
                self._last_used.pop(key, None)
                raise
            finally:
                self._last_used[key] = time.monotonic()

    def _reap_idle(self) -> None:
        now = time.monotonic()
        stale = [key for key, used in self._last_used.items() if now - used > self.idle_seconds]
        for key in stale:
            client = self._clients.pop(key, None)
            self._last_used.pop(key, None)
            if client is not None:
                asyncio.create_task(client.hard_stop())

    async def release_task(self, task_id: str) -> None:
        for key in [k for k in self._clients if k[1] == str(task_id)]:
            client = self._clients.pop(key)
            self._last_used.pop(key, None)
            await client.hard_stop()

    def close_all_for_exit(self) -> None:
        """主进程退出时同步关 Job：KILL_ON_JOB_CLOSE 由内核执行，不依赖事件循环还活着。"""
        for client in list(self._clients.values()):
            if client.job is not None:
                windows_job.close_job(client.job)
                client.job = None
            if client.proc is not None:
                try:
                    # 尽力关 transport；此时事件循环可能已经关了，关不上也只是退场噪声，
                    # 真正的收尸由上面那句关 Job（内核态）兜住。
                    client.proc.close()
                except Exception:  # noqa: BLE001 - 退场路径不做判定
                    pass
                client.proc = None
        self._clients.clear()


_MANAGER: FsBrokerManager | None = None


def manager() -> FsBrokerManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = FsBrokerManager()
        atexit.register(_MANAGER.close_all_for_exit)
    return _MANAGER


def reset_manager_for_tests() -> None:
    global _MANAGER
    _MANAGER = None


async def call_broker(root: Path, task_id: str, op: str, args: dict) -> dict:
    return await manager().call(root, task_id, op, args)


def broker_failure_outcome(op: str, error: FsBrokerError) -> ToolOutcome:
    """失败判定的唯一出口：取消还它 CancelledError 的原形，其余一律失败并写下是哪一层坏的。

    `side_effects` 那一格回答"这次动作到底发生没有"：`none` 是请求根本没进 broker，
    `unknown` 是请求已经进去了但响应丢了——后者不许被读成"什么都没发生"。
    """
    from blank_api.services.tools.base import ToolOutcome

    if error.layer == LAYER_CANCELLED:
        raise asyncio.CancelledError() from error
    payload_error = {
        "layer": error.layer,
        "reason": error.reason,
        "op": op,
        "fallback": "none_refused",  # 红线：没有宿主回退，这一格永远写 none_refused
        "side_effects": error.side_effects,
    }
    effect_note = (
        "本次动作确定未发生" if error.side_effects == SIDE_EFFECTS_NONE
        else "注意：请求已进 broker，磁盘动作可能已经发生，结果不可知"
    )
    return ToolOutcome(
        ok=False,
        status="error",
        message=f"fs-broker 执行边界失败（{error.layer}）：{error.reason}（{effect_note}）",
        data={"fs_broker_error": payload_error, "requires_approval": False},
    )


def broker_success_marker(op: str, envelope: dict) -> dict:
    """成功调用的边界标记：网关按 data.fs_broker 存在与否判 boundary_kind=fs_broker。"""
    return {
        "fs_broker": {"op": op, **(envelope.get("status") or {})},
        "fs_broker_latency": envelope.get("latency") or {},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fs-broker", description="文件工具子进程执行边界")
    parser.add_argument("--root", required=True, help="工作区绝对路径，所有操作以此为界")
    args = parser.parse_args(argv)
    return serve(args.root)


if __name__ == "__main__":
    raise SystemExit(main())
