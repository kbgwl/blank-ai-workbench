"""Per-task git worktree sessions (enter_worktree / exit_worktree).

Claude Code 的两个工具在这里落成同一份数据：任务当前在哪个工作树里干活。

* ``enter_worktree(name=...)`` 在当前仓库里建一棵新工作树（新分支，
  位置 ``<repo>/.blank-worktrees/<name>``），之后这个任务的**每一次工具调用**
  都在那棵树里跑——shell 的 cwd、文件工具的根、安全策略的路径判定读的是同一个
  ``ToolContext.root``，所以换根只换一处；
* ``enter_worktree(path=...)`` 进入一棵已经存在的、``git worktree list`` 列出来的
  工作树。这种会话不是本任务创建的，退出时只能 ``keep``；
* ``exit_worktree(action=keep|remove)`` 回到原工作区。``remove`` 只作用于自己建的
  工作树，并且在工作树里还有未提交改动或未合并提交时**拒绝删除**，除非调用方明确
  传 ``discard_changes=true``。

会话按 task_id 存在进程内（与后台命令、子代理对话同一口径）：审批恢复会新建
``ToolContext``，但任务还在同一棵树上，所以恢复时按这里记录的根重新装配。
任务结束只摘记录，不删目录——用户的工作要留在盘上，删不删由 ``exit_worktree`` 决定。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# Claude 的命名约束：按 / 分段，每段只允许字母、数字、点、下划线、短横线，总计 64 字符。
# 我们不允许分段：工作树目录名就是标签，嵌一层只会让退出时的定位更绕。
NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
WORKTREE_DIRNAME = ".blank-worktrees"
BRANCH_PREFIX = "blank/worktree-"
# 新树的基准引用（对齐 Claude 的 worktree.baseRef）：fresh 从远端默认分支，
# head 从当前本地 HEAD。都没有可用的远端跟踪分支时一律回落 HEAD。
BASE_REFS: tuple[str, ...] = ("fresh", "head")
DEFAULT_BRANCH_CANDIDATES: tuple[str, ...] = ("main", "master")
GIT_TIMEOUT = 120.0
MAX_REPORTED_ENTRIES = 20

# ── 写者唯一（worktree owner）─────────────────────────────────────────────
# 移植自 qwen-code 的 ``worktree-owner-state.ts``（见
# ``docs/external-qwen-code-2026-11.md`` 可移植清单第 4 项）。理由不是"对齐"，是
# 这里真的会两个写者：``enter_worktree(path=...)`` 允许进入任何已注册的工作树，
# 于是任务 A 在里面干活时，任务 B 也能进去改同一批文件——两边的 diff、验证结论
# 都建立在对方随时会改的树上，最后的"改完了"谁都不算数。
#
# 落点选工作树自己的 git 目录（``git rev-parse --absolute-git-dir``，实际是
# ``<repo>/.git/worktrees/<name>``）：它天然按工作树分目录，跨进程共享，而且
# **不在工作树里**——写进工作树根会让 ``git status`` 多一个未跟踪文件，
# 我们自己的 ``_dirty_entries`` 会把它算成"有未提交改动"，remove 直接被自己拦住。
OWNER_FILENAME = "blank-owner.json"
# 基准线记录（baseline）。和认领文件放在同一个 git 目录，但**生命周期不同**：
# 认领随任务结束就松开（否则下一棵树永远进不去），基准线要留到回滚做完——
# 任务收尾之后用户才发现改错了，这时候恰恰最需要它。合成一个文件就只能二选一。
BASELINE_FILENAME = "blank-baseline.json"
# 认领结果。``unavailable`` 不是失败，是"读不到/写不进 git 目录"——那种时候
# 不能假装拿到了锁，调用方要按"不知道"处理而不是按"独占"处理。
OWNER_OWNED = "owned"
OWNER_OTHER = "owned_by_another_task"
OWNER_UNAVAILABLE = "unavailable"
OWNER_RECLAIMED = "reclaimed"

# 认领的租约。跨进程判断原先只认「pid 不同即他人所有」，代价是别的进程崩溃之后
# 留下的认领会把整棵树永远锁住——用户在界面上看到「正被某个任务使用」，而那个
# 任务其实早就没了。于是加一条续约：持有者每次用到这棵树就刷新 ``heartbeat_at``，
# 超过 ``LEASE_SECONDS`` 没续即视为失效，下一个任务可以接手。
#
# 取 30 分钟而不是几分钟：续约点是「每次工具调用」，而单次工具调用本身可能跑很久
# （全量测试、构建）。租约若短于最长的一次工具调用，正常任务会在自己干活时被
# 别人接手——那比锁死更糟。真正崩溃的进程不需要等租约：pid 探测直接判定已死。
LEASE_SECONDS = 1800.0
# 续约节流：每次工具调用都写一次文件没有收益，租约的 1/3 内不重复写。
RENEW_INTERVAL = LEASE_SECONDS / 3



@dataclass
class WorktreeSession:
    task_id: str
    name: str
    root: Path
    origin: Path
    repo: Path
    branch: str
    base_commit: str
    # 实际用的基准线："origin/<默认分支>" 或 "HEAD"（fresh 无远端跟踪时回落）。
    base_ref: str
    # 本任务用 name 创建的才是 True；path 进入的为 False，remove 会被拒绝。
    owned: bool
    # 写者认领的结果（OWNER_*）。默认 owned 只是为了让不使用认领的老调用点
    # 构造得出来；真实值由 enter() 写。unavailable 时事件里必须如实说"没守住"，
    # 不能因为流程走通了就当成独占。
    owner_status: str = OWNER_OWNED
    # 认领文件的位置。进树时算一次存下来：续约是每次工具调用都做的事，
    # 每次都跑一遍 ``git rev-parse --absolute-git-dir`` 是白花的时间。
    owner_file: Path | None = None

    @property
    def display(self) -> str:
        """给模型看的路径：仓库内相对路径；树在仓库外时只报名字（不泄漏主机路径）。"""
        try:
            return self.root.relative_to(self.repo).as_posix()
        except ValueError:
            return self.name

    def as_dict(self) -> dict:
        """事件里用：这些都是用户要能核对的坐标。"""
        return {
            "task_id": self.task_id,
            "name": self.name,
            "root": str(self.root),
            "origin": str(self.origin),
            "repo": str(self.repo),
            "branch": self.branch,
            "base_commit": self.base_commit,
            # 回滚的基准就是这一格：事件里带上它，事后能直接 git diff 复核。
            "baseline": self.base_commit,
            "base_ref": self.base_ref,
            "owned": self.owned,
            "owner_status": self.owner_status,
        }

    def tool_data(self) -> dict:
        """回给模型的 data：不带主机绝对路径。"""
        return {
            "name": self.name,
            "branch": self.branch,
            "base_ref": self.base_ref,
            "baseline": self.base_commit,
            "path": self.display,
            "owned": self.owned,
            # 写者唯一的结果也要给模型看：unavailable 时它照常能干活，但得知道
            # "这棵树没有登记独占"，否则会按独占去规划后面的改动。
            "owner_status": self.owner_status,
        }


@dataclass
class ExitReport:
    session: WorktreeSession | None
    removed: bool = False
    branch_deleted: bool = False
    branch_error: str = ""


@dataclass
class _GitResult:
    code: int
    out: str = ""
    err: str = ""


_SESSIONS: dict[str, WorktreeSession] = {}
_LOCK = threading.Lock()


def _run_git(args: list[str], cwd: Path, timeout: float = GIT_TIMEOUT) -> _GitResult:
    try:
        completed = subprocess.run(  # noqa: S603 - 参数由本模块构造，不含用户输入
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        return _GitResult(127, err="未找到 git 可执行文件")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _GitResult(1, err=f"git 执行失败：{exc.__class__.__name__}")
    return _GitResult(completed.returncode, completed.stdout or "", completed.stderr or "")


def _owner_file(root: Path) -> Path | None:
    """工作树的认领文件位置。

    用 ``--absolute-git-dir`` 而不是 ``root / ".git"``：工作树里 ``.git`` 是个
    指向 ``<repo>/.git/worktrees/<name>`` 的**文件**，认领信息要落在那个私有目录里
    ——它是按工作树分的，跨进程共享，且不进工作树（写进去会让 ``git status`` 多
    一个未跟踪文件，我们自己的 remove 闸门会因此拒绝删除）。
    """
    result = _run_git(["rev-parse", "--absolute-git-dir"], root)
    if result.code != 0:
        return None
    first = next((line.strip() for line in result.out.splitlines() if line.strip()), "")
    if not first:
        return None
    return Path(first) / OWNER_FILENAME


def read_owner(root: Path) -> dict:
    """读当前认领。读不到/形状不对一律当"没有认领"——脏文件不该挡住干活。"""
    path = _owner_file(root)
    if path is None:
        return {}
    return _read_owner_file(path)


def _baseline_file(root: Path, owner_path: Path | None = None) -> Path | None:
    """基准线记录的位置：与认领文件同一个 git 目录（按工作树分、不进工作树）。"""
    anchor = owner_path or _owner_file(root)
    if anchor is None:
        return None
    return anchor.parent / BASELINE_FILENAME


def _read_owner_file(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        "task_id": str(payload.get("task_id") or ""),
        "pid": int(payload.get("pid") or 0),
        "claim_id": str(payload.get("claim_id") or ""),
        "at": str(payload.get("at") or ""),
        # 老版本写下的认领没有 heartbeat_at：退回 at 当心跳，租约照常算得出。
        "heartbeat_at": str(payload.get("heartbeat_at") or payload.get("at") or ""),
        # 基准线：老记录没有这个键，回滚时会退回 blank-baseline.json。
        "baseline": str(payload.get("baseline") or ""),
    }


def _parse_stamp(raw: str) -> datetime | None:
    """认领里的时间戳。认不出就返回 None（= 不知道年龄，按"还新鲜"处理）。"""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # 只有旧记录可能没有时区；项目里写的一律是 UTC。
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _lease_age(record: dict, *, now: datetime | None = None) -> float | None:
    """距上次心跳多少秒。None = 认不出时间戳（不能据此判失效）。"""
    stamp = _parse_stamp(str(record.get("heartbeat_at") or record.get("at") or ""))
    if stamp is None:
        return None
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current - stamp).total_seconds())


def _pid_alive(pid: int) -> bool | None:
    """进程还在不在。``None`` 表示判断不了——那不等于"活着"，也不等于"死了"。

    Windows 上 ``os.kill(pid, 0)`` 会把 0 当成 CTRL_C_EVENT 发给目标进程（真能把
    人家打断），所以按平台分开：Windows 用 ``OpenProcess`` 拿句柄，
    拿得到就是活着；``ERROR_INVALID_PARAMETER``（87）说明 pid 不存在。
    权限不足（5）说明进程存在但归别人——那也是活着。
    """
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        # 259 = STILL_ACTIVE。退出码恰好是 259 的进程会被误判成活着，这种情况
        # 只会让它多占一会儿树（保守拒绝），比反过来放两个写者进去便宜。
        still_active = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
        if not handle:
            error = int(kernel32.GetLastError())
            if error == 87:  # ERROR_INVALID_PARAMETER：没有这个 pid
                return False
            if error == 5:  # ERROR_ACCESS_DENIED：有，但不归我管
                return True
            return None
        try:
            # 拿到句柄不等于进程还在：Windows 的进程对象要等所有句柄关掉才消失，
            # 一个已经崩溃、但还有别的进程握着句柄的 pid 会被 OpenProcess 打开。
            # 所以再看退出码——这一层是"崩溃留下的锁能不能收回"的关键。
            code = ctypes.c_ulong()
            got = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            if got and int(code.value) != still_active:
                return False
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _still_held(current: dict, *, now: datetime | None = None) -> bool:
    """别人留下的认领还算不算"有人在用"。

    同进程以内存会话为准（比文件可靠）；跨进程先看 pid 死活，再用租约兜底：
    进程已经不在 → 立刻可以接手，不用等租约；活着或判断不了 → 新鲜就拒绝、
    过期才接手。判断不了且没有时间戳时保守拒绝——放行的代价是两个写者。
    """
    pid = int(current.get("pid") or 0)
    if pid == os.getpid():
        return str(current.get("task_id") or "") in _SESSIONS
    alive = _pid_alive(pid)
    if alive is False:
        return False
    age = _lease_age(current, now=now)
    if age is None:
        return True
    return age <= LEASE_SECONDS


def _write_owner(path: Path, record: dict) -> bool:
    """先写临时文件再替换：跨进程读到半个 JSON 会让另一个任务误判成"没有认领"。"""
    try:
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
    except OSError:
        return False
    return True


def claim_owner(
    root: Path, task_id: str, *, path: Path | None = None, baseline: str = ""
) -> tuple[str, dict]:
    """认领这棵工作树，返回 ``(状态, 认领信息)``。

    四种结果，调用方按它决定"要不要拒绝"：

    * ``owned``：本任务拿到了（或本来就持有）；
    * ``reclaimed``：上一个持有者已经不在（进程没了，或超过租约没续）——文件还在、
      人没了，说明它没走到正常释放（崩溃、被杀、异常退出），可以接手；
    * ``owned_by_another_task``：另一个还在干活的持有者，拒绝进入；
    * ``unavailable``：读不到 git 目录或写不进去。**不能当成拿到了**——那样
      等于静默放弃唯一性；调用方要么拒绝，要么如实标注"没守住"。
    """
    if path is None:
        path = _owner_file(root)
    if path is None:
        return OWNER_UNAVAILABLE, {}
    current = _read_owner_file(path)
    reclaimed = False
    if current and current.get("task_id") != task_id:
        if _still_held(current):
            return OWNER_OTHER, current
        reclaimed = True
    stamp = datetime.now(timezone.utc).isoformat()
    record = {
        "task_id": str(task_id),
        "pid": os.getpid(),
        "claim_id": uuid.uuid4().hex[:12],
        "at": stamp,
        "heartbeat_at": stamp,
    }
    # 基准线写进认领记录（用户要求"进 blank-owner.json"）：它同时说明"这个任务从哪一版
    # 开始改的"，审计时不用再去找第二个文件。持久的副本另存在 blank-baseline.json。
    if baseline:
        record["baseline"] = str(baseline)
    if not _write_owner(path, record):
        return OWNER_UNAVAILABLE, current
    return (OWNER_RECLAIMED if reclaimed else OWNER_OWNED), record


def renew_owner(
    root: Path, task_id: str, *, path: Path | None = None, interval: float = RENEW_INTERVAL
) -> bool:
    """给认领续一口气。只续自己的；别人的认领一个字节都不动。

    节流到 ``interval`` 秒：每次工具调用都写盘没有收益，而写盘本身会失败
    （目录只读、文件被占用），失败一次不该让工具调用变红。
    """
    if path is None:
        path = _owner_file(root)
    if path is None:
        return False
    current = _read_owner_file(path)
    if current.get("task_id") != task_id:
        return False
    age = _lease_age(current)
    if age is not None and age < interval:
        return True
    stamp = datetime.now(timezone.utc).isoformat()
    record = {**current, "pid": os.getpid(), "heartbeat_at": stamp}
    record.setdefault("at", stamp)
    return _write_owner(path, record)


def release_owner(root: Path, task_id: str, *, path: Path | None = None) -> None:
    """松手。只有持有者能删——别的任务的认领不能被路过的人清掉。"""
    if path is None:
        path = _owner_file(root)
        if path is None:
            return
    current = _read_owner_file(path)
    if not current or current.get("task_id") != task_id:
        return
    try:
        path.unlink()
    except OSError:
        # 删不掉不影响正确性：下次认领会看到"持有者在自己进程里但会话已没了"，
        # 按 reclaimed 接手。
        pass


# ── 基准线与回滚 ────────────────────────────────────────────────────────
# 隔离做得再好，也只解决"别把主工作区改坏"；它不回答"改错了怎么退回去"。
# 提交之后发现问题，靠人手动 revert 是唯一出路——而人往往不记得这一轮到底碰过
# 哪几个文件。所以进树时记一次基准（sha + 当时的未跟踪文件集），回滚按这份记录算
# 改动集合：工具改的、shell 改的、提交过的都算，因为它们都相对同一个基准。


@dataclass
class RestoreReport:
    """回滚结果。``clean`` 是回滚后的实测值，不是"我以为干净了"。"""

    root: Path
    baseline: str
    changed: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    commits_dropped: int = 0
    dirty_after: list[str] = field(default_factory=list)
    revision: str = ""

    @property
    def clean(self) -> bool:
        return not self.dirty_after

    def as_dict(self) -> dict:
        return {
            "root": str(self.root),
            "baseline": self.baseline,
            "changed": list(self.changed),
            "restored": list(self.restored),
            "removed": list(self.removed),
            "commits_dropped": int(self.commits_dropped),
            "dirty_after": list(self.dirty_after),
            "revision": self.revision,
            "clean": self.clean,
        }


def write_baseline(
    root: Path,
    task_id: str,
    *,
    baseline: str,
    base_ref: str = "",
    branch: str = "",
    untracked: list[str] | None = None,
    path: Path | None = None,
) -> Path | None:
    """记一次基准线。写不进去返回 None（调用方要如实说"回滚没底"）。

    ``untracked`` 是进树那一刻就已经存在的未跟踪文件。回滚只删"基准之后新增的"，
    不碰它们——path 进入的树里可能有别人没提交的草稿，那不是本任务改的。
    """
    target = _baseline_file(root, path)
    if target is None:
        return None
    record = {
        "task_id": str(task_id),
        "baseline": str(baseline),
        "base_ref": str(base_ref or ""),
        "branch": str(branch or ""),
        "untracked": sorted(str(name) for name in (untracked or [])),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return target if _write_owner(target, record) else None


def read_baseline(root: Path, *, path: Path | None = None) -> dict:
    """读基准线。没有记录返回空 dict——"不知道基准"必须与"基准就是当前"分开。"""
    target = _baseline_file(root, path)
    if target is None or not target.is_file():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        "task_id": str(payload.get("task_id") or ""),
        "baseline": str(payload.get("baseline") or ""),
        "base_ref": str(payload.get("base_ref") or ""),
        "branch": str(payload.get("branch") or ""),
        "untracked": [str(name) for name in (payload.get("untracked") or []) if str(name).strip()],
        "at": str(payload.get("at") or ""),
    }


def _status_entries(path: Path) -> list[tuple[str, str]]:
    """``(状态码, 相对路径)``。未跟踪与已暂存新增分开给：前者要删，后者要 checkout。"""
    result = _run_git(["status", "--porcelain", "-z", "-uall"], path)
    if result.code != 0:
        return []
    parts = result.out.split("\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(parts):
        entry = parts[index]
        index += 1
        if len(entry) < 4:
            continue
        status, relative = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            index += 1
        entries.append((status, relative))
    return entries


def _untracked_entries(path: Path) -> list[str]:
    return [name for status, name in _status_entries(path) if status == "??"]


def changed_paths(root: Path, baseline: str, *, preexisting: Iterable[str] = ()) -> dict:
    """相对基准改动的路径集合：已跟踪的（改/删/新增并暂存）+ 基准之后新建的未跟踪文件。

    ``git diff`` 只看已跟踪的；shell 里新建的文件只能从 status 里拿。两路都要，
    否则"回滚漏掉新建文件"——那恰好是最常见的改法（加一个模块）。
    """
    if not baseline:
        return {"tracked": [], "added": [], "all": []}
    tracked: list[str] = []
    for args in (
        ["diff", "--name-only", "--no-renames", baseline, "--"],
        ["diff", "--name-only", "--no-renames", "--cached", baseline, "--"],
    ):
        result = _run_git(args, root)
        if result.code != 0:
            continue
        for line in result.out.splitlines():
            name = line.strip()
            if name and name not in tracked:
                tracked.append(name)
    known = {str(name) for name in preexisting}
    added = [name for name in _untracked_entries(root) if name not in known]
    union: list[str] = []
    for name in [*tracked, *added]:
        if name not in union:
            union.append(name)
    return {"tracked": tracked, "added": added, "all": union}


def _safe_relative(root: Path, names: Iterable[str]) -> tuple[list[str], list[str]]:
    """把调用方给的路径折成工作树内相对路径；越界的单独返回（调用方拒绝）。"""
    inside: list[str] = []
    outside: list[str] = []
    resolver = root.resolve()
    for raw in names:
        text = str(raw or "").strip().replace("\\", "/")
        if not text:
            continue
        candidate = Path(text)
        if candidate.is_absolute():
            try:
                text = candidate.resolve().relative_to(resolver).as_posix()
            except (OSError, ValueError):
                outside.append(str(raw))
                continue
        normalized = os.path.normpath(text).replace("\\", "/")
        if normalized.startswith("../") or normalized == ".." or normalized.startswith("/"):
            outside.append(str(raw))
            continue
        inside.append(normalized)
    return inside, outside


def _restore_target(
    task_id: str, *,
    name: str = "",
    search: Path | str | None = None,
) -> tuple[Path, str, dict, dict]:
    """定位要回滚的树与它的基准，顺带查一遍归属。

    返回 ``(root, baseline, baseline_record, owner)``。三种拒绝集中在这里，
    免得 plan 与 restore 两边各写一份、再走偏成"plan 也悄悄回滚了"。
    """
    key = str(task_id or "")
    session = get(key)
    root: Path | None = session.root if session is not None else None
    if root is None:
        label = str(name or "").strip()
        if not label:
            raise ValueError(
                "当前不在 worktree 会话里。请传 worktree 名字（.blank-worktrees/ 下的目录名）"
                "指定要回滚哪一棵树。"
            )
        base = Path(search).resolve() if search is not None else Path.cwd()
        # 用拥有它的主仓库当搜索根：search 很可能本身就是某棵工作树（调用方在树里），
        # 直接拼 .blank-worktrees/ 会得到"树里套树"的路径。
        repo = _owner_repo(base) or _repo_of(base)
        if repo is None:
            raise ValueError(f"找不到工作树「{label}」所属的仓库。")
        candidate = repo / WORKTREE_DIRNAME / _validate_name(label)
        if not candidate.is_dir():
            raise ValueError(f"工作树「{label}」不存在：{candidate}")
        root = candidate

    owner = read_owner(root)
    owner_task = str(owner.get("task_id") or "")
    if owner_task and owner_task != key and _still_held(owner):
        raise ValueError(
            f"这棵 worktree 现在归任务 {owner_task[:8]} 使用，不能回滚："
            "回滚会把对方正在写的改动一起退掉。等那个任务结束，或换一棵树。"
        )

    record = read_baseline(root)
    baseline = str(record.get("baseline") or owner.get("baseline") or "")
    if not baseline:
        raise ValueError(
            "这棵工作树没有基准线记录（可能是老版本建的树，或者手动 git worktree add 的）。"
            "不知道退到哪一版时不做回滚——请手动 git reset --hard <目标提交>。"
        )
    return root, baseline, record, owner


def plan_restore(
    task_id: str, *,
    name: str = "",
    search: Path | str | None = None,
) -> RestoreReport:
    """只算"相对基准改了哪些路径"，一个字节都不动。

    与 ``restore`` 共用定位与拒绝逻辑：两边口径一旦不一致，"先看一眼再决定"就没意义了。
    """
    root, baseline, record, _owner = _restore_target(task_id, name=name, search=search)
    facts = changed_paths(root, baseline, preexisting=record.get("untracked") or [])
    report = RestoreReport(
        root=root,
        baseline=baseline,
        changed=list(facts["all"]),
        revision=_head_commit(root),
    )
    return report


def restore(
    task_id: str,
    *,
    paths: Iterable[str] | None = None,
    name: str = "",
    search: Path | str | None = None,
) -> RestoreReport:
    """把工作树回滚到基准线：整棵回滚（不传 paths）或只回滚指定路径。

    拒绝的三种情况都要说清，不能默默跳过：

    * 这棵树正被**另一个还活着的任务**持有——回滚等于把对方正在写的东西抽走；
    * 没有基准线记录（老树、手动建的树）——不知道退到哪一版，猜一个比拒绝更糟；
    * 路径越出工作树根——那是别的任务的地盘。

    整棵回滚会 ``git reset --hard <baseline>``：已提交的改动也一并退掉（这正是
    "已提交的改动没有回去的路"要修的那条），所以它必须是显式请求，不能顺手做。
    """
    root, baseline, record, _owner = _restore_target(task_id, name=name, search=search)
    facts = changed_paths(root, baseline, preexisting=record.get("untracked") or [])
    selected = list(facts["all"])
    if paths is not None:
        wanted, outside = _safe_relative(root, paths)
        if outside:
            raise ValueError(
                "这些路径不在工作树里，拒绝回滚：" + "、".join(outside[:5])
            )
        selected = [name for name in selected if name in set(wanted)]
        missing = [name for name in wanted if name not in set(facts["all"])]
        if missing:
            raise ValueError(
                "这些路径相对基准没有改动，无需回滚：" + "、".join(missing[:5])
            )

    report = RestoreReport(
        root=root,
        baseline=baseline,
        changed=list(selected),
        revision=_head_commit(root),
    )
    if not selected:
        report.dirty_after = _untracked_entries(root) + [
            name for status, name in _status_entries(root) if status != "??"
        ]
        report.revision = _head_commit(root)
        return report

    restored: list[str] = []
    removed: list[str] = []
    whole = paths is None
    ahead = _commits_ahead(root, baseline)
    if whole and ahead:
        # 已提交的改动：只有整棵回滚才动历史。--hard 同时清掉工作区与索引，
        # 之后再把"基准之后新建的文件"删掉（reset --hard 不删未跟踪文件）。
        dropped = _run_git(["reset", "--hard", baseline], root)
        if dropped.code != 0:
            raise ValueError(f"回滚失败：{_error_text(dropped, 'git reset --hard 返回非零退出码')}")
        report.commits_dropped = ahead

    for name in selected:
        if name in facts["added"]:
            # 基准里没有这个文件：回滚的正确形态是"不存在"，不是"恢复成空文件"。
            target = (root / name).resolve()
            try:
                target.relative_to(root.resolve())
            except ValueError:
                continue
            try:
                if target.is_file() or target.is_symlink():
                    target.unlink()
                    removed.append(name)
            except OSError:
                continue
            continue
        result = _run_git(["checkout", baseline, "--", name], root)
        if result.code == 0:
            restored.append(name)
        else:
            raise ValueError(f"回滚 {name} 失败：{_error_text(result, 'git checkout 返回非零退出码')}")

    report.restored = restored
    report.removed = removed
    remaining = _status_entries(root)
    # 基准之前就存在的未跟踪文件不算脏：它们不是本任务改的，回滚不该动它们。
    known = set(record.get("untracked") or [])
    report.dirty_after = [name for _status, name in remaining if name not in known]
    report.revision = _head_commit(root)
    return report


def _error_text(result: _GitResult, fallback: str) -> str:
    text = (result.err or result.out or "").strip()
    if not text:
        return fallback
    first = next((line.strip() for line in text.splitlines() if line.strip()), text)
    return first[:300]


def _same(left: Path | str, right: Path | str) -> bool:
    def norm(value: Path | str) -> str:
        return os.path.normcase(os.path.realpath(str(value)))

    return norm(left) == norm(right)


def _repo_of(path: Path) -> Path | None:
    result = _run_git(["rev-parse", "--show-toplevel"], path)
    if result.code != 0:
        return None
    lines = [line.strip() for line in result.out.splitlines() if line.strip()]
    return Path(lines[-1]) if lines else None


def _owner_repo(path: Path) -> Path | None:
    """工作树自己 ``rev-parse --show-toplevel`` 只返回它自己；要找的是拥有它的主仓库。"""
    result = _run_git(["rev-parse", "--git-common-dir"], path)
    if result.code != 0 or not result.out.strip():
        return _repo_of(path)
    raw = result.out.strip()
    common = Path(raw) if Path(raw).is_absolute() else (path / raw)
    try:
        common = common.resolve()
    except OSError:
        return _repo_of(path)
    if common.name == ".git":
        return common.parent
    return _repo_of(path)


def _worktree_paths(repo: Path) -> list[Path]:
    # core.quotePath=false：中文路径原样输出，否则 porcelain 会给出 C 风格转义。
    result = _run_git(["-c", "core.quotePath=false", "worktree", "list", "--porcelain"], repo)
    if result.code != 0:
        return []
    paths: list[Path] = []
    for line in result.out.splitlines():
        if line.startswith("worktree "):
            paths.append(Path(line[len("worktree ") :].strip()))
    return paths


def _current_branch(path: Path) -> str:
    result = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], path)
    branch = result.out.strip() if result.code == 0 else ""
    # 游离 HEAD 的 abbrev-ref 是字面量 HEAD，不是分支名。
    return "" if branch == "HEAD" else branch


def _head_commit(repo: Path) -> str:
    result = _run_git(["rev-parse", "HEAD"], repo)
    return result.out.strip() if result.code == 0 else ""


def _ref_commit(repo: Path, ref: str) -> str:
    result = _run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], repo)
    return result.out.strip() if result.code == 0 else ""


def _default_branch(repo: Path) -> str:
    """远端默认分支：origin/HEAD 的符号引用 → main/master → 按 main 试一次。"""
    result = _run_git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], repo)
    if result.code == 0:
        name = result.out.strip()
        if name.startswith("origin/"):
            name = name[len("origin/") :]
        if name and _ref_commit(repo, f"refs/remotes/origin/{name}"):
            return name
    for name in DEFAULT_BRANCH_CANDIDATES:
        if _ref_commit(repo, f"refs/remotes/origin/{name}"):
            return name
    return "main"


def _resolve_base(repo: Path, base_ref: str) -> tuple[str, str]:
    """返回 (commit, 可读基准名)。

    fresh 只读已有的远端跟踪分支：本机不在建树路径上发起 ``git fetch``，网络出口
    统一归沙箱策略管。远端跟踪分支不存在时回落本地 HEAD（与上游 fetch 失败后的
    回落一致），基准名跟着实际用的那条线走。
    """
    if base_ref == "fresh":
        branch = _default_branch(repo)
        commit = _ref_commit(repo, f"refs/remotes/origin/{branch}")
        if commit:
            return commit, f"origin/{branch}"
    return _head_commit(repo), "HEAD"


def _ensure_excluded(repo: Path) -> None:
    """把工作树目录写进本仓库的 info/exclude。

    放进仓库里是为了让用户找得到；不写进 .gitignore（那是用户要提交的文件），
    只写本地的 info/exclude，`git status` 才不会把整棵树当成未跟踪目录。
    """
    result = _run_git(["rev-parse", "--git-path", "info/exclude"], repo)
    if result.code != 0 or not result.out.strip():
        return
    raw = result.out.strip()
    exclude = Path(raw) if Path(raw).is_absolute() else repo / raw
    try:
        existing = exclude.read_text(encoding="utf-8", errors="replace")
    except OSError:
        existing = ""
    if any(line.strip() == "/.blank-worktrees/" for line in existing.splitlines()):
        return
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        suffix = "" if not existing or existing.endswith("\n") else "\n"
        exclude.write_text(
            f"{existing}{suffix}# Blank agent worktrees\n/.blank-worktrees/\n", encoding="utf-8"
        )
    except OSError:
        # 写不进去不影响工作树本身；最坏的结果只是 git status 多一行未跟踪目录。
        return


def _dirty_entries(path: Path) -> list[str]:
    """未提交改动（含未跟踪文件），逐个文件列出；读不到就返回空。"""
    result = _run_git(["status", "--porcelain", "-z", "-uall"], path)
    if result.code != 0:
        return []
    parts = result.out.split("\0")
    entries: list[str] = []
    index = 0
    while index < len(parts):
        entry = parts[index]
        index += 1
        if len(entry) < 4:
            continue
        status, relative = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            index += 1
        entries.append(relative)
    return entries


def _commits_ahead(path: Path, base: str) -> int:
    if not base:
        return 0
    result = _run_git(["rev-list", "--count", f"{base}..HEAD"], path)
    if result.code != 0:
        return 0
    try:
        return int(result.out.strip() or 0)
    except ValueError:
        return 0


def get(task_id: str) -> WorktreeSession | None:
    with _LOCK:
        return _SESSIONS.get(str(task_id or ""))


def active_root(task_id: str) -> Path | None:
    """任务当前的根；没有会话或目录已经不在时返回 None（调用方退回原工作区）。

    顺便续一次租约：这个函数就是"这个任务正要在这棵树里干活"的信号，比任何
    定时器都准。不续的话，一个跑得久但一直在干活的进程会被当成失效而被人接手。
    """
    session = get(task_id)
    if session is None:
        return None
    if not session.root.is_dir():
        release(task_id)
        return None
    renew_owner(session.root, task_id, path=session.owner_file)
    return session.root


def release(task_id: str) -> None:
    """任务结束：摘记录、松开写者认领，工作树留在盘上。

    松手要在这里做而不是只在 exit：任务收尾、取消、崩溃恢复都会走到这儿，
    漏了它，那棵树的认领会一直挂着"别人的任务"，下一个任务永远进不去
    （文件里的 pid 是同一个进程，但会话已经没了 → 会被判 reclaimed，能用，
    但那是在靠自愈逻辑兜底，不该当成正常路径）。
    """
    key = str(task_id or "")
    with _LOCK:
        session = _SESSIONS.pop(key, None)
    if session is not None:
        release_owner(session.root, key, path=session.owner_file)


def _validate_name(name: str) -> str:
    text = (name or "").strip()
    if text in (".", ".."):
        raise ValueError("worktree 名字不能是 . 或 ..")
    if not NAME_PATTERN.match(text):
        raise ValueError(
            "worktree 名字只能用字母、数字、点、下划线、短横线，最多 64 个字符；"
            "不能带路径分隔符。"
        )
    return text


def _enter_by_path(root: Path, task_id: str, raw_path: str, current: WorktreeSession | None) -> WorktreeSession:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    if not candidate.is_dir():
        raise ValueError(f"worktree 路径不存在：{raw_path}")
    if _same(candidate, root):
        raise ValueError("path 指向的就是当前工作区，不需要进入 worktree。")

    repo = _owner_repo(candidate)
    if repo is None:
        raise ValueError(f"{raw_path} 不在任何 git 仓库里，不是可用的 worktree。")
    if not any(_same(candidate, item) for item in _worktree_paths(repo)):
        raise ValueError(
            f"{raw_path} 不在 git worktree list 里。path 只能进入已经注册过的工作树"
            "（先用 git worktree add 建好，或者用 enter_worktree 的 name 参数新建）。"
        )

    root_repo = _repo_of(root)
    accepted = (root_repo is not None and _same(repo, root_repo)) or (
        root in repo.parents or _same(repo, root)
    )
    if not accepted:
        raise ValueError(
            f"{raw_path} 属于 {repo}，既不是当前工作区所在仓库，也不是工作区里嵌套的仓库。"
        )

    if current is not None and _same(current.root, candidate):
        raise ValueError(f"已经在这个 worktree 里：{current.display}")

    origin = current.origin if current is not None else root
    return WorktreeSession(
        task_id=task_id,
        name=candidate.name or "worktree",
        root=candidate,
        origin=origin,
        repo=repo,
        branch=_current_branch(candidate),
        base_commit="",
        base_ref="",
        owned=False,
    )


def _create(
    root: Path, task_id: str, raw_name: str, *, base_ref: str = "fresh"
) -> WorktreeSession:
    name = _validate_name(raw_name) if raw_name else f"wt-{uuid.uuid4().hex[:8]}"
    repo = _repo_of(root)
    if repo is None:
        raise ValueError(
            "当前工作区不在 git 仓库里，无法创建 worktree。"
            "EnterWorktree 需要 git（或者先在工作区里 git init 并提交一次）。"
        )
    base, effective_ref = _resolve_base(repo, base_ref)
    if not base:
        raise ValueError(
            f"仓库还没有任何提交，无法从 {effective_ref} 创建工作树；先提交一次再试。"
        )

    branch = BRANCH_PREFIX + name
    verify = _run_git(["rev-parse", "--verify", f"refs/heads/{branch}"], repo)
    if verify.code == 0:
        raise ValueError(f"分支 {branch} 已经存在；换一个 worktree 名字，或先处理那个分支。")

    target = repo / WORKTREE_DIRNAME / name
    if target.exists():
        raise ValueError(
            f"目录 {WORKTREE_DIRNAME}/{name} 已经存在；换一个名字，或先移除那棵工作树。"
        )

    _ensure_excluded(repo)
    result = _run_git(["worktree", "add", "-b", branch, str(target), base], repo)
    if result.code != 0 or not target.is_dir():
        raise ValueError(f"创建 worktree 失败：{_error_text(result, 'git worktree add 返回非零退出码')}")
    return WorktreeSession(
        task_id=task_id,
        name=name,
        root=target,
        origin=root,
        repo=repo,
        branch=branch,
        base_commit=base,
        base_ref=effective_ref,
        owned=True,
    )


def enter(
    root: Path | str,
    task_id: str,
    *,
    name: str = "",
    path: str = "",
    base_ref: str = "fresh",
) -> WorktreeSession:
    """建新工作树或进入已有的；失败抛 ValueError（消息直接回给模型）。"""
    task_id = str(task_id or "")
    if not task_id:
        raise ValueError("缺少任务 id，无法记录 worktree 会话")
    if name and path:
        raise ValueError("name 与 path 只能给一个：name 新建工作树，path 进入已有的。")
    base_ref = str(base_ref or "").strip().lower()
    if base_ref not in BASE_REFS:
        base_ref = BASE_REFS[0]

    origin_root = Path(root).resolve()
    if not origin_root.is_dir():
        raise ValueError("当前工作区不存在，无法进入 worktree。")

    current = get(task_id)
    if current is not None and not current.root.is_dir():
        # 目录被外部删了：记录已经失效，按"不在会话里"处理，而不是让后续写入落空。
        release(task_id)
        current = None

    if path:
        session = _enter_by_path(origin_root, task_id, path, current)
    else:
        if current is not None:
            raise ValueError(
                f"已经在 worktree「{current.name}」里。要换一棵已有的树请用 path；"
                "要新建请先 exit_worktree。"
            )
        session = _create(origin_root, task_id, name, base_ref=base_ref)

    # 认领必须在记账之前：拿不到独占就不要把这个任务记成"在这棵树里"，
    # 否则它后续的写入会绕过下面 exit 时的归属检查。
    owner_path = _owner_file(session.root)
    # 基准线：新建的树用建树时那条基准；path 进入的树用进树那一刻的 HEAD。
    # 没有它就无从回答"这一轮相对哪一版改了什么"，回滚也就只能靠人。
    baseline = session.base_commit or _head_commit(session.root)
    preexisting = _untracked_entries(session.root)
    status, owner = claim_owner(session.root, task_id, path=owner_path, baseline=baseline)
    session.owner_status = status
    session.owner_file = owner_path
    if status == OWNER_OTHER:
        raise ValueError(
            f"这个 worktree（{session.display}）正被任务 {str(owner.get('task_id') or '')[:8]} "
            "使用，同一棵工作树不能有两个写者：两边会互相覆盖对方的改动，"
            "最后的验证结论对谁都不成立。请用 name 新建一棵，或等那个任务结束。"
        )
    if baseline:
        session.base_commit = baseline
        write_baseline(
            session.root,
            task_id,
            baseline=baseline,
            base_ref=session.base_ref,
            branch=session.branch,
            untracked=preexisting,
            path=owner_path,
        )

    with _LOCK:
        _SESSIONS[task_id] = session
    return session


def exit_session(task_id: str, *, action: str, discard_changes: bool = False) -> ExitReport:
    """退出当前工作树。``remove`` 遇到未提交改动/未合并提交时抛 ValueError。"""
    action = (action or "").strip()
    if action not in ("keep", "remove"):
        raise ValueError("action 只能是 keep 或 remove")
    session = get(task_id)
    if session is None:
        return ExitReport(session=None)

    if action == "keep":
        release(task_id)
        return ExitReport(session=session)

    if not session.owned:
        raise ValueError(
            f"这个 worktree（{session.name}）是通过 path 进入的，不是本任务创建的，不能 remove。"
            '用 action="keep" 退出；确实要删请手动执行 git worktree remove。'
        )

    dirty = _dirty_entries(session.root)
    ahead = _commits_ahead(session.root, session.base_commit)
    if (dirty or ahead) and not discard_changes:
        parts: list[str] = []
        if dirty:
            shown = dirty[:MAX_REPORTED_ENTRIES]
            more = "" if len(dirty) == len(shown) else f"…（共 {len(dirty)} 个）"
            parts.append(f"{len(dirty)} 个文件有未提交改动：{'、'.join(shown)}{more}")
        if ahead:
            parts.append(f"分支上有 {ahead} 个未合并的提交")
        raise ValueError(
            "worktree 里还有工作没落地，拒绝删除：" + "；".join(parts)
            + "。确认丢弃请传 discard_changes=true，或者先提交/合并。"
        )

    # 删除前的归属检查：本任务没持有这棵树的认领时，说明是别人在写——
    # 这时候删掉等于把对方正在做的工作从盘上抽走（它下一次写入会落到空路径）。
    # 只在"有认领且不是自己"时拦；没有认领文件（手动建的树、老版本留下的）照旧处理。
    owner = read_owner(session.root)
    owner_task = str(owner.get("task_id") or "")
    if owner_task and owner_task != task_id:
        raise ValueError(
            f"这棵 worktree 现在归任务 {owner_task[:8]} 使用，不能由本任务删除。"
            '先用 action="keep" 退出；确实要删请等那个任务结束。'
        )

    result = _run_git(["worktree", "remove", "--force", str(session.root)], session.repo)
    if result.code != 0 or session.root.exists():
        raise ValueError(
            f"删除 worktree 失败：{_error_text(result, 'git worktree remove 返回非零退出码')}。"
            '可以先 exit_worktree(action="keep") 保留它。'
        )

    branch_deleted = False
    branch_error = ""
    if session.branch:
        deleted = _run_git(["branch", "-D", session.branch], session.repo)
        branch_deleted = deleted.code == 0
        if not branch_deleted:
            branch_error = _error_text(deleted, "git branch -D 返回非零退出码")
    release(task_id)
    return ExitReport(
        session=session,
        removed=True,
        branch_deleted=branch_deleted,
        branch_error=branch_error,
    )
