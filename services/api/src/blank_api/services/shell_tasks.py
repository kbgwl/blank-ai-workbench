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
#
# 分两段，因为「只留开头 2MB」会把最有用的部分丢掉：服务类命令跑十分钟才报错，
# 前 2MB 全是启动日志，真正的报错一个字都没落盘（旧实现超限后直接停止写入）。
#   头段：主文件，写满 HEAD_LOG_BYTES 就封笔，留启动上下文；
#   尾段：`*.tail.log`，一直追加最新输出，到 TAIL_LOG_BYTES 时压缩成最后 TAIL_KEEP_BYTES。
# 磁盘占用上界 = HEAD_LOG_BYTES + TAIL_LOG_BYTES，和原来同一数量级。
HEAD_LOG_BYTES = 1_000_000
TAIL_LOG_BYTES = 1_000_000
TAIL_KEEP_BYTES = 500_000
# 总预算 = 头段 + 尾段。对外仍叫这个名字：磁盘占用上界看它，不按单个文件算。
MAX_LOG_BYTES = HEAD_LOG_BYTES + TAIL_LOG_BYTES
# 读回时只取尾部：结论/报错通常在最后，前面的进度不适合整段塞进上下文。
TAIL_BYTES = 64_000
# 溢出后读回时，头部只留这么多（够看出「它在干什么」，不必给整段启动日志）。
HEAD_KEEP_BYTES = 4_000
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
# 溢出后的读回提示：说明「中间少了一段」，而不是让人以为日志就这么短。
_OMIT_MARK = "\n[blank] 中间省略了 {n} 字节（只保留最早一段和最新一段）\n"

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
    # 尾段文件。头段（log_path）写满之后，后续输出落在这里。
    tail_path: Path | None = None
    status: str = "running"
    exit_code: int | None = None
    timed_out: bool = False
    error: str = ""
    notified: bool = False
    started_at: float = 0.0
    finished_at: float | None = None
    # 已落盘字节（头段 + 尾段）。沙箱那条"只在结束时给整段输出"的分支靠它判断
    # 有没有流过式输出，所以语义必须是「真的写进文件了」，不是「收到了多少」。
    written: int = 0
    # 收到的总字节（含溢出后被丢掉的部分）。增量读取的 offset 用它，单调递增。
    produced: int = 0
    # 被压缩掉的字节数（尾段滚动时丢的中段）。读回时如实告诉调用方「少了一段」。
    omitted: int = 0
    truncated: bool = False
    data: dict = field(default_factory=dict)
    task: asyncio.Task | None = None
    # "shell" = run_shell(run_in_background)；"monitor" = monitor 的逐行事件流。
    kind: str = "shell"
    events: deque = field(default_factory=deque)
    dropped_events: int = 0
    # 本轮丢弃的最旧事件从哪个字节开始（没丢就是 None）。drain 时用它算出
    # 「这次投递覆盖的区间」的左界：区间要包含被丢掉的那段，模型才知道去哪补看。
    dropped_events_from: int | None = None
    ended_notified: bool = False

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.perf_counter()
        start = self.started_at or end
        return round(max(0.0, end - start), 2)


