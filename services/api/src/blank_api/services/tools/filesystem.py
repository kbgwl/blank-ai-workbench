"""Read-only workspace tools: list_files, read_file, search_code."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, ok
from blank_api.services.tools.paths import iter_text_files, resolve_existing
from blank_api.services.workspace import list_tree, read_text_file

MAX_MATCHES_DEFAULT = 50
_INVALID_REGEX_TEMPLATE = "正则表达式无效：{exc}"


def _broker():
    """延迟导入：宿主路径不该为 broker 的存在付导入成本，也避开与 patch.py 的环。"""
    from blank_api.services.tools import fs_broker

    return fs_broker


async def _list_files(context: ToolContext, arguments: dict) -> ToolOutcome:
    path = arguments.get("path", "")
    depth = int(arguments.get("depth", 3))
    broker = _broker()
    marker: dict = {}
    if broker.broker_enabled():
        try:
            envelope = await broker.call_broker(
                context.root, context.task_id, "list_tree",
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


async def _read_file(context: ToolContext, arguments: dict) -> ToolOutcome:
    broker = _broker()
    marker: dict = {}
    if broker.broker_enabled():
        try:
            envelope = await broker.call_broker(
                context.root, context.task_id, "read_text_file", {"path": arguments["path"]},
            )
        except broker.FsBrokerError as exc:
            return broker.broker_failure_outcome("read_text_file", exc)
        payload = envelope["result"]["payload"]
        marker = broker.broker_success_marker("read_text_file", envelope)
    else:
        payload = read_text_file(context.root, arguments["path"])
    if payload["binary"]:
        return ToolOutcome(
            ok=False, status="error", message=f"该文件是二进制文件，无法作为文本读取：{payload['path']}",
            data=marker,
        )
    return ok(
        payload["content"],
        path=payload["path"],
        size=payload["size"],
        truncated=payload["truncated"],
        **marker,
    )


async def _search_code(context: ToolContext, arguments: dict) -> ToolOutcome:
    pattern = arguments["pattern"]
    mode = arguments.get("mode", "literal")
    prefix = arguments.get("path", "")
    limit = int(arguments.get("max_matches", MAX_MATCHES_DEFAULT))
    broker = _broker()
    marker: dict = {}
    if mode == "regex" and not broker.broker_enabled():
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ValueError(_INVALID_REGEX_TEMPLATE.format(exc=exc)) from exc
    elif mode == "regex":
        regex = None  # 正则编译放进 broker 里做，错误经 IPC 原样重建（同一句模板，两个口径不漂移）
    else:
        regex = None
    if broker.broker_enabled():
        # 开了 fs-broker：遍历与读取都在子进程边界里做，失败不回退宿主。
        try:
            envelope = await broker.call_broker(
                context.root, context.task_id, "search",
                {"pattern": pattern, "mode": mode, "path": str(prefix), "max_matches": limit},
            )
        except broker.FsBrokerError as exc:
            return broker.broker_failure_outcome("search", exc)
        matches = envelope["result"]["matches"]
        marker = broker.broker_success_marker("search", envelope)
    else:
        # 搜索是纯同步的文件遍历，必须下放线程：留在事件循环里时，一次全库搜索
        # 就能让后端的 /health 也不回包，界面看起来是"应用卡死"。
        matches = await asyncio.to_thread(_search_blocking, context.root, pattern, regex, prefix, limit)
    rendered = "\n".join(f"{m['path']}:{m['line']}: {m['text']}" for m in matches)
    return ok(
        rendered or "未找到匹配结果",
        matches=matches,
        count=len(matches),
        truncated=len(matches) >= limit,
        **marker,
    )


def _search_blocking(root, pattern: str | None, regex, prefix: str, limit: int) -> list[dict]:
    """一次读取同时用于预过滤与逐行匹配；解码失败的文件直接跳过。"""
    matches: list[dict] = []
    for relative, path in iter_text_files(root, prefix):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if regex is None and pattern not in path.name and pattern not in text:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            hit = regex.search(line) if regex else pattern in line
            if not hit:
                continue
            matches.append({"path": relative, "line": lineno, "text": line.strip()[:200]})
            if len(matches) >= limit:
                return matches
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
            description="读取工作区内的文本文件，参数 path 必须是工作区相对路径",
            input_schema={
                "type": "object",
                "required": ["path"],
                "properties": {"path": {"type": "string", "description": "工作区相对文件路径"}},
            },
            risk_level="low",
            handler=_read_file,
        ),
        ToolDefinition(
            name="search_code",
            description="在工作区内按文本或正则搜索代码，返回文件、行号与命中行",
            input_schema={
                "type": "object",
                "required": ["pattern"],
                "properties": {
                    "pattern": {"type": "string", "description": "搜索文本或正则表达式"},
                    "mode": {"type": "string", "enum": ["literal", "regex"]},
                    "path": {"type": "string", "description": "限定搜索的子目录"},
                    "max_matches": {"type": "integer", "description": "最多返回多少条命中"},
                },
            },
            risk_level="low",
            handler=_search_code,
        ),
    ]
