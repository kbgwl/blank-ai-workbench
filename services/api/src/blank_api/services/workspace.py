"""Workspace path validation and safe resolution inside a workspace root."""

from __future__ import annotations

import base64
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from blank_api.api.errors import ApiError
from blank_api.config import get_settings

MAX_READ_BYTES = 512 * 1024
TREE_IGNORE = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    ".next",
    "dist",
    "build",
    ".pytest_cache",
    # enter_worktree 建在工作区仓库里；不跳过的话搜索/快照会把整棵树再遍历一遍。
    ".blank-worktrees",
}

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


def leaf_name(value: str | Path) -> str:
    """取路径的最后一段。两种分隔符都算边界。

    审计视图要的是"解释器叫什么""壳叫什么"，不是主机路径。判断一段字符串的末段时
    不能只按本平台的 ``os.sep``：快照里出现另一种平台的写法是常态（Linux 进程读到
    Windows 风格的配置、或反过来），只认 ``/`` 的话整条 ``C:\\Users\\<用户名>\\…``
    会被当成一个名字原样回显——2026-10-29 Linux runner 上 ``test_world_state_v22``
    与 ``test_handoff_v12`` 就是这么红的。
    """
    text = str(value).rstrip("/\\")
    if not text:
        return ""
    return re.split(r"[\\/]", text)[-1]


def mask_path(absolute: str | Path) -> str:
    """Hide host absolute paths unless the operator explicitly exposes them.

    只留叶子名，不留任何上级目录。分隔符口径见 ``leaf_name``。
    """
    if get_settings().expose_workspace_paths:
        return str(absolute)
    return f"<workspace:{leaf_name(absolute) or 'root'}>"


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


# 预检（resolve_inside / resolve_for_write）到真正 open 之间有一段窗口：目录可以被
# 换成指向工作区外的 junction/符号链接，open 照着新目标走，预检却只看过旧路径。
# 这一层的判据不是"再看一眼路径"，而是**打开的到底是哪个对象**——按句柄问内核要最终
# 路径（Windows `GetFinalPathNameByHandleW`；POSIX 用 realpath + inode 对上号）。
_OPEN_FLAGS = {
    "rb": os.O_RDONLY,
    "r+b": os.O_RDWR,
    # 不带 O_TRUNC：截断必须发生在身份复验之后，否则 junction 会先毁掉外面的文件。
    "wb": os.O_WRONLY | os.O_CREAT | os.O_EXCL,
}
if os.name == "nt":
    for _key in list(_OPEN_FLAGS):
        _OPEN_FLAGS[_key] |= getattr(os, "O_BINARY", 0)


def _inside(path: str, root: str) -> bool:
    key = os.path.normcase(os.path.abspath(path))
    root_key = os.path.normcase(os.path.abspath(root))
    if key == root_key:
        return True
    prefix = root_key if root_key.endswith(os.sep) else root_key + os.sep
    return key.startswith(prefix)


def _final_path_of(fd: int, candidate: Path) -> str:
    """已打开对象的最终路径。拿不到就拒绝——"验不了"不能当作"没问题"。"""
    if os.name == "nt":
        import ctypes
        import ctypes.wintypes as wt
        import msvcrt

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetFinalPathNameByHandleW.restype = wt.DWORD
        k32.GetFinalPathNameByHandleW.argtypes = [
            ctypes.c_void_p, ctypes.c_wchar_p, wt.DWORD, wt.DWORD,
        ]
        handle = ctypes.c_void_p(msvcrt.get_osfhandle(fd))
        need = k32.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if not need:
            raise ApiError(code="path_escape_denied", message="无法复验已打开文件的真实路径", status_code=403)
        buf = ctypes.create_unicode_buffer(int(need) + 1)
        written = k32.GetFinalPathNameByHandleW(handle, buf, int(need) + 1, 0)
        if not written or written > need:
            raise ApiError(code="path_escape_denied", message="无法复验已打开文件的真实路径", status_code=403)
        text = buf.value
        if text.startswith("\\\\?\\UNC\\"):
            return "\\\\" + text[8:]
        if text.startswith("\\\\?\\"):
            return text[4:]
        return text
    real = os.path.realpath(candidate)
    try:
        opened = os.fstat(fd)
        on_disk = os.stat(real)
    except OSError:
        raise ApiError(code="path_escape_denied", message="无法复验已打开文件的真实路径", status_code=403) from None
    # realpath 与 fd 必须是同一个 inode：否则中间的符号链接在 open 之后又被换过。
    if (opened.st_dev, opened.st_ino) != (on_disk.st_dev, on_disk.st_ino):
        raise ApiError(code="path_escape_denied", message="已打开的文件在复验时已被替换", status_code=403)
    return real


