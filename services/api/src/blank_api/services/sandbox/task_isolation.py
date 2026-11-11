"""任务级隔离：每个任务一个独立可写根，一个只属于它的 AppContainer 包。

为什么不是"路径闸门再写严一点"：路径闸门拦的是字符串。同一个 OS 用户下的另一个进程
（另一个任务）拿到绝对路径就能直接 `open()`——J3 的判据明确把这种形状算红。要挡住它，
只能让两个任务在内核眼里是**两个身份**：每个任务一个 AppContainer 包（零能力），包 SID
只在自己任务根上拿 ACE。实测（`E:/tmp/j3-probe/`）：

- 两个随机包、两个私有根：A 写标记成功，B 用绝对路径与 `..\\A\\marker.txt` 都得到
  `PermissionError:13`，宿主令牌读得到（对照）；
- 根是**带文件的副本**时同样成立（不是只有空目录干净）；
- 用 `git clone --shared` 而不是 worktree：worktree 的 `git add` 会写共享对象库，
  克隆副本的写入落在自己的 `.git`，主仓库对象库文件数不变。

开关默认关：它会改变"任务在哪干活"（在副本里），只给需要并行隔离的部署打开。
打开后如果 AppContainer 起不来（非 Windows / 非管理员），**整任务拒绝**，不降级成
"看起来隔离了"。打开 `BLANK_TASK_DISK_MB` 时任务根落在一个固定容量 VHDX 卷上，
写满由内核返回 ENOSPC——这条与临时区容量配额共用 `windows_volume` 的同一套挂载。
"""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import os
import shutil
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from blank_api.services import worktree
from blank_api.services.sandbox import windows_appcontainer as wac
from blank_api.services.sandbox import windows_restricted as wr
from blank_api.services.sandbox import windows_volume

ENV_FLAG = "BLANK_TASK_ISOLATION"
ENV_DISK_MB = "BLANK_TASK_DISK_MB"
PROFILE_PREFIX = "BlankTask"


class TaskIsolationError(RuntimeError):
    """隔离根建不起来，或建出来的边界不完整。调用方必须拒绝任务，不许退回共享根。"""


def enabled() -> bool:
    return str(os.environ.get(ENV_FLAG, "")).strip().lower() in {"1", "true", "yes"}


def disk_mb() -> int:
    from blank_api.services.sandbox.policy import _parse_mb

    return _parse_mb(os.environ.get(ENV_DISK_MB, "0"))


def profile_name(key: str) -> str:
    """按任务键派生**稳定**的包名：同一任务重跑落到同一个包，不同任务必不相同。

    稳定是必须的：包与 ACE 都是持久对象，名字随机会让每次续跑都新建一个包，
    旧包连同它拿到的 ACE 一起留在机器上。
    """
    digest = hashlib.sha256(str(key or "").encode("utf-8")).hexdigest()[:12]
    return f"{PROFILE_PREFIX}-{digest}"


@dataclass
class TaskRoot:
    task_id: str
    path: Path
    workspace: Path
    tree: worktree.Worktree
    profile_name: str
    sid: str
    mode: str
    vhdx: str = ""
    mount: str = ""
    disk_mb: int = 0
    notes: list[str] = field(default_factory=list)
    baseline_error: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "profile": self.profile_name,
            "root": str(self.path),
            "workspace": str(self.workspace),
            "disk_mb": self.disk_mb,
            "isolated": True,
            "notes": list(self.notes),
            "baseline_error": self.baseline_error,
        }


_LIVE: dict[str, TaskRoot] = {}
_LIVE_LOCK = threading.RLock()


def get(task_id: str) -> TaskRoot | None:
    with _LIVE_LOCK:
        return _LIVE.get(task_id)


