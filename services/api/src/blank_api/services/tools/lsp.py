"""The single ``lsp`` tool: nine code-intelligence operations, one schema.

Claude Code 2.1.271 把这个能力做成**一个**工具（``LSP``）而不是九个：模型先选
operation，再给 ``filePath`` 与 1-based 的 ``line``/``character``。这里照抄那层
契约——包括每种操作的文本结果、空结果文案、计数口径（``resultCount`` /
``fileCount``）与 ``git check-ignore`` 过滤，因为模型是照这些字眼判断下一步的。

四条不变量：

* **位置换算是唯一一处减法。** 模型给 1-based，LSP 要 0-based；只在
  :func:`_method_for` 里减一次，别的地方不许再减。
* **没有服务端不是错误。** 文件类型没配语言服务器时返回一句可读的
  ``No LSP server available for file type: <ext>``，而不是抛异常——这不是"调用失败"，
  是"这台机器没接这家语言"。
* **12 处文案要逐字对齐。** 空结果文案是 Claude 写在 client 里的常量，模型读到的
  是它；换一句意思相近的话就会改变模型对"要不要再查一次"的判断。
* **结果先过 git 忽略表再格式化。** 定义/引用/实现/工作区符号四类结果里，被
  ``.gitignore`` 排除的文件（构建产物、生成代码）不进结果；否则模型会去读一个
  它根本不该改的生成文件。
"""

from __future__ import annotations

import asyncio
import math
import os
import stat as stat_module
from pathlib import Path

from blank_api.services.lsp.client import (
    LSPError,
    LSPServerManager,
    format_uri,
    uri_to_path,
)
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok

OPERATIONS = (
    "goToDefinition",
    "findReferences",
    "hover",
    "documentSymbol",
    "workspaceSymbol",
    "goToImplementation",
    "prepareCallHierarchy",
    "incomingCalls",
    "outgoingCalls",
)

# Claude 的文件上限是 10,000,000 字节（十进制 10MB），提示里的 MB 用 ceil。
MAX_FILE_BYTES = 10_000_000
IGNORE_BATCH = 50
IGNORE_TIMEOUT = 5.0

_NO_DEFINITION = (
    "No definition found. This may occur if the cursor is not on a symbol, or if the "
    "definition is in an external library not indexed by the LSP server."
)
_NO_REFERENCES = (
    "No references found. This may occur if the symbol has no usages, or if the LSP "
    "server has not fully indexed the workspace."
)
_NO_DOC_SYMBOLS = (
    "No symbols found in document. This may occur if the file is empty, not supported "
    "by the LSP server, or if the server has not fully indexed the file."
)
_NO_WORKSPACE_SYMBOLS = (
    "No symbols found in workspace. This may occur if the workspace is empty, or if the "
    "LSP server has not finished indexing the project."
)
_NO_HOVER = (
    "No hover information available. This may occur if the cursor is not on a symbol, "
    "or if the LSP server has not fully indexed the file."
)
_NO_CALL_ITEM = "No call hierarchy item found at this position"
_NO_INCOMING = "No incoming calls found (nothing calls this function)"
_NO_OUTGOING = "No outgoing calls found (this function calls nothing)"

KIND_NAMES = {
    1: "File",
    2: "Module",
    3: "Namespace",
    4: "Package",
    5: "Class",
    6: "Method",
    7: "Property",
    8: "Field",
    9: "Constructor",
    10: "Enum",
    11: "Interface",
    12: "Function",
    13: "Variable",
    14: "Constant",
    15: "String",
    16: "Number",
    17: "Boolean",
    18: "Array",
    19: "Object",
    20: "Key",
    21: "Null",
    22: "EnumMember",
    23: "Struct",
    24: "Event",
    25: "Operator",
    26: "TypeParameter",
}

# 没有运行时注入时的兜底：同一个根目录共用一个管理器，避免每次调用都拉起新进程。
_FALLBACK: dict[str, LSPServerManager] = {}