class _Sink:
    """把子进程输出边到边写进日志文件；tag 切换时加一行分隔。

    写盘分两段（原因见 ``MAX_LOG_BYTES`` 那段注释）：
    先填头段主文件，写满 ``HEAD_LOG_BYTES`` 后转写尾段 ``*.tail.log``；
    尾段到 ``TAIL_LOG_BYTES`` 时压成最后 ``TAIL_KEEP_BYTES``，
    所以「最新输出」永远在，磁盘占用仍有上界。
    """

    def __init__(self, record: ShellTask) -> None:
        self.record = record
        self._last_tag = ""
        self._head = None
        self._tail = None
        self.head_bytes = 0
        self.tail_bytes = 0
        try:
            record.log_path.parent.mkdir(parents=True, exist_ok=True)
            if record.tail_path is None:
                record.tail_path = _tail_path(record.log_path)
            self._head = open(record.log_path, "ab", buffering=0)
            # 重建 sink 时（stop 里补「已取消」）要接着头段的真实长度算，
            # 否则会以为头段还是空的，把尾巴继续塞进已经封笔的主文件。
            self.head_bytes = min(_file_size(record.log_path), HEAD_LOG_BYTES)
            self.tail_bytes = _file_size(record.tail_path)
        except OSError as exc:
            record.error = f"后台命令的日志文件打不开：{exc}"
            self._head = None

    def write(self, tag: str, chunk: bytes) -> int | None:
        """写一段输出，返回这段内容在 ``produced`` 字节流里的起始位置。

        逐行事件要带 offset 才能和日志文件对账，而 offset 的唯一口径就是
        ``produced``（含分段标记、含 tag 切换时插的分隔行）。所以位置必须由
        写盘这一层给出：切行那一层自己数字节，会漏掉它看不到的分隔行。
        没写进去（句柄坏了 / 空块）就返回 None，切行侧退回按 ``produced`` 估算。
        """
        record = self.record
        if self._head is None or not chunk:
            return None
        header = b""
        if self._last_tag and tag != self._last_tag:
            header = f"\n[{tag}]\n".encode("utf-8")
        self._last_tag = tag
        start = record.produced + len(header)
        self._emit(header + chunk)
        return start

    def _emit(self, data: bytes) -> None:
        """先填头段，满了转尾段。切点对齐到字符边界，文件不会以半个汉字结尾。"""
        if not data:
            return
        # produced 跟着「真正落进日志的字节」走（含分段标记行），
        # 这样它才能当增量读取的 offset 用；两个口径混着算，offset 会漂。
        self.record.produced += len(data)
        if self.head_bytes < HEAD_LOG_BYTES:
            room = HEAD_LOG_BYTES - self.head_bytes
            if len(data) <= room:
                self._write_head(data)
                return
            head_part = _cut_utf8(data, room)
            self._write_head(head_part)
            data = data[len(head_part) :]
        self._write_tail(data)

    def _write_head(self, data: bytes) -> None:
        if not data or self._head is None:
            return
        try:
            self._head.write(data)
            self.head_bytes += len(data)
            self.record.written += len(data)
        except OSError as exc:
            self.record.error = f"日志写入失败：{exc}"
            self._head = None

    def _write_tail(self, data: bytes) -> None:
        if not data or self.record.tail_path is None:
            return
        if self._tail is None:
            try:
                self._tail = open(self.record.tail_path, "ab", buffering=0)
            except OSError as exc:
                self.record.error = f"日志写入失败：{exc}"
                self._head = None
                return
        try:
            self._tail.write(data)
            self.tail_bytes += len(data)
            self.record.written += len(data)
        except OSError as exc:
            self.record.error = f"日志写入失败：{exc}"
            self._tail = None
            self._head = None
            return
        if self.tail_bytes >= TAIL_LOG_BYTES:
            self._compact_tail()

    def _compact_tail(self) -> None:
        """尾段到上限：只留最后一段。丢掉的字节数记在 record 上，读回时如实写出来。"""
        path = self.record.tail_path
        if path is None:
            return
        try:
            if self._tail is not None:
                self._tail.close()
                self._tail = None
            size = path.stat().st_size
            keep = min(TAIL_KEEP_BYTES, size)
            with open(path, "rb") as handle:
                handle.seek(size - keep)
                raw = handle.read()
            raw = _drop_partial_prefix(raw)
            omitted = size - len(raw)
            if omitted > 0:
                self.record.omitted += omitted
                self.record.truncated = True
                raw = _TRUNCATED_MARK + raw
            with open(path, "wb") as handle:
                handle.write(raw)
            self.tail_bytes = len(raw)
            self.record.written = self.head_bytes + self.tail_bytes
            self._tail = open(path, "ab", buffering=0)
        except OSError as exc:
            self.record.error = f"日志压缩失败：{exc}"
            self._tail = None

    def _append(self, data: bytes) -> None:
        if self._head is None or not data:
            return
        try:
            self._emit(data)
        except OSError:
            pass

    def text(self, text: str) -> None:
        if text:
            self._append(text.encode("utf-8"))

    def close(self) -> None:
        for handle_name in ("_head", "_tail"):
            handle = getattr(self, handle_name)
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
                setattr(self, handle_name, None)