def secure_tree(
    tree: worktree.Worktree, key: str, *, source: str | Path | None = None
) -> tuple[str, str]:
    """把副本根收紧成"宿主自己 + 本任务包"两方可见，并返回 (包名, SID)。

    `_set_private` 去掉继承来的 Users/Everyone ACE；`_grant` 再给本任务包加一条
    可继承的 M。实测：先有文件的嵌套副本在这两步之后，另一个包连绝对路径都读不到。

    `source` 是克隆来源仓库：`clone --shared` 通过 alternates 只读借用它的对象库，
    不单独授权时任务里的 git 会以「拒绝访问。」失败（probe_clone.py 实测）。所以把
    源仓库的对象库（只这一个子树）以 RX 授给本任务包——它本来就只该读。
    """
    name = profile_name(key)
    sid, _ = wac.profile(name)
    try:
        owner = wr.current_user_sid()
    except wr.TokenError as exc:
        raise TaskIsolationError(f"取当前用户 SID 失败，无法收紧任务根：{exc}") from exc
    try:
        wr._set_private(str(tree.path), owner)
    except wr.RestrictedUnavailable as exc:
        raise TaskIsolationError(f"任务根 ACL 收紧失败：{exc}") from exc
    ok, detail = wr._grant(str(tree.path), "M", sid)
    if not ok:
        raise TaskIsolationError(f"任务根授权失败：{detail}")
    if source is not None and tree.mode == "git":
        objects = _source_objects(Path(source))
        if objects is not None and objects.is_dir():
            ok, detail = wr._grant(str(objects), "RX", sid, recursive=True)
            if not ok:
                raise TaskIsolationError(f"源仓库对象库只读授权失败：{detail}")
    return name, sid


def _source_objects(base: Path) -> Path | None:
    """源仓库真正的对象库路径。worktree/子模块的 `.git` 是文件，不能直接拼 `.git/objects`。"""
    code, output = worktree._run_git(["rev-parse", "--git-path", "objects"], base)
    if code != 0 or not output.strip():
        return None
    line = output.strip().splitlines()[0].strip()
    if not line:
        return None
    candidate = Path(line)
    return candidate if candidate.is_absolute() else (base / candidate)


def create(workspace: str | Path, task_id: str, *, label: str = "") -> TaskRoot:
    """建一个任务根：克隆/复制工作区 → 叠上未提交改动 → 基线提交 → 收紧 ACL。

    克隆拿到的只是 HEAD；用户工作区里的未提交改动由 `overlay_working_tree` 叠上去，
    再提交成基线。这样任务看到的是真实工作树，而 `git diff` 只算任务自己的改动。
    """
    base = Path(workspace).resolve()
    if not base.is_dir():
        raise TaskIsolationError("工作区不存在，无法创建任务根")
    if not wac.supported():
        raise TaskIsolationError("任务级隔离需要 AppContainer（仅 Windows）")
    # 建根之前先拿到"这个后端真能执行"的全绿结论：自检是同步的，create_async 在线程里跑，
    # 不冻事件循环。少了它，执行层会以"当前选中的不是 AppContainer"拒绝每条命令——任务根
    # 变成一个没人能进去的副本。Docker 在别的后端里优先级更高，也要在这里说清而不是等命令撞墙。
    check = wac.self_check()
    if not check.ok:
        raise TaskIsolationError(
            f"AppContainer 行为自检未通过，任务未隔离：{check.error or '文件/网络边界事实不全绿'}"
        )
    from blank_api.services.sandbox.tiers import resolve_tier

    resolved = resolve_tier("workspace")
    if resolved.backend != "appcontainer":
        raise TaskIsolationError(
            f"任务级隔离要求 AppContainer 作为命令后端，当前会选中 {resolved.backend}；任务未隔离"
        )
    name = profile_name(task_id)
    # 先取包 SID：拿不到就在这里失败，别等复制完整个仓库才发现边界建不出来。
    sid, _ = wac.profile(name)

    size = disk_mb()
    vhdx = mount = ""
    target: Path | None = None
    if size > 0:
        root_dir = windows_volume._base_dir()
        token = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
        vhdx_path = root_dir / f"task-{token}.vhdx"
        mount_path = root_dir / f"taskmnt-{token}"
        report = windows_volume.mount(vhdx_path, mount_path, size)
        if not report.ok:
            raise TaskIsolationError(f"任务根容量卷挂载失败：{report.error}")
        vhdx, mount = str(vhdx_path), str(mount_path)
        target = mount_path / "root"

    tree: worktree.Worktree | None = None
    try:
        tree = worktree.create_clone(base, label=label or f"task-{task_id[:8]}", target=target)
        worktree.overlay_working_tree(tree)
        baseline_error = worktree.commit_baseline(tree)
        secure_tree(tree, task_id, source=base)
    except Exception:
        if tree is not None:
            shutil.rmtree(tree.path, ignore_errors=True)
        if mount:
            windows_volume.detach_and_remove(vhdx, mount)
        wac.delete_profile(name)
        raise

    notes = [tree.notes] if tree.notes else []
    if size > 0:
        notes.append(f"任务根容量上限 {size}MB（VHDX）")
    root = TaskRoot(
        task_id=task_id,
        path=tree.path,
        workspace=base,
        tree=tree,
        profile_name=name,
        sid=sid,
        mode=tree.mode,
        vhdx=vhdx,
        mount=mount,
        disk_mb=size,
        notes=notes,
        baseline_error=baseline_error,
    )
    with _LIVE_LOCK:
        _LIVE[task_id] = root
    return root


