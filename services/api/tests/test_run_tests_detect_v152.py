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


# ── 识别出了测试命令、但本机没有这个运行器（Go 样例在评测机上的真实形态）──
# 以前直接交给执行层：shell=False 时 create_subprocess_exec 找不到 go 会直接抛，
# 被记成 error/failed，任务去"修"一段本来对的代码。跳过的判据必须落在工具层，
# 因为"缺哪个工具"只有这里知道。


def test_missing_test_runner_only_checks_bare_names_on_path(monkeypatch):
    absent = "definitely-not-installed-runner-xyz"
    # 带路径的由 normalize_relative_executable 负责，不能在这里误判成缺失。
    assert testing_mod.missing_test_runner(["/abs/path/to/runner"], "") == ""
    assert testing_mod.missing_test_runner([], "") == ""
    assert testing_mod.missing_test_runner([absent], "") == absent
    # 容器档在镜像里执行，宿主有没有这个工具与它无关（档位能不能真落到 docker
    # 由部署与 tiers 决定，这里直接钉住"容器里就不查宿主 PATH"这条分支）。
    monkeypatch.setattr(testing_mod, "runs_in_docker", lambda tier: True)
    assert testing_mod.missing_test_runner([absent], "workspace") == ""


async def test_go_project_without_go_toolchain_is_skipped_not_failed(tmp_path: Path, monkeypatch):
    root = tmp_path / "goproj"
    root.mkdir()
    (root / "go.mod").write_text("module example.com/strutil\n\ngo 1.23\n", encoding="utf-8")
    monkeypatch.setattr(testing_mod, "policy_gate", lambda *a, **k: None)
    monkeypatch.setattr(testing_mod.shutil, "which", lambda name: None)
    called = []

    async def _never(argv, *args, **kwargs):
        called.append(argv)
        raise AssertionError("缺少工具链时不该起进程")

    monkeypatch.setattr(testing_mod, "execute_command", _never)
    context = ToolContext(root=root, task_id="t-go", session_factory=None)
    outcome = await testing_mod._run_tests(context, {})
    assert outcome.status == "skipped"
    assert outcome.ok is False
    assert called == []
    assert outcome.data["missing_test_runner"] == "go"
    assert "go" in outcome.data["skipped_reason"]
    assert "本机没有" in outcome.data["skipped_reason"]


async def test_go_project_with_go_toolchain_still_runs(tmp_path: Path, monkeypatch):
    # 反向守卫：别把"Go 项目"一律跳过——装了工具链就必须真的跑。
    root = tmp_path / "goproj"
    root.mkdir()
    (root / "go.mod").write_text("module example.com/strutil\n\ngo 1.23\n", encoding="utf-8")
    monkeypatch.setattr(testing_mod, "policy_gate", lambda *a, **k: None)
    monkeypatch.setattr(testing_mod.shutil, "which", lambda name: f"C:/fake/{name}.exe")
    monkeypatch.setattr(
        testing_mod,
        "execute_command",
        await _fake_execute({"stdout": "ok  example.com/strutil  0.01s", "exit_code": 0}),
    )
    context = ToolContext(root=root, task_id="t-go-ok", session_factory=None)
    outcome = await testing_mod._run_tests(context, {})
    assert outcome.status == "ok"
    assert outcome.data["passed"] is True
    assert outcome.data["command"].startswith("go test")
