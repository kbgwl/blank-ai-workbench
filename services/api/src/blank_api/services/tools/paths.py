"""Workspace-bound path helpers shared by all tools."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from blank_api.api.errors import ApiError
from blank_api.services.workspace import TREE_IGNORE, resolve_inside

BACKUP_DIRNAME = ".blank_backups"
IGNORED_DIRS = TREE_IGNORE | {BACKUP_DIRNAME}
MAX_SEARCH_BYTES = 1024 * 1024


def resolve_existing(root: Path, relative: str) -> Path:
    return resolve_inside(root, relative)


def resolve_for_write(root: Path, relative: str) -> Path:
    """Path safety for files whose parent directories may not exist yet."""
    candidate = Path(relative)
    if candidate.is_absolute():
        raise ApiError(code="path_escape_denied", message="不允许使用绝对路径", status_code=403)
    if ".." in candidate.parts:
        raise ApiError(code="path_escape_denied", message="不允许越出工作区", status_code=403)
    if not candidate.parts:
        raise ApiError(code="path_invalid", message="文件路径不能为空", status_code=422)
    root_path = Path(root).resolve()
    target = (root_path / candidate.as_posix()).resolve()
    if target != root_path and root_path not in target.parents:
        raise ApiError(code="path_escape_denied", message="目标路径越出工作区", status_code=403)
    if target.is_dir():
        raise ApiError(code="is_a_directory", message="该路径是目录，无法写入", status_code=422)
    return target


def iter_text_files(root: Path, prefix: str = "") -> Iterator[tuple[str, Path]]:
    """Yield workspace-relative text files, honouring the tree ignore list."""
    root_path = Path(root).resolve()
    base = resolve_inside(root_path, prefix)
    for directory, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for filename in sorted(filenames):
            path = Path(directory) / filename
            try:
                if not path.is_file() or path.stat().st_size > MAX_SEARCH_BYTES:
                    continue
                path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            yield path.relative_to(root_path).as_posix(), path