def write_verified(root: Path | str, relative: str, content: bytes) -> None:
    """整文件写入，目标已存在就原地覆盖（句柄复验后才截断）。"""
    try:
        handle = open_verified(root, relative, mode="r+b")
        truncate = True
    except FileNotFoundError:
        handle = open_verified(root, relative, mode="wb", create=True)
        truncate = False
    with handle:
        if truncate:
            handle.seek(0)
            handle.truncate()
        handle.write(content)


def open_verified(
    root: Path | str,
    relative: str,
    *,
    mode: str = "rb",
    create: bool = False,
) -> BinaryIO:
    """打开工作区里的文件，并按句柄复验最终路径仍在工作区内。

    返回二进制文件对象（``rb`` / ``r+b`` / ``wb``），调用方用 ``with`` 关它。
    ``wb`` 仅配 ``create=True``：目标已存在时抛 ``FileExistsError``。
    O_EXCL 让"这个文件确实是我刚创建的"成为事实，复验失败时才能安全删掉自己留下的空文件。
    """
    if mode not in _OPEN_FLAGS or (mode == "wb" and not create):
        raise ValueError(f"open_verified 不支持的打开方式：{mode}（create={create}）")
    root_path = Path(root).resolve()
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise ApiError(code="path_escape_denied", message="不允许越出工作区", status_code=403)
    path = root_path / candidate
    fd = os.open(str(path), _OPEN_FLAGS[mode], 0o666)
    try:
        try:
            final = _final_path_of(fd, path)
        except ApiError:
            os.close(fd)
            fd = -1
            if create:
                # 只可能删我们自己用 O_EXCL 建出来的那一个空文件。
                try:
                    os.unlink(path)
                except OSError:
                    pass
            raise
        if not _inside(final, root_path):
            os.close(fd)
            fd = -1
            if create:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            raise ApiError(code="path_escape_denied", message="目标路径越出工作区", status_code=403)
        handle = os.fdopen(fd, mode)
        fd = -1
        return handle
    finally:
        if fd >= 0:
            os.close(fd)


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


def _decode_text(raw: bytes) -> tuple[str, str, bool]:
    """(内容, 编码, 是否二进制)。

    先按 utf-8 严格解；失败再试本机 ANSI（中文 Windows 上大量源码是 GBK 存的，
    一律判 binary 等于读不了这些文件）。真正的二进制里几乎一定有 NUL 字节，
    用它兜底比"解码器报不报错"更可靠。
    """
    if b"\x00" in raw:
        return "", "binary", True
    for encoding in ("utf-8", "gbk") if os.name == "nt" else ("utf-8",):
        try:
            return raw.decode(encoding), encoding, False
        except UnicodeDecodeError:
            continue
    return "", "binary", True


def _sniff_encoding(head: bytes) -> tuple[str, bool]:
    """只看开头一段判断编码，返回 (编码, 是否二进制)。

    和 ``_decode_text`` 的区别只有一点：这里的 head 是**截断过的**，末尾很可能
    正好切在一个多字节字符中间。严格解码会因此失败，把一个纯中文文件判成二进制
    （实测 1.2MB 的中文单行文件就这样变成了"不可读"）。所以从末尾退 0–3 字节再试。
    """
    if b"\x00" in head:
        return "binary", True
    for encoding in ("utf-8", "gbk") if os.name == "nt" else ("utf-8",):
        for cut in range(4):
            sample = head[: len(head) - cut] if cut else head
            try:
                sample.decode(encoding)
                return encoding, False
            except UnicodeDecodeError:
                continue
    return "binary", True


