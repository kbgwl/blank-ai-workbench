"""跑不完的测试命令要记住，下次别再白等一次。

真机一天撞了三次同一个形状：模型改了 `deliverables/` 下的静态页面，调 `run_tests {}`，
自动识别到的是**仓库根的 `npm test`**（全量后端单测 ~25 分钟 vs 单次 180 秒上限），
必然超时；它白等 3 分钟、烧一轮上下文，最后还被判失败（后者已单独修）。

不去猜"这次该跑哪个"——`src/` 改动 + 根 `tests/` 这类结构会让任何猜测误伤。
只记一个确定的事实：这条命令在这台机器上跑不完。
"""

from __future__ import annotations

import json
import pathlib

import pytest

from blank_api.services.tools import testing
from blank_api.services.tools.base import ToolContext


@pytest.fixture
def history(tmp_path, monkeypatch):
    path = tmp_path / "test-command-history.json"
    monkeypatch.setattr(testing, "_history_path", lambda: path)
    return path


def _context(tmp_path) -> ToolContext:
    return ToolContext(root=tmp_path, task_id="t", session_factory=None)


def _result(**over) -> dict:
    base = {
        "stdout": "", "stderr": "", "exit_code": 0, "timed_out": False,
        "sandbox": {}, "tier": {}, "enforcement": {}, "timeout_budget": 180,
    }
    base.update(over)
    return base


def test_a_timed_out_command_is_remembered_and_skipped_next_time(tmp_path, monkeypatch, history):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "npm test"}}), encoding="utf-8")

    async def slow(*args, **kwargs):
        return _result(timed_out=True, timeout_budget=180)

    monkeypatch.setattr(testing, "execute_command", slow)
    import asyncio

    first = asyncio.run(testing._run_tests(_context(tmp_path), {}))
    assert first.status == "timeout"
    assert history.exists(), "超时之后没记住，下一个任务还会再撞一次"

    second = asyncio.run(testing._run_tests(_context(tmp_path), {}))
    assert second.status == "skipped", "同一条命令又跑了一遍"
    assert "上次就没跑完" in second.message
    assert "command 显式指定" in second.message


def test_an_explicit_command_is_never_blocked(tmp_path, monkeypatch, history):
    """记忆只拦"没指定时的自动识别"。模型明确要跑，就让它跑。"""
    history.write_text(json.dumps({"npm test": {"timeout_seconds": 180, "at": "x"}}), encoding="utf-8")
    called = {}

    async def record(argv, *args, **kwargs):
        called["argv"] = argv
        return _result()

    monkeypatch.setattr(testing, "execute_command", record)
    import asyncio

    result = asyncio.run(testing._run_tests(_context(tmp_path), {"command": "npm test"}))
    assert called.get("argv"), "显式指定的命令被拦住了"
    assert result.ok


def test_a_failure_is_not_a_reason_to_remember(tmp_path, monkeypatch, history):
    """失败是有结论的（代码没过），超时才是没有结论的。混在一起会让真失败的测试被跳过。"""
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "npm test"}}), encoding="utf-8")

    async def failing(*args, **kwargs):
        return _result(exit_code=1, stdout="1 failed")

    monkeypatch.setattr(testing, "execute_command", failing)
    import asyncio

    result = asyncio.run(testing._run_tests(_context(tmp_path), {}))
    assert result.status == "failed"
    assert testing.load_command_history() == {}, "失败被记成「跑不完」，下次就会漏掉真失败"


def test_a_broken_history_file_does_not_block_testing(tmp_path, monkeypatch, history):
    history.write_text("{ 这不是 JSON", encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "npm test"}}), encoding="utf-8")

    async def fine(*args, **kwargs):
        return _result()

    monkeypatch.setattr(testing, "execute_command", fine)
    import asyncio

    result = asyncio.run(testing._run_tests(_context(tmp_path), {}))
    assert result.ok, "一份坏掉的「经验」不该让测试跑不了"


def test_the_history_is_capped(history):
    for index in range(testing.HISTORY_LIMIT + 6):
        testing.remember_timeout(f"cmd-{index}", 100 + index)
    saved = json.loads(history.read_text(encoding="utf-8"))
    assert len(saved) <= testing.HISTORY_LIMIT
    # 留下的是最近的：最后写进去的一定在
    assert f"cmd-{testing.HISTORY_LIMIT + 5}" in saved
