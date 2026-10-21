"""shell 方言：让模型一开始就用对命令。

Windows 上 run_shell 走 cmd.exe，而模型默认按 POSIX 写。真实库里 83 次
run_shell 调用有 21 次失败，其中相当一部分是 ``'pwd' 不是内部或外部命令``
——错一次重试一次，既慢又贵，而且完全可以在第一次就说清楚。
"""

from __future__ import annotations

import os

from blank_api.services.tools.shell import shell_dialect, shell_mismatch_hint, shell_tools


def test_tool_description_states_the_real_shell():
    description = shell_tools()[0].description
    if shell_dialect() == "cmd":
        assert "cmd.exe" in description
        assert "ls" in description, "要明确点名不能用的命令，模型才改得过来"
    else:
        assert "sh" in description


def test_posix_command_on_windows_gets_a_fixable_hint():
    if os.name != "nt":
        return
    hint = shell_mismatch_hint("pwd && ls", "'pwd' 不是内部或外部命令，也不是可运行的程序")
    assert "cmd.exe" in hint
    assert "pwd → cd" in hint
    assert "list_files" in hint, "除了换命令，更要引导它用专用工具"


def test_other_failures_do_not_get_the_windows_blame():
    if os.name != "nt":
        return
    # 测试没通过、命令本身不存在，都不该被说成"用错了 shell"
    assert shell_mismatch_hint("python -m pytest -q", "1 failed, 3 passed") == ""