_SCAN_CHUNK = 1 << 20
# 数行时要扫全文件；再大就停：调用方要的是"从哪行开始读"，不是一份精确到字节的账本。
# 停下来时行数变成下限，消息里如实写成"至少 N 行"，而不是编一个看起来很准的数。
MAX_SCAN_BYTES = 256 * 1024 * 1024


def _scan_line_index(target: Path, start_line: int) -> tuple[int | None, int, bool]:
    """扫一遍文件，回答两件事：第 ``start_line`` 行从哪个字节开始、总共有多少行。

    只留两个整数，不建行表：十万行的日志建行表要 800KB 内存，而调用方要的
    只是"从哪儿开始读"。这也是 Claude 的 Read 对大文件的读法——按字节定位，
    而不是把整个文件读进内存再切行。

    返回 (起始字节, 总行数, 行数是否精确)。起始字节为 None 表示这一行不存在。
    """
    line = 1
    line_start = 0
    found: int | None = None
    newlines = 0
    scanned = 0
    last = b""
    with open(target, "rb") as handle:
        while True:
            chunk = handle.read(_SCAN_CHUNK)
            if not chunk:
                break
            base = scanned
            scanned += len(chunk)
            index = 0
            while True:
                newline = chunk.find(b"\n", index)
                if newline < 0:
                    break
                if found is None and line == start_line:
                    found = line_start
                newlines += 1
                line += 1
                line_start = base + newline + 1
                index = newline + 1
            last = chunk[-1:]
            if scanned >= MAX_SCAN_BYTES:
                if found is None and line == start_line:
                    found = line_start
                return found, newlines, False
    # 结尾没有换行时还有一行（"a\nb" 是两行）。以换行结尾的文件不能多算一行，
    # 否则最后一行会变成一行永远读不到的空行。
    if found is None and last != b"\n" and last and line == start_line:
        found = line_start
    total = newlines if last == b"\n" or scanned == 0 else newlines + 1
    return found, total, True


def _read_text_window(
    target: Path, start_byte: int, size: int, encoding: str, limit: int | None
) -> tuple[str, int, bool, bool]:
    """从起始字节按行读一个窗口。

    返回 (文本, 行数, 是否读到文件尾, 是否切在行中间)。
    最后一个标记要单独给出来：预算被一行本身吃光时，"用 offset 续读"是句空话——
    下一行根本不存在，续读只会读到空。
    """
    with open(target, "rb") as handle:
        handle.seek(start_byte)
        raw = handle.read(MAX_READ_BYTES)
    if not raw:
        return "", 0, True, False
    at_eof = start_byte + len(raw) >= size
    cut_mid_line = False
    if not at_eof:
        cut = raw.rfind(b"\n")
        if cut >= 0:
            raw = raw[: cut + 1]
        else:
            cut_mid_line = True
    codec = encoding if encoding not in ("", "binary") else "utf-8"
    text = raw.decode(codec, errors="replace")
    # 窗口从字符中间切开时，首尾会各剩半个字。只在这一侧不是文件边界时才剥掉，
    # 否则会把文件里真实的替换字符也当成残渣。
    if start_byte > 0 and text.startswith("\ufffd"):
        text = text[1:]
    if not at_eof and text.endswith("\ufffd"):
        text = text[:-1]
    chunk_lines = text.splitlines(keepends=True)
    kept = chunk_lines if limit is None else chunk_lines[: max(int(limit), 0)]
    return "".join(kept), len(kept), at_eof and len(kept) == len(chunk_lines), cut_mid_line


