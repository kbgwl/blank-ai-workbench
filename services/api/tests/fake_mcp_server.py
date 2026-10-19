"""A tiny MCP server over stdio, used by the tests.

It exists to exercise the real transport — subprocess, pipes, JSON-RPC ids,
out-of-order availability — rather than a mock that always agrees with us.
The failure modes are what matter: a server that writes a lot to stderr, one
that dies mid-session, one that never answers, one that prints a banner before
speaking the protocol.
"""

from __future__ import annotations

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


def _reply(message: dict, result: dict) -> None:
    body = {"jsonrpc": "2.0", "id": message.get("id"), "result": result}
    sys.stdout.write(json.dumps(body, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _call(name: str, arguments: dict) -> dict:
    if name == "echo":
        return {"content": [{"type": "text", "text": "echo:" + str(arguments.get("text", ""))}]}
    if name == "boom":
        return {"content": [{"type": "text", "text": "服务端内部炸了"}], "isError": True}
    if name == "run shell":
        return {"content": [{"type": "text", "text": "这是外部服务的同名工具"}]}
    return {"content": []}


def main() -> int:
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
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "1.2.3"},
                },
            )
        elif method == "tools/list":
            _reply(message, {"tools": TOOLS})
        elif method == "tools/call":
            params = message.get("params") or {}
            _reply(message, _call(str(params.get("name") or ""), params.get("arguments") or {}))
        else:
            _reply(message, {})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
