"""run_shell 超时也转后台。

`run_tests` 先做了这件事，`run_shell` 一直没做——而模型没主动选 `run_in_background`
不代表这条命令短：真机里最常见的恰恰是"它以为是快的，其实要跑 25 分钟"。
两家的做法一致（DeepSeek Harness 的 background job、opencode 的转后台并通知），
回执里都要明说"别轮询、后台是从头跑的"。
"""

from __future__ import annotations

import asyncio
import pathlib
import types

import pytest

from blank_api.services import shell_tasks
from blank_api.services.tools import shell as shell_module
from blank_api.services.tools.base import ToolContext


def _live_context(tmp_path: pathlib.Path) -> ToolContext:
    return ToolContext(root=tmp_path, task_id="t", session_factory=lambda: None)


def _result(**over) -> dict:
    base = {
        "stdout": "partial output", "stderr": "", "exit_code": -1, "timed_out": True,
        "sandbox": "local", "tier": {}, "enforcement": {}, "timeout_budget": 30,
    }
    base.update(over)
    return base


@pytest.fixture
def slow_command(monkeypatch):
    async def fake(*args, **kwargs):
        return _result()

    monkeypatch.setattr(shell_module, "execute_command", fake)


def test_a_timed_out_shell_command_is_handed_to_the_background(tmp_path, slow_command, monkeypatch):
    started = {}

    def fake_start(**kwargs):
        started.update(kwargs)
        return types.SimpleNamespace(shell_id="sh-shell-1")

    monkeypatch.setattr(shell_tasks, "start", fake_start)

    outcome = asyncio.run(shell_module._run_shell(_live_context(tmp_path), {"command": "npm run build"}))

    assert outcome.status == "timeout"
    assert outcome.data["background_shell_id"] == "sh-shell-1"
    assert "转到后台" in outcome.message
    assert "不要为此 sleep" in outcome.message, "回执必须拦住模型去轮询"
    assert started.get("command") == "npm run build"


def test_a_failed_handover_still_reports_the_timeout(tmp_path, slow_command, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("后台管理器没起来")

    monkeypatch.setattr(shell_tasks, "start", boom)

    outcome = asyncio.run(shell_module._run_shell(_live_context(tmp_path), {"command": "npm run build"}))

    assert outcome.status == "timeout"
    assert outcome.data["background_shell_id"] == ""
    assert "没起来" in outcome.message


def test_no_session_means_no_background_process(tmp_path, slow_command, monkeypatch):
    """没有会话工厂（单测/外部集成）就不起后台：没人在收尾时清理子进程。"""
    def must_not_run(**kwargs):
        raise AssertionError("没有会话工厂却起了后台任务")

    monkeypatch.setattr(shell_tasks, "start", must_not_run)

    context = ToolContext(root=tmp_path, task_id="t", session_factory=None)
    outcome = asyncio.run(shell_module._run_shell(context, {"command": "npm run build"}))

    assert outcome.status == "timeout"
    assert outcome.data["background_shell_id"] == ""