def _manager(context: ToolContext) -> LSPServerManager:
    injected = getattr(context, "lsp", None)
    if injected is not None:
        return injected
    key = str(Path(context.root).resolve())
    manager = _FALLBACK.get(key)
    if manager is None:
        manager = LSPServerManager(root=context.root)
        _FALLBACK[key] = manager
    return manager


def _kind(kind) -> str:
    try:
        return KIND_NAMES.get(int(kind), "Unknown")
    except (TypeError, ValueError):
        return "Unknown"


def _plural(count: int, word: str) -> str:
    return word if count == 1 else word + "s"


def _line_of(item: dict, key: str = "range") -> dict:
    """`range.start`；结构不对就让外层按 Claude 的 catch 分支报错。"""
    return item[key]["start"]


def _location_text(location: dict, root) -> str:
    start = _line_of(location)
    return f"{format_uri(location['uri'], root)}:{start['line'] + 1}:{start['character'] + 1}"


def _as_location(item):
    """LSP 的 Location 或 LocationLink → 统一成 {uri, range}。"""
    if not isinstance(item, dict):
        return None
    if "targetUri" in item:
        return {
            "uri": item.get("targetUri"),
            "range": item.get("targetSelectionRange") or item.get("targetRange"),
        }
    return item


def _group_by_file(items, uri_of, root) -> dict[str, list]:
    groups: dict[str, list] = {}
    for item in items:
        path = format_uri(uri_of(item) or "", root)
        groups.setdefault(path, []).append(item)
    return groups


def _definition_text(result, root) -> str:
    if not result:
        return _NO_DEFINITION
    if isinstance(result, list):
        valid = [item for item in (_as_location(entry) for entry in result) if item and item.get("uri")]
        if not valid:
            return _NO_DEFINITION
        if len(valid) == 1:
            return f"Defined in {_location_text(valid[0], root)}"
        locations = "\n".join(f"  {_location_text(item, root)}" for item in valid)
        return f"Found {len(valid)} definitions:\n{locations}"
    # 单条结果 Claude 不做过滤：畸形数据由外层 catch 报成 Error performing。
    return f"Defined in {_location_text(_as_location(result), root)}"


def _references_text(result, root) -> str:
    if not result:
        return _NO_REFERENCES
    valid = [item for item in result if item and item.get("uri")]
    if not valid:
        return _NO_REFERENCES
    if len(valid) == 1:
        return f"Found 1 reference:\n  {_location_text(valid[0], root)}"
    groups = _group_by_file(valid, lambda item: item.get("uri"), root)
    lines = [f"Found {len(valid)} references across {len(groups)} files:"]
    for path, items in groups.items():
        lines.append(f"\n{path}:")
        for item in items:
            start = _line_of(item)
            lines.append(f"  Line {start['line'] + 1}:{start['character'] + 1}")
    return "\n".join(lines)


def _hover_contents(contents) -> str:
    if isinstance(contents, list):
        return "\n".join(
            entry if isinstance(entry, str) else str((entry or {}).get("value", ""))
            for entry in contents
        )
    if isinstance(contents, str):
        return contents
    if isinstance(contents, dict):
        return str(contents.get("value", ""))
    return str(contents)


def _hover_text(result, root) -> str:
    if not result:
        return _NO_HOVER
    text = _hover_contents(result.get("contents"))
    rng = result.get("range")
    if rng:
        start = rng["start"]
        return f"Hover info at {start['line'] + 1}:{start['character'] + 1}:\n{text}"
    return text


def _symbol_lines(symbol: dict, depth: int = 0) -> list[str]:
    prefix = "  " * depth
    text = f"{prefix}{symbol.get('name')} ({_kind(symbol.get('kind'))})"
    if symbol.get("detail"):
        text += f" {symbol['detail']}"
    text += f" - Line {_line_of(symbol)['line'] + 1}"
    lines = [text]
    for child in symbol.get("children") or []:
        lines.extend(_symbol_lines(child, depth + 1))
    return lines


