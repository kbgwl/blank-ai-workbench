"""REA 的 MCP 服务端：把实物解析能力按标准 MCP 协议端出去。

为什么自己写而不装 ``mcp`` 包：这条链路要的是**能被截断、能被弄坏**的那一层。
第三方 SDK 会把工具签名、schema、错误形状都替你决定好，而 REA-1 要检验的恰恰是
「解析失败时端出来的是指名道姓的一步，还是一句笼统的内部错误」。手写 JSON-RPC
只有约两百行，且每一行的形状都能被测试直接引用（与 ``tests/fake_mcp_server.py`` 同一口径）。

启动方式（客户端就是这么拉起来的）::

    python -m blank_api.services.rea.mcp_server --root <授权根目录>

三条硬规则：

* **根目录是唯一授权边界**。所有 target 都按 ``--root`` 相对解析，绝对路径与 ``..``
  一律拒绝——宿主绝对路径绝不能进 finding 或报错文本。
* **``--root`` 不存在就不是一个能用的服务端**：往 stderr 说清原因、以 3 退出。
  客户端会把那句 stderr 原样带回来，这正是"外部服务端不可用时明确报状态"的实物。
* **解析失败是工具结果，不是协议错误**。JSON-RPC 的 ``error`` 表示"这次调用没发生"，
  而"发生了、但字节读不下去"必须用 ``isError`` 表达，并带上第几步、被解析对象是谁。

``BLANK_REA_OMIT_TOOLS=inspect_target`` 可以让服务端少报一个工具，用来真实复现
"版本不匹配的服务端给了半份工具表"——桥接层必须拒绝把它当成功。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from blank_api.services.rea import artifact

SERVER_NAME = "rea-artifacts"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2025-06-18"

# 工具表的可选项：省略哪个由环境变量决定（测试用来自真实服务端的半份工具表）
TOOL_NAMES = ("inspect_target", "scan_targets", "find_strings")

MAX_SCAN_FILES = 400
SCAN_DEPTH = 3
STRINGS_WINDOW = 4 * 1024 * 1024  # 只在前 4 MiB 里抽字符串：这条链路不做全文扫描
_PRINTABLE = re.compile(rb"[\x20-\x7e]{4,}")


# --------------------------------------------------------------------------- #
# 协议骨架
# --------------------------------------------------------------------------- #
def _write(message: dict) -> None:
    sys.stdout.buffer.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def _result(request_id, result: dict) -> None:
    _write({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id, code: int, message: str) -> None:
    _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _tool_result(*, is_error: bool, text: str, structured: dict) -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
        "structuredContent": structured,
    }


def _omitted() -> set[str]:
    raw = str(os.environ.get("BLANK_REA_OMIT_TOOLS") or "")
    return {item.strip() for item in raw.split(",") if item.strip() in TOOL_NAMES}


def tools_payload() -> list[dict]:
    omitted = _omitted()
    specs = {
        "inspect_target": {
            "name": "inspect_target",
            "description": "解析一个真实产物（asar / PE / ELF），返回格式、入口线索与逐条证据",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "相对授权根目录的产物路径"}
                },
                "required": ["target"],
            },
        },
        "scan_targets": {
            "name": "scan_targets",
            "description": "列出根目录内的候选逆向目标，并按幻数给出格式预判（不解析正文）",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对授权根目录的子目录，默认根目录"},
                    "limit": {"type": "integer", "description": "最多列举多少个文件"},
                },
            },
        },
        "find_strings": {
            "name": "find_strings",
            "description": "从产物头部区间抽取可读字符串线索（偏移 + 原文），用于入口与命名取证",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "相对授权根目录的产物路径"},
                    "min_length": {"type": "integer", "description": "最短串长，默认 4"},
                    "limit": {"type": "integer", "description": "最多返回多少条，默认 80"},
                },
                "required": ["target"],
            },
        },
    }
    return [specs[name] for name in TOOL_NAMES if name not in omitted]


# --------------------------------------------------------------------------- #
# 工具实现（全部只读，全部被 --root 圈住）
# --------------------------------------------------------------------------- #
def _relative_name(root: Path, path: Path) -> str:
    """相对正斜杠名字。绝对路径一旦出现在结果里就等于把用户目录端出去了。"""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - resolve_inside 已经挡在外面
        return path.name


def tool_inspect_target(root: Path, arguments: dict) -> dict:
    target = str(arguments.get("target") or "")
    if not target.strip():
        return _tool_result(
            is_error=True,
            text="缺少参数 target（相对授权根目录的产物路径）",
            structured={"kind": "bad_arguments", "target": target},
        )
    try:
        name, raw = artifact.load_target(str(root), target)
    except artifact.TargetUnavailable as error:
        # 目标读不到与解析失败是两类事：前者是"没拿到实物"，后者是"拿到了读不下去"
        return _tool_result(
            is_error=True,
            text=f"目标不可读｜被解析对象 <{target}>｜{error}",
            structured={"kind": "target_unavailable", "target": target, "reason": str(error)},
        )
    try:
        report = artifact.inspect_bytes(name, raw)
    except artifact.ParseFailure as failure:
        return _tool_result(
            is_error=True,
            text=failure.reason,
            structured={"failure": failure.as_dict(), "findings": []},
        )
    return _tool_result(
        is_error=False,
        text=_summary_text(report),
        structured=report,
    )


def _summary_text(report: dict) -> str:
    lines = [f"被解析对象 <{report['target']}>：{report['format']}，{report['size']:,} 字节"]
    for item in report["findings"]:
        lines.append(f"- {item['kind']}｜{item['value']}（依据 {item['evidence_id']}）")
    for note in report["limitations"]:
        lines.append(f"- 限制｜{note}")
    return "\n".join(lines)


def tool_scan_targets(root: Path, arguments: dict) -> dict:
    from blank_api.api.errors import ApiError
    from blank_api.services.workspace import resolve_inside

    sub = str(arguments.get("path") or "")
    try:
        limit = int(arguments.get("limit") or 80)
    except (TypeError, ValueError):
        limit = 80
    limit = max(1, min(MAX_SCAN_FILES, limit))
    try:
        base = resolve_inside(root, sub)
    except ApiError as error:
        return _tool_result(
            is_error=True,
            text=f"目录不可读｜路径 <{sub or '.'}>｜{error.message}",
            structured={"kind": "target_unavailable", "path": sub, "reason": error.message},
        )
    if not base.is_dir():
        return _tool_result(
            is_error=True,
            text=f"路径不是目录（{sub}）",
            structured={"kind": "not_a_directory", "path": sub},
        )

    # 与列目录同一套忽略口径：node_modules/.git 这类目录里有几万个文件，
    # 遍历它们会把一次工具调用拖到分钟级。
    from blank_api.services.workspace import ignored_name

    def _head(path: Path) -> bytes:
        try:
            with path.open("rb") as handle:
                return handle.read(16)
        except OSError:  # 读不到的文件不该让整次列举失败
            return b""

    rows: list[dict] = []
    skipped = 0

    def walk(directory: Path, depth: int) -> bool:
        nonlocal skipped
        for child in sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if ignored_name(child.name):
                continue
            if child.is_dir():
                if depth < SCAN_DEPTH:
                    if not walk(child, depth + 1):
                        return False
                continue
            if len(rows) >= limit:
                skipped += 1
                continue
            rows.append(
                {
                    "target": _relative_name(root, child),
                    "size": child.stat().st_size,
                    "format": artifact.identify(_head(child)),
                    "runnable_guess": child.suffix.lower() in {".exe", ".dll", ".so", ".node", ".asar", ".elf"},
                }
            )
        return len(rows) < limit

    walk(base, 1)
    detected = [row for row in rows if row["format"]]
    return _tool_result(
        is_error=False,
        text=(
            f"列举 {len(rows)} 个文件：可认领格式 {len(detected)} 个"
            f"（{'、'.join(sorted({row['format'] for row in detected}) or '无')}）"
            + (f"，另有 {skipped} 个因超出上限未列举" if skipped else "")
        ),
        structured={
            "path": sub,
            "limit": limit,
            "truncated": skipped,
            "targets": rows,
            "note": "只比对幻数前 16 字节，没有解析正文",
        },
    )


def tool_find_strings(root: Path, arguments: dict) -> dict:
    target = str(arguments.get("target") or "")
    try:
        min_length = max(2, int(arguments.get("min_length") or 4))
    except (TypeError, ValueError):
        min_length = 4
    try:
        limit = max(1, min(500, int(arguments.get("limit") or 80)))
    except (TypeError, ValueError):
        limit = 80
    try:
        name, raw = artifact.load_target(str(root), target)
    except artifact.TargetUnavailable as error:
        return _tool_result(
            is_error=True,
            text=f"目标不可读｜被解析对象 <{target}>｜{error}",
            structured={"kind": "target_unavailable", "target": target, "reason": str(error)},
        )
    window = raw[:STRINGS_WINDOW]
    hits = []
    for match in _PRINTABLE.finditer(window):
        if len(match.group()) < min_length:
            continue
        hits.append({"offset": match.start(), "text": match.group().decode("ascii", "replace")})
        if len(hits) >= limit:
            break
    if not hits:
        # 「没有线索」与「读不下去」必须长得不一样：这条空结果也要能说清为什么空
        return _tool_result(
            is_error=False,
            text=f"<{name}> 的前 {len(window):,} 字节里没有长度 ≥{min_length} 的可读串",
            structured={"target": name, "strings": [], "scanned_bytes": len(window), "min_length": min_length},
        )
    return _tool_result(
        is_error=False,
        text="\n".join(f"0x{item['offset']:08x}  {item['text'][:120]}" for item in hits),
        structured={
            "target": name,
            "strings": hits,
            "scanned_bytes": len(window),
            "min_length": min_length,
            "note": "只扫前导区间，加壳/压缩后的正文本就抽不出串",
        },
    )


HANDLERS = {
    "inspect_target": tool_inspect_target,
    "scan_targets": tool_scan_targets,
    "find_strings": tool_find_strings,
}


# --------------------------------------------------------------------------- #
# 分发
# --------------------------------------------------------------------------- #
def handle(message: dict, root: Path) -> dict | None:
    """一条入站消息 → 一条回包（通知没有回包，返回 None）。"""
    request_id = message.get("id")
    method = str(message.get("method") or "")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    if request_id is None:  # 通知
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools_payload()}}
    if method == "tools/call":
        name = str(params.get("name") or "")
        handler = HANDLERS.get(name)
        if handler is None:
            declared = {item["name"] for item in tools_payload()}
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32602,
                    "message": (
                        f"未知工具 {name}"
                        + ("" if declared else "（本服务端没有声明任何工具）")
                        + (f"，已声明：{'、'.join(sorted(declared))}" if declared else "")
                    ),
                },
            }
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            return {"jsonrpc": "2.0", "id": request_id, "result": handler(root, arguments)}
        except Exception as error:  # noqa: BLE001 - 服务端崩了也要回一条，而不是让客户端等超时
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _tool_result(
                    is_error=True,
                    text=f"服务端内部错误｜被解析工具 {name}｜{error.__class__.__name__}: {error}",
                    structured={"kind": "server_internal_error"},
                ),
            }

    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"不支持的方法：{method}"}}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="blank_api.services.rea.mcp_server")
    parser.add_argument("--root", required=True, help="授权根目录：所有 target 都相对它解析")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    root = Path(args.root)
    if not root.is_dir():
        # 这句是给客户端的 _exit_reason() 看的：进程退出的原因要能被原样端给用户。
        # 路径要过 mask_path：这条 stderr 会一路走到 HTTP 响应体和事件时间线，
        # 宿主绝对路径在那里出现就等于把用户的目录结构端出去了。
        from blank_api.services.workspace import mask_path

        sys.stderr.write(
            f"REA MCP 服务端启动失败：授权根目录不存在或不是目录（{mask_path(args.root)}）\n"
        )
        return 3
    root = root.resolve()
    sys.stderr.write(f"REA MCP 服务端就绪：{SERVER_NAME} {SERVER_VERSION}，工具 {len(tools_payload())} 个\n")
    for line in sys.stdin.buffer:
        text = line.decode("utf-8", "replace").strip()
        if not text or not text.startswith("{"):
            continue
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        reply = handle(message, root)
        if reply is not None:
            _write(reply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
