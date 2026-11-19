"""改完自动校验：机制在这里，命令由你给。

社区口径很清楚（HumanLayer / Addy Osmani）：**成功沉默、失败喧哗**。它解决的
不是"多一道检查"，而是"模型不必自己想起来验证"——真机里它连调两次 `run_tests`
都超时，最后干脆不验了。

为什么不给默认命令：跑什么校验是项目知识（pytest / npm test / tsc / 自定义脚本）。
猜错的代价我们刚付过——把全仓 25 分钟的后端单测当成"改完就跑"的检查，
撞上 180 秒上限，一个能跑的页面被判成失败。所以这里只提供机制，
命令从 `BLANK_POST_EDIT_CHECK` 读，**没配就整段空转，零成本**。

判据盯四件事：不该跑的时候一次都不跑、该跑的时候真跑、成功不留痕迹、
失败必须带原文和退出码。
"""

from __future__ import annotations

from blank_api.services.agent.verify_hook import (
    EDITS_BETWEEN_CHECKS,
    ERROR_TAIL_CHARS,
    STEPS_BETWEEN_CHECKS,
    argv_of,
    check_command,
    failure_note,
    should_check,
)


def test_without_a_configured_command_nothing_ever_runs(monkeypatch):
    monkeypatch.delenv("BLANK_POST_EDIT_CHECK", raising=False)

    assert check_command() == ""
    assert should_check(command="", edits_since_check=99, steps_since_check=99, changed_in_this_step=True) is False


def test_one_edit_is_not_enough(monkeypatch):
    """改一个文件就跑一次，等于每步都在跑测试。"""
    assert (
        should_check(
            command="pytest -q",
            edits_since_check=EDITS_BETWEEN_CHECKS - 1,
            steps_since_check=STEPS_BETWEEN_CHECKS,
            changed_in_this_step=True,
        )
        is False
    )


def test_too_soon_since_the_last_check(monkeypatch):
    assert (
        should_check(
            command="pytest -q",
            edits_since_check=EDITS_BETWEEN_CHECKS,
            steps_since_check=STEPS_BETWEEN_CHECKS - 1,
            changed_in_this_step=True,
        )
        is False
    )


def test_a_step_that_changed_nothing_does_not_trigger():
    assert (
        should_check(
            command="pytest -q",
            edits_since_check=99,
            steps_since_check=99,
            changed_in_this_step=False,
        )
        is False
    )


def test_all_conditions_met_runs_exactly_once():
    assert should_check(
        command="pytest -q",
        edits_since_check=EDITS_BETWEEN_CHECKS,
        steps_since_check=STEPS_BETWEEN_CHECKS,
        changed_in_this_step=True,
    )


def test_the_command_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("BLANK_POST_EDIT_CHECK", "  npm run typecheck  ")

    assert check_command() == "npm run typecheck", "前后空白要清掉，否则启动器会当成另一个命令"
    assert argv_of(check_command()) == ["npm run typecheck"]


def test_a_failure_note_carries_the_command_exit_code_and_the_tail():
    note = failure_note("pytest -q", 1, "E   AssertionError: 期望 5，实际 3\n" * 200)

    assert "pytest -q" in note and "退出码 1" in note
    assert len(note) < ERROR_TAIL_CHARS + 300, "失败原文也要截断，否则一次断言把上下文灌满"
    assert "AssertionError" in note


def test_a_failure_without_output_says_why_that_matters():
    note = failure_note("tsc --noEmit", 127, "")

    assert "没有输出" in note, "空输出最常见的两种原因（命令不存在、环境没就绪）要写出来"
