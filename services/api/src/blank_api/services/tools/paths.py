"""Workspace-bound path helpers shared by all tools."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

from blank_api.api.errors import ApiError
from blank_api.services.workspace import ignored_name, resolve_inside

BACKUP_DIRNAME = ".blank_backups"
MAX_SEARCH_BYTES = 1024 * 1024
# 搜索预算：这个函数是同步的，跑在事件循环里（调用方负责下放线程）。
# 没有预算的话，一次 search_code 就能把整个后端占到不响应 /health。
MAX_SCAN_FILES = 20000
MAX_SCAN_SECONDS = 8.0


def _skipped_dir(name: str) -> bool:
    return ignored_name(name) or name == BACKUP_DIRNAME


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
    """Yield workspace-relative text files, honouring the tree ignore list.

    只做遍历与粗筛（大小、目录忽略），不预读文件内容——预读一次、搜索时再读一次，
    等于把 IO 翻倍。解码失败由调用方在读取时跳过。

    文件数与时间都有上限：搜索是同步的，无上限会把事件循环占死。上限按"够用"设计，
    命中通常是前几百个文件的事，扫完两万个还没找到也不值得继续。
    """
    root_path = Path(root).resolve()
    base = resolve_inside(root_path, prefix)
    scanned = 0
    deadline = time.monotonic() + MAX_SCAN_SECONDS
    for directory, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if not _skipped_dir(d))
        for filename in sorted(filenames):
            path = Path(directory) / filename
            try:
                if not path.is_file() or path.stat().st_size > MAX_SEARCH_BYTES:
                    continue
            except OSError:
                continue
            scanned += 1
            yield path.relative_to(root_path).as_posix(), path
            if scanned >= MAX_SCAN_FILES or time.monotonic() > deadline:
                return
