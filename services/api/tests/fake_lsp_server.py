"""A tiny LSP server over stdio, used by the tests.

It speaks the real wire format — ``Content-Length: N\\r\\n\\r\\n<body>`` — because
the point is to exercise framing, request ids, server-initiated requests and
document synchronization, not a mock that agrees with us.

Modes:

* ``normal``       — deterministic results for all nine operations
* ``empty``        — every operation returns an empty result
* ``multi_def``    — definition returns two locations in two files
* ``single_ref``   — references returns exactly one location
* ``empty_calls``  — prepareCallHierarchy finds an item, but the call lists are empty
* ``boom``         — every request answers JSON-RPC error -32000
* ``slow``         — requests are accepted and never answered
* ``server_request`` — asks ``workspace/configuration`` before answering a definition
* ``crash``        — dies during startup after writing one stderr line
* ``noisy``        — writes 200 KiB to stderr before speaking the protocol

Every message (both directions) is appended to the log file given as argv[2] as
one JSON object per line. Tests use that log as the evidence for what was
actually sent on the wire: the 1-based→0-based conversion, ``includeDeclaration``,
``didOpen``/``didChange`` and the two-step call hierarchy.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse


def _reconfigure() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


class Wire:
    def __init__(self, log_path: str) -> None:
        self.input = sys.stdin.buffer
        self.output = sys.stdout.buffer
        self.log_path = log_path

    def read(self) -> dict | None:
        headers: dict[bytes, bytes] = {}
        while True:
            line = self.input.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                break
            if b":" in line:
                name, _, value = line.partition(b":")
                headers[name.strip().lower()] = value.strip()
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return None
        if length <= 0:
            return None
        chunks = []
        remaining = length
        while remaining > 0:
            chunk = self.input.read(remaining)
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        try:
            message = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        self._log("in", message)
        return message if isinstance(message, dict) else None

    def send(self, message: dict) -> None:
        self._log("out", message)
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.output.write(b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body)
        self.output.flush()

    def _log(self, direction: str, message: dict) -> None:
        if not self.log_path:
            return
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"dir": direction, "message": message}, ensure_ascii=False) + "\n")


class Server:
    def __init__(self, mode: str, wire: Wire) -> None:
        self.mode = mode
        self.wire = wire
        self.root_uri = ""
        self.documents: dict[str, str] = {}
        self.server_request_id = 900
        self.configuration_answer: object = "unanswered"

    # -- 协议 ---------------------------------------------------------------
    def run(self) -> int:
        while True:
            message = self.wire.read()
            if message is None:
                return 0
            if message.get("method") == "exit":
                return 0
            self.handle(message)
            if message.get("method") == "textDocument/definition" and self.mode == "server_request":
                # 客户端不回 workspace/configuration 的话，这里会一直等下去；
                # 用例以"定义请求超时"失败，正好把"双向请求"这条判据点亮。
                self.ask_configuration()
                self.reply(message, self.definition())

    def handle(self, message: dict) -> None:
        method = str(message.get("method") or "")
        if not method:
            if message.get("id") == self.server_request_id:
                self.configuration_answer = message.get("result")
            return
        request_id = message.get("id")
        notify = request_id is None
        if method == "initialize":
            params = message.get("params") or {}
            self.root_uri = str(params.get("rootUri") or "")
            if self.mode == "crash":
                sys.stderr.write("lsp 配置缺 runtime 路径\n")
                sys.stderr.flush()
                raise SystemExit(3)
            result = {
                "capabilities": {
                    "definitionProvider": True,
                    "referencesProvider": True,
                    "hoverProvider": True,
                    "documentSymbolProvider": True,
                    "workspaceSymbolProvider": True,
                    "implementationProvider": True,
                    "callHierarchyProvider": True,
                },
                "serverInfo": {"name": "fake-lsp", "version": "0.1"},
            }
            self.reply(message, result)
            return
        if notify:
            self.note_notification(method, message.get("params") or {})
            return
        if method == "shutdown":
            self.reply(message, None)
            return
        if self.mode == "slow" and method != "initialized":
            return
        if self.mode == "boom":
            self.wire.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32000, "message": "server exploded"},
                }
            )
            return

        empty = self.mode in ("empty", "empty_calls")
        if method == "textDocument/definition":
            if self.mode == "server_request":
                # run() 会先发 workspace/configuration、收到回包后再答这一条。
                return
            if empty:
                self.reply(message, [])
            else:
                self.reply(message, self.definitions_two() if self.mode == "multi_def" else self.definition())
        elif method == "textDocument/references":
            if empty:
                self.reply(message, [])
            else:
                self.reply(message, self.references_one() if self.mode == "single_ref" else self.references())
        elif method == "textDocument/hover":
            self.reply(message, None if empty else self.hover())
        elif method == "textDocument/documentSymbol":
            self.reply(message, [] if empty else self.document_symbols())
        elif method == "workspace/symbol":
            self.reply(message, [] if empty else self.workspace_symbols())
        elif method == "textDocument/implementation":
            self.reply(message, [] if empty else self.definition())
        elif method == "textDocument/prepareCallHierarchy":
            if self.mode == "empty":
                self.reply(message, [])
            elif self.mode == "empty_calls":
                self.reply(message, [self.call_item("target", 7)])
            else:
                self.reply(message, [self.call_item("outer", 7), self.call_item("inner", 12)])
        elif method == "callHierarchy/incomingCalls":
            self.reply(message, [] if empty else self.incoming())
        elif method == "callHierarchy/outgoingCalls":
            self.reply(message, [] if empty else self.outgoing())
        else:
            self.wire.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )

    def note_notification(self, method: str, params: dict) -> None:
        if method == "textDocument/didOpen":
            document = params.get("textDocument") or {}
            if document.get("uri"):
                self.documents[str(document["uri"])] = str(document.get("text") or "")
        elif method == "textDocument/didChange":
            document = params.get("textDocument") or {}
            changes = params.get("contentChanges") or []
            if document.get("uri") and changes and isinstance(changes[0], dict):
                self.documents[str(document["uri"])] = str(changes[0].get("text") or "")

    def reply(self, message: dict, result) -> None:
        self.wire.send({"jsonrpc": "2.0", "id": message.get("id"), "result": result})

    def ask_configuration(self) -> None:
        self.wire.send(
            {
                "jsonrpc": "2.0",
                "id": self.server_request_id,
                "method": "workspace/configuration",
                "params": {"items": [{"section": "fake"}]},
            }
        )
        deadline = time.time() + 5
        while self.configuration_answer == "unanswered" and time.time() < deadline:
            message = self.wire.read()
            if message is None:
                return
            if message.get("id") == self.server_request_id and not message.get("method"):
                self.configuration_answer = message.get("result")
            else:
                # 客户端在等我们回包；这里只处理握手后的通知，其余照常记账。
                self.handle(message)

    # -- 结果 ---------------------------------------------------------------
    def uri(self, name: str) -> str:
        return self.root_uri.rstrip("/") + "/" + urllib.parse.quote(name)

    def definition(self) -> list[dict]:
        return [
            {
                "uri": self.uri("a.fake"),
                "range": {"start": {"line": 1, "character": 2}, "end": {"line": 1, "character": 8}},
            }
        ]

    def definitions_two(self) -> list[dict]:
        return [
            {
                "uri": self.uri("a.fake"),
                "range": {"start": {"line": 1, "character": 2}, "end": {"line": 1, "character": 8}},
            },
            {
                "uri": self.uri("b.fake"),
                "range": {"start": {"line": 3, "character": 0}, "end": {"line": 3, "character": 3}},
            },
        ]

    def references(self) -> list[dict]:
        return [
            {
                "uri": self.uri("a.fake"),
                "range": {"start": {"line": 1, "character": 2}, "end": {"line": 1, "character": 8}},
            },
            {
                "uri": self.uri("b.fake"),
                "range": {"start": {"line": 3, "character": 0}, "end": {"line": 3, "character": 3}},
            },
        ]

    def references_one(self) -> list[dict]:
        return self.references()[:1]

    def hover(self) -> dict:
        return {
            "contents": {"kind": "markdown", "value": "**文档** 类型信息"},
            "range": {"start": {"line": 2, "character": 4}, "end": {"line": 2, "character": 9}},
        }

    def document_symbols(self) -> list[dict]:
        return [
            {
                "name": "Greeter",
                "kind": 5,
                "detail": "class",
                "range": {"start": {"line": 0, "character": 0}, "end": {"line": 9, "character": 0}},
                "children": [
                    {
                        "name": "hello",
                        "kind": 6,
                        "detail": "()",
                        "range": {
                            "start": {"line": 2, "character": 2},
                            "end": {"line": 4, "character": 3},
                        },
                    }
                ],
            },
            {
                "name": "main",
                "kind": 12,
                "range": {"start": {"line": 5, "character": 0}, "end": {"line": 8, "character": 0}},
            },
        ]

    def workspace_symbols(self) -> list[dict]:
        return [
            {
                "name": "alpha",
                "kind": 12,
                "location": {
                    "uri": self.uri("a.fake"),
                    "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 5}},
                },
            },
            {
                "name": "beta",
                "kind": 5,
                "containerName": "NS",
                "location": {
                    "uri": self.uri("ignored.fake"),
                    "range": {"start": {"line": 2, "character": 0}, "end": {"line": 2, "character": 4}},
                },
            },
            {
                "name": "gamma",
                "kind": 12,
                "location": {
                    "uri": self.uri("b.fake"),
                    "range": {"start": {"line": 8, "character": 0}, "end": {"line": 8, "character": 5}},
                },
            },
        ]

    def call_item(self, name: str, line: int) -> dict:
        return {
            "name": name,
            "kind": 12,
            "detail": "fn",
            "uri": self.uri("a.fake"),
            "range": {"start": {"line": line, "character": 0}, "end": {"line": line, "character": 5}},
            "selectionRange": {"start": {"line": line, "character": 0}, "end": {"line": line, "character": 5}},
        }

    def incoming(self) -> list[dict]:
        return [
            {
                "from": {
                    "name": "caller",
                    "kind": 12,
                    "uri": self.uri("a.fake"),
                    "range": {"start": {"line": 2, "character": 0}, "end": {"line": 2, "character": 6}},
                },
                "fromRanges": [{"start": {"line": 3, "character": 4}, "end": {"line": 3, "character": 10}}],
            },
            {
                "from": {
                    "name": "other",
                    "kind": 12,
                    "uri": self.uri("b.fake"),
                    "range": {"start": {"line": 6, "character": 0}, "end": {"line": 6, "character": 5}},
                },
                "fromRanges": [],
            },
        ]

    def outgoing(self) -> list[dict]:
        return [
            {
                "to": {
                    "name": "callee",
                    "kind": 12,
                    "uri": self.uri("b.fake"),
                    "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 6}},
                },
                "fromRanges": [{"start": {"line": 3, "character": 1}, "end": {"line": 3, "character": 7}}],
            }
        ]


def main() -> int:
    _reconfigure()
    mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
    log_path = sys.argv[2] if len(sys.argv) > 2 else ""
    if mode == "noisy":
        for _ in range(2000):
            sys.stderr.write("debug: 正在连接上游 " + "x" * 64 + "\n")
        sys.stderr.flush()
    return Server(mode, Wire(log_path)).run()


if __name__ == "__main__":
    raise SystemExit(main())