def _workspace_symbols_text(result, root) -> str:
    if not result:
        return _NO_WORKSPACE_SYMBOLS
    valid = [
        item
        for item in result
        if item and isinstance(item.get("location"), dict) and item["location"].get("uri")
    ]
    if not valid:
        return _NO_WORKSPACE_SYMBOLS
    groups = _group_by_file(valid, lambda item: item["location"]["uri"], root)
    lines = [f"Found {len(valid)} {_plural(len(valid), 'symbol')} in workspace:"]
    for path, items in groups.items():
        lines.append(f"\n{path}:")
        for item in items:
            line = (
                f"  {item.get('name')} ({_kind(item.get('kind'))}) - "
                f"Line {_line_of(item['location'])['line'] + 1}"
            )
            if item.get("containerName"):
                line += f" in {item['containerName']}"
            lines.append(line)
    return "\n".join(lines)


def _document_symbols_text(result, root) -> str:
    if not result:
        return _NO_DOC_SYMBOLS
    if isinstance(result[0], dict) and "location" in result[0]:
        # SymbolInformation[]：与工作区符号同一种形状，Claude 也复用那套文案。
        return _workspace_symbols_text(result, root)
    lines = ["Document symbols:"]
    for symbol in result:
        lines.extend(_symbol_lines(symbol))
    return "\n".join(lines)


def _call_item_text(item: dict, root) -> str:
    uri = item.get("uri")
    if not uri:
        return f"{item.get('name')} ({_kind(item.get('kind'))}) - <unknown location>"
    text = (
        f"{item.get('name')} ({_kind(item.get('kind'))}) - "
        f"{format_uri(uri, root)}:{_line_of(item)['line'] + 1}"
    )
    if item.get("detail"):
        text += f" [{item['detail']}]"
    return text


def _prepare_text(result, root) -> str:
    if not result:
        return _NO_CALL_ITEM
    if len(result) == 1:
        return f"Call hierarchy item: {_call_item_text(result[0], root)}"
    lines = [f"Found {len(result)} call hierarchy items:"]
    for item in result:
        lines.append(f"  {_call_item_text(item, root)}")
    return "\n".join(lines)


def _ranges_text(ranges) -> str:
    return ", ".join(
        f"{entry['start']['line'] + 1}:{entry['start']['character'] + 1}" for entry in ranges
    )


def _calls_text(result, root, side: str, label: str) -> str:
    empty = _NO_INCOMING if side == "from" else _NO_OUTGOING
    if not result:
        return empty
    direction = "incoming" if side == "from" else "outgoing"
    lines = [f"Found {len(result)} {direction} {_plural(len(result), 'call')}:"]
    valid = [item for item in result if item and isinstance(item.get(side), dict)]
    groups = _group_by_file(valid, lambda item: item[side].get("uri"), root)
    for path, items in groups.items():
        lines.append(f"\n{path}:")
        for item in items:
            target = item[side]
            line = (
                f"  {target.get('name')} ({_kind(target.get('kind'))}) - "
                f"Line {_line_of(target)['line'] + 1}"
            )
            if item.get("fromRanges"):
                line += f" [{label}: {_ranges_text(item['fromRanges'])}]"
            lines.append(line)
    return "\n".join(lines)


def _symbol_count(symbols) -> int:
    total = len(symbols)
    for symbol in symbols:
        if symbol.get("children"):
            total += _symbol_count(symbol["children"])
    return total


