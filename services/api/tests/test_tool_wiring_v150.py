"""接线测试：底层函数测绿了，不代表工具层真的接上了。

这一组专门盯「参数有没有一路送到磁盘」：

- ``read_file`` 的 offset/limit 从工具参数 → workspace 读取（也包括 fs-broker 那条 IPC）；
- ``apply_patch`` 的 Codex 包装从工具参数 → 真正写进文件。

单测只验函数本身时，漏掉一根线（比如 broker 那条只传了 path）测试照样全绿，
用户在真机上却读不到半个文件。这类断线只能靠端到端接线用例发现。
"""

from __future__ import annotations

import pytest

from blank_api.db import get_session_factory
from blank_api.services.tools import fs_broker
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.filesystem import _read_file
from blank_api.services.tools.patch import _patch_on_disk


@pytest.fixture(autouse=True)
def _no_broker(monkeypatch):
    """默认走宿主路径；broker 单独用一条用例验它自己的转发。"""
    monkeypatch.setattr(fs_broker, "broker_enabled", lambda: False)


def _context(root) -> ToolContext:
    return ToolContext(root=root, task_id="task-wiring", session_factory=get_session_factory())


def _big(workspace, total: int = 40) -> None:
    lines = [f"line{index:03d}" for index in range(1, total + 1)]
    (workspace / "big.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def test_read_file_tool_forwards_offset_and_limit(workspace) -> None:
    _big(workspace)

    outcome = await _read_file(_context(workspace), {"path": "big.txt", "offset": 11, "limit": 5})

    assert outcome.ok, outcome.message
    assert outcome.data["start_line"] == 11
    assert outcome.data["end_line"] == 15
    assert outcome.data["total_lines"] == 40
    assert outcome.output.splitlines()[0] == "line011"


async def test_read_file_without_offset_still_returns_the_whole_file(workspace) -> None:
    _big(workspace, total=4)

    outcome = await _read_file(_context(workspace), {"path": "big.txt"})

    assert outcome.data["start_line"] == 1
    assert outcome.data["end_line"] == 4
    assert len(outcome.output.splitlines()) == 4


async def test_broker_branch_carries_the_window_too(workspace) -> None:
    """fs-broker 是另一条 IPC 路径：它少传一个参数，用户开了 broker 就回到旧行为。"""
    _big(workspace)
    payloads: list[dict] = []

    async def fake_call(root, task_id, op, args):
        payloads.append(dict(args))
        # 用真实实现回答，验证的是"参数送到了"，不是"broker 自己会算"。
        from blank_api.services.workspace import read_text_file

        window = {key: int(args[key]) for key in ("offset", "limit") if args.get(key) is not None}
        return {"result": {"payload": read_text_file(root, args["path"], **window)}}

    import blank_api.services.tools.filesystem as filesystem

    monkeypatch_target = filesystem._broker
    original = monkeypatch_target

    def fake_broker():
        class Proxy:
            broker_enabled = staticmethod(lambda: True)
            call_broker = staticmethod(fake_call)
            broker_success_marker = staticmethod(lambda *args, **kwargs: {})

        return Proxy()

    filesystem._broker = fake_broker
    try:
        outcome = await _read_file(_context(workspace), {"path": "big.txt", "offset": 3, "limit": 2})
    finally:
        filesystem._broker = original

    assert payloads and payloads[0]["offset"] == 3 and payloads[0]["limit"] == 2
    assert outcome.data["start_line"] == 3
    assert outcome.output.splitlines() == ["line003", "line004"]


def test_patch_on_disk_accepts_the_codex_wrapper(workspace) -> None:
    """模型发 ``*** Begin Patch`` 时，磁盘侧必须真的落下文件，而不是回一句"缺少段落头"。"""
    result = _patch_on_disk(
        workspace,
        "task-wiring",
        {"patch": "*** Begin Patch\n*** Add File: index.html\n+<h1>hi</h1>\n+<p>x</p>\n*** End Patch"},
    )

    assert "patch_error" not in result, result
    assert result["path"] == "index.html"
    assert (workspace / "index.html").read_text(encoding="utf-8") == "<h1>hi</h1>\n<p>x</p>"


def test_patch_on_disk_keeps_plain_patches_working(workspace) -> None:
    (workspace / "main.py").write_text("print('hi')\n", encoding="utf-8")

    result = _patch_on_disk(
        workspace,
        "task-wiring",
        {"patch": "--- a/main.py\n+++ b/main.py\n@@ -1,1 +1,2 @@\n print('hi')\n+print('more')\n"},
    )

    assert "patch_error" not in result, result
    assert (workspace / "main.py").read_text(encoding="utf-8") == "print('hi')\nprint('more')\n"
