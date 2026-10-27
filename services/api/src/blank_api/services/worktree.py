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


def remove_worktree(tree: Worktree) -> None:
    if tree.mode == "git":
        code, _ = _run_git(["worktree", "remove", "--force", str(tree.path)], tree.base)
        if code == 0:
            return
    shutil.rmtree(tree.path, ignore_errors=True)


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