class _LineSplitter:
    """把输出字节流切成行事件（Claude Monitor 的「每条 stdout 行是一个事件」）。

    按字节切再解码，不按解码后的字符串切：一次读取可能落在一个多字节汉字的中间，
    先解码会得到一个替换符，把本来正确的日志变成乱码。换行符是单字节，切在它上面
    永远不会切坏字符。没有换行的尾段留在缓冲区里，命令结束时再 flush。
    """

    def __init__(self, record: ShellTask) -> None:
        self.record = record
        self._buffers = {_STDOUT: b"", _STDERR: b""}
        # 每条缓冲区第一个字节在 ``produced`` 流里的绝对位置。跨块拼起来的一行
        # 要能说出它从哪个字节开始，所以缓冲内容必须和它的起点一起存。
        self._starts = {_STDOUT: 0, _STDERR: 0}
        # 日志写不进去时（句柄坏了）没有权威位置，用自己数过的字节兜底：
        # offset 仍然单调递增，只是无法与一个不存在的文件对齐。这比每块都报 0
        # 诚实——模型至少能看出「这几条是新来的」。
        self._cursor = 0

    def feed(self, tag: str, chunk: bytes, start: int | None = None) -> None:
        """喂一段输出；``start`` 是这段字节在日志字节流里的起点（由 ``_Sink`` 给）。

        不给就退回自己数过的游标——写盘失败时的兜底，见 ``_cursor`` 的注释。
        """
        if not chunk:
            return
        if start is None:
            start = self._cursor
        self._cursor = max(self._cursor, start + len(chunk))
        buffered = self._buffers.get(tag, b"")
        pos = self._starts.get(tag, start) if buffered else int(start)
        parts = (buffered + chunk).split(b"\n")
        tail = parts.pop()
        for raw in parts:
            # 这一行占 [pos, pos+len(raw)+1)：+1 是那个被 split 吃掉的换行符。
            self._push(tag, raw, pos, len(raw) + 1)
            pos += len(raw) + 1
        # 一直没有换行的输出（进度条、二进制流）不能无限攒在内存里：
        # 到上限就先切出去，剩下的继续等换行。
        limit = MONITOR_MAX_EVENT_CHARS * 4
        while len(tail) > limit:
            self._push(tag, tail[:limit], pos, limit)
            tail = tail[limit:]
            pos += limit
        self._buffers[tag] = tail
        self._starts[tag] = pos

    def flush(self) -> None:
        for tag, raw in list(self._buffers.items()):
            if raw.strip():
                # 结尾没有换行，这一行只占它自己的字节数，别再 +1。
                self._push(tag, raw, self._starts.get(tag, 0), len(raw))
            self._buffers[tag] = b""
            self._starts[tag] = self.record.produced

    def _push(self, tag: str, raw: bytes, start: int, size: int) -> None:
        """把一行放进事件队列；``start``/``size`` 描述它在日志字节流里占的区间。

        ``size`` 必须用**原始整行**（含换行符）的长度：文本超过
        ``MONITOR_MAX_EVENT_CHARS`` 时只会截断投递的内容，不会截断它在日志里
        占的字节。若拿截断后的长度算 ``end``，模型用 ``end`` 当 offset 增量读，
        会把被截掉的那截尾巴再读一遍。
        """
        line = _decode(raw).rstrip("\r")
        # 空行不占一次注入：日志里的空白分隔不携带信息，投递它只会挤掉真正的行。
        # 但它占的字节由调用方的游标记着，后面每一行的 offset 才不会偏。
        if not line.strip():
            return
        end = start + max(0, size)
        if len(line) > MONITOR_MAX_EVENT_CHARS:
            line = line[:MONITOR_MAX_EVENT_CHARS] + "…"
        events = self.record.events
        events.append({"tag": tag, "text": line, "offset": start, "end": end})
        while len(events) > MONITOR_MAX_PENDING_EVENTS:
            oldest = events.popleft()
            self.record.dropped_events += 1
            # 记住被丢掉那段从哪个字节开始：drain 时要把「去哪补看」说清楚，
            # 只说「丢了 37 行」等于让模型重读整段。
            offset = oldest.get("offset") if isinstance(oldest, dict) else None
            if isinstance(offset, int) and (
                self.record.dropped_events_from is None
                or offset < self.record.dropped_events_from
            ):
                self.record.dropped_events_from = offset


