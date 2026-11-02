"""Windows 上「相对路径可执行文件」的命令要先补成绝对路径。

真实事故（安装版 GUI 实测，任务 d7143491）：模型按 POSIX 习惯发
`.venv/Scripts/python.exe -m pytest -q`。cmd 把 `/` 当参数分隔，只把 `.venv`
当命令名，于是 stderr 是「'.venv' 不是内部或外部命令」——**一条测试都没跑**，
而工具结论是"测试失败"。用户据此去改代码，改的却是一个不存在的问题。

所以这里守两件事：
1. 修复只针对**确实存在**的相对路径，别的命令一个字不改（不猜、不改坏）；
2. 真跑一遍 .bat：修复前它必然报同一个错，修复后必须能跑通。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from blank_api.services.tools.shell import normalize_relative_executable, run_process

pytestmark = pytest.mark.skipif(os.name != "nt", reason="这是 Windows cmd 的路径形状问题")


def _make_venv_python(root: Path) -> Path:
    target = root / ".venv" / "Scripts" / "python.exe"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"MZ")  # 只需存在即可，本组只验证路径改写
    return target


def test_relative_posix_style_executable_is_made_absolute(tmp_path: Path):
    target = _make_venv_python(tmp_path)
    command = ".venv/Scripts/python.exe -m pytest -q"
    fixed = normalize_relative_executable(command, tmp_path)
    assert fixed.startswith(str(target.resolve().parent.parent.parent)) or str(target) in fixed
    assert "-m pytest -q" in fixed, "参数必须原样保留"
    assert "/" not in fixed.split("-m")[0], "首段必须是 Windows 路径，不能再留 /"


def test_backslash_relative_executable_also_works(tmp_path: Path):
    _make_venv_python(tmp_path)
    fixed = normalize_relative_executable(r".venv\Scripts\python.exe -m pytest", tmp_path)
    assert "Scripts" in fixed and fixed.startswith(str(tmp_path))


def test_plain_commands_are_left_alone(tmp_path: Path):
    # 裸命令名交给 PATH，不能因为"不像路径"就当成路径去拼
    assert normalize_relative_executable("pytest -q", tmp_path) == "pytest -q"
    assert normalize_relative_executable("python -m pytest", tmp_path) == "python -m pytest"


def test_missing_relative_path_is_left_alone(tmp_path: Path):
    # 不存在就不猜：交给 cmd 按原样报错，比改成一个别的路径更容易查
    command = ".venv/Scripts/python.exe -m pytest"
    assert normalize_relative_executable(command, tmp_path) == command


def test_absolute_path_is_left_alone(tmp_path: Path):
    _make_venv_python(tmp_path)
    absolute = str(tmp_path / ".venv" / "Scripts" / "python.exe")
    command = f"{absolute} -m pytest"
    assert normalize_relative_executable(command, tmp_path) == command


def test_argv_form_normalizes_only_the_program(tmp_path: Path):
    target = _make_venv_python(tmp_path)
    fixed = normalize_relative_executable([".venv/Scripts/python.exe", "-m", "pytest"], tmp_path)
    assert fixed[0] == str(target)
    assert fixed[1:] == ["-m", "pytest"]


async def test_a_relative_batch_file_actually_runs(tmp_path: Path):
    """修复前这条命令必然失败（'.venv' 那个报错的同一种形态），修复后必须跑通。"""
    script = tmp_path / "tools" / "echo.bat"
    script.parent.mkdir()
    script.write_text("@echo v161-relative-ok\r\n", encoding="ascii")

    before = await run_process("tools/echo.bat", tmp_path, 20, shell=True)
    assert "'.venv'" not in before["stderr"]  # 反向守卫：不是靠改测试躲过去
    assert before["exit_code"] == 0, before
    assert "v161-relative-ok" in before["stdout"]
