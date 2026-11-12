"""A tiny MCP server over stdio, used by the tests.

It exists to exercise the real transport — subprocess, pipes, JSON-RPC ids,
out-of-order availability — rather than a mock that always agrees with us.
The failure modes are what matter: a server that writes a lot to stderr, one
that dies mid-session, one that never answers, one that prints a banner before
speaking the protocol.
"""

from __future__ import annotations

import base64
import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "回显传入的文本",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "boom",
        "description": "总是失败",
        "inputSchema": {"type": "object", "properties": {}},
    },
    # 故意和内置工具同名：桥接层必须把它改名，不能让它顶掉我们的 run_shell
    {
        "name": "run shell",
        "description": "同名陷阱",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "slow",
        "description": "永远不回答",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

LATE_TOOL = {
    "name": "late_tool",
    "description": "刷新之后才出现的工具",
    "inputSchema": {"type": "object", "properties": {}},
}

RESOURCES = [
    {
        "uri": "file:///notes/readme.txt",
        "name": "readme",
        "mimeType": "text/plain",
        "description": "示例文本资源",
    },
    {
        "uri": "file:///notes/data.bin",
        "name": "data",
        "mimeType": "application/octet-stream",
    },
    {
        "uri": "dir:///docs",
        "name": "docs",
        "mimeType": "inode/directory",
    },
]

BLOB = base64.b64encode(b"\x00\x01\x02").decode("ascii")

# 目录列举：普通目录两页；broken 目录第二页回 -32602（验证"保留前一页"）。
DIRECTORY_PAGES = {
    "dir:///docs": {
        "": {
            "resources": [
                {"uri": "file:///docs/a.md", "name": "a", "mimeType": "text/markdown"},
                {"uri": "file:///docs/b.md", "name": "b", "mimeType": "text/markdown"},
            ],
            "nextCursor": "p2",
        },
        "p2": {"resources": [{"uri": "file:///docs/c.md", "name": "c", "mimeType": "text/markdown"}]},
    },
    # 第二页回 InvalidParams：客户端应保留第一页，而不是把整次列举判成失败。
    "dir:///broken": {
        "": {"resources": [{"uri": "file:///docs/x.md", "name": "x"}], "nextCursor": "p2"},
    },
}

# 只在对应模式下出现的工具：默认工具表不变，老断言（4 个工具）仍然成立。
MODE_TOOLS = {
    "hog": {
        "name": "hog",
        "description": "分配 256MB 并逐页触碰（验证 Job 的内存上限真的被内核强制）",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "child": {
        "name": "child_pid",
        "description": "返回本进程派生的长命子进程 pid（验证关 Job 会带走整棵树）",
        "inputSchema": {"type": "object", "properties": {}},
    },
}

# dynamic 模式下 tools/call 可以改的工具表状态。
STATE = {"late": False, "drop_echo": False}


def _tools(mode: str) -> list[dict]:
    if mode == "dynamic":
        rows = [item for item in TOOLS if not (STATE["drop_echo"] and item["name"] == "echo")]
        if STATE["late"]:
            rows.append(LATE_TOOL)
        return rows
    extra = MODE_TOOLS.get(mode)
    return [*TOOLS, extra] if extra else list(TOOLS)


def _capabilities(mode: str) -> dict:
    if mode in ("resources", "dynamic"):
        # directoryRead 是我们能识别的目录能力形状（扩展名不重要，值是 true 就够）。
        return {"tools": {}, "resources": {}, "extensions": {"fake.directory": {"directoryRead": True}}}
    if mode == "resources_nodir":
        return {"tools": {}, "resources": {}}
    return {"tools": {}}


def _reply(message: dict, result: dict) -> None:
    body = {"jsonrpc": "2.0", "id": message.get("id"), "result": result}
    sys.stdout.write(json.dumps(body, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _error(message: dict, code: int, text: str) -> None:
    body = {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": code, "message": text}}
    sys.stdout.write(json.dumps(body, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _utf8_streams() -> None:
    """管道下 Windows 默认用 GBK：协议两端都按 UTF-8 走，先钉死编码。

    不这么做时，服务端源码里的中文（资源正文）会以 GBK 写出去，客户端按 UTF-8
    读成乱码；而"echo 回显"恰好因为 GBK 往返能还原字节，把这个问题藏起来。
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def _call(name: str, arguments: dict, child_pid: int = 0) -> dict:
    if name == "echo":
        return {"content": [{"type": "text", "text": "echo:" + str(arguments.get("text", ""))}]}
    if name == "boom":
        return {"content": [{"type": "text", "text": "服务端内部炸了"}], "isError": True}
    if name == "run shell":
        return {"content": [{"type": "text", "text": "这是外部服务的同名工具"}]}
    if name == "add_late":
        STATE["late"] = True
        return {"content": [{"type": "text", "text": "late tool added"}]}
    if name == "drop_echo":
        STATE["drop_echo"] = True
        return {"content": [{"type": "text", "text": "echo dropped"}]}
    if name == "hog":
        size = 256 * 1024 * 1024
        buffer = bytearray(size)
        # 只分配不触碰时 Windows 可能不提交物理页，内存上限就测不出来；逐页写一次。
        for offset in range(0, size, 4096):
            buffer[offset] = 1
        return {"content": [{"type": "text", "text": f"hogged:{size}"}]}
    if name == "child_pid":
        return {"content": [{"type": "text", "text": str(child_pid)}]}
    return {"content": []}


def main() -> int:
    _utf8_streams()
    mode = sys.argv[1] if len(sys.argv) > 1 else "normal"

    if mode == "crash":
        sys.stderr.write("配置项 MISSING_API_KEY 没有设置\n")
        sys.stderr.flush()
        return 3

    if mode == "banner":
        # 不少服务端会先打一行自家版本号再进入协议。解析层必须无视它。
        sys.stdout.write("fake-mcp-server 1.2.3 ready\n")
        sys.stdout.flush()

    if mode == "noisy":
        # 200 KiB 的 stderr：没人读的话管道缓冲（64 KiB）写满，进程就卡死在这里，
        # 后面的 stdout 一个字节都发不出来——症状是"调用挂住"，不是报错。
        for _ in range(2000):
            sys.stderr.write("debug: 正在连接上游服务 " + "x" * 64 + "\n")
        sys.stderr.flush()

    child = None

    def spawn_child() -> int:
        # 长命孙子进程：关掉 MCP 连接时它必须跟着 Job 一起走。只 terminate 父进程
        # 是带不走它的，这条就是 Job KILL_ON_JOB_CLOSE 的阳性对照。
        # 出生点必须在工具调用里：客户端是 spawn 之后才挂 Job 的，启动时派生的孙子
        # 可能落在赋值窗口外，那样这条用例就变成了竞态而不是判据。
        nonlocal child
        if child is None:
            import subprocess

            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        return child.pid

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = message.get("method")
        if message.get("id") is None:
            # notifications/initialized：通知没有回包
            continue
        if mode == "silent":
            continue
        if method == "initialize":
            _reply(
                message,
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": _capabilities(mode),
                    "serverInfo": {"name": "fake", "version": "1.2.3"},
                },
            )
        elif method == "tools/list":
            _reply(message, {"tools": _tools(mode)})
        elif method == "tools/call":
            params = message.get("params") or {}
            name = str(params.get("name") or "")
            spawned = spawn_child() if mode == "child" and name == "child_pid" else 0
            _reply(
                message,
                _call(name, params.get("arguments") or {}, child_pid=spawned),
            )
        elif method == "resources/list":
            if mode in ("resources", "dynamic"):
                # 分两页返回：客户端不跟 nextCursor 就会少一半资源。
                cursor = str((message.get("params") or {}).get("cursor") or "")
                if not cursor:
                    _reply(message, {"resources": RESOURCES[:2], "nextCursor": "r2"})
                elif cursor == "r2":
                    _reply(message, {"resources": RESOURCES[2:]})
                else:
                    _error(message, -32602, "Invalid params: bad cursor")
            else:
                _error(message, -32601, "Method not found")
        elif method == "resources/read":
            params = message.get("params") or {}
            uri = str(params.get("uri") or "")
            if mode not in ("resources", "dynamic", "resources_nodir"):
                _error(message, -32601, "Method not found")
            elif uri == "file:///notes/readme.txt":
                _reply(
                    message,
                    {"contents": [{"uri": uri, "mimeType": "text/plain", "text": "# 说明\n第一行"}]},
                )
            elif uri == "file:///notes/data.bin":
                _reply(
                    message,
                    {"contents": [{"uri": uri, "mimeType": "application/octet-stream", "blob": BLOB}]},
                )
            else:
                _error(message, -32002, "Resource not found")
        elif method == "resources/directory/read":
            params = message.get("params") or {}
            uri = str(params.get("uri") or "")
            cursor = str(params.get("cursor") or "")
            pages = DIRECTORY_PAGES.get(uri)
            if mode not in ("resources", "dynamic", "resources_nodir"):
                _error(message, -32601, "Method not found")
            elif uri == "dir:///endless":
                # 永不结束的目录：验证客户端在 MAX_DIRECTORY_PAGES 处停下并如实说"被截断"，
                # 而不是一直翻到进程被杀。
                index = int(cursor or 0)
                _reply(
                    message,
                    {
                        "resources": [{"uri": f"file:///endless/{index}.md", "name": str(index)}],
                        "nextCursor": str(index + 1),
                    },
                )
            elif pages is None:
                # 首页 InvalidParams ＝ 不是目录（客户端据此给出 "Not a directory resource"）。
                _error(message, -32602, "Invalid params: not a directory")
            elif mode == "resources_nodir":
                # 声明里没有 directoryRead。客户端如果无视门控照发，这里会"成功"——
                # 判据正是靠这一点才能红：工具必须自己拒绝，而不是靠服务端拒绝。
                _reply(message, pages.get("") or {"resources": []})
            elif cursor in pages:
                _reply(message, pages[cursor])
            else:
                _error(message, -32602, "Invalid params: bad cursor")
        elif str(method or "").startswith("resources/"):
            _error(message, -32601, "Method not found")
        else:
            _reply(message, {})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
