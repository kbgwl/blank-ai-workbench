"""Throwaway worktrees for sub-agents.

A sub-agent that wants to try something risky should not touch the real
workspace. Codex gives its sub-agents isolated copies; we use ``git worktree``
when the workspace is a repo (cheap, shares objects) and fall back to a bounded
copy when it is not.

Worktrees live outside the workspace on purpose: putting them under the project
would let them show up in search results, diffs and snapshots.
"""

from __future__ import annotations

import difflib
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

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
    root = Path(tempfile.gettempdir()) / "blank-worktrees"
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