def _counts(operation: str, result) -> tuple[int, int]:
    """Claude 的计数口径：definition/references/workspace 按有效条目与唯一 URI；
    documentSymbol 按递归符号数、fileCount 只有 0/1；call hierarchy 按条目与各自端点。"""
    if operation in ("goToDefinition", "goToImplementation"):
        items = result if isinstance(result, list) else ([result] if result else [])
        valid = [item for item in (_as_location(entry) for entry in items) if item and item.get("uri")]
        return len(valid), len({item["uri"] for item in valid})
    if operation == "findReferences":
        valid = [item for item in (result or []) if item and item.get("uri")]
        return len(valid), len({item["uri"] for item in valid})
    if operation == "hover":
        return (1, 1) if result else (0, 0)
    if operation == "documentSymbol":
        symbols = result or []
        if symbols and isinstance(symbols[0], dict) and "range" in symbols[0]:
            return _symbol_count(symbols), 1 if symbols else 0
        return len(symbols), 1 if symbols else 0
    if operation == "workspaceSymbol":
        valid = [
            item
            for item in (result or [])
            if item and isinstance(item.get("location"), dict) and item["location"].get("uri")
        ]
        return len(valid), len({item["location"]["uri"] for item in valid})
    if operation == "prepareCallHierarchy":
        items = result or []
        return len(items), len({item.get("uri") for item in items if item.get("uri")})
    if operation in ("incomingCalls", "outgoingCalls"):
        side = "from" if operation == "incomingCalls" else "to"
        items = result or []
        uris = {item[side].get("uri") for item in items if isinstance(item.get(side), dict)}
        return len(items), len({uri for uri in uris if uri})
    return 0, 0


# --------------------------------------------------------------------------- #
# git check-ignore
# --------------------------------------------------------------------------- #
def _norm(path: str) -> str:
    return os.path.normcase(str(path).replace("\\", "/"))


async def _git_ignored(paths: list[str], root: Path) -> set[str]:
    """批量问 git 哪些文件被忽略；不是仓库 / 没装 git 时返回空集（不过滤）。

    工作区自己就被上层仓库忽略时（例如测试临时目录、把 build 目录当工作区打开），
    上层 .gitignore 会把工作区里的**每个**文件都判成 ignored，LSP 结果会被整批丢光。
    这种嵌套场景按"无法判断工作区自己的 ignore 语义"处理：不过滤。
    """
    ignored: set[str] = set()
    root_arg = Path(root).as_posix()
    root_key = _norm(root_arg)
    for start in range(0, len(paths), IGNORE_BATCH):
        # 把工作区根一起送进去：一次进程就能分清"根被忽略"与"只有个别文件被忽略"。
        batch = [root_arg, *paths[start:start + IGNORE_BATCH]]
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                "check-ignore",
                *batch,
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, ValueError):
            return set()
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=IGNORE_TIMEOUT)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            continue
        if process.returncode == 0 and stdout:
            lines = {
                _norm(line.strip())
                for line in stdout.decode("utf-8", "replace").splitlines()
                if line.strip()
            }
            if root_key in lines:
                return set()
            ignored.update(lines)
    return ignored


async def _filter_ignored(items, uri_of, root: Path):
    if not items:
        return items
    uris: list[str] = []
    seen: set[str] = set()
    for item in items:
        uri = uri_of(item)
        if uri and uri not in seen:
            seen.add(uri)
            uris.append(uri)
    if not uris:
        return items
    ignored = await _git_ignored([uri_to_path(uri) for uri in uris], root)
    if not ignored:
        return items
    return [
        item
        for item in items
        if not (uri_of(item) and _norm(uri_to_path(uri_of(item))) in ignored)
    ]


def _symbol_uri(item):
    location = item.get("location") if isinstance(item, dict) else None
    return location.get("uri") if isinstance(location, dict) else None


async def _apply_ignore_filter(operation: str, result, root: Path):
    if operation in ("goToDefinition", "goToImplementation"):
        items = result if isinstance(result, list) else ([result] if result else [])
        filtered = await _filter_ignored(
            items, lambda item: (_as_location(item) or {}).get("uri"), root
        )
        return filtered if isinstance(result, list) else (filtered[0] if filtered else None)
    if operation == "findReferences":
        return await _filter_ignored(result or [], lambda item: item.get("uri"), root)
    if operation == "workspaceSymbol":
        return await _filter_ignored(result or [], _symbol_uri, root)
    return result


