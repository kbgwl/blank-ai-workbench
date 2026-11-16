"""cmd.exe 不把 `;` 当命令分隔符（#410）。

真机（2026-11-16 严格评测 coding-012 t1，后端任务 bae4e454）：模型调
``run_tests(command="python -m pytest; echo \\"PYTEST_EXIT=$ERRORLEVEL\\"")``，
cmd 把整段当成 python 的参数，模块名读成 ``pytest;``，回执是
``No module named pytest;``。``missing_runner`` 认出「pytest 缺失」，模型据此
去 where python / pip install / ensurepip，把整个 10 分钟窗口烧在找环境上。

同一条回执在两种缺陷下完全一样（命令写错 / 运行器真没装），改法却相反：
前者要换 `&&`，后者才要装包。回执必须把分号这条说清楚，不能只报「缺 pytest」。
"""

from __future__ import annotations

import pytest

from blank_api.services.tools import shell as shell_mod
from blank_api.services.tools import testing as testing_mod
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.shell import cmd_semicolon_hint

# 真机原文（t012 工件）：前缀是工作区 venv 解释器，末尾那个 `;` 是 cmd 吞进去的。
PRODUCTION_COMMAND = 'python -m pytest; echo "PYTEST_EXIT=$ERRORLEVEL"'
PRODUCTION_OUTPUT = (
    "[stderr]\n"
    "E:\\projects\\kbgai\\kbgai\\services\\api\\.venv\\Scripts\\python.exe: "
    "No module named pytest;"
)


@pytest.fixture
def as_cmd(monkeypatch):
    monkeypatch.setattr(shell_mod, "shell_dialect", lambda: "cmd")


@pytest.fixture
def as_sh(monkeypatch):
    monkeypatch.setattr(shell_mod, "shell_dialect", lambda: "sh")


def test_production_swallowed_semicolon_gets_the_real_explanation(as_cmd):
    hint = cmd_semicolon_hint(PRODUCTION_COMMAND, PRODUCTION_OUTPUT)
    assert ";" in hint, "要点名是分号造成的"
    assert "cmd" in hint.lower(), "要说清是 cmd.exe 的规则"
    assert "&&" in hint, "要给能立刻改对的写法"
    # 关键：不能把这条报错当成「环境缺 pytest」。模型真机就是这么被带偏的。
    assert "不要" in hint and ("缺" in hint or "当成" in hint)


def test_and_chain_on_the_same_output_is_not_blamed(as_cmd):
    # 已经用了 `&&` 还报 `No module named pytest`（没有分号）→ 这才是真的运行器缺失，
    # 不该再拿分号提示干扰它去装包。
    assert cmd_semicolon_hint("python -m pytest && echo ok", "No module named pytest") == ""


def test_semicolon_in_command_without_a_swallowed_token_is_not_blamed(as_cmd):
    # 命令里有 `;` ≠ 分号被吞：回执里没有带分号的 token，就没有证据说它被当成参数，
    # 这时报「环境缺 pytest」可能才是对的，不能拿分号提示盖过去。
    assert cmd_semicolon_hint(PRODUCTION_COMMAND, "python.exe: No module named pytest") == ""


def test_semicolon_with_a_real_test_failure_is_not_blamed(as_cmd):
    # 命令里确实有 `;`，但输出是正常的测试失败：别把失败洗成「命令写错了」。
    assert cmd_semicolon_hint(PRODUCTION_COMMAND, "1 failed, 2 passed in 0.3s") == ""


def test_sh_dialect_never_gets_the_cmd_hint(as_sh):
    # POSIX shell 里 `;` 本来就是分隔符，这条提示在 sh 上全是错的。
    assert cmd_semicolon_hint(PRODUCTION_COMMAND, PRODUCTION_OUTPUT) == ""


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


async def test_run_tests_explains_semicolon_instead_of_reporting_missing_pytest(
    tmp_path, monkeypatch, as_cmd
):
    monkeypatch.setattr(testing_mod, "policy_gate", lambda *a, **k: None)
    monkeypatch.setattr(
        testing_mod,
        "execute_command",
        await _fake_execute({"stderr": PRODUCTION_OUTPUT, "exit_code": 1}),
    )
    context = ToolContext(root=tmp_path, task_id="t-semicolon", session_factory=None)
    outcome = await testing_mod._run_tests(context, {"command": PRODUCTION_COMMAND})
    assert outcome.status == "error"
    assert outcome.data["cmd_semicolon"] is True
    # 不许再给出「环境里没有 pytest / 去安装」的结论——真机就是这样被带偏的。
    assert "missing_test_runner" not in outcome.data
    assert "环境里没有" not in outcome.message
    assert "&&" in outcome.message


async def test_run_tests_still_reports_missing_pytest_without_semicolon(tmp_path, monkeypatch, as_cmd):
    # 反向守卫：命令没写错、pytest 真没装时，原来的环境缺失回执必须保留。
    monkeypatch.setattr(testing_mod, "policy_gate", lambda *a, **k: None)
    monkeypatch.setattr(
        testing_mod,
        "execute_command",
        await _fake_execute({"stderr": "python.exe: No module named pytest", "exit_code": 1}),
    )
    context = ToolContext(root=tmp_path, task_id="t-really-missing", session_factory=None)
    outcome = await testing_mod._run_tests(context, {"command": "python -m pytest -q"})
    assert outcome.status == "error"
    assert outcome.data["missing_test_runner"] == "pytest"
    assert "cmd_semicolon" not in outcome.data


async def test_run_shell_reports_semicolon_swallow_on_nonzero(tmp_path, monkeypatch, as_cmd):
    from blank_api.services.sandbox import tiers

    monkeypatch.setattr(shell_mod, "policy_gate", lambda *a, **k: None)
    monkeypatch.setattr(tiers, "host_bypass_requested", lambda *a, **k: False)
    monkeypatch.setattr(
        shell_mod,
        "execute_command",
        await _fake_execute({"stderr": PRODUCTION_OUTPUT, "exit_code": 1}),
    )
    context = ToolContext(root=tmp_path, task_id="t-shell-semicolon", session_factory=None)
    outcome = await shell_mod._run_shell(context, {"command": PRODUCTION_COMMAND})
    assert outcome.ok is False
    assert outcome.data["hint"] == "windows_cmd_semicolon"
    assert "&&" in outcome.message