def _read_large_text(
    relative: str,
    target: Path,
    size: int,
    mtime_ns: int,
    head: bytes,
    offset: int | None,
    limit: int | None,
) -> dict:
    """超过 MAX_READ_BYTES 的文件：按字节定位后只读需要的那一段。

    原来这里把"只读了前 512KB"当成"整个文件"，于是 4 万行的日志会自称
    "共 9893 行"，第 3 万行直接读不到——而截断提示里还教模型"把输出重定向到
    文件再分批读"。分批读到 512KB 就断，等于把建议骗回去。
    """
    encoding, is_binary = _sniff_encoding(head)
    start_line = max(int(offset or 1), 1)
    base = {
        "path": relative,
        "size": size,
        "mtime_ns": mtime_ns,
        # 只读了窗口，拿不到全文哈希：读后写闸门退回只比 stat。
        "sha256": "",
        "encoding": encoding,
    }
    if is_binary:
        return {
            **base,
            "binary": True,
            "truncated": True,
            "content": "",
            "start_line": start_line,
            "end_line": start_line - 1,
            "total_lines": 0,
            "message": "该文件是二进制文件，界面不展示内容",
        }
    found, total_lines, exact = _scan_line_index(target, start_line)
    floor = "" if exact else "（文件很大，行数为已统计的下限）"
    if found is None:
        return {
            **base,
            "binary": False,
            "truncated": True,
            "content": "",
            "start_line": start_line,
            "end_line": start_line - 1,
            "total_lines": total_lines,
            "message": f"该文件共 {total_lines} 行{floor}，offset={start_line} 已超出末尾",
        }
    content, count, reached_eof, cut_mid_line = _read_text_window(target, found, size, encoding, limit)
    end_line = start_line + max(count - 1, 0)
    if reached_eof:
        message = ""
    elif cut_mid_line:
        # 一行本身就把预算吃光了：这一行没有"下一行"可以续读，别开空头支票。
        message = (
            f"第 {start_line} 行本身超过单次读取上限（{MAX_READ_BYTES // 1024}KB），"
            "只返回了它的前半段。这一行不能用 offset 续读——"
            "改用 search_code 定位，或先把它在磁盘上拆成多行。"
        )
    elif count > 0:
        message = (
            f"已返回第 {start_line}–{end_line} 行（共 {total_lines} 行{floor}），"
            f"继续请用 offset={end_line + 1}"
        )
    else:
        message = f"单次最多读 {MAX_READ_BYTES // 1024}KB，这一段一行都放不下，请缩小 limit 或换 offset"
    return {
        **base,
        "binary": False,
        "truncated": not (start_line == 1 and reached_eof and exact),
        "content": content,
        "start_line": start_line,
        "end_line": end_line,
        "total_lines": total_lines,
        "message": message,
    }


