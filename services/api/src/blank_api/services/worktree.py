"""Throwaway worktrees for sub-agents.

A sub-agent that wants to try something risky should not touch the real
workspace. Codex gives its sub-agents isolated copies; we use ``git worktree``
when the workspace is a repo (cheap, shares objects) and fall back to a bounded
copy when it is not.

Worktrees live outside the workspace on purpose: putting them under the project
would let them show up in search results, diffs and snapshots.
"""

from __future__ import annotations

import asyncio
import difflib
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from blank_api.services.hooks import HookResult, HookRunner

COPY_IGNORE = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".next",
    ".pytest_cache",
    "dist",
    "build",
    "target",
    ".blank_backups",
    ".blank",
    ".blank-worktrees",
}
MAX_COPY_FILES = 4_000
MAX_COPY_BYTES = 200 * 1024 * 1024
MAX_DIFF_CHARS = 20_000
GIT_TIMEOUT = 30.0


@dataclass
class Worktree:
    path: Path
    mode: str  # "git" | "copy"
    base: Path
    name: str = ""
    notes: str = ""

    def as_dict(self) -> dict:
        return {"path": str(self.path), "mode": self.mode, "name": self.name, "notes": self.notes}


def _run_git(args: list[str], cwd: Path, timeout: float = GIT_TIMEOUT) -> tuple[int, str]:
    try:
        completed = subprocess.run(  # noqa: S603 - 参数由本模块构造，不含用户输入
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def _worktree_root() -> Path:
    # 允许把副本放到指定盘（部署时约定所有临时产物落同一个卷）；默认跟系统临时目录
    override = str(os.environ.get("BLANK_WORKTREE_ROOT") or "").strip()
    root = Path(override) if override else Path(tempfile.gettempdir()) / "blank-worktrees"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _copy_tree(source: Path, target: Path) -> tuple[bool, str]:
    files = 0
    total = 0
    for directory, dirnames, filenames in os.walk(source):
        dirnames[:] = [name for name in dirnames if name not in COPY_IGNORE]
        for filename in filenames:
            path = Path(directory) / filename
            try:
                total += path.stat().st_size
            except OSError:
                continue
            files += 1
            if files > MAX_COPY_FILES or total > MAX_COPY_BYTES:
                return False, "工作区过大，无法复制隔离副本（超过 4000 个文件或 200MB）"
    try:
        shutil.copytree(
            source,
            target,
            ignore=shutil.ignore_patterns(*COPY_IGNORE),
            dirs_exist_ok=False,
        )
    except OSError as exc:
        return False, f"复制隔离副本失败：{exc.__class__.__name__}"
    return True, ""


def create_worktree(root: Path, *, label: str = "") -> Worktree:
    """Create an isolated copy of the workspace; raises ValueError when impossible."""
    base = Path(root).resolve()
    if not base.is_dir():
        raise ValueError("工作区不存在，无法创建隔离副本")
    name = f"{label or 'sub'}-{uuid.uuid4().hex[:8]}"
    target = _worktree_root() / name
    code, output = _run_git(["worktree", "add", "--detach", str(target), "HEAD"], base)
    if code == 0 and target.is_dir():
        return Worktree(path=target, mode="git", base=base, name=name)
    ok, error = _copy_tree(base, target)
    if not ok:
        shutil.rmtree(target, ignore_errors=True)
        raise ValueError(error or "无法创建隔离副本")
    return Worktree(
        path=target,
        mode="copy",
        base=base,
        name=name,
        notes=output.strip()[:200] if code != 0 else "",
    )


def create_clone(root: Path, *, label: str = "", target: Path | None = None) -> Worktree:
    """独立仓库副本：`git clone --shared`，对象只读借用主库，写入落在副本自己的 .git。

    任务级隔离不能用 `git worktree`：worktree 的 `.git` 指向主仓库的
    `.git/worktrees/<name>`，`git add` 会把对象写进**共享**对象库——那等于给任务一把
    能改整个仓库历史的钥匙（J3 实测：只授权任务根时 git 直接「拒绝访问。」，授权共享
    对象库 M 后才成功）。克隆副本有自己的对象库，主仓库对象库只给读，写一个字节都落不进来。

    `target` 由调用方给（容量卷挂载点下的空路径）；不给就放临时根。克隆失败退回整树复制，
    与 `create_worktree` 的兜底同一口径，原因写进 ``notes``。
    """
    base = Path(root).resolve()
    if not base.is_dir():
        raise ValueError("工作区不存在，无法创建隔离副本")
    name = f"{label or 'task'}-{uuid.uuid4().hex[:8]}"
    destination = Path(target) if target is not None else _worktree_root() / name
    code, output = _run_git(["clone", "--shared", "--quiet", str(base), str(destination)], base.parent)
    if code == 0 and destination.is_dir():
        return Worktree(path=destination, mode="git", base=base, name=name)
    shutil.rmtree(destination, ignore_errors=True)
    ok, error = _copy_tree(base, destination)
    if not ok:
        shutil.rmtree(destination, ignore_errors=True)
        raise ValueError(error or "无法创建隔离副本")
    return Worktree(
        path=destination,
        mode="copy",
        base=base,
        name=name,
        notes=(output.strip()[:200] if code != 0 else ""),
    )


def _dirty_entries(base: Path) -> list[tuple[str, str]]:
    """工作区相对 HEAD 的改动清单（含未跟踪文件，逐个文件列出）。读不到就返回空。"""
    code, output = _run_git(["status", "--porcelain", "-z", "-uall"], base)
    if code != 0:
        return []
    parts = output.split("\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(parts):
        entry = parts[index]
        index += 1
        if len(entry) < 4:
            continue
        status, relative = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            # porcelain -z 的重命名/复制把原路径放在下一段，我们只关心新路径
            index += 1
        entries.append((status, relative))
    return entries


def overlay_working_tree(tree: Worktree) -> None:
    """把任务开始时工作区里**未提交**的改动叠到克隆副本上。

    克隆只拿得到 HEAD。用户工作区常常带着未提交的改动，让任务在旧代码上干活再合并回来，
    等于把别人的半成品覆盖掉。叠完之后，任务看到的就是「此刻的真实工作树」。
    """
    if tree.mode != "git":
        return
    for status, relative in _dirty_entries(tree.base):
        if any(part in COPY_IGNORE for part in Path(relative).parts):
            continue
        source = tree.base / relative
        target = tree.path / relative
        if "D" in status:
            try:
                target.unlink()
            except OSError:
                pass
            continue
        if not source.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source, target)
        except OSError:
            # 单个文件复制失败不中断整体：任务至少看到其余真实内容，
            # 漏掉的那份会在基线提交里保留 HEAD 版本，合并时按"未提交改动"跳过。
            continue


def commit_baseline(tree: Worktree) -> str:
    """在克隆里把当前工作树提交成基线，返回错误文本（空 = 成功或无改动）。

    基线之后，副本里的 `git diff` 只包含**任务自己**的改动——用户带进来的未提交改动
    已经进了基线，不会被算成任务的产出，也不会被合并回主工作区。
    """
    if tree.mode != "git":
        return ""
    _run_git(["add", "-A"], tree.path)
    code, output = _run_git(
        [
            "-c", "user.name=Blank Task",
            "-c", "user.email=blank-task@localhost",
            "-c", "commit.gpgsign=false",
            # 这是隔离机制的内部快照，不是用户的提交：跑用户的 commit 钩子只会让
            # 一个与交付无关的副作用决定任务能不能开始。
            "commit", "-qm", "blank task baseline", "--no-verify",
        ],
        tree.path,
    )
    if code == 0:
        return ""
    lowered = output.lower()
    if "nothing to commit" in lowered or "无文件要提交" in output or "干净的工作区" in output:
        return ""
    return output.strip()[:300] or f"基线提交失败（rc={code}）"


def _remove_tree(path: Path) -> None:
    """删整棵树，Windows 上先清只读位：git 对象是不可写文件，`rmtree` 会逐个失败。

    `ignore_errors=True` 会把"只删了一半"读成成功——J3 实测：release 报无错误，副本目录
    还留在 `%TEMP%/blank-worktrees` 里。这里重试失败项（先 chmod 再删），仍删不掉的
    留给调用方按"目录是否还在"记账，不在这里吞掉。
    """
    def _retry(func, target, _exc_info):
        try:
            os.chmod(target, 0o700)
        except OSError:
            pass
        try:
            func(target)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_retry)


