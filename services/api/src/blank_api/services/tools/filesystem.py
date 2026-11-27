"""Read-only workspace tools: list_files, glob, read_file, search_code."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import time
from functools import lru_cache
from pathlib import Path

from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok
from blank_api.services.tools.file_state import content_summary
from blank_api.services.tools.paths import (
    MAX_SCAN_FILES,
    MAX_SCAN_SECONDS,
    _skipped_dir,
    iter_text_files,
    resolve_existing,
)
from blank_api.services.workspace import list_tree, read_binary_file, read_text_file

MAX_MATCHES_DEFAULT = 50
MAX_GLOB_RESULTS = 200
_INVALID_REGEX_TEMPLATE = "正则表达式无效：{exc}"

# read_file 能直接交给多模态模型的图片类型。Claude 的 Read 就是这种形状：
# 图片不进"文本内容"那条路，而是作为图像随结果一起给模型看。
# 只收通用位图：SVG 是文本（按文本读更有用），ICO/TIFF 各家供应商支持不一致。
IMAGE_SUFFIXES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}
# base64 会把体积撑大 4/3。4MB 原图约 5.3MB 文本，已经贴着多数网关的单条消息上限；
# 再大就只报尺寸并让调用方自己决定（缩图、裁切），而不是发出去被上游 400。
MAX_IMAGE_BYTES = 4 * 1024 * 1024

# rg --type 的常用映射。只收编程场景真正会用的类型；未知类型直接报错并列出可用项，
# 静默返回空列表会让模型以为"项目里没有这个文件"。
TYPE_EXTENSIONS = {
    "js": {".js", ".mjs", ".cjs"},
    "jsx": {".jsx"},
    "ts": {".ts", ".mts", ".cts"},
    "tsx": {".tsx"},
    "py": {".py"},
    "rust": {".rs"},
    "go": {".go"},
    "java": {".java"},
    "c": {".c", ".h"},
    "cpp": {".cpp", ".cc", ".cxx", ".hpp", ".hh"},
    "cs": {".cs"},
    "ruby": {".rb"},
    "php": {".php"},
    "html": {".html", ".htm"},
    "css": {".css", ".scss", ".sass", ".less"},
    "json": {".json"},
    "yaml": {".yml", ".yaml"},
    "md": {".md", ".markdown"},
    "sh": {".sh", ".bash", ".zsh"},
    "sql": {".sql"},
}


def _broker():
    """延迟导入：宿主路径不该为 broker 的存在付导入成本，也避开与 patch.py 的环。"""
    from blank_api.services.tools import fs_broker

    return fs_broker


def _routed(context: ToolContext) -> bool:
    """这个任务的读路径要不要出宿主进程。

    任务级隔离下**必须**出去：路径检查跑在宿主里只是策略，只有换进任务包执行，
    "另一个任务读不到这个根"才是内核拒。broker 开关关着也照走隔离通道。
    """
    return _broker().broker_enabled() or bool(getattr(context, "isolation_profile", ""))


async def _call(context: ToolContext, op: str, args: dict) -> dict:
    broker = _broker()
    profile = str(getattr(context, "isolation_profile", "") or "")
    if profile:
        return await broker.call_isolated(
            context.root, context.task_id, op, args, profile_name=profile
        )
    return await broker.call_broker(context.root, context.task_id, op, args)


async def _list_files(context: ToolContext, arguments: dict) -> ToolOutcome:
    path = arguments.get("path", "")
    depth = int(arguments.get("depth", 3))
    broker = _broker()
    marker: dict = {}
    if _routed(context):
        try:
            envelope = await _call(
                context, "list_tree",
                {"path": str(path), "depth": depth},
            )
        except broker.FsBrokerError as exc:
            return broker.broker_failure_outcome("list_tree", exc)
        entries = envelope["result"]["entries"]
        marker = broker.broker_success_marker("list_tree", envelope)
    else:
        entries = [vars(entry) for entry in list_tree(context.root, path, max_depth=depth)]
    lines = [
        f"{'  ' * (Path(entry['path']).as_posix().count('/'))}{entry['name']}{'' if entry['is_dir'] else f'  ({entry['size']} 字节)'}"
        for entry in entries
    ]
    return ok("\n".join(lines) or "目录为空", files=entries, count=len(entries), **marker)


async def _read_image(context: ToolContext, relative: str, suffix: str) -> ToolOutcome:
    """把图片读成模型能看的形式。

    走的是「工具结果 + 一条附图的用户消息」这条路，不是把 base64 塞进文本：
    four 家供应商的 tool 消息都只吃字符串，只有用户消息能带 image part。
    ``data["image"]`` 由 ``_append_tool_response`` 取走并挂到那条消息上。

    读字节也必须走文件边界：broker/任务隔离打开时由子进程读，不能因为图片
    就退回宿主进程绕过 ``open_verified``。
    """
    broker = _broker()
    marker: dict = {}
    if _routed(context):
        try:
            envelope = await _call(
                context,
                "read_bytes",
                {"path": relative, "max_bytes": MAX_IMAGE_BYTES},
            )
        except broker.FsBrokerError as exc:
            return broker.broker_failure_outcome("read_bytes", exc)
        except Exception as exc:  # broker 侧重建的 ApiError（越界/不存在/读不了）
            return fail("error", f"无法读取图片：{exc}")
        payload = envelope["result"]["payload"]
        marker = broker.broker_success_marker("read_bytes", envelope)
    else:
        try:
            payload = read_binary_file(context.root, relative, max_bytes=MAX_IMAGE_BYTES)
        except Exception as exc:  # 越界、非法路径都由 paths/workspace 层抛 ApiError
            return fail("error", f"无法读取图片：{exc}")
    if payload.get("too_large"):
        size = int(payload.get("size") or 0)
        return fail(
            "error",
            f"图片过大：{relative} 有 {size / 1024 / 1024:.1f} MB，"
            f"超过 {MAX_IMAGE_BYTES // 1024 // 1024} MB 上限。"
            "先缩小或裁切到需要看的部分，再读一次。",
            output="",
        )
    size = int(payload["size"])
    media = IMAGE_SUFFIXES[suffix]
    return ok(
        f"已读取图片：{relative}（{media}，{size / 1024:.0f} KB）。"
        "图片本身随这条结果一起给你了，直接看图回答；不要再尝试用文本方式读它，"
        "也不要根据文件名猜内容。",
        path=payload["path"],
        size=size,
        image={"media_type": media, "data": payload["data"], "path": payload["path"], "bytes": size},
        **marker,
    )


async def _read_file(context: ToolContext, arguments: dict) -> ToolOutcome:
    broker = _broker()
    marker: dict = {}
    # 图片走单独一条路：它不是"文本内容"，而是随结果附给模型看的图像。
    # 放在最前，是因为后面的 read_text_file 会把图片判成二进制直接报错。
    suffix = Path(str(arguments.get("path") or "")).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return await _read_image(context, str(arguments["path"]), suffix)
    # offset/limit 只在显式传了才放进去：老调用方（含 broker 协议）看到的参数形状不变。
    window = {
        key: int(arguments[key])
        for key in ("offset", "limit")
        if arguments.get(key) is not None
    }
    if _routed(context):
        try:
            envelope = await _call(
                context, "read_text_file",
                {"path": arguments["path"], **window},
            )
        except broker.FsBrokerError as exc:
            return broker.broker_failure_outcome("read_text_file", exc)
        payload = envelope["result"]["payload"]
        marker = broker.broker_success_marker("read_text_file", envelope)
    else:
        payload = read_text_file(context.root, arguments["path"], **window)
    if payload["binary"]:
        return ToolOutcome(
            ok=False, status="error", message=f"该文件是二进制文件，无法作为文本读取：{payload['path']}",
            data=marker,
        )
    if context.file_state is not None:
        # 读到什么就记什么：写工具靠这三格判断"是不是读过、读的是不是这一份"。
        context.file_state.record_read(
            payload["path"],
            size=payload["size"],
            mtime_ns=int(payload.get("mtime_ns") or 0),
            sha256=str(payload.get("sha256") or ""),
            lines=int(payload.get("total_lines") or 0),
            summary=content_summary(str(payload.get("content") or "")),
        )
    fields = {
        "path": payload["path"],
        "size": payload["size"],
        "truncated": payload["truncated"],
        "start_line": payload.get("start_line", 1),
        "end_line": payload.get("end_line", 0),
        "total_lines": payload.get("total_lines", 0),
        "message": payload.get("message", ""),
    }
    content = payload["content"]
    from blank_api.services.tools.notebook import NOTEBOOK_SUFFIX, render_notebook

    if (
        not window
        and not payload["truncated"]
        and str(payload["path"]).lower().endswith(NOTEBOOK_SUFFIX)
    ):
        # notebook 的"读"给的是单元格视图：Read 与 NotebookEdit 共用 cell_ids 口径，
        # 模型从 <cell id="..."> 里抄到的 id 就是编辑时要传的那个。解析不了就退回原始
        # JSON——至少让人看见文件确实坏了。
        rendered = render_notebook(content, path=str(payload["path"]))
        if rendered is not None:
            content, ids = rendered
            fields.update(
                notebook=True,
                cells=ids,
                # 单元格视图没有"第几行"这回事：行号归零，要原始 JSON 时带 offset/limit 重读。
                start_line=1,
                end_line=0,
                total_lines=0,
                message="已按单元格渲染；需要原始 JSON 行请带 offset/limit 重读。",
            )
    return ok(content, **fields, **marker)


def _expand_braces(pattern: str) -> list[str]:
    """把 `*.{ts,tsx}` 展开成 `*.ts` 与 `*.tsx`；没有花括号就原样返回。"""
    match = re.search(r"\{([^{}]+)\}", pattern)
    if not match:
        return [pattern]
    head, tail = pattern[: match.start()], pattern[match.end():]
    return [
        item
        for choice in match.group(1).split(",")
        for item in _expand_braces(head + choice + tail)
    ]


def _glob_match(relative: str, pattern: str) -> bool:
    name = relative.rsplit("/", 1)[-1]
    for candidate in _expand_braces(pattern):
        while candidate.startswith("./"):
            candidate = candidate[2:]
        if not candidate:
            continue
        # 不带斜杠的模式按文件名匹配（rg --glob 的默认口径）；带斜杠的按相对路径匹配。
        if "/" not in candidate:
            if fnmatch.fnmatch(name, candidate):
                return True
        elif _glob_regex(candidate).match(relative):
            return True
    return False


@lru_cache(maxsize=256)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """把带路径的 glob 转成正则：`**/` 要能匹配零层目录（`**/*.md` 命中根部 README.md）。"""
    out: list[str] = ["^"]
    index, length = 0, len(pattern)
    while index < length:
        char = pattern[index]
        if char == "*":
            if index + 1 < length and pattern[index + 1] == "*":
                index += 2
                if index < length and pattern[index] == "/":
                    out.append("(?:.*/)?")
                    index += 1
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = index + 1
            if end < length and pattern[end] in "!^":
                end += 1
            if end < length and pattern[end] == "]":
                end += 1
            while end < length and pattern[end] != "]":
                end += 1
            if end >= length:
                out.append(re.escape(char))
            else:
                body = pattern[index + 1 : end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                elif body.startswith("^"):
                    body = "\\" + body
                out.append("[" + body + "]")
                index = end
        else:
            out.append(re.escape(char))
        index += 1
    out.append("$")
    return re.compile("".join(out))


def _glob_blocking(root, pattern: str, prefix: str, limit: int) -> list[dict]:
    """按 glob 模式列文件，按 mtime 倒序。忽略目录与大目录预算跟搜索同一套。"""
    root_path = Path(root).resolve()
    base = resolve_existing(root_path, prefix)
    if base.is_file():
        relative = base.relative_to(root_path).as_posix()
        if _glob_match(relative, pattern):
            try:
                return [{"path": relative, "mtime_ns": base.stat().st_mtime_ns}]
            except OSError:
                return []
        return []
    found: list[dict] = []
    scanned = 0
    deadline = time.monotonic() + MAX_SCAN_SECONDS
    for directory, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if not _skipped_dir(d))
        for filename in sorted(filenames):
            path = Path(directory) / filename
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
            except OSError:
                continue
            scanned += 1
            relative = path.relative_to(root_path).as_posix()
            if _glob_match(relative, pattern):
                found.append({"path": relative, "mtime_ns": stat.st_mtime_ns})
            if scanned >= MAX_SCAN_FILES or time.monotonic() > deadline:
                break
        else:
            continue
        break
    found.sort(key=lambda item: item["mtime_ns"], reverse=True)
    return found[:limit]


async def _glob(context: ToolContext, arguments: dict) -> ToolOutcome:
    pattern = str(arguments.get("pattern") or "").strip()
    if not pattern:
        return fail("error", "glob 需要 pattern，例如 **/*.py 或 src/**/*.{ts,tsx}")
    prefix = str(arguments.get("path") or "")
    try:
        limit = max(1, min(int(arguments.get("limit") or MAX_GLOB_RESULTS), 1000))
    except (TypeError, ValueError):
        limit = MAX_GLOB_RESULTS
    broker = _broker()
    marker: dict = {}
    if _routed(context):
        try:
            envelope = await _call(
                context, "glob",
                {"pattern": pattern, "path": prefix, "limit": limit},
            )
        except broker.FsBrokerError as exc:
            return broker.broker_failure_outcome("glob", exc)
        files = envelope["result"]["files"]
        marker = broker.broker_success_marker("glob", envelope)
    else:
        files = await asyncio.to_thread(_glob_blocking, context.root, pattern, prefix, limit)
    paths = [item["path"] for item in files]
    return ok(
        "\n".join(paths) or "没有匹配的文件",
        files=paths,
        count=len(paths),
        truncated=len(paths) >= limit,
        **marker,
    )


async def _search_code(context: ToolContext, arguments: dict) -> ToolOutcome:
    pattern = arguments["pattern"]
    mode = arguments.get("mode", "literal")
    prefix = arguments.get("path", "")
    try:
        limit = int(arguments.get("max_matches", MAX_MATCHES_DEFAULT))
    except (TypeError, ValueError):
        limit = MAX_MATCHES_DEFAULT
    output_mode = str(arguments.get("output_mode") or "content")
    if output_mode not in ("content", "files_with_matches", "count"):
        return fail(
            "error",
            "output_mode 只能是 content / files_with_matches / count",
        )
    file_glob = str(arguments.get("glob") or "")
    file_type = str(arguments.get("type") or "")
    if file_type and file_type not in TYPE_EXTENSIONS:
        return fail(
            "error",
            f"未知文件类型：{file_type}。常用：{', '.join(sorted(TYPE_EXTENSIONS))}",
        )
    try:
        case_insensitive = bool(arguments.get("-i") or arguments.get("case_insensitive"))
        multiline = bool(arguments.get("multiline"))
        before = max(0, int(arguments.get("-B") or arguments.get("before_context") or 0))
        after = max(0, int(arguments.get("-A") or arguments.get("after_context") or 0))
        context_lines = max(0, int(arguments.get("-C") or arguments.get("context") or 0))
        only_matching = bool(arguments.get("-o") or arguments.get("only_matching"))
        with_line_numbers = arguments.get("-n")
        with_line_numbers = True if with_line_numbers is None else bool(with_line_numbers)
        head_limit = arguments.get("head_limit")
        head_limit = limit if head_limit is None else max(0, int(head_limit))
        offset = max(0, int(arguments.get("offset") or 0))
    except (TypeError, ValueError):
        return fail("error", "搜索参数必须是数字或布尔值：-A / -B / -C / head_limit / offset")
    if context_lines and output_mode != "content":
        context_lines = 0
    if context_lines:
        before = after = context_lines
    broker = _broker()
    marker: dict = {}
    routed = _routed(context)
    flags = re.IGNORECASE if case_insensitive else 0
    regex = None
    if not routed:
        if mode == "regex":
            try:
                regex = re.compile(pattern, flags)
            except re.error as exc:
                raise ValueError(_INVALID_REGEX_TEMPLATE.format(exc=exc)) from exc
    request = {
        "pattern": pattern,
        "mode": mode,
        "path": str(prefix),
        # 内容模式要多取一些再去掉 context 与 offset，否则 head_limit 会被早期截断。
        "max_matches": 0 if head_limit == 0 else max(limit, head_limit + offset) * (before + after + 1),
        "file_glob": file_glob,
        "file_type": file_type,
        "multiline": multiline,
        "case_insensitive": case_insensitive,
        "before": before,
        "after": after,
        "only_matching": only_matching,
        "output_mode": "files_with_matches" if output_mode == "files_with_matches" else (
            "count" if output_mode == "count" else "content"
        ),
    }
    if routed:
        # 开了 fs-broker（或任务级隔离）：遍历与读取都在子进程边界里做，失败不回退宿主。
        try:
            envelope = await _call(context, "search", request)
        except broker.FsBrokerError as exc:
            return broker.broker_failure_outcome("search", exc)
        matches = envelope["result"]["matches"]
        marker = broker.broker_success_marker("search", envelope)
    else:
        # 搜索是纯同步的文件遍历，必须下放线程：留在事件循环里时，一次全库搜索
        # 就能让后端的 /health 也不回包，界面看起来是"应用卡死"。
        matches = await asyncio.to_thread(
            _search_blocking,
            context.root,
            pattern,
            regex,
            prefix,
            request["max_matches"],
            file_glob=file_glob,
            file_type=file_type,
            multiline=multiline,
            case_insensitive=case_insensitive,
            before=before,
            after=after,
            only_matching=only_matching,
            output_mode=output_mode,
        )
    if output_mode == "files_with_matches":
        entries = [item["path"] for item in matches]
        entries = entries[offset:]
        if head_limit:
            entries = entries[:head_limit]
        return ok(
            "\n".join(entries) or "未找到匹配结果",
            files=entries,
            count=len(entries),
            truncated=bool(head_limit) and len(matches) > offset + head_limit,
            **marker,
        )
    if output_mode == "count":
        entries = [f"{item['path']}: {item['count']}" for item in matches]
        entries = entries[offset:]
        if head_limit:
            entries = entries[:head_limit]
        return ok(
            "\n".join(entries) or "未找到匹配结果",
            counts=matches,
            count=len(entries),
            truncated=bool(head_limit) and len(matches) > offset + head_limit,
            **marker,
        )
    lines: list[str] = []
    for item in matches:
        if item.get("is_context"):
            prefix_text = f"{item['path']}-{item['line']}-" if with_line_numbers else f"{item['path']}-"
        else:
            prefix_text = f"{item['path']}:{item['line']}:" if with_line_numbers else f"{item['path']}:"
        lines.append(f"{prefix_text} {item['text']}")
    lines = lines[offset:]
    if head_limit:
        lines = lines[:head_limit]
    return ok(
        "\n".join(lines) or "未找到匹配结果",
        matches=[item for item in matches if not item.get("is_context")],
        count=len([item for item in matches if not item.get("is_context")]),
        truncated=bool(head_limit) and (len(matches) > offset + head_limit),
        **marker,
    )


def _file_matches_filters(relative: str, file_glob: str, file_type: str) -> bool:
    if file_type:
        extensions = TYPE_EXTENSIONS.get(file_type)
        if extensions is None:
            raise ValueError(
                f"未知文件类型：{file_type}。常用：{', '.join(sorted(TYPE_EXTENSIONS))}"
            )
        if Path(relative).suffix.lower() not in extensions:
            return False
    if file_glob and not _glob_match(relative, file_glob):
        return False
    return True


def _search_blocking(
    root,
    pattern: str | None,
    regex,
    prefix: str,
    limit: int,
    *,
    file_glob: str = "",
    file_type: str = "",
    multiline: bool = False,
    case_insensitive: bool = False,
    before: int = 0,
    after: int = 0,
    only_matching: bool = False,
    output_mode: str = "content",
) -> list[dict]:
    """一次读取同时用于预过滤与逐行匹配；解码失败的文件直接跳过。

    返回形状按 `output_mode`：content 是命中行（可夹 context 行），
    files_with_matches 是每个文件一条，count 是每个文件一条计数。
    """
    if regex is None and pattern is not None:
        flags = re.IGNORECASE if case_insensitive else 0
        if multiline:
            flags |= re.DOTALL
        regex = re.compile(re.escape(pattern), flags)
    elif regex is not None and multiline:
        regex = re.compile(regex.pattern, regex.flags | re.DOTALL)
    matches: list[dict] = []
    seen_files: set[str] = set()
    for relative, path in iter_text_files(root, prefix):
        if not _file_matches_filters(relative, file_glob, file_type):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if multiline:
            file_hits: list[dict] = []
            for hit in regex.finditer(text):
                line = text.count("\n", 0, hit.start()) + 1
                value = hit.group(0) if only_matching else text.splitlines()[line - 1]
                file_hits.append({"path": relative, "line": line, "text": value.strip()[:200]})
                if limit and len(file_hits) >= limit:
                    break
        else:
            lines = text.splitlines()
            file_hits = []
            hit_lines: set[int] = set()
            for index, line in enumerate(lines):
                found = list(regex.finditer(line))
                if not found:
                    continue
                hit_lines.add(index)
                if only_matching:
                    for hit in found:
                        file_hits.append(
                            {"path": relative, "line": index + 1, "text": hit.group(0)[:200]}
                        )
                else:
                    file_hits.append(
                        {"path": relative, "line": index + 1, "text": line.strip()[:200]}
                    )
                if limit and len(file_hits) >= limit:
                    break
            if before or after:
                context: list[dict] = []
                emitted: set[int] = set()
                for index in sorted(hit_lines):
                    for near in range(max(0, index - before), min(len(lines), index + after + 1)):
                        if near in hit_lines or near in emitted:
                            continue
                        emitted.add(near)
                        context.append(
                            {
                                "path": relative,
                                "line": near + 1,
                                "text": lines[near].strip()[:200],
                                "is_context": True,
                            }
                        )
                file_hits = sorted([*file_hits, *context], key=lambda item: item["line"])
        if output_mode == "files_with_matches":
            if file_hits and relative not in seen_files:
                seen_files.add(relative)
                matches.append({"path": relative})
                if limit and len(matches) >= limit:
                    return matches
        elif output_mode == "count":
            if file_hits:
                matches.append({"path": relative, "count": len(file_hits)})
                if limit and len(matches) >= limit:
                    return matches
        else:
            matches.extend(file_hits)
            if limit and len(matches) >= limit:
                return matches[:limit]
    return matches


def filesystem_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="list_files",
            description="列出工作区目录树（自动忽略 .git、node_modules 等目录）",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区相对目录，默认根目录"},
                    "depth": {"type": "integer", "description": "展开层级，1-6"},
                },
            },
            risk_level="low",
            handler=_list_files,
        ),
        ToolDefinition(
            name="read_file",
            description=(
                "读取工作区内的文本文件，参数 path 必须是工作区相对路径。"
                "长文件不要反复整读：结果被裁剪时会告诉你「已返回第 M–N 行（共 T 行）」，"
                "接着用 offset=M2 往下读就行（offset 是 1 基起始行，limit 是行数）。"
                "编辑前必须先读；编辑成功后不要重读验证——write/edit 失败会直接报错。"
            ),
            input_schema={
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string", "description": "工作区相对文件路径"},
                    "offset": {
                        "type": "integer",
                        "description": "从第几行开始读（1 基）。省略=从第 1 行读",
                    },
                    "limit": {"type": "integer", "description": "最多读多少行。省略=读到文件末尾"},
                },
            },
            risk_level="low",
            handler=_read_file,
        ),
        ToolDefinition(
            name="glob",
            description=(
                "按 glob 模式找文件，返回工作区相对路径，最近修改的排在前面。"
                "例：**/*.py、src/**/*.{ts,tsx}、*.md。"
                "不要用 list_files 逐层翻目录来找某类文件——直接用 glob。"
            ),
            input_schema={
                "type": "object",
                "required": ["pattern"],
                "properties": {
                    "pattern": {"type": "string", "description": "glob 模式，支持 ** 与 {a,b}"},
                    "path": {"type": "string", "description": "限定的子目录，默认整个工作区"},
                    "limit": {"type": "integer", "description": "最多返回多少个路径，默认 200"},
                },
            },
            risk_level="low",
            handler=_glob,
        ),
        ToolDefinition(
            name="search_code",
            description=(
                "在工作区内按文本或正则搜代码，返回文件、行号与命中行。"
                "path 可指子目录或单个文件；只看哪些文件命中用 output_mode=files_with_matches，"
                "按文件计数用 count；用 glob / type 收窄范围。"
            ),
            input_schema={
                "type": "object",
                "required": ["pattern"],
                "properties": {
                    "pattern": {"type": "string", "description": "搜索文本或正则表达式"},
                    "mode": {"type": "string", "enum": ["literal", "regex"]},
                    "path": {"type": "string", "description": "限定的子目录或单个文件"},
                    "glob": {"type": "string", "description": "只搜匹配该 glob 的文件，如 *.py、src/**/*.ts"},
                    "type": {"type": "string", "description": "只搜某种语言：py/ts/tsx/js/go/rust/java/md…"},
                    "output_mode": {
                        "type": "string",
                        "enum": ["content", "files_with_matches", "count"],
                        "description": "content=命中行（默认）；files_with_matches=只列文件；count=按文件计数",
                    },
                    "multiline": {"type": "boolean", "description": "让 . 匹配换行；literal 模式按整段文本匹配"},
                    "-i": {"type": "boolean", "description": "忽略大小写"},
                    "-n": {"type": "boolean", "description": "是否显示行号，默认 true"},
                    "-A": {"type": "integer", "description": "命中后附带的上下文行数"},
                    "-B": {"type": "integer", "description": "命中前附带的上下文行数"},
                    "-C": {"type": "integer", "description": "前后各附带多少行"},
                    "-o": {"type": "boolean", "description": "只输出命中的那一段"},
                    "head_limit": {"type": "integer", "description": "最多返回多少个结果，0=不限"},
                    "offset": {"type": "integer", "description": "跳过前多少个结果（先于 head_limit）"},
                    "max_matches": {"type": "integer", "description": "旧参数，等价于 head_limit"},
                },
            },
            risk_level="low",
            handler=_search_code,
        ),
    ]