def new_id() -> str:
    return "shell-" + uuid.uuid4().hex[:8]


def new_monitor_id() -> str:
    return MONITOR_ID_PREFIX + uuid.uuid4().hex[:8]


def _task_dir(task_id: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(task_id or ""))[:60] or "task"
    return Path(tempfile.gettempdir()) / "blank-shell" / safe


def _tail_path(log_path: Path) -> Path:
    """尾段文件名：``shell-xxx.log`` → ``shell-xxx.tail.log``。"""
    return log_path.with_name(log_path.stem + ".tail.log")


def _cut_utf8(data: bytes, limit: int) -> bytes:
    """取前 ``limit`` 字节，但不切在一个多字节字符中间。

    切坏了会怎样：文件末尾留下半个汉字，读回时解出替换符，看起来像日志损坏。
    回退范围只往回收 3 字节；若 decode 在更早的位置就失败，说明原始字节本身
    不是 UTF-8（例如 gbk 输出），那就不该继续往回找，直接按原切点交出去。
    """
    if limit >= len(data):
        return data
    end = max(0, limit)
    while end > 0:
        try:
            data[:end].decode("utf-8")
            return data[:end]
        except UnicodeDecodeError as exc:
            if exc.start < end - 4:
                return data[:end]
            end = exc.start
    return b""


def _drop_partial_prefix(raw: bytes) -> bytes:
    """去掉开头不完整的续字节（0b10xxxxxx），最多三个。"""
    index = 0
    while index < len(raw) and index < 4 and (raw[index] & 0b1100_0000) == 0b1000_0000:
        index += 1
    return raw[index:]


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
        # 位置由写盘侧给出：事件里的 offset 必须和 task_output(after=...) 同一个口径。
        start = sink.write(tag, chunk)
        if splitter is not None:
            # 写盘失败（句柄打不开 / 磁盘满）时 start 是 None，但事件仍要投递：
            # 日志落不了盘是「存档缺了一份」，不能连带把模型的眼睛也关上。
            # 这条路径下 offset 只能按 produced 估算，事件照样带上它。
            splitter.feed(tag, chunk, start)

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
        # 补写进日志的这段也有字节位置：按写盘前的 produced 记起点，
        # 否则沙箱里跑的监控只有事件、没有 offset，前面那条增量读接口就白给了。
        stdout_start = record.produced
        sink.text(stdout)
        stderr_start = record.produced
        if stderr:
            sink.text(("[stderr]\n" if stdout else "") + stderr)
        if splitter is not None:
            splitter.feed(_STDOUT, stdout.encode("utf-8"), stdout_start)
            splitter.feed(
                _STDERR,
                stderr.encode("utf-8"),
                stderr_start + (len("[stderr]\n") if stdout else 0),
            )
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