def remove_worktree(tree: Worktree) -> None:
    if tree.mode == "git":
        code, _ = _run_git(["worktree", "remove", "--force", str(tree.path)], tree.base)
        if code == 0:
            return
    _remove_tree(tree.path)


async def create_worktree_with_hooks(
    root: Path, *, label: str = "", hooks: HookRunner | None = None
) -> tuple[Worktree, list[HookResult]]:
    """建隔离副本：先问 ``worktree_create`` 钩子，再由默认路径兜底。

    钩子是**可选实现**，不是权限关口：它说"副本在这条路径"，就用那条路径；
    路径不可用（不存在、就是工作区本身）时退回 git / 整树复制，并把原因写进
    ``notes``——静默换路会让钩子作者以为自己的实现生效了。显式 ``deny`` 是唯一的
    硬拒绝：作者已经明确说不该建副本，这时不该由兜底逻辑替他改口。

    默认路径的创建是同步文件 I/O，放线程里跑，避免大仓库复制卡住事件循环。
    """
    base = Path(root).resolve()
    if not base.is_dir():
        raise ValueError("工作区不存在，无法创建隔离副本")

    results: list[HookResult] = []
    hook_error = ""
    if hooks is not None and hooks.enabled:
        results = await hooks.run(
            "worktree_create", "", {"root": str(base), "workspace": str(base), "label": label}
        )
        denied = next((item for item in results if item.denied), None)
        if denied is not None:
            raise ValueError(f"隔离钩子拒绝了副本创建：{denied.reason or denied.command}")
        supplied = next((item for item in results if item.worktree_path), None)
        if supplied is not None:
            candidate = Path(supplied.worktree_path)
            if not candidate.is_absolute():
                candidate = base / candidate
            candidate = candidate.resolve()
            if candidate.is_dir() and candidate != base:
                return (
                    Worktree(
                        path=candidate,
                        mode="hook",
                        base=base,
                        name=candidate.name,
                        notes=f"由钩子提供：{supplied.command}",
                    ),
                    results,
                )
            hook_error = f"隔离钩子返回的路径不可用：{supplied.worktree_path}"

    tree = await asyncio.to_thread(create_worktree, base, label=label)
    if hook_error:
        tree.notes = f"{tree.notes}；{hook_error}" if tree.notes else hook_error
    return tree, results


