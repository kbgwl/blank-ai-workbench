"""后台 shell 句柄表：``run_shell(run_in_background=true)`` 与 ``monitor`` 的落地。

对齐 Claude Code 的 Bash/PowerShell 语义：命令在后台跑，工具立刻返回句柄；
结束时由系统通知（不是让模型轮询），输出落在句柄后面随时可取，不要了能终止。
``monitor`` 复用同一张表：进程、日志、终止、收尾都一样，只是多出一条逐行事件
通道（``drain_monitor_events``），让"命令又输出了一行"成为可投递的系统事件。

三条边界：

* **句柄只在本进程内有效**，按 task 分桶；任务收尾时取消仍在跑的并删掉日志，
  与后台子代理同一套纪律（任务显示完成后不该还有进程在烧 CPU、写事件）。
* **日志落在系统临时目录**，不写进工作区：工作区里多出来的文件会被 ``git status``
  和验收当成产物，后台输出是运行时证据，不是用户的文件。
* **输出不是无限缓冲**：写盘有上限，读回只取尾部；超限时在日志里注明，
  不静默丢——"看到一半"和"只有这么多"必须能分辨。
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from blank_api.services.log_safety import redact

# 日志上限：后台跑的可能是一直不退出的服务，不能让它把磁盘写满。
MAX_LOG_BYTES = 2_000_000
# 读回时只取尾部：结论/报错通常在最后，前面的进度不适合整段塞进上下文。
TAIL_BYTES = 64_000
# 通知里带的输出尾巴，够判断"成了没有"，完整文本仍走 task_output。
NOTIFICATION_TAIL_CHARS = 1_200
# 与 Claude 的 Bash 上限对齐（600s）；未显式给 timeout 时后台命令用这个，而不是 30s。
DEFAULT_BACKGROUND_TIMEOUT = 600.0

# monitor：逐行事件队列。它是"边跑边通知"的，堆在内存里的不是日志而是还没投递的行，
# 所以必须有上限——否则一条刷屏命令就能把进程内存吃光。
# 单行上限只防一条超长行把一次注入撑爆；完整内容仍在日志里。
MONITOR_ID_PREFIX = "mon-"
MONITOR_MAX_EVENT_CHARS = 1_000
MONITOR_MAX_PENDING_EVENTS = 200
# 一次模型调用最多投递这么多行：输出很密的命令不该把每次调用都变成日志回放。
# 剩下的留在队列里，下一轮继续投；丢弃只发生在队列上限处，且计数会如实上报。
MONITOR_EVENTS_PER_DRAIN = 25

_TRUNCATED_MARK = "\n[blank] 输出超过上限，后续内容未写入\n".encode("utf-8")

_STDOUT = "stdout"
_STDERR = "stderr"

_TASKS: dict[str, dict[str, "ShellTask"]] = {}


@dataclass
class ShellTask:
    shell_id: str
    task_id: str
    command: str
    description: str
    timeout_seconds: float
    log_path: Path
    status: str = "running"
    exit_code: int | None = None
    timed_out: bool = False
    error: str = ""
    notified: bool = False
    started_at: float = 0.0
    finished_at: float | None = None
    written: int = 0
    truncated: bool = False
    data: dict = field(default_factory=dict)
    task: asyncio.Task | None = None
    # "shell" = run_shell(run_in_background)；"monitor" = monitor 的逐行事件流。
    kind: str = "shell"
    events: deque = field(default_factory=deque)
    dropped_events: int = 0
    ended_notified: bool = False

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.perf_counter()
        start = self.started_at or end
        return round(max(0.0, end - start), 2)


class _Sink:
    """把子进程输出边到边写进日志文件；tag 切换时加一行分隔。"""

    def __init__(self, record: ShellTask) -> None:
        self.record = record
        self._last_tag = ""
        self._handle = None
        try:
            record.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(record.log_path, "ab", buffering=0)
        except OSError as exc:
            record.error = f"后台命令的日志文件打不开：{exc}"
            self._handle = None

    def write(self, tag: str, chunk: bytes) -> None:
        record = self.record
        if self._handle is None or record.truncated or not chunk:
            return
        header = b""
        if self._last_tag and tag != self._last_tag:
            header = f"\n[{tag}]\n".encode("utf-8")
        self._last_tag = tag
        room = MAX_LOG_BYTES - record.written
        data = header + chunk
        if len(data) >= room:
            data = data[: max(0, room)]
            record.truncated = True
        try:
            self._handle.write(data)
            record.written += len(data)
        except OSError as exc:
            record.error = f"日志写入失败：{exc}"
            self._handle = None
            return
        if record.truncated:
            self._append(_TRUNCATED_MARK)

    def _append(self, data: bytes) -> None:
        if self._handle is None:
            return
        try:
            self._handle.write(data)
            self.record.written += len(data)
        except OSError:
            pass

    def text(self, text: str) -> None:
        if text:
            self._append(text.encode("utf-8"))

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None


class _LineSplitter:
    """把输出字节流切成行事件（Claude Monitor 的「每条 stdout 行是一个事件」）。

    按字节切再解码，不按解码后的字符串切：一次读取可能落在一个多字节汉字的中间，
    先解码会得到一个替换符，把本来正确的日志变成乱码。换行符是单字节，切在它上面
    永远不会切坏字符。没有换行的尾段留在缓冲区里，命令结束时再 flush。
    """

    def __init__(self, record: ShellTask) -> None:
        self.record = record
        self._buffers = {_STDOUT: b"", _STDERR: b""}

    def feed(self, tag: str, chunk: bytes) -> None:
        if not chunk:
            return
        text = self._buffers.get(tag, b"") + chunk
        parts = text.split(b"\n")
        self._buffers[tag] = parts.pop()
        for raw in parts:
            self._push(tag, raw)
        # 一直没有换行的输出（进度条、二进制流）不能无限攒在内存里：
        # 到上限就先切出去，剩下的继续等换行。
        limit = MONITOR_MAX_EVENT_CHARS * 4
        pending = self._buffers[tag]
        while len(pending) > limit:
            self._push(tag, pending[:limit])
            pending = pending[limit:]
        self._buffers[tag] = pending

    def flush(self) -> None:
        for tag, raw in list(self._buffers.items()):
            if raw.strip():
                self._push(tag, raw)
            self._buffers[tag] = b""

    def _push(self, tag: str, raw: bytes) -> None:
        line = _decode(raw).rstrip("\r")
        # 空行不占一次注入：日志里的空白分隔不携带信息，投递它只会挤掉真正的行。
        if not line.strip():
            return
        if len(line) > MONITOR_MAX_EVENT_CHARS:
            line = line[:MONITOR_MAX_EVENT_CHARS] + "…"
        events = self.record.events
        events.append({"tag": tag, "text": line})
        while len(events) > MONITOR_MAX_PENDING_EVENTS:
            events.popleft()
            self.record.dropped_events += 1


def new_id() -> str:
    return "shell-" + uuid.uuid4().hex[:8]


def new_monitor_id() -> str:
    return MONITOR_ID_PREFIX + uuid.uuid4().hex[:8]


def _task_dir(task_id: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(task_id or ""))[:60] or "task"
    return Path(tempfile.gettempdir()) / "blank-shell" / safe


def start(
    *,
    task_id: str,
    command: str,
    description: str,
    timeout_seconds: float,
    runner: Callable[[Callable[[str, bytes], None]], Awaitable[dict]],
    recorder: Any = None,
    mask: Callable[[str], str] | None = None,
    kind: str = "shell",
) -> ShellTask:
    """起一个后台命令并返回句柄；``runner`` 收到一个输出回调，返回 execute_command 的结果。

    ``kind="monitor"`` 时同一个句柄表再承担一件事：把输出逐行切进事件队列，
    由 ``drain_monitor_events`` 投给模型。进程、日志、取消、收尾仍是这一套。
    """
    shell_id = new_monitor_id() if kind == "monitor" else new_id()
    record = ShellTask(
        shell_id=shell_id,
        task_id=str(task_id or ""),
        command=command,
        description=description,
        timeout_seconds=float(timeout_seconds),
        log_path=_task_dir(task_id) / f"{shell_id}.log",
        kind=kind,
    )
    record.task = asyncio.create_task(
        _execute(record, runner, recorder, mask), name=f"blank-{shell_id}"
    )
    _TASKS.setdefault(record.task_id, {})[shell_id] = record
    return record


async def _execute(
    record: ShellTask,
    runner: Callable[[Callable[[str, bytes], None]], Awaitable[dict]],
    recorder: Any,
    mask: Callable[[str], str] | None,
) -> None:
    record.started_at = time.perf_counter()
    sink = _Sink(record)
    splitter = _LineSplitter(record) if record.kind == "monitor" else None

    def emit(tag: str, chunk: bytes) -> None:
        sink.write(tag, chunk)
        if splitter is not None:
            splitter.feed(tag, chunk)

    try:
        result = await runner(emit)
    except asyncio.CancelledError:
        record.status = "cancelled"
        record.finished_at = time.perf_counter()
        if splitter is not None:
            splitter.flush()
        sink.text("\n[blank] 后台命令已取消\n")
        sink.close()
        await _record_event(recorder, "cancelled", record)
        raise
    except Exception as exc:  # noqa: BLE001 - 后台任务的异常必须落成状态，不能凭空消失
        record.status = "failed"
        record.error = str(exc)[:300]
        record.finished_at = time.perf_counter()
        if splitter is not None:
            splitter.flush()
        sink.text(f"\n[blank] 后台命令执行失败：{record.error}\n")
        sink.close()
        await _record_event(recorder, "finished", record)
        return
    record.exit_code = result.get("exit_code")
    record.timed_out = bool(result.get("timed_out"))
    record.data = {
        key: result.get(key)
        for key in ("sandbox", "isolation", "tier", "enforcement")
        if key in result
    }
    if record.timed_out:
        record.status = "timeout"
    elif result.get("isolation_blocked") or result.get("tier_blocked"):
        record.status = "failed"
    elif record.exit_code == 0:
        record.status = "completed"
    else:
        record.status = "failed"
    # 沙箱路径不流式（run_process 之外的实现只在结束时给整段输出）：这时候把
    # 结果补写进日志，否则 task_output 会读到空文件。监控的事件也在这一步补切，
    # 否则"沙箱里跑的监控"只有结束时才有事件，等于没有监控。
    if record.written == 0:
        stdout = str(result.get("stdout") or "")
        stderr = str(result.get("stderr") or "")
        if mask is not None:
            stdout, stderr = mask(stdout), mask(stderr)
        sink.text(stdout)
        if stderr:
            sink.text(("[stderr]\n" if stdout else "") + stderr)
        if splitter is not None:
            splitter.feed(_STDOUT, stdout.encode("utf-8"))
            splitter.feed(_STDERR, stderr.encode("utf-8"))
    if splitter is not None:
        splitter.flush()
    if record.status == "timeout":
        sink.text(f"\n[blank] 命令超时（{record.timeout_seconds:.0f}s），进程已终止\n")
    elif record.exit_code is not None:
        sink.text(f"\n[blank] 退出码 {record.exit_code}\n")
    sink.close()
    record.finished_at = time.perf_counter()
    await _record_event(recorder, "finished", record)


async def _record_event(recorder: Any, event: str, record: ShellTask) -> None:
    if recorder is None:
        return
    monitor = record.kind == "monitor"
    try:
        await recorder.add(
            f"{'monitor' if monitor else 'shell'}.{event}",
            "monitor" if monitor else "run_shell",
            {
                "monitor_id" if monitor else "shell_id": record.shell_id,
                "command": redact(record.command)[:300],
                "description": record.description[:200],
                "status": record.status,
                "exit_code": record.exit_code,
                "timed_out": record.timed_out,
                "duration_seconds": record.elapsed,
                "output_bytes": record.written,
                "truncated": record.truncated,
                "error": record.error[:300],
            },
        )
    except Exception:  # noqa: BLE001 - 记账失败不该改变命令本身的结局
        pass


def _decode(raw: bytes) -> str:
    from blank_api.services.tools.shell import _decode as decode

    return decode(raw)


def tail(record: ShellTask, limit: int = TAIL_BYTES) -> str:
    try:
        size = record.log_path.stat().st_size
        with open(record.log_path, "rb") as handle:
            if size > limit:
                handle.seek(size - limit)
                raw = handle.read()
                return "…（只显示最后 %d 字节）\n%s" % (limit, _decode(raw))
            return _decode(handle.read())
    except OSError:
        return ""


def find(task_id: str, shell_id: str) -> ShellTask | None:
    return _TASKS.get(str(task_id or ""), {}).get(shell_id)


def snapshot(record: ShellTask) -> dict:
    return {
        "shell_id": record.shell_id,
        "monitor_id": record.shell_id if record.kind == "monitor" else "",
        "kind": record.kind,
        "command": record.command,
        "description": record.description,
        "status": record.status,
        "exit_code": record.exit_code,
        "timed_out": record.timed_out,
        "elapsed_seconds": record.elapsed,
        "truncated": record.truncated,
        "error": record.error,
        **record.data,
    }


async def output(
    task_id: str,
    shell_id: str,
    *,
    block: bool = True,
    timeout: float = 60.0,
) -> dict:
    """取后台命令的现状与已产生的输出；``block`` 时最多等 ``timeout`` 秒。

    等待用 ``asyncio.wait`` 而不是 ``wait_for``：后者超时会取消被等的 Task，
    把「等一会儿没等到」变成「把命令杀了」——那是 task_stop 的语义。
    """
    record = find(task_id, shell_id)
    if record is None:
        return {
            "ok": False,
            "status": "not_found",
            "shell_id": shell_id,
            "message": f"没有找到后台命令或监控：{shell_id}（句柄只在本进程、本任务内有效）",
        }
    if block and record.task is not None and not record.task.done():
        wait_seconds = max(0.0, min(600.0, float(60 if timeout is None else timeout)))
        await asyncio.wait({record.task}, timeout=wait_seconds)
    body = tail(record)
    return {
        "ok": record.status == "completed",
        **snapshot(record),
        "text": body,
        "message": _status_message(record),
    }


def _status_message(record: ShellTask) -> str:
    label = "监控" if record.kind == "monitor" else "后台命令"
    if record.status == "running":
        if record.kind == "monitor":
            return f"监控仍在进行（已 {record.elapsed:.1f}s），下面是目前的日志，可能不完整"
        return f"后台命令仍在运行（已 {record.elapsed:.1f}s），下面是目前的输出，可能不完整"
    if record.status == "completed":
        if record.kind == "monitor":
            return "监控的命令已结束（退出码 0），监控随之结束"
        return "后台命令已结束（退出码 0）"
    if record.status == "timeout":
        return f"{label}超时（{record.timeout_seconds:.0f}s），进程已终止"
    if record.status == "cancelled":
        return f"{label}已取消"
    if record.exit_code is not None:
        return f"{label}的命令失败（退出码 {record.exit_code}）"
    return f"{label}失败：{record.error or '未给出原因'}"


async def stop(task_id: str, shell_id: str) -> dict:
    record = find(task_id, shell_id)
    if record is None:
        return {
            "ok": False,
            "status": "not_found",
            "shell_id": shell_id,
            "message": f"没有找到后台命令或监控：{shell_id}（句柄只在本进程、本任务内有效）",
        }
    if record.task is not None and not record.task.done():
        record.task.cancel()
        try:
            await record.task
        except (asyncio.CancelledError, Exception):
            pass
    if record.status == "running":
        # 协程还没跑起来就被取消：不会走到 _execute 的取消分支，这里补状态与日志。
        record.status = "cancelled"
        record.finished_at = time.perf_counter()
        try:
            with open(record.log_path, "ab") as handle:
                handle.write("\n[blank] 后台命令已取消\n".encode("utf-8"))
        except OSError:
            pass
    return {**snapshot(record), "message": _status_message(record)}


def drain_notifications(task_id: str) -> list[dict]:
    """取走「后台命令已结束」的通知，每个句柄只投递一次。

    与后台子代理同一套语义：结束是系统事件，模型不该靠轮询发现；已通知的
    句柄仍留在表里，task_output 照样能取到完整输出。
    """
    bucket = _TASKS.get(str(task_id or ""), {})
    notes: list[dict] = []
    for record in bucket.values():
        # 监控的结束走它自己的事件通道（drain_monitor_events），不能在这里再报一遍：
        # 同一次结束被两条通道各说一次，模型会以为它结束了两次。
        if record.kind != "shell" or record.notified or record.status == "running":
            continue
        record.notified = True
        notes.append(
            {
                "shell_id": record.shell_id,
                "command": record.command[:300],
                "description": record.description[:200],
                "status": record.status,
                "exit_code": record.exit_code,
                "timed_out": record.timed_out,
                "elapsed_seconds": record.elapsed,
                "tail": tail(record, limit=NOTIFICATION_TAIL_CHARS * 4)[-NOTIFICATION_TAIL_CHARS:],
            }
        )
    return notes


def drain_monitor_events(task_id: str) -> list[dict]:
    """取走监控待投递的行事件；``ended`` 每个监控只出现一次。

    与后台命令的完成通知是同一套「系统事件」语义，但粒度不同：这里是边跑边投递，
    结束只是最后一条事件。一次最多投 ``MONITOR_EVENTS_PER_DRAIN`` 行，剩下的留在
    队列里下一轮继续投；积压超过队列上限时只丢最旧的行，并把丢弃条数如实带上。
    """
    bucket = _TASKS.get(str(task_id or ""), {})
    notes: list[dict] = []
    for record in bucket.values():
        if record.kind != "monitor":
            continue
        events: list[dict] = []
        while record.events and len(events) < MONITOR_EVENTS_PER_DRAIN:
            events.append(record.events.popleft())
        dropped = record.dropped_events
        record.dropped_events = 0
        # 结束必须是「最后一条事件」：先把还在队列里的行投完，再在同一条通知里
        # 说"监控已结束"。提前报结束会让剩下的行看起来像结束后的鬼影。
        ended = (
            record.status != "running"
            and not record.ended_notified
            and not record.events
        )
        if not events and not dropped and not ended:
            continue
        if ended:
            record.ended_notified = True
        notes.append(
            {
                "monitor_id": record.shell_id,
                "command": record.command[:300],
                "description": record.description[:200],
                "status": record.status,
                "exit_code": record.exit_code,
                "timed_out": record.timed_out,
                "elapsed_seconds": record.elapsed,
                "events": events,
                "dropped": dropped,
                "pending": len(record.events),
                "ended": ended,
                "message": _status_message(record) if ended else "",
            }
        )
    return notes


def list_tasks(task_id: str) -> list[dict]:
    return [snapshot(record) for record in _TASKS.get(str(task_id or ""), {}).values()]


async def aclose(task_id: str = "", *, reason: str = "task_finished") -> list[str]:
    """任务收尾：取消仍在跑的后台命令、删掉日志，返回被取消的句柄。

    不收走它们，任务显示「已完成」之后进程还活着，日志占着盘，事件也会写进
    一个已经收尾的任务里——与后台子代理 `aclose` 是同一道防线。
    """
    bucket = _TASKS.pop(str(task_id or ""), {})
    if not bucket:
        return []
    cancelled = [
        record.shell_id
        for record in bucket.values()
        if record.task is not None and not record.task.done()
    ]
    for record in bucket.values():
        if record.task is not None and not record.task.done():
            record.task.cancel()
    for record in bucket.values():
        if record.task is None:
            continue
        try:
            await record.task
        except (asyncio.CancelledError, Exception):
            pass
    for record in bucket.values():
        try:
            record.log_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            os.rmdir(record.log_path.parent)
        except OSError:
            pass
    return cancelled
