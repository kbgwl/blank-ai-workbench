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

    def record_read(self, relative: str, *, size: int, mtime_ns: int, sha256: str = "") -> None:
        key = self._key(relative)
        if not key:
            return
        with self._lock:
            self._states[key] = FileState(int(size), int(mtime_ns), str(sha256 or ""))

    def record_write(self, root: Path | str, relative: str) -> None:
        """写成功后按磁盘现状重记一次：写过 = 知道内容，后续编辑不必再读。"""
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
        if stat.st_size <= MAX_HASH_BYTES:
            try:
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
            except OSError:
                digest = ""
        with self._lock:
            self._states[key] = FileState(int(stat.st_size), int(stat.st_mtime_ns), digest)

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
                key: {"size": state.size, "mtime_ns": state.mtime_ns, "sha256": bool(state.sha256)}
                for key, state in self._states.items()
            }

    def export(self) -> dict[str, dict]:
        """完整导出（含哈希）：跟着图状态一起进检查点，中断恢复后还能接着用。

        审批卡与提问都会结束这一次运行、由 resume 新起一个会话；读过的文件状态
        如果只活在新会话的内存里，用户答完一句"就用这个"，模型就得把同一个文件
        再读一遍——Claude 的读前写状态是跨回合的，这里也必须跨。
        """
        with self._lock:
            return {
                key: {"size": state.size, "mtime_ns": state.mtime_ns, "sha256": state.sha256}
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
