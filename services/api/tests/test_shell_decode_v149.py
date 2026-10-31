"""cmd.exe 的中文输出要能读出来，否则「命令写法错了」的提示等于没装。

本机是中文 Windows，cmd.exe 按 GBK 输出。原先 ``_decode`` 用
``utf-8 + errors="replace"``，于是整句变成 ``'ls' �����ڲ����ⲿ���``：
模型读不懂，``_UNKNOWN_COMMAND_MARKS`` 那两条中文标记也永远匹配不上，
``shell_mismatch_hint`` 直接返回空串——下一轮它还会照着 POSIX 再写一遍。
"""

from __future__ import annotations

import pytest

from blank_api.services.tools import shell as shell_tools_mod
from blank_api.services.tools.shell import _decode, shell_mismatch_hint

GBK_STDERR = "'ls' 不是内部或外部命令，也不是可运行的程序或批处理文件。".encode("gbk")


def test_decode_reads_cmd_gbk_output():
    assert "不是内部或外部命令" in _decode(GBK_STDERR)


def test_decode_keeps_utf8_output():
    text = "编译通过，用时 1.2 秒"
    assert _decode(text.encode("utf-8")) == text


def test_decode_never_raises_on_garbage():
    assert isinstance(_decode(b"\xff\xfe\x00bad\x81"), str)


def test_mismatch_hint_fires_on_chinese_cmd_error(monkeypatch):
    """提示函数必须能认出「不是内部或外部命令」——这正是原先断掉的一环。"""
    monkeypatch.setattr(shell_tools_mod, "shell_dialect", lambda: "cmd")

    hint = shell_mismatch_hint("ls -la", _decode(GBK_STDERR))

    assert "cmd.exe" in hint
    assert "ls → dir" in hint


@pytest.mark.parametrize("command", ["pwd", "cat a.txt", "grep -r x ."])
def test_mismatch_hint_covers_common_posix_commands(monkeypatch, command):
    monkeypatch.setattr(shell_tools_mod, "shell_dialect", lambda: "cmd")
    assert shell_mismatch_hint(command, _decode(GBK_STDERR))
