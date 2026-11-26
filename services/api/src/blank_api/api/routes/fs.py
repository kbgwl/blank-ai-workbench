"""本机目录导航：给浏览器环境一个可用的「选择目录」入口。

浏览器拿不到本机绝对路径（File System Access API 给的是句柄，不是路径），
所以目录选择必须由后端列。这里只列**目录**，不列文件、不读文件内容；
接口要 token，默认只在本机回环服务上使用。
"""

from __future__ import annotations

import asyncio
import os
import string
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from blank_api.api.auth import require_token
from blank_api.api.errors import ApiError

router = APIRouter(prefix="/fs", tags=["fs"], dependencies=[Depends(require_token)])

MAX_ENTRIES = 500
MAX_PATH_CHARS = 4096


def _roots() -> list[dict]:
    if os.name == "nt":
        roots: list[dict] = []
        for letter in string.ascii_uppercase:
            path = Path(f"{letter}:\\")
            if path.is_dir():
                roots.append({"name": f"{letter}:\\", "path": str(path)})
        return roots
    home = Path.home()
    roots = [{"name": "/", "path": "/"}]
    if str(home) not in {"/", ""}:
        roots.append({"name": str(home), "path": str(home)})
    return roots


def _parent_of(target: Path) -> str | None:
    parent = target.parent
    return None if parent == target else str(parent)


def _entries(target: Path) -> tuple[list[dict], bool]:
    directories: list[dict] = []
    truncated = False
    try:
        with os.scandir(target) as iterator:
            for entry in iterator:
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                directories.append({"name": entry.name or entry.path, "path": entry.path})
                if len(directories) >= MAX_ENTRIES:
                    truncated = True
                    break
    except PermissionError as exc:
        raise ApiError(
            code="directory_forbidden",
            message="没有权限读取这个目录",
            status_code=403,
        ) from exc
    except OSError as exc:
        raise ApiError(
            code="directory_unreadable",
            message=f"这个目录读不了：{exc.__class__.__name__}",
            status_code=422,
        ) from exc
    directories.sort(key=lambda item: str(item["name"]).lower())
    return directories, truncated


def _browse(raw: str) -> dict:
    """同步实现：目录枚举全部放进线程跑，慢盘不能卡住事件循环。"""
    if not raw:
        return {
            "path": "",
            "parent": None,
            "roots": _roots(),
            "entries": [],
            "truncated": False,
        }
    if "\0" in raw:
        raise ApiError(code="invalid_path", message="路径不合法", status_code=400)
    expanded = os.path.expanduser(raw)
    target = Path(expanded)
    if not target.is_absolute():
        raise ApiError(code="invalid_path", message="路径必须是绝对路径", status_code=400)
    try:
        target = target.resolve()
    except OSError as exc:
        raise ApiError(code="invalid_path", message=f"路径无法解析：{exc}", status_code=400) from exc
    if not target.exists():
        raise ApiError(code="directory_not_found", message="目录不存在", status_code=404)
    if not target.is_dir():
        raise ApiError(code="not_a_directory", message="选中的不是目录", status_code=422)
    entries, truncated = _entries(target)
    return {
        "path": str(target),
        "parent": _parent_of(target),
        "roots": _roots(),
        "entries": entries,
        "truncated": truncated,
    }


@router.get("/directories")
async def list_directories(
    path: str = Query(default="", max_length=MAX_PATH_CHARS),
) -> dict:
    """列出根目录或 ``path`` 下的子目录。只读、不碰文件。"""
    return await asyncio.to_thread(_browse, path.strip())