def _file_size(path: Path | None) -> int:
    if path is None:
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _read_span(path: Path | None, limit: int, *, from_start: bool = False) -> bytes:
    """读文件的一段：``from_start`` 取开头，否则取结尾。"""
    if path is None or limit <= 0:
        return b""
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size <= limit:
                return handle.read()
            if from_start:
                return handle.read(limit)
            handle.seek(size - limit)
            return handle.read()
    except OSError:
        return b""


def _ensure_tail(record: ShellTask) -> int:
    """尾段存在就返回它的字节数（0 表示还没溢出过头段）。"""
    if record.tail_path is None:
        record.tail_path = _tail_path(record.log_path)
    return _file_size(record.tail_path)


def read_log(record: ShellTask, limit: int = TAIL_BYTES, *, with_head: bool = True) -> str:
    """读回日志正文。

    没溢出 → 和以前一样只取尾部 ``limit`` 字节（结论与报错在最后）。
    溢出过头段 → 头一小段（它在干什么）+ 一行省略提示 + 尾段最后一段（最新输出）。
    只给尾部看不到启动参数，只给头部看不到报错，两段都给才读得出因果。

    ``with_head=False`` 用于「结束通知」这种只想看最后一句的场景：那里带不带启动日志
    都无所谓，但它会实打实占掉通知的字符额度，把真正的报错挤出去。
    """
    tail_size = _ensure_tail(record)
    if tail_size <= 0:
        size = _file_size(record.log_path)
        raw = _read_span(record.log_path, limit)
        if size > limit:
            # 从中间截的起点可能是半个汉字，先丢掉开头那几个续字节。
            return "…（只显示最后 %d 字节）\n%s" % (limit, _decode(_drop_partial_prefix(raw)))
        return _decode(raw)
    if not with_head:
        return _decode(_drop_partial_prefix(_read_span(record.tail_path, limit)))
    head_raw = _read_span(record.log_path, HEAD_KEEP_BYTES, from_start=True)
    room = max(1_000, limit - len(head_raw))
    tail_raw = _read_span(record.tail_path, room)
    omitted = record.omitted + max(0, _file_size(record.log_path) - len(head_raw))
    parts = []
    if head_raw:
        parts.append(_decode(head_raw))
    parts.append(_OMIT_MARK.format(n=omitted))
    if tail_raw:
        parts.append(_decode(tail_raw))
    return "".join(parts)


def read_since(record: ShellTask, after: int) -> tuple[str, int, bool]:
    """增量读：返回 ``(新增正文, 新 offset, 是否丢了中间一段)``。

    ``after`` 用的是上一轮拿到的 offset（== ``record.produced``），
    不是文件偏移——日志被压缩过之后文件偏移会往回跳，拿它当游标会出现重复内容。
    """
    offset = record.produced
    start = max(0, int(after))
    if start >= offset:
        return "", offset, False
    window = offset - start
    tail_size = _ensure_tail(record)
    # 能往回追溯多少：溢出后只有尾段可回溯（压缩过的尾段更少），否则是头段全文。
    can_go_back = tail_size if tail_size > 0 else _file_size(record.log_path)
    dropped = window > can_go_back
    read_bytes = min(window, can_go_back) if dropped else window
    source = record.tail_path if tail_size > 0 else record.log_path
    raw = _read_span(source, read_bytes)
    if dropped:
        raw = _drop_partial_prefix(raw)
    return _decode(raw), offset, dropped


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
        # 增量读取的游标：下一次拿它当 after 传回来，就只拿到新增的部分。
        "offset": record.produced,
        "omitted_bytes": record.omitted,
        "error": record.error,
        **record.data,
    }