# --------------------------------------------------------------------------- #
# 请求映射
# --------------------------------------------------------------------------- #
def _method_for(operation: str, uri: str, line: int, character: int, query: str):
    position = {"line": line - 1, "character": character - 1}
    document = {"uri": uri}
    if operation == "goToDefinition":
        return "textDocument/definition", {"textDocument": document, "position": position}
    if operation == "findReferences":
        return (
            "textDocument/references",
            {
                "textDocument": document,
                "position": position,
                # 声明处也是引用：模型问"谁用了它"时不带上定义会漏。
                "context": {"includeDeclaration": True},
            },
        )
    if operation == "hover":
        return "textDocument/hover", {"textDocument": document, "position": position}
    if operation == "documentSymbol":
        return "textDocument/documentSymbol", {"textDocument": document}
    if operation == "workspaceSymbol":
        return "workspace/symbol", {"query": query or ""}
    if operation == "goToImplementation":
        return "textDocument/implementation", {"textDocument": document, "position": position}
    if operation in ("prepareCallHierarchy", "incomingCalls", "outgoingCalls"):
        # 三个调用层级操作都先走 prepare；后两个再拿第一个 item 追问一次。
        return "textDocument/prepareCallHierarchy", {"textDocument": document, "position": position}
    return "", {}


def _invalid_input(detail: str) -> ToolOutcome:
    # Claude 的 zod 原文在 Python 里没有一一对应物；保留前缀与"拒绝执行"语义，
    # 细节写成可读的英文（见 CHANGELOG 偏离项）。
    return fail("error", f"Invalid input: {detail}")


def _argument_error(arguments: dict) -> str:
    operation = arguments.get("operation")
    if operation not in OPERATIONS:
        return "operation must be one of: " + ", ".join(OPERATIONS)
    path = arguments.get("filePath")
    if not isinstance(path, str) or not path.strip():
        return "filePath is required"
    for key in ("line", "character"):
        value = arguments.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return f"{key} must be a positive integer (1-based)"
    query = arguments.get("query")
    if query is not None and not isinstance(query, str):
        return "query must be a string"
    return ""


def _data(operation: str, result_text: str, file_path: str, counts=None) -> dict:
    body = {"operation": operation, "result": result_text, "filePath": file_path}
    if counts is not None:
        body["resultCount"], body["fileCount"] = counts
    return body