def harvest(root: TaskRoot) -> dict[str, object]:
    """收任务：先算 diff，再按保守规则合并回主工作区。

    合并规则来自 `worktree.apply_changes`：同一文件被并发改过、或工作区该文件在任务
    开始后又被改过，都只交回 diff，不覆盖。删除类改动一律不自动执行。
    """
    diff = worktree.render_diff(root.tree)
    report = worktree.apply_changes(root.tree)
    return {
        "diff": diff,
        "diff_chars": len(diff),
        "applied": list(report.applied),
        "conflicts": list(report.conflicts),
        "skipped": list(report.skipped),
        "mode": root.mode,
        "profile": root.profile_name,
    }


def release(root: TaskRoot) -> dict[str, object]:
    """删副本、卸卷、删包。返回每一步的错误文本；清理失败不说成"干净"。"""
    errors: list[str] = []
    try:
        worktree.remove_worktree(root.tree)
    except Exception as exc:  # noqa: BLE001 - 清理要尽量走完每一步
        errors.append(f"删任务根失败：{exc.__class__.__name__}: {exc}")
    if root.tree.path.exists():
        # 删了半棵也算没删干净：目录还在就意味着被收紧的副本还在盘上，不能报"已释放"。
        errors.append(f"删任务根未完成：目录仍存在 {root.tree.path}")
    if root.mount:
        error = windows_volume.detach_and_remove(root.vhdx, root.mount)
        if error:
            errors.append(f"卸载任务卷：{error}")
    error = wac.delete_profile(root.profile_name)
    if error:
        errors.append(error)
    with _LIVE_LOCK:
        _LIVE.pop(root.task_id, None)
    return {"released": True, "errors": errors, "root": str(root.path)}


async def create_async(workspace: str | Path, task_id: str, *, label: str = "") -> TaskRoot:
    return await asyncio.to_thread(create, workspace, task_id, label=label)


async def harvest_async(root: TaskRoot) -> dict[str, object]:
    return await asyncio.to_thread(harvest, root)


async def release_async(root: TaskRoot) -> dict[str, object]:
    return await asyncio.to_thread(release, root)


def ac_read_roots() -> tuple[str, ...]:
    """隔离任务额外要读的根：git 自身与用户级 git 配置。

    实测（`E:/tmp/j3-probe/probe_git.py`）：AC 里 `git status` 在没授权 git 安装根时
    直接「拒绝访问。」；授权后能跑，但读不到 `~/.config/git/ignore` 与身份配置，
    `git commit` 会缺 author。这些是任务的工具链，不是用户工作区。
    """
    if sys.platform != "win32":
        return ()
    paths: list[str] = []
    git = shutil.which("git")
    if git:
        real = Path(os.path.realpath(git))
        root = real.parents[2] if len(real.parents) > 2 else real.parent
        paths.append(str(root))
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    for candidate in (home / ".gitconfig", home / ".config" / "git"):
        if candidate.exists():
            paths.append(str(candidate))
    return tuple(dict.fromkeys(paths))


def _cleanup_at_exit() -> None:
    with _LIVE_LOCK:
        roots = list(_LIVE.values())
    for root in roots:
        try:
            release(root)
        except Exception:  # noqa: BLE001 - 退出清理不能再抛
            pass


atexit.register(_cleanup_at_exit)
