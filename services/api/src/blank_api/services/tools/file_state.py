"""读后写闸门：把「这个会话读过哪个文件、读的时候它长什么样」记在一处。

Claude Code 的 Edit/Write 有一条硬规则：没读过的文件不许改，读过之后文件又被改过的
也不许改（必须先重读）。我们以前只有系统提示里一句「写之前先读」，模型跳过它时没有
任何东西会失败。本模块把这条规则变成数据：read_file 成功时记录 (size, mtime_ns,
sha256)，写工具执行前拿这三格与磁盘对照。

为什么 size+mtime 之外还要 sha256：mtime 的精度取决于文件系统，备份恢复/复制工具也能
把时间戳改回去；真正能回答「内容还是不是读到的那一份」的只有内容哈希。sha256 只在读到
完整文件时记录——read_text_file 有 512KB 上限，截断时留空，半个文件的哈希拿来比对只会
制造假 stale。

闸门只在 ToolContext.file_state 非空时生效：生产路径（runtime / subagent）一定注入，
直接构造 ToolContext 的单元测试默认不受影响。
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass
from pathlib import Path

# 与 workspace.read_text_file 的上限一致：超过它的读取拿不到全文哈希。
MAX_HASH_BYTES = 512 * 1024

GUARD_NOT_READ = "not_read"
GUARD_STALE = "stale"


@dataclass(frozen=True)
class FileState:
    size: int
    mtime_ns: int
    # 读到的内容哈希；读被截断时为空串（只有 stat 可比较）。
    sha256: str = ""
    # 文本行数（二进制/超大文件为 0）与最近一次操作：快照里给模型一个可读的现状。
    lines: int = 0
    last_op: str = ""
    # 快照里的内容摘要：第一行非空文本，够模型认出"这是哪个文件"而不必重读。
    summary: str = ""
    # 本次会话对这个文件的累计写次数与 +/- 行数（只统计能拿到计数的写工具）。
    writes: int = 0
    added: int = 0
    removed: int = 0


def _line_count(raw: bytes) -> int:
    """文本行数按换行符数，末行没有换行也算一行；空文件为 0。"""
    if not raw:
        return 0
    return raw.count(b"\n") + (0 if raw.endswith(b"\n") else 1)


def content_summary(text: str, limit: int = 60) -> str:
    """内容摘要：第一行非空文本，压成一行、去掉行尾空白并截断。

    只做"认得出文件"这一件事：不猜语义、不总结正文。截断用省略号明示，
    免得模型拿半句话当事实。
    """
    # maxsplit 限住分割量：512KB 的文件不必为一行摘要切出几万段。
    for line in str(text or "").split("\n", 50)[:51]:
        stripped = " ".join(line.split())
        if stripped:
            return stripped if len(stripped) <= limit else stripped[: limit - 1] + "…"
    return ""


def _summary_of_bytes(raw: bytes) -> str:
    if not raw:
        return ""
    try:
        return content_summary(raw.decode("utf-8", "replace"))
    except (UnicodeDecodeError, ValueError):
        return ""


class FileStateCache:
    """按任务/会话记录读取状态。键是规范化后的工作区相对路径。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, FileState] = {}

    @staticmethod
    def _key(relative: str) -> str:
        text = str(relative or "").strip().replace("\\", "/")
        while text.startswith("./"):
            text = text[2:]
        return text.strip("/")

    def record_read(
        self,
        relative: str,
        *,
        size: int,
        mtime_ns: int,
        sha256: str = "",
        lines: int = 0,
        summary: str = "",
    ) -> None:
        key = self._key(relative)
        if not key:
            return
        with self._lock:
            previous = self._states.get(key)
            self._states[key] = FileState(
                int(size),
                int(mtime_ns),
                str(sha256 or ""),
                int(lines or 0),
                "read",
                str(summary or "") or (previous.summary if previous else ""),
                previous.writes if previous else 0,
                previous.added if previous else 0,
                previous.removed if previous else 0,
            )

    def record_write(
        self,
        root: Path | str,
        relative: str,
        *,
        added: int = 0,
        removed: int = 0,
    ) -> None:
        """写成功后按磁盘现状重记一次：写过 = 知道内容，后续编辑不必再读。

        ``added`` / ``removed`` 由写工具的回执计数带进来；拿不到计数的写工具
        （notebook / patch）只加一次写入次数，不编造行数。
        """
        key = self._key(relative)
        if not key:
            return
        target = Path(root) / key
        try:
            stat = target.stat()
        except OSError:
            self.forget(relative)
            return
        digest = ""
        lines = 0
        summary = ""
        if stat.st_size <= MAX_HASH_BYTES:
            try:
                raw = target.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                lines = _line_count(raw)
                summary = _summary_of_bytes(raw)
            except OSError:
                digest = ""
        with self._lock:
            previous = self._states.get(key)
            self._states[key] = FileState(
                int(stat.st_size),
                int(stat.st_mtime_ns),
                digest,
                lines,
                "write",
                summary,
                (previous.writes if previous else 0) + 1,
                (previous.added if previous else 0) + max(0, int(added or 0)),
                (previous.removed if previous else 0) + max(0, int(removed or 0)),
            )

    def forget(self, relative: str) -> None:
        with self._lock:
            self._states.pop(self._key(relative), None)

    def guard(self, relative: str) -> dict:
        """写前要带进磁盘函数的那一格事实。永远返回 dict，``was_read`` 回答读没读过。"""
        key = self._key(relative)
        with self._lock:
            state = self._states.get(key)
        if state is None:
            return {"path": key, "was_read": False, "size": None, "mtime_ns": None, "sha256": ""}
        return {
            "path": key,
            "was_read": True,
            "size": state.size,
            "mtime_ns": state.mtime_ns,
            "sha256": state.sha256,
        }

    def snapshot(self) -> dict[str, dict]:
        """诊断用：当前记住了哪些文件、各自的大小与时间戳。"""
        with self._lock:
            return {
                key: {
                    "size": state.size,
                    "mtime_ns": state.mtime_ns,
                    "sha256": bool(state.sha256),
                    "lines": state.lines,
                    "last_op": state.last_op,
                    "summary": state.summary,
                    "writes": state.writes,
                    "added": state.added,
                    "removed": state.removed,
                }
                for key, state in self._states.items()
            }

    def render_snapshot(self, *, limit: int = 20, root: Path | str | None = None) -> str:
        """把已访问文件压成一段可读状态，替代历史里反复重发的文件正文。

        只给路径、行数/大小、最近一次读写与短指纹；需要原文时模型仍然可以
        `read_file`。没有记录时返回空串，调用方据此避免注入空块。

        ``root`` 非空时只列仍在磁盘上的文件：shell 删掉的文件不能继续作为
        「当前状态」出现，它的旧读取结果也不该被折成指向快照的指针。
        """
        with self._lock:
            items = list(self._states.items())
        if root is not None:
            base = Path(root)
            items = [(path, state) for path, state in items if (base / path).is_file()]
        if not items:
            return ""
        # 最近改过的文件排前面；mtime 相同再按路径，保证输出可复现。
        items.sort(key=lambda item: (item[1].mtime_ns, item[0]), reverse=True)
        lines = ["[文件状态快照] 较早的文件内容已折叠；需要原文时用 read_file 精确读取。"]
        for path, state in items[:limit]:
            bits = [path, f"{state.lines} 行" if state.lines else f"{state.size} 字节"]
            if state.last_op == "write":
                bits.append("最近写入")
            elif state.last_op == "read":
                bits.append("最近读取")
            if state.writes:
                # notebook / patch 拿不到行数：只报写入次数，不拿 +0/-0 冒充事实。
                changes = f" +{state.added}/-{state.removed}" if state.added or state.removed else ""
                bits.append(f"本次 {state.writes} 次写入{changes}")
            if state.sha256:
                bits.append(f"sha {state.sha256[:8]}")
            if state.summary:
                bits.append(f"摘要：{state.summary}")
            lines.append("- " + "，".join(bits))
        if len(items) > limit:
            lines.append(f"…另有 {len(items) - limit} 个文件未列出；需要时用 read_file。")
        return "\n".join(lines)

    def export(self) -> dict[str, dict]:
        """完整导出（含哈希）：跟着图状态一起进检查点，中断恢复后还能接着用。

        审批卡与提问都会结束这一次运行、由 resume 新起一个会话；读过的文件状态
        如果只活在新会话的内存里，用户答完一句"就用这个"，模型就得把同一个文件
        再读一遍——Claude 的读前写状态是跨回合的，这里也必须跨。
        """
        with self._lock:
            return {
                key: {
                    "size": state.size,
                    "mtime_ns": state.mtime_ns,
                    "sha256": state.sha256,
                    "lines": state.lines,
                    "last_op": state.last_op,
                    "summary": state.summary,
                    "writes": state.writes,
                    "added": state.added,
                    "removed": state.removed,
                }
                for key, state in self._states.items()
            }

    def restore(self, data: dict | None) -> None:
        """从检查点补回读状态。已有记录不覆盖：本进程里后来真读过的那份更新。"""
        if not isinstance(data, dict):
            return
        with self._lock:
            for key, value in data.items():
                if not isinstance(value, dict):
                    continue
                key = self._key(key)
                if not key or key in self._states:
                    continue
                self._states[key] = FileState(
                    int(value.get("size") or 0),
                    int(value.get("mtime_ns") or 0),
                    str(value.get("sha256") or ""),
                    int(value.get("lines") or 0),
                    str(value.get("last_op") or ""),
                    str(value.get("summary") or ""),
                    int(value.get("writes") or 0),
                    int(value.get("added") or 0),
                    int(value.get("removed") or 0),
                )


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def check_write_guard(target: Path, guard: dict | None, raw: bytes | None = None) -> dict | None:
    """判定一次写能不能落盘。返回 None 表示放行，否则返回给调用方的 ``guard_error``。

    ``raw`` 是刚刚从磁盘读到的完整字节（有的话）：它让「预检之后、写入之前被换掉」
    这件事也能被发现——stat 可以与记录一致，内容却已经不是读过的那份。
    """
    if not guard:
        return None
    relative = str(guard.get("path") or "")
    try:
        exists = target.exists()
        stat = target.stat() if exists else None
    except OSError as exc:
        return {
            "code": GUARD_STALE,
            "path": relative,
            "message": f"无法确认文件 {relative} 的当前状态（{exc.__class__.__name__}），先重新 read_file 再改。",
        }
    if not guard.get("was_read"):
        if not exists:
            return None  # 新建文件不需要先读
        return {
            "code": GUARD_NOT_READ,
            "path": relative,
            "message": (
                f"文件 {relative} 还没在本会话读过。修改前先用 read_file 读它——"
                "凭文件名猜内容去改，改错的地方比省下的时间多。"
            ),
        }
    if not exists:
        # 读完之后文件被删了：当作新建，允许写入。
        return None
    expected_size = guard.get("size")
    expected_mtime = guard.get("mtime_ns")
    changed = (
        (expected_size is not None and int(stat.st_size) != int(expected_size))
        or (expected_mtime is not None and int(stat.st_mtime_ns) != int(expected_mtime))
    )
    if not changed and guard.get("sha256") and raw is not None:
        changed = _digest(raw) != str(guard["sha256"])
    if not changed:
        return None
    return {
        "code": GUARD_STALE,
        "path": relative,
        "message": (
            f"文件 {relative} 在读过之后被改过（可能是用户或格式化/生成工具）。"
            "先重新 read_file 拿到最新内容，再基于它修改。"
        ),
        "read": {
            "size": guard.get("size"),
            "mtime_ns": guard.get("mtime_ns"),
            "sha256": str(guard.get("sha256") or "")[:12],
        },
        "disk": {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)},
    }


def stat_of(handle) -> tuple[int, int]:
    """(size, mtime_ns)：读取时顺手记下的两格事实。"""
    info = os.fstat(handle.fileno())
    return int(info.st_size), int(info.st_mtime_ns)