async def _perform(context: ToolContext, arguments: dict) -> ToolOutcome:
    detail = _argument_error(arguments)
    if detail:
        return _invalid_input(detail)

    operation = str(arguments["operation"])
    file_path = str(arguments["filePath"])
    line = int(arguments["line"])
    character = int(arguments["character"])
    query = str(arguments.get("query") or "")

    path = Path(file_path)
    if not path.is_absolute():
        path = Path(context.root) / path
    try:
        stat = path.stat()
    except FileNotFoundError:
        return fail("error", f"File does not exist: {file_path}")
    except OSError as error:
        return fail("error", f"Cannot access file: {file_path}. {error}")
    if not stat_module.S_ISREG(stat.st_mode):
        return fail("error", f"Path is not a file: {file_path}")
    if stat.st_size > MAX_FILE_BYTES:
        text = (
            f"File too large for LSP analysis "
            f"({math.ceil(stat.st_size / 1_000_000)}MB exceeds 10MB limit)"
        )
        return ok(text, **_data(operation, text, file_path))

    manager = _manager(context)
    try:
        found = await manager.client_for(path)
    except LSPError as error:
        text = f"LSP initialization failed: {error.message}"
        return ok(text, **_data(operation, text, file_path))
    if found is None:
        text = f"No LSP server available for file type: {path.suffix}"
        return ok(text, **_data(operation, text, file_path))
    client, language = found

    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        text = f"Error performing {operation}: {error}"
        return ok(text, **_data(operation, text, file_path))

    root = Path(context.root)
    try:
        uri = await client.open_document(path, language, content)
        method, params = _method_for(operation, uri, line, character, query)
        result = await client.request(method, params)
        if operation in ("incomingCalls", "outgoingCalls"):
            if not result:
                text = _NO_CALL_ITEM
                return ok(text, **_data(operation, text, file_path, counts=(0, 0)))
            follow = (
                "callHierarchy/incomingCalls"
                if operation == "incomingCalls"
                else "callHierarchy/outgoingCalls"
            )
            result = await client.request(follow, {"item": result[0]})
        result = await _apply_ignore_filter(operation, result, root)
    except LSPError as error:
        text = f"Error performing {operation}: {error.message}"
        return ok(text, **_data(operation, text, file_path))
    except Exception as error:  # noqa: BLE001 - 与 Claude 的 catch 同口径：畸形结果也报同一句
        text = f"Error performing {operation}: {error}"
        return ok(text, **_data(operation, text, file_path))

    if operation == "goToDefinition" or operation == "goToImplementation":
        rendered = _definition_text(result, root)
    elif operation == "findReferences":
        rendered = _references_text(result, root)
    elif operation == "hover":
        rendered = _hover_text(result, root)
    elif operation == "documentSymbol":
        rendered = _document_symbols_text(result, root)
    elif operation == "workspaceSymbol":
        rendered = _workspace_symbols_text(result, root)
    elif operation == "prepareCallHierarchy":
        rendered = _prepare_text(result, root)
    elif operation == "incomingCalls":
        rendered = _calls_text(result, root, "from", "calls at")
    else:
        rendered = _calls_text(result, root, "to", "called from")

    counts = _counts(operation, result)
    return ok(rendered, **_data(operation, rendered, file_path, counts=counts))


def lsp_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="lsp",
            description=(
                "查询语言服务器（LSP）拿到代码智能信息。operation 九选一：\n"
                "- goToDefinition：符号定义在哪\n"
                "- findReferences：符号被哪些地方引用\n"
                "- hover：符号的文档、类型信息\n"
                "- documentSymbol：文件里的全部符号（函数、类、变量）\n"
                "- workspaceSymbol：按 query 在整个工作区搜符号（务必给 query，"
                "多数服务端对空 query 返回空）\n"
                "- goToImplementation：接口/抽象方法的实现处\n"
                "- prepareCallHierarchy：该位置函数/方法的调用层级条目\n"
                "- incomingCalls：谁调用了该位置的函数\n"
                "- outgoingCalls：该位置的函数调用了谁\n"
                "所有操作都要 filePath、line（1-based，与编辑器显示一致）、"
                "character（1-based）。语言服务器必须已按文件类型配置；没有服务端时会"
                "直接说明，而不是猜一个启动命令。"
            ),
            input_schema={
                "type": "object",
                "required": ["operation", "filePath", "line", "character"],
                "additionalProperties": False,
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": list(OPERATIONS),
                        "description": "要执行的 LSP 操作",
                    },
                    "filePath": {
                        "type": "string",
                        "description": "文件路径（绝对路径或相对工作区）",
                    },
                    "line": {
                        "type": "integer",
                        "description": "行号（1-based，与编辑器显示一致）",
                    },
                    "character": {
                        "type": "integer",
                        "description": "字符偏移（1-based，与编辑器显示一致）",
                    },
                    "query": {
                        "type": "string",
                        "description": "workspaceSymbol 的符号名或片段；其他操作忽略",
                    },
                },
            },
            risk_level="low",
            handler=_perform,
            timeout_seconds=60.0,
        )
    ]


__all__ = [
    "MAX_FILE_BYTES",
    "OPERATIONS",
    "lsp_tools",
]