async def output(
    task_id: str,
    shell_id: str,
    *,
    block: bool = True,
    timeout: float = 60.0,
    after: int | None = None,
) -> dict:
    """取后台命令的现状与已产生的输出；``block`` 时最多等 ``timeout`` 秒。

    等待用 ``asyncio.wait`` 而不是 ``wait_for``：后者超时会取消被等的 Task，
    把「等一会儿没等到」变成「把命令杀了」——那是 task_stop 的语义。

    ``after`` 给了就按增量读：只返回那个 offset 之后的新内容，并带回新的 offset。
    长跑的服务一直刷日志时，每轮都重发最后 64KB 既费上下文又看不出「这轮多了什么」。
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
    if after is None:
        body = read_log(record)
        extra: dict = {}
    else:
        body, next_offset, dropped = read_since(record, after)
        extra = {
            "offset": next_offset,
            "dropped": dropped,
            # 这次正文覆盖的字节区间：下一轮要接着读，就把 to_offset 当 after 传回来。
            "from_offset": max(0, int(after)),
            "to_offset": next_offset,
        }
    return {
        "ok": record.status == "completed",
        **snapshot(record),
        **extra,
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
        # 走 sink 而不是直接 open 主文件：头段可能已经写满，直接 open 会把标记
        # 追加到已封笔的头段末尾，读回时它出现在「最早一段」后面，位置就错了。
        record.status = "cancelled"
        record.finished_at = time.perf_counter()
        marker = _Sink(record)
        try:
            marker.text("\n[blank] 后台命令已取消\n")
        finally:
            marker.close()
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
                "tail": read_log(record, limit=NOTIFICATION_TAIL_CHARS * 4, with_head=False)[-NOTIFICATION_TAIL_CHARS:],
            }
        )
    return notes


def _first_event_offset(events: list[dict]) -> int | None:
    """这批事件里最早的那条的起点；没有就返回 None。"""
    for event in events:
        offset = event.get("offset") if isinstance(event, dict) else None
        if isinstance(offset, int):
            return offset
    return None


def drain_monitor_events(task_id: str) -> list[dict]:
    """取走监控待投递的行事件；``ended`` 每个监控只出现一次。

    与后台命令的完成通知是同一套「系统事件」语义，但粒度不同：这里是边跑边投递，
    结束只是最后一条事件。一次最多投 ``MONITOR_EVENTS_PER_DRAIN`` 行，剩下的留在
    队列里下一轮继续投；积压超过队列上限时只丢最旧的行，并把丢弃条数如实带上。

    每条 note 还带 ``from_offset``/``to_offset``：这次投递覆盖的日志字节区间。
    左界取「最早一条还没给模型看过的字节」——有丢弃时就是被丢掉那段的起点；
    右界是最后一条事件的 ``end``。模型据此可以对被丢掉的区间或想补看的上下文
    调 ``task_output(after=...)``，而不是整段重读最后 64KB。
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
        dropped_from = record.dropped_events_from
        record.dropped_events = 0
        record.dropped_events_from = None
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
        note = {
            "monitor_id": record.shell_id,
            "command": record.command[:300],
            "description": record.description[:200],
            "status": record.status,
            "exit_code": record.exit_code,
            "timed_out": record.timed_out,
            "elapsed_seconds": record.elapsed,
            "events": events,
            "dropped": dropped,
            # 与 dropped 同值的新名字：这一刀之后事件通道对齐了字节区间，
            # 名字里带 events 才说得清「丢的是行、不是字节」。
            "dropped_events": dropped,
            "pending": len(record.events),
            "ended": ended,
            "message": _status_message(record) if ended else "",
        }
        if events or dropped:
            first = dropped_from if dropped_from is not None else _first_event_offset(events)
            if first is None:
                first = record.produced
            last = events[-1].get("end") if events else None
            note["from_offset"] = int(first)
            note["to_offset"] = int(last if isinstance(last, int) else record.produced)
        notes.append(note)
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
        for path in (record.log_path, record.tail_path):
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            os.rmdir(record.log_path.parent)
        except OSError:
            pass
    return cancelled
