# -*- coding: utf-8 -*-
"""py 启动器改写与两类误报提示的回归测试。

真实台账（2026-11-23「给我写一个项目计划书」）：模型用 `py -3` 找可装包的
Python，被工具改写成了应用自带的精简解释器，得到「Unknown option: -3」——
它据此以为机器上没有 Python，又连试了十条命令。这里锁住三件事：

1. `py -3` / `py -0p` 原样保留，`py -m 模块` 才替换成项目解释器；
2. 解释器没有 pip 时，回执里直接说"换解释器或别装包"；
3. cmd 里误用 `| tail` / `| head` 时，回执里给 cmd 口径的替代做法。
"""
from __future__ import annotations

from blank_api.services.tools import shell as shell_mod
from blank_api.services.tools.shell import (
    _BARE_PYTHON,
    normalize_python_command,
    pip_missing_hint,
    posix_text_tool_hint,
)


def _make_venv(root):
    venv = root / ".venv" / "Scripts"
    venv.mkdir(parents=True)
    (venv / "python.exe").write_text("")


def test_py_launcher_flags_survive_rewrite(tmp_path):
    _make_venv(tmp_path)
    assert "py" not in _BARE_PYTHON
    assert normalize_python_command("py -3 -c \"print(1)\"", tmp_path) == "py -3 -c \"print(1)\""
    assert normalize_python_command("py -0p", tmp_path) == "py -0p"
    assert normalize_python_command("py --list", tmp_path) == "py --list"


def test_py_module_still_uses_project_venv(tmp_path):
    _make_venv(tmp_path)
    out = normalize_python_command("py -m pytest -q", tmp_path)
    assert out.startswith(str(tmp_path / ".venv" / "Scripts" / "python.exe"))


def test_pip_hint_needs_pip_in_command_and_output():
    cmd = "python -m pip install python-docx"
    assert pip_missing_hint(cmd, "python.exe: No module named pip")
    # 有 pip 但装不上（版本/网络）不是这条提示管的范围，别抢别的失败的诊断。
    assert pip_missing_hint(cmd, "ERROR: Could not find a version") == ""
    assert pip_missing_hint("python -c \"import docx\"", "No module named pip") == ""
    assert pip_missing_hint(cmd, "Successfully installed python-docx") == ""


def test_posix_text_hint_only_on_cmd(monkeypatch):
    command = "python -c \"print(1)\" 2>&1 | tail -5"
    output = "tail: option used in invalid context -- 5"
    monkeypatch.setattr(shell_mod, "shell_dialect", lambda: "cmd")
    hint = posix_text_tool_hint(command, output)
    assert "head" in hint and "重定向" in hint
    monkeypatch.setattr(shell_mod, "shell_dialect", lambda: "sh")
    assert posix_text_tool_hint(command, output) == ""


def test_posix_text_hint_ignores_normal_failures(monkeypatch):
    monkeypatch.setattr(shell_mod, "shell_dialect", lambda: "cmd")
    assert posix_text_tool_hint("python -m pytest", "3 failed, 1 passed") == ""
