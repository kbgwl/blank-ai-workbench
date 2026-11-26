"""LSP 单工具九操作对齐 Claude 的判据（R5）。

四层，每层都能单独失败：

1. **线上协议**：Content-Length 帧、initialize/initialized、1-based→0-based 只减一次、
   ``references`` 带 ``includeDeclaration``、``didOpen`` 后每次请求前用全文
   ``didChange`` 同步、服务端反向请求（workspace/configuration）被应答、stderr
   灌满 200 KiB 也不会把进程卡死。
2. **九种操作**：definition/references/hover/documentSymbol/workspaceSymbol/
   implementation/prepareCallHierarchy/incomingCalls/outgoingCalls 的文本结果与
   计数口径（resultCount/fileCount）逐字对齐。
3. **隔离与失败**：没有语言服务器、文件过大、初始化崩溃、请求超时、服务端报错
   各自说自己的话；畸形输入在拉起进程之前被拒绝。
4. **装配**：单工具默认收起（shouldDefer）、可检索、三读可并行；运行时把每轮的
   ``LSPServerManager`` 注入 ToolContext，任务收尾时把进程关掉；子代理工具表里
   没有 ``lsp``（任务级管理器不外溢到隔离副本）。

外部依赖只有 ``tests/fake_lsp_server.py`` 的子进程与 ``git``（git-ignore 那一格）；
每个用例自己关管理器，不碰用户 home 的 ``lsp.json``。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.lsp import LSPServerManager
from blank_api.services.models.base import AdapterConfig, ModelAdapter, ModelResponse, Usage
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.registry import DEFERRED_TOOLS, PARALLEL_SAFE_TOOLS, ToolRegistry

SERVER = Path(__file__).with_name("fake_lsp_server.py")

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


def _tool(name: str = "lsp"):
    return {tool.name: tool for tool in build_default_tools()}[name]


def _workspace(tmp_path: Path, git: bool = False) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.fake").write_text("line1\nline2\nline3\n", encoding="utf-8")
    (root / "b.fake").write_text("bee\n", encoding="utf-8")
    if git:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        (root / ".gitignore").write_text("ignored.fake\n", encoding="utf-8")
        (root / "ignored.fake").write_text("hidden\n", encoding="utf-8")
    return root


def _config(
    tmp_path: Path,
    mode: str = "normal",
    log: Path | None = None,
    ext: str = ".fake",
    # 默认给足 15s：全量套件跑在负载高的机器上时，5s 启动+应答会把调度抖动
    # 误报成 LSP 失败。超时语义另有 timeout=1.0 的专用用例。
    timeout: float = 15.0,
    entry: dict | None = None,
) -> Path:
    body = {
        "command": sys.executable,
        "args": [str(SERVER), mode, str(log or "")],
        "extensionToLanguage": {ext: "fake"},
        "timeout": timeout,
    }
    if entry:
        body.update(entry)
    path = tmp_path / "lsp.json"
    path.write_text(json.dumps({"lspServers": {"fake": body}}), encoding="utf-8")
    return path


def _manager(tmp_path: Path, root: Path, **kwargs) -> LSPServerManager:
    return LSPServerManager(path=_config(tmp_path, **kwargs), root=root)


async def _call(manager, root: Path, **arguments):
    context = ToolContext(root=root, task_id="task-lsp", session_factory=None, lsp=manager)
    return await _tool().handler(context, arguments)


def _messages(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [
        json.loads(line)["message"]
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _methods(log: Path) -> list[str]:
    return [str(message.get("method")) for message in _messages(log) if message.get("method")]


def _definition_args(root: Path, **extra) -> dict:
    return {
        "operation": "goToDefinition",
        "filePath": "a.fake",
        "line": 3,
        "character": 5,
        **extra,
    }


# --------------------------------------------------------------------------- #
# 1. 线上协议
# --------------------------------------------------------------------------- #
async def test_definition_is_one_based_and_the_request_is_zero_based(tmp_path):
    root = _workspace(tmp_path)
    log = tmp_path / "wire.log"
    manager = _manager(tmp_path, root, log=log)
    try:
        outcome = await _call(manager, root, **_definition_args(root))
    finally:
        await manager.close()

    assert outcome.ok is True
    assert outcome.output == "Defined in a.fake:2:3"
    assert outcome.data["resultCount"] == 1
    assert outcome.data["fileCount"] == 1

    messages = _messages(log)
    methods = [str(message.get("method")) for message in messages if message.get("method")]
    assert "initialize" in methods and "initialized" in methods
    assert methods.index("textDocument/didOpen") < methods.index("textDocument/definition")

    request = next(
        message for message in messages if message.get("method") == "textDocument/definition"
    )
    assert request["params"]["position"] == {"line": 2, "character": 4}
    assert request["params"]["textDocument"]["uri"].startswith("file://")
    assert request["params"]["textDocument"]["uri"].endswith("a.fake")

    opened = next(
        message for message in messages if message.get("method") == "textDocument/didOpen"
    )
    assert opened["params"]["textDocument"]["languageId"] == "fake"
    assert opened["params"]["textDocument"]["text"].startswith("line1")


async def test_references_include_declaration_and_group_by_file(tmp_path):
    root = _workspace(tmp_path)
    log = tmp_path / "wire.log"
    manager = _manager(tmp_path, root, log=log)
    try:
        outcome = await _call(
            manager,
            root,
            operation="findReferences",
            filePath="a.fake",
            line=2,
            character=3,
        )
    finally:
        await manager.close()

    assert outcome.ok is True
    assert outcome.output == (
        "Found 2 references across 2 files:\n"
        "\n"
        "a.fake:\n"
        "  Line 2:3\n"
        "\n"
        "b.fake:\n"
        "  Line 4:1"
    )
    assert (outcome.data["resultCount"], outcome.data["fileCount"]) == (2, 2)

    request = next(
        message
        for message in _messages(log)
        if message.get("method") == "textDocument/references"
    )
    assert request["params"]["context"] == {"includeDeclaration": True}
    assert request["params"]["position"] == {"line": 1, "character": 2}


async def test_multiple_definitions_are_one_per_line(tmp_path):
    root = _workspace(tmp_path)
    manager = _manager(tmp_path, root, mode="multi_def")
    try:
        outcome = await _call(manager, root, **_definition_args(root))
    finally:
        await manager.close()

    assert outcome.ok is True
    assert outcome.output == (
        "Found 2 definitions:\n"
        "  a.fake:2:3\n"
        "  b.fake:4:1"
    )
    assert (outcome.data["resultCount"], outcome.data["fileCount"]) == (2, 2)


async def test_single_reference_is_two_spaces_on_the_next_line(tmp_path):
    root = _workspace(tmp_path)
    manager = _manager(tmp_path, root, mode="single_ref")
    try:
        outcome = await _call(
            manager, root, operation="findReferences", filePath="a.fake", line=2, character=3
        )
    finally:
        await manager.close()

    assert outcome.ok is True
    assert outcome.output == "Found 1 reference:\n  a.fake:2:3"
    assert (outcome.data["resultCount"], outcome.data["fileCount"]) == (1, 1)


async def test_second_request_syncs_new_file_text_with_did_change(tmp_path):
    root = _workspace(tmp_path)
    log = tmp_path / "wire.log"
    manager = _manager(tmp_path, root, log=log)
    try:
        await _call(manager, root, **_definition_args(root))
        (root / "a.fake").write_text("changed\ncontent\n", encoding="utf-8")
        await _call(manager, root, **_definition_args(root))
    finally:
        await manager.close()

    changes = [
        message
        for message in _messages(log)
        if message.get("method") == "textDocument/didChange"
    ]
    assert len(changes) == 1
    document = changes[0]["params"]["textDocument"]
    assert document["version"] == 2
    assert changes[0]["params"]["contentChanges"] == [{"text": "changed\ncontent\n"}]


async def test_server_initiated_configuration_request_is_answered(tmp_path):
    root = _workspace(tmp_path)
    log = tmp_path / "wire.log"
    manager = _manager(tmp_path, root, mode="server_request", log=log, timeout=15.0)
    try:
        outcome = await _call(manager, root, **_definition_args(root))
    finally:
        await manager.close()

    assert outcome.ok is True
    assert outcome.output == "Defined in a.fake:2:3"
    messages = _messages(log)
    answered = [
        message
        for message in messages
        if message.get("id") == 900 and not message.get("method")
    ]
    assert len(answered) == 1
    assert answered[0]["result"] == [None]


async def test_noisy_stderr_does_not_block_the_protocol(tmp_path):
    root = _workspace(tmp_path)
    manager = _manager(tmp_path, root, mode="noisy")
    try:
        outcome = await _call(manager, root, **_definition_args(root))
    finally:
        await manager.close()

    assert outcome.ok is True
    assert outcome.output == "Defined in a.fake:2:3"


async def test_concurrent_calls_start_only_one_server(tmp_path):
    root = _workspace(tmp_path)
    log = tmp_path / "wire.log"
    manager = _manager(tmp_path, root, log=log)
    try:
        outcomes = await asyncio.gather(
            *(_call(manager, root, **_definition_args(root)) for _ in range(3))
        )
    finally:
        await manager.close()

    assert all(outcome.ok for outcome in outcomes)
    assert all(outcome.output == "Defined in a.fake:2:3" for outcome in outcomes)
    assert _methods(log).count("initialize") == 1


# --------------------------------------------------------------------------- #
# 2. 九种操作
# --------------------------------------------------------------------------- #
async def test_hover_returns_the_markdown_and_a_one_based_position(tmp_path):
    root = _workspace(tmp_path)
    manager = _manager(tmp_path, root)
    try:
        outcome = await _call(
            manager, root, operation="hover", filePath="a.fake", line=3, character=5
        )
    finally:
        await manager.close()

    assert outcome.ok is True
    assert outcome.output == "Hover info at 3:5:\n**文档** 类型信息"
    assert (outcome.data["resultCount"], outcome.data["fileCount"]) == (1, 1)


async def test_document_symbols_are_nested_and_counted_recursively(tmp_path):
    root = _workspace(tmp_path)
    manager = _manager(tmp_path, root)
    try:
        outcome = await _call(
            manager, root, operation="documentSymbol", filePath="a.fake", line=1, character=1
        )
    finally:
        await manager.close()

    assert outcome.ok is True
    assert outcome.output == (
        "Document symbols:\n"
        "Greeter (Class) class - Line 1\n"
        "  hello (Method) () - Line 3\n"
        "main (Function) - Line 6"
    )
    assert (outcome.data["resultCount"], outcome.data["fileCount"]) == (3, 1)


async def test_workspace_symbols_use_the_query_and_drop_ignored_files(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git 不在 PATH，无法验证 check-ignore 过滤")
    root = _workspace(tmp_path, git=True)
    log = tmp_path / "wire.log"
    manager = _manager(tmp_path, root, log=log)
    try:
        outcome = await _call(
            manager,
            root,
            operation="workspaceSymbol",
            filePath="a.fake",
            line=1,
            character=1,
            query="a",
        )
    finally:
        await manager.close()

    assert outcome.ok is True
    assert outcome.output == (
        "Found 2 symbols in workspace:\n"
        "\n"
        "a.fake:\n"
        "  alpha (Function) - Line 2\n"
        "\n"
        "b.fake:\n"
        "  gamma (Function) - Line 9"
    )
    assert (outcome.data["resultCount"], outcome.data["fileCount"]) == (2, 2)
    request = next(
        message for message in _messages(log) if message.get("method") == "workspace/symbol"
    )
    assert request["params"] == {"query": "a"}


async def test_workspace_symbols_keep_everything_outside_a_repository(tmp_path):
    root = _workspace(tmp_path)  # 不是 git 仓库：没有忽略表可问，不该误删结果
    manager = _manager(tmp_path, root)
    try:
        outcome = await _call(
            manager,
            root,
            operation="workspaceSymbol",
            filePath="a.fake",
            line=1,
            character=1,
            query="a",
        )
    finally:
        await manager.close()

    assert outcome.ok is True
    assert "beta (Class) - Line 3 in NS" in outcome.output
    assert outcome.data["resultCount"] == 3


async def test_go_to_implementation_uses_the_definition_wording(tmp_path):
    root = _workspace(tmp_path)
    manager = _manager(tmp_path, root)
    try:
        outcome = await _call(
            manager,
            root,
            operation="goToImplementation",
            filePath="a.fake",
            line=3,
            character=5,
        )
    finally:
        await manager.close()

    assert outcome.ok is True
    assert outcome.output == "Defined in a.fake:2:3"


async def test_prepare_call_hierarchy_lists_each_item(tmp_path):
    root = _workspace(tmp_path)
    manager = _manager(tmp_path, root)
    try:
        outcome = await _call(
            manager,
            root,
            operation="prepareCallHierarchy",
            filePath="a.fake",
            line=8,
            character=1,
        )
    finally:
        await manager.close()

    assert outcome.ok is True
    assert outcome.output == (
        "Found 2 call hierarchy items:\n"
        "  outer (Function) - a.fake:8 [fn]\n"
        "  inner (Function) - a.fake:13 [fn]"
    )
    assert (outcome.data["resultCount"], outcome.data["fileCount"]) == (2, 1)


async def test_incoming_calls_prepare_then_ask_with_the_first_item(tmp_path):
    root = _workspace(tmp_path)
    log = tmp_path / "wire.log"
    manager = _manager(tmp_path, root, log=log)
    try:
        outcome = await _call(
            manager,
            root,
            operation="incomingCalls",
            filePath="a.fake",
            line=8,
            character=1,
        )
    finally:
        await manager.close()

    assert outcome.ok is True
    assert outcome.output == (
        "Found 2 incoming calls:\n"
        "\n"
        "a.fake:\n"
        "  caller (Function) - Line 3 [calls at: 4:5]\n"
        "\n"
        "b.fake:\n"
        "  other (Function) - Line 7"
    )
    assert (outcome.data["resultCount"], outcome.data["fileCount"]) == (2, 2)

    methods = [
        message
        for message in _messages(log)
        if message.get("method") in ("textDocument/prepareCallHierarchy", "callHierarchy/incomingCalls")
    ]
    assert [message["method"] for message in methods] == [
        "textDocument/prepareCallHierarchy",
        "callHierarchy/incomingCalls",
    ]
    item = methods[1]["params"]["item"]
    assert item["name"] == "outer"
    assert item["uri"].endswith("a.fake")


async def test_outgoing_calls_use_the_called_from_label(tmp_path):
    root = _workspace(tmp_path)
    manager = _manager(tmp_path, root)
    try:
        outcome = await _call(
            manager,
            root,
            operation="outgoingCalls",
            filePath="a.fake",
            line=8,
            character=1,
        )
    finally:
        await manager.close()

    assert outcome.ok is True
    assert outcome.output == (
        "Found 1 outgoing call:\n"
        "\n"
        "b.fake:\n"
        "  callee (Function) - Line 2 [called from: 4:2]"
    )
    assert (outcome.data["resultCount"], outcome.data["fileCount"]) == (1, 1)


@pytest.mark.parametrize(
    ("operation", "expected", "counts"),
    [
        ("goToDefinition", _NO_DEFINITION, (0, 0)),
        ("findReferences", _NO_REFERENCES, (0, 0)),
        ("hover", _NO_HOVER, (0, 0)),
        ("documentSymbol", _NO_DOC_SYMBOLS, (0, 0)),
        ("workspaceSymbol", _NO_WORKSPACE_SYMBOLS, (0, 0)),
        ("goToImplementation", _NO_DEFINITION, (0, 0)),
        ("prepareCallHierarchy", _NO_CALL_ITEM, (0, 0)),
        ("incomingCalls", _NO_CALL_ITEM, (0, 0)),
        ("outgoingCalls", _NO_CALL_ITEM, (0, 0)),
    ],
)
async def test_empty_results_say_why_they_are_empty(tmp_path, operation, expected, counts):
    root = _workspace(tmp_path)
    manager = _manager(tmp_path, root, mode="empty")
    try:
        outcome = await _call(
            manager,
            root,
            operation=operation,
            filePath="a.fake",
            line=3,
            character=5,
            query="x",
        )
    finally:
        await manager.close()

    assert outcome.ok is True
    assert outcome.output == expected
    assert (outcome.data["resultCount"], outcome.data["fileCount"]) == counts


async def test_empty_call_lists_use_their_own_wording(tmp_path):
    root = _workspace(tmp_path)
    manager = _manager(tmp_path, root, mode="empty_calls")
    try:
        incoming = await _call(
            manager, root, operation="incomingCalls", filePath="a.fake", line=8, character=1
        )
        outgoing = await _call(
            manager, root, operation="outgoingCalls", filePath="a.fake", line=8, character=1
        )
    finally:
        await manager.close()

    assert incoming.output == "No incoming calls found (nothing calls this function)"
    assert outgoing.output == "No outgoing calls found (this function calls nothing)"
    assert incoming.data["resultCount"] == 0 and outgoing.data["fileCount"] == 0


# --------------------------------------------------------------------------- #
# 3. 隔离与失败
# --------------------------------------------------------------------------- #
async def test_missing_server_is_reported_not_guessed(tmp_path):
    root = _workspace(tmp_path)
    (root / "notes.txt").write_text("plain\n", encoding="utf-8")
    manager = _manager(tmp_path, root, ext=".fake")
    try:
        outcome = await _call(
            manager, root, operation="goToDefinition", filePath="notes.txt", line=1, character=1
        )
    finally:
        await manager.close()

    assert outcome.ok is True
    assert outcome.output == "No LSP server available for file type: .txt"
    assert set(outcome.data) == {"operation", "result", "filePath"}


async def test_oversized_file_is_rejected_before_the_server_is_touched(tmp_path):
    root = _workspace(tmp_path)
    log = tmp_path / "wire.log"
    big = root / "big.fake"
    with big.open("wb") as handle:
        handle.seek(10_000_000)
        handle.write(b"x")
    manager = _manager(tmp_path, root, log=log)
    try:
        outcome = await _call(
            manager, root, operation="goToDefinition", filePath="big.fake", line=1, character=1
        )
    finally:
        await manager.close()

    assert outcome.ok is True
    assert outcome.output == "File too large for LSP analysis (11MB exceeds 10MB limit)"
    assert "resultCount" not in outcome.data
    assert _messages(log) == []


async def test_input_guards_fail_before_any_server_starts(tmp_path):
    root = _workspace(tmp_path)
    log = tmp_path / "wire.log"
    manager = _manager(tmp_path, root, log=log)
    try:
        missing = await _call(
            manager, root, operation="goToDefinition", filePath="ghost.fake", line=1, character=1
        )
        not_file = await _call(
            manager, root, operation="goToDefinition", filePath=str(root), line=1, character=1
        )
        relative_dir = await _call(
            manager, root, operation="goToDefinition", filePath=".", line=1, character=1
        )
        bad_line = await _call(manager, root, **_definition_args(root, line=0))
        no_path = await _call(
            manager, root, operation="goToDefinition", filePath="", line=1, character=1
        )
        bad_operation = await _call(manager, root, **_definition_args(root, operation="teleport"))
    finally:
        await manager.close()

    assert missing.ok is False
    assert missing.message == "File does not exist: ghost.fake"
    assert not_file.ok is False
    assert not_file.message == f"Path is not a file: {root}"
    assert relative_dir.ok is False
    assert relative_dir.message == "Path is not a file: ."
    assert bad_line.ok is False
    assert bad_line.message == "Invalid input: line must be a positive integer (1-based)"
    assert no_path.ok is False
    assert no_path.message == "Invalid input: filePath is required"
    assert bad_operation.ok is False
    assert bad_operation.message.startswith("Invalid input: operation must be one of:")
    assert _messages(log) == []


async def test_server_error_is_reported_for_that_operation(tmp_path):
    root = _workspace(tmp_path)
    manager = _manager(tmp_path, root, mode="boom")
    try:
        outcome = await _call(manager, root, **_definition_args(root))
    finally:
        await manager.close()

    assert outcome.ok is True
    assert outcome.output == "Error performing goToDefinition: server exploded"
    assert "resultCount" not in outcome.data


async def test_initialization_crash_names_the_stderr_line(tmp_path):
    root = _workspace(tmp_path)
    manager = _manager(tmp_path, root, mode="crash")
    try:
        outcome = await _call(manager, root, **_definition_args(root))
    finally:
        await manager.close()

    assert outcome.ok is True
    assert outcome.output.startswith("LSP initialization failed:")
    assert "lsp 配置缺 runtime 路径" in outcome.output


async def test_a_failed_server_is_not_respawned_on_every_call(tmp_path):
    root = _workspace(tmp_path)
    log = tmp_path / "wire.log"
    manager = _manager(tmp_path, root, mode="crash", log=log)
    try:
        first = await _call(manager, root, **_definition_args(root))
        second = await _call(manager, root, **_definition_args(root))
    finally:
        await manager.close()

    assert first.output == second.output
    # 每次拉起都会重新写一遍 initialize；只有第一次真的尝试过
    assert _methods(log).count("initialize") == 1


async def test_request_timeout_is_not_silence(tmp_path):
    root = _workspace(tmp_path)
    manager = _manager(tmp_path, root, mode="slow", timeout=1.0)
    try:
        outcome = await _call(manager, root, **_definition_args(root))
    finally:
        await manager.close()

    assert outcome.ok is True
    assert outcome.output.startswith("Error performing goToDefinition:")
    assert "1s" in outcome.output


def test_invalid_config_entries_are_listed_and_skipped(tmp_path):
    root = _workspace(tmp_path)
    path = tmp_path / "lsp.json"
    path.write_text(
        json.dumps({"lspServers": {"fake": {"command": sys.executable, "args": []}}}),
        encoding="utf-8",
    )
    manager = LSPServerManager(path=path, root=root)
    assert manager.config_for(root / "a.fake") is None
    assert manager.errors == ["fake: 缺少 extensionToLanguage（不知道它管哪些文件）"]


def test_manager_close_is_idempotent(tmp_path):
    root = _workspace(tmp_path)

    async def scenario():
        manager = _manager(tmp_path, root)
        try:
            await _call(manager, root, **_definition_args(root))
            client = manager.clients["fake"]
            assert client.alive is True
            await manager.close()
            assert client.alive is False
            assert client._process is not None
            await asyncio.wait_for(client._process.wait(), timeout=5)
            # 第二次关一个已经关干净的管理器：不炸，也不假装还握着进程
            await manager.close()
            assert manager.clients == {}

            # 关掉之后同一个管理器还能重新拉起一个全新的服务端进程
            again = await _call(manager, root, **_definition_args(root))
            assert again.ok is True
            assert again.output.startswith("Defined in a.fake:2:3")
            fresh = manager.clients["fake"]
            assert fresh is not client
            assert fresh.alive is True
        finally:
            await manager.close()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 4. 装配
# --------------------------------------------------------------------------- #
def test_lsp_is_deferred_searchable_and_parallel_safe():
    names = {tool.name for tool in build_default_tools()}
    assert "lsp" in names
    assert "lsp" in DEFERRED_TOOLS
    assert "lsp" in PARALLEL_SAFE_TOOLS

    registry = ToolRegistry(build_default_tools())
    matched, _ = registry.search("查看符号定义")
    assert "lsp" in {tool.name for tool in matched}
    matched, _ = registry.search("谁调用了这个函数")
    assert "lsp" in {tool.name for tool in matched}


def test_subagents_do_not_get_the_task_level_lsp_manager():
    """子代理跑在隔离副本里，拿到的是兜底管理器，进程没人收；工具表直接不给。"""
    from types import SimpleNamespace

    from blank_api.services.subagent import _child_tools

    tools = [SimpleNamespace(name=name) for name in ("read_file", "lsp", "skill", "repl")]
    assert [t.name for t in _child_tools(tools, skill_catalog=True)] == ["read_file", "skill"]
    assert [t.name for t in _child_tools(tools, skill_catalog=False)] == ["read_file"]


def test_schema_matches_the_nine_operations_shape():
    schema = _tool().input_schema
    assert schema["required"] == ["operation", "filePath", "line", "character"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["operation"]["enum"] == [
        "goToDefinition",
        "findReferences",
        "hover",
        "documentSymbol",
        "workspaceSymbol",
        "goToImplementation",
        "prepareCallHierarchy",
        "incomingCalls",
        "outgoingCalls",
    ]
    assert schema["properties"]["line"]["type"] == "integer"
    assert schema["properties"]["query"]["type"] == "string"


class _StubAdapter(ModelAdapter):
    def __init__(self) -> None:
        super().__init__(
            AdapterConfig(
                name="stub",
                kind="openai",
                base_url="http://localhost",
                api_key="",
                model="stub",
                supports_tools=True,
            )
        )

    async def invoke(self, messages, **kwargs):  # pragma: no cover
        return ModelResponse(text="{}", usage=Usage(1, 1), model="stub")

    async def stream(self, messages, **kwargs):  # pragma: no cover
        yield ""

    async def bind_tools(self, specs, messages, **kwargs):  # pragma: no cover
        return ModelResponse(text="{}", usage=Usage(1, 1), model="stub")


async def test_runtime_injects_a_per_run_lsp_manager(tmp_path: Path, workspace: Path, monkeypatch):
    config = tmp_path / "lsp.json"
    config.write_text(
        json.dumps(
            {
                "lspServers": {
                    "fake": {
                        "command": sys.executable,
                        "args": [str(SERVER), "normal", ""],
                        "extensionToLanguage": {".py": "python"},
                        "timeout": 15,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BLANK_LSP_FILE", str(config))
    monkeypatch.setenv("BLANK_MCP_FILE", str(tmp_path / "missing-mcp.json"))

    runtime = AgentRuntime(session_factory=None)
    request = RunRequest(
        task_id="task-lsp-runtime",
        provider_id="p",
        provider_name="stub",
        adapter=_StubAdapter(),
        root=workspace,
        goal="查一下这个符号定义在哪",
    )
    scope = await runtime._prepare(request, "task-lsp-runtime:1")
    try:
        manager = scope.session.context.lsp
        assert isinstance(manager, LSPServerManager)
        assert manager.root == scope.session.context.root
        assert scope.session.gateway.get("lsp") is not None
        assert "lsp" in scope.session.gateway.registry.deferred_names()
    finally:
        await scope.session.context.lsp.close()


async def test_finish_closes_the_lsp_manager_even_when_waiting_approval(monkeypatch):
    from types import SimpleNamespace

    closed: list[bool] = []

    class Manager:
        async def close(self):
            closed.append(True)

    class Store:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, model, key):
            return None

    scope = SimpleNamespace(
        task_id="task-lsp-finish",
        store=lambda: Store(),
        subagents=None,
        session=SimpleNamespace(context=SimpleNamespace(lsp=Manager())),
    )
    runtime = AgentRuntime(None)
    # 等待审批不是终点，但语言服务器进程留着没有用；恢复后会按需重拉。
    await runtime._finish(scope, "waiting_approval", summary="", reason="")
    assert closed == [True]