async def remove_worktree_with_hooks(
    tree: Worktree, *, hooks: HookRunner | None = None
) -> list[HookResult]:
    """先通知 ``worktree_remove`` 钩子，再删副本；钩子失败不影响清理。

    钩子建的副本（``mode="hook"``）由我们负责删——它可能建在任意位置，
    但清理契约仍应归调用方，否则子代理跑完就留下一份没人认领的目录。
    """
    results: list[HookResult] = []
    if hooks is not None and hooks.enabled:
        results = await hooks.run(
            "worktree_remove",
            "",
            {"path": str(tree.path), "mode": tree.mode, "base": str(tree.base)},
        )
    if tree.mode == "hook":
        shutil.rmtree(tree.path, ignore_errors=True)
    else:
        await asyncio.to_thread(remove_worktree, tree)
    return results


def _unified(old: str, new: str, path: str) -> str:
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=2,
    )
    return "".join(diff)


def _looks_text(path: Path) -> bool:
    try:
        if path.stat().st_size > 512 * 1024:
            return False
        path.read_text(encoding="utf-8")
        return True
    except (OSError, UnicodeDecodeError):
        return False


def render_diff(tree: Worktree, max_chars: int = MAX_DIFF_CHARS) -> str:
    """Diff of what the sub-agent did, relative to the workspace it started from."""
    if tree.mode == "git":
        _run_git(["add", "-A"], tree.path)
        code, output = _run_git(["diff", "--cached", "--no-color"], tree.path)
        if code == 0:
            return output[:max_chars]
    chunks: list[str] = []
    base_files: dict[str, Path] = {}
    for directory, dirnames, filenames in os.walk(tree.base):
        dirnames[:] = [name for name in dirnames if name not in COPY_IGNORE]
        for filename in filenames:
            path = Path(directory) / filename
            base_files[path.relative_to(tree.base).as_posix()] = path
    seen: set[str] = set()
    for relative, origin in sorted(base_files.items()):
        target = tree.path / relative
        seen.add(relative)
        if not target.is_file():
            chunks.append(f"--- a/{relative}\n+++ /dev/null\n（文件被删除）\n")
            continue
        if not (_looks_text(origin) and _looks_text(target)):
            continue
        old = origin.read_text(encoding="utf-8")
        new = target.read_text(encoding="utf-8")
        if old != new:
            chunks.append(_unified(old, new, relative))
    for directory, dirnames, filenames in os.walk(tree.path):
        dirnames[:] = [name for name in dirnames if name not in COPY_IGNORE]
        for filename in filenames:
            path = Path(directory) / filename
            relative = path.relative_to(tree.path).as_posix()
            if relative in seen or not _looks_text(path):
                continue
            chunks.append(f"--- /dev/null\n+++ b/{relative}\n（新增文件）\n")
    text = "".join(chunks)
    if len(text) > max_chars:
        return text[:max_chars] + "\n…（diff 已截断）"
    return text


