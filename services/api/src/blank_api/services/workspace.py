"""Workspace path validation and safe resolution inside a workspace root."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from blank_api.api.errors import ApiError
from blank_api.config import get_settings

MAX_READ_BYTES = 512 * 1024
TREE_IGNORE = {".git", "node_modules", "__pycache__", ".venv", ".next", "dist", "build", ".pytest_cache"}

# 构建/打包残留会带后缀（dist-prev-0.4.3、out-old6、.next-old5），精确名盖不住。
# 这些目录里常常躺着整份 Python 运行时，几万个文件——一旦被遍历，搜索与快照都会拖到
# 分钟级，而搜索是同步跑在事件循环里的，表现就是「后端连 /health 都不回」。
# 规则只此一份：快照、搜索、目录列举都调这里，避免各处口径漂移。
IGNORED_PREFIXES = (
    "dist-",
    "dist.",
    "build-",
    "out-old",
    "out-0",
    ".next-old",
    ".next-0",
    "node_modules-",
    "venv-",
    ".venv-",
)


def ignored_name(name: str) -> bool:
    """目录名是否应当跳过（精确名 + 构建产物前缀）。"""
    return name in TREE_IGNORE or name.startswith(IGNORED_PREFIXES)


@dataclass(frozen=True)
class FileEntry:
    name: str
    path: str
    is_dir: bool
    size: int | None


def mask_path(absolute: str | Path) -> str:
    """Hide host absolute paths unless the operator explicitly exposes them."""
    if get_settings().expose_workspace_paths:
        return str(absolute)
    return f"<workspace:{Path(str(absolute)).name or 'root'}>"


def validate_workspace_path(raw: str) -> Path:
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise ApiError(
            code="workspace_path_invalid",
            message="工作区路径必须是绝对路径",
            status_code=422,
        )
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, RuntimeError, OSError):
        raise ApiError(
            code="workspace_not_found",
            message="目录不存在",
            status_code=422,
        ) from None
    if not resolved.is_dir():
        raise ApiError(code="workspace_not_a_directory", message="不是目录", status_code=422)
    return resolved


def resolve_inside(root: Path | str, relative: str) -> Path:
    """Resolve a workspace-relative path, refusing traversal outside the root."""
    root_path = Path(root).resolve()
    if Path(relative).is_absolute():
        raise ApiError(code="path_escape_denied", message="不允许使用绝对路径", status_code=403)
    if ".." in Path(relative).parts:
        raise ApiError(code="path_escape_denied", message="不允许越出工作区", status_code=403)
    target = (root_path / relative).resolve() if relative else root_path
    if target != root_path and root_path not in target.parents:
        raise ApiError(code="path_escape_denied", message="目标路径越出工作区", status_code=403)
    if not target.exists():
        raise ApiError(code="not_found", message=f"路径不存在：{relative or '.'}", status_code=404)
    return target


def to_relative(root: Path, target: Path) -> str:
    return target.relative_to(root).as_posix() if target != root else ""


def list_tree(root: Path | str, relative: str = "", max_depth: int = 2, limit: int = 500) -> list[FileEntry]:
    root_path = Path(root).resolve()
    base = resolve_inside(root_path, relative)
    if not base.is_dir():
        raise ApiError(code="not_a_directory", message="该路径不是目录", status_code=422)
    entries: list[FileEntry] = []

    def walk(directory: Path, depth: int) -> None:
        for child in sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if ignored_name(child.name):
                continue
            if len(entries) >= limit:
                return
            entries.append(
                FileEntry(
                    name=child.name,
                    path=to_relative(root_path, child),
                    is_dir=child.is_dir(),
                    size=None if child.is_dir() else child.stat().st_size,
                )
            )
            if child.is_dir() and depth < max_depth:
                walk(child, depth + 1)

    walk(base, 1)
    return entries


def read_text_file(root: Path | str, relative: str) -> dict:
    target = resolve_inside(root, relative)
    if target.is_dir():
        raise ApiError(code="is_a_directory", message="该路径是目录，无法作为文件读取", status_code=422)
    size = target.stat().st_size
    raw = target.read_bytes()[:MAX_READ_BYTES]
    truncated = size > MAX_READ_BYTES
    try:
        content: str = raw.decode("utf-8")
        encoding = "utf-8"
        is_binary = False
    except UnicodeDecodeError:
        content = ""
        encoding = "binary"
        is_binary = True
    return {
        "path": relative,
        "size": size,
        "encoding": encoding,
        "binary": is_binary,
        "truncated": truncated,
        "content": content,
        "message": (
            "该文件是二进制文件，界面不展示内容"
            if is_binary
            else ("文件过大，仅返回前 512KB" if truncated else "")
        ),
    }
