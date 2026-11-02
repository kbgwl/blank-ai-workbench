"""run_tests 的测试命令识别：Go / pytest / npm / 空目录的优先级与返回值。

这些断言不真正执行 go/pytest，只验证 detect_test_command 的判定——所以不需要 Go 装在 CI 上。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blank_api.services.tools import testing as testing_mod
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.shell import project_python
from blank_api.services.tools.testing import detect_test_command, missing_runner


def test_detect_go_mod_returns_go_test(tmp_path: Path):
    root = tmp_path / "goproj"
    root.mkdir()
    (root / "go.mod").write_text("module example.com/strutil\n\ngo 1.23\n", encoding="utf-8")
    argv, shell = detect_test_command(root)
    assert argv[:3] == ["go", "test", "./..."]
    assert "-count=1" in argv
    assert shell is False  # go 直接执行，不走 shell，也不会被 docker 的 python 替换误伤


def test_detect_prefers_package_json_test_script_over_go(tmp_path: Path):
    root = tmp_path / "mix"
    root.mkdir()
    (root / "go.mod").write_text("module example\n\ngo 1.23\n", encoding="utf-8")
    (root / "package.json").write_text('{"scripts":{"test":"npm test"}}', encoding="utf-8")
    argv, shell = detect_test_command(root)
    assert argv == ["npm", "test"]
    assert shell is True


def test_detect_pytest_marker_before_go_glob(tmp_path: Path):
    root = tmp_path / "py"
    root.mkdir()
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    argv, _ = detect_test_command(root)
    assert argv and "pytest" in " ".join(argv)


def test_detect_empty_workspace_returns_none(tmp_path: Path):
    root = tmp_path / "empty"
    root.mkdir()
    assert detect_test_command(root) is None


def test_detect_npm_test_script(tmp_path: Path):
    root = tmp_path / "js"
    root.mkdir()
    (root / "package.json").write_text('{"scripts":{"test":"jest"}}', encoding="utf-8")
    argv, shell = detect_test_command(root)
    assert argv == ["npm", "test"]
    assert shell is True


# ── 项目自己的虚拟环境优先 ────────────────────────────────────────────────
# 桌面版带的是干净 Python 运行时（没有 pytest）。旧逻辑一律 sys.executable，
# 用户项目里明明有 .venv 也用不上，于是"跑不了测试"被当成"测试没通过"。


def test_detect_uses_project_venv_python(tmp_path: Path):
    root = tmp_path / "proj"
    (root / ".venv" / "Scripts").mkdir(parents=True)
    venv_python = root / ".venv" / "Scripts" / "python.exe"
    venv_python.write_bytes(b"")
    (root / "tests").mkdir()
    argv, _ = detect_test_command(root)
    assert argv[0] == str(venv_python)
    assert argv[1:] == ["-m", "pytest", "-q"]


def test_detect_uses_posix_venv_layout(tmp_path: Path):
    root = tmp_path / "proj"
    (root / ".venv" / "bin").mkdir(parents=True)
    venv_python = root / ".venv" / "bin" / "python"
    venv_python.write_bytes(b"")
    (root / "conftest.py").write_text("", encoding="utf-8")
    argv, _ = detect_test_command(root)
    assert argv[0] == str(venv_python)


def test_detect_falls_back_to_current_interpreter(tmp_path: Path):
    import sys

    root = tmp_path / "plain"
    root.mkdir()
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    argv, _ = detect_test_command(root)
    assert argv[0] == sys.executable


def test_project_python_returns_empty_when_absent(tmp_path: Path):
    assert project_python(tmp_path) == ""
    assert project_python(None) == ""


# ── 「测试挂了」与「跑不了测试」必须分开 ──────────────────────────────
# 真实台账：同一条 `python -m pytest test*.py -v` 四次退出码 1，输出是
# `No module named pytest`，交付却写成「测试未通过」，任务被判能力失败。


@pytest.mark.parametrize(
    "output",
    [
        "C:\\...\\python.exe: No module named pytest",
        "ModuleNotFoundError: No module named 'pytest'",
        "bash: pytest: command not found",
        "'pytest' is not recognized as an internal or external command",
    ],
)
def test_missing_runner_recognized(output: str):
    assert missing_runner(output) == "pytest"


@pytest.mark.parametrize(
    "output",
    [
        "1 failed, 3 passed in 0.21s",
        "E   AssertionError: assert 3 == 2",
        "ModuleNotFoundError: No module named 'requests'",  # 项目依赖缺失不是运行器缺失
        "",
    ],
)
def test_missing_runner_does_not_swallow_real_failures(output: str):
    assert missing_runner(output) == ""


async def _fake_execute(payload: dict):
    async def run(argv, root, timeout, **kwargs):
        return {
            "stdout": payload.get("stdout", ""),
            "stderr": payload.get("stderr", ""),
            "exit_code": payload["exit_code"],
            "timed_out": False,
            "sandbox": {},
            "enforcement": {},
            "tier": {},
        }

    return run


async def test_run_tests_reports_environment_error_when_pytest_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(testing_mod, "policy_gate", lambda *a, **k: None)
    monkeypatch.setattr(
        testing_mod,
        "execute_command",
        await _fake_execute({"stderr": "python.exe: No module named pytest", "exit_code": 1}),
    )
    context = ToolContext(root=tmp_path, task_id="t-missing", session_factory=None)
    outcome = await testing_mod._run_tests(context, {"command": "python -m pytest -q"})
    assert outcome.status == "error"
    assert outcome.data["missing_test_runner"] == "pytest"
    assert "pytest" in outcome.message


async def test_run_tests_keeps_failed_for_real_test_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(testing_mod, "policy_gate", lambda *a, **k: None)
    monkeypatch.setattr(
        testing_mod,
        "execute_command",
        await _fake_execute({"stdout": "1 failed, 2 passed in 0.3s", "exit_code": 1}),
    )
    context = ToolContext(root=tmp_path, task_id="t-fail", session_factory=None)
    outcome = await testing_mod._run_tests(context, {"command": "python -m pytest -q"})
    assert outcome.status == "failed"
    assert outcome.data["passed"] is False