# -- 并行编排用的改动清单与保守合并 -------------------------------------------
#
# 合并规则刻意保守：并行子代理的价值是"分头探索"，不是"自动改写主工作区"。
# 因此只接受满足下面全部条件的文件：
#   1. 没有第二个子代理改过同一个文件（否则算冲突，谁的都不写）；
#   2. 目标文件当前内容与"副本创建时的原始内容"一致（避免覆盖用户未提交的改动）；
#   3. 不是删除类改动（删除必须由主代理显式确认）。
# 不满足时只把 diff 交回去，让主代理自己决定怎么落。

MAX_MERGE_FILES = 200


@dataclass
class MergeReport:
    applied: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.applied) and not self.conflicts

    def as_dict(self) -> dict:
        return {
            "applied": list(self.applied),
            "conflicts": list(self.conflicts),
            "skipped": list(self.skipped),
            "applied_count": len(self.applied),
        }

    def render(self) -> str:
        parts: list[str] = []
        if self.applied:
            parts.append("已合并：" + "、".join(self.applied))
        if self.conflicts:
            parts.append("冲突未合并（多个子代理改了同一文件）：" + "、".join(self.conflicts))
        for item in self.skipped:
            parts.append(f"跳过 {item.get('path')}：{item.get('reason')}")
        return "；".join(parts) or "没有可合并的改动"


def _git_original(tree: Worktree, relative: str) -> str | None:
    """HEAD 里的原始内容；新文件返回 None。"""
    code, output = _run_git(["show", f"HEAD:{relative}"], tree.path)
    return output if code == 0 else None


def changed_files(tree: Worktree) -> list[dict]:
    """本次子代理改过哪些文件，及其当前内容。

    只收文本文件：二进制改动无法用文本三方安全判断，交回 diff 更诚实。
    """
    entries: list[dict] = []
    if tree.mode == "git":
        _run_git(["add", "-A"], tree.path)
        code, output = _run_git(["status", "--porcelain"], tree.path)
        if code != 0:
            return entries
        for line in output.splitlines():
            if len(line) < 4:
                continue
            status, relative = line[:2].strip(), line[3:].strip().strip('"')
            if not relative:
                continue
            if "D" in status:
                entries.append({"path": relative, "change_type": "delete", "content": ""})
                continue
            path = tree.path / relative
            if not path.is_file() or not _looks_text(path):
                continue
            entries.append(
                {
                    "path": relative,
                    "change_type": "add" if "A" in status or "?" in status else "modify",
                    "content": path.read_text(encoding="utf-8"),
                }
            )
        return entries[:MAX_MERGE_FILES]

    for directory, dirnames, filenames in os.walk(tree.path):
        dirnames[:] = [name for name in dirnames if name not in COPY_IGNORE]
        for filename in filenames:
            path = Path(directory) / filename
            relative = path.relative_to(tree.path).as_posix()
            origin = tree.base / relative
            if not _looks_text(path):
                continue
            content = path.read_text(encoding="utf-8")
            if not origin.is_file():
                entries.append({"path": relative, "change_type": "add", "content": content})
            elif _looks_text(origin) and origin.read_text(encoding="utf-8") != content:
                entries.append({"path": relative, "change_type": "modify", "content": content})
            if len(entries) >= MAX_MERGE_FILES:
                return entries
    return entries


def apply_changes(
    tree: Worktree,
    *,
    blocked: set[str] | None = None,
) -> MergeReport:
    """把副本里非冲突的文本改动写回主工作区。"""
    report = MergeReport()
    blocked = blocked or set()
    for entry in changed_files(tree):
        relative = entry["path"]
        if relative in blocked:
            report.conflicts.append(relative)
            continue
        if entry["change_type"] == "delete":
            report.skipped.append({"path": relative, "reason": "删除类改动需主代理显式确认"})
            continue
        target = tree.base / relative
        content = entry["content"]
        if target.is_file():
            if tree.mode != "git":
                report.skipped.append(
                    {"path": relative, "reason": "非 git 工作区无法确认原始内容，请人工合并"}
                )
                continue
            original = _git_original(tree, relative)
            if original is None:
                report.skipped.append({"path": relative, "reason": "目标文件不在版本库中"})
                continue
            try:
                current = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                current = ""
            if current != original:
                report.skipped.append(
                    {"path": relative, "reason": "工作区该文件有未提交改动，未覆盖"}
                )
                continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            report.skipped.append({"path": relative, "reason": f"写入失败：{exc.__class__.__name__}"})
            continue
        report.applied.append(relative)
    return report