def read_text_file(
    root: Path | str,
    relative: str,
    offset: int | None = None,
    limit: int | None = None,
) -> dict:
    """读文本文件，可按行取窗口。

    ``offset`` 是 1 基的起始行，``limit`` 是行数；都不传时行为与原来完全一致
    （整文件）。分页不是锦上添花：产物写到 500 行时，进上下文的部分会被裁到
    6,000 字符，模型要改尾部只能靠 search_code 一行行摸锚点——实测 29 步里
    13 步是这么烧掉的。裁剪提示里既然写了 offset，这个参数就必须真的存在。
    """
    target = resolve_inside(root, relative)
    if target.is_dir():
        raise ApiError(code="is_a_directory", message="该路径是目录，无法作为文件读取", status_code=422)
    try:
        with open_verified(root, relative, mode="rb") as handle:
            info = os.fstat(handle.fileno())
            size = int(info.st_size)
            mtime_ns = int(info.st_mtime_ns)
            raw = handle.read(MAX_READ_BYTES)
    except FileNotFoundError:
        raise ApiError(code="not_found", message=f"路径不存在：{relative or '.'}", status_code=404) from None
    except PermissionError:
        if target.is_dir():
            raise ApiError(code="is_a_directory", message="该路径是目录，无法作为文件读取", status_code=422) from None
        raise ApiError(code="path_unreadable", message="该路径无法读取", status_code=403) from None
    truncated = size > MAX_READ_BYTES
    if truncated:
        # 大文件不能拿"前 512KB"冒充整个文件：行数、offset 续读都得以真实字节为准。
        return _read_large_text(relative, target, size, mtime_ns, raw, offset, limit)
    content, encoding, is_binary = _decode_text(raw)

    lines = content.splitlines(keepends=True) if content else []
    total_lines = len(lines)
    start_line = max(int(offset or 1), 1)
    if is_binary:
        window: list[str] = []
    elif limit is None:
        window = lines[start_line - 1 :]
    else:
        count = max(int(limit), 0)
        window = lines[start_line - 1 : start_line - 1 + count] if count else []
    # 空窗口时 end_line = start_line - 1（第 1 行起读 0 行 → 0），与「一行都没给」同义。
    end_line = start_line + len(window) - 1

    if is_binary:
        message = "该文件是二进制文件，界面不展示内容"
    elif truncated:
        message = "文件过大，仅返回前 512KB"
    elif start_line > total_lines:
        message = f"该文件共 {total_lines} 行，offset={start_line} 已超出末尾"
    elif end_line < total_lines:
        message = f"已返回第 {start_line}–{end_line} 行（共 {total_lines} 行），继续请用 offset={end_line + 1}"
    else:
        message = ""
    return {
        "path": relative,
        "size": size,
        # 读后写闸门的两格事实：截断读拿不到全文，哈希留空只比 stat。
        "mtime_ns": mtime_ns,
        "sha256": "" if truncated else hashlib.sha256(raw).hexdigest(),
        "encoding": encoding,
        "binary": is_binary,
        "truncated": truncated,
        "content": "".join(window),
        "start_line": start_line,
        "end_line": end_line,
        "total_lines": total_lines,
        "message": message,
    }


def read_binary_file(root: Path | str, relative: str, *, max_bytes: int = 0) -> dict:
    """读任意文件的原始字节，按 base64 返回。

    图片这类多模态附件不走文本解码，但路径边界必须与 ``read_text_file`` 同一套：
    ``resolve_inside`` 拒绝越界，``open_verified`` 按句柄复验最终路径。
    超过 ``max_bytes`` 时只回尺寸和 ``too_large``，不把大文件读进内存再发出去。
    """
    root_path = Path(root).resolve()
    target = resolve_inside(root_path, relative)
    if target.is_dir():
        raise ApiError(code="is_a_directory", message="该路径是目录，无法作为文件读取", status_code=422)
    try:
        with open_verified(root_path, relative, mode="rb") as handle:
            size = int(os.fstat(handle.fileno()).st_size)
            if max_bytes and size > max_bytes:
                return {"path": relative, "size": size, "too_large": True, "data": ""}
            raw = handle.read()
    except FileNotFoundError:
        raise ApiError(code="not_found", message=f"路径不存在：{relative or '.'}", status_code=404) from None
    except PermissionError:
        raise ApiError(code="path_unreadable", message="该路径无法读取", status_code=403) from None
    return {
        "path": relative,
        "size": size,
        "too_large": False,
        "data": base64.b64encode(raw).decode("ascii"),
    }


# 分支缓存：项目列表一次要问 N 个目录，每次都起 git 进程会明显拖慢首屏。
# 30 秒足够——分支不会在一秒内变两次，而切分支后用户会重进页面。
_BRANCH_CACHE: dict[str, tuple[float, str]] = {}
_BRANCH_TTL = 30.0


def git_branch(path: str | Path) -> str:
    """读目录当前的 git 分支；不是仓库 / 读不到时返回空串。

    用 ``symbolic-ref`` 而不是 ``rev-parse --abbrev-ref``：后者在空仓库上会报错，
    而"刚 init、还没提交"是本产品的常见状态（新建项目就是空目录）。
    """
    import subprocess
    import time

    key = str(path)
    now = time.monotonic()
    hit = _BRANCH_CACHE.get(key)
    if hit and now - hit[0] < _BRANCH_TTL:
        return hit[1]
    try:
        out = subprocess.run(
            ["git", "-C", key, "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        value = out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        value = ""
    _BRANCH_CACHE[key] = (now, value)
    return value
