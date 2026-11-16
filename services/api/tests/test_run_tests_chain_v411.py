"""run_tests 的链式命令：每一段都要解析到同一个解释器（#411）。

真机（2026-11-16 严格评测 coding-031 t1，后端任务 7150924d）：
``python -m pytest -q 2>nul || python -m unittest -v 2>nul || python -c "..."``
第一段 pytest 正常跑完（no tests ran），后两段的裸 ``python`` 落到 PATH 上的商店
占位符，被 ``2>nul`` 吞掉报错但把退出码改成 9009；run_tests 据此回「测试命令不存在：
系统找不到 python.exe」，模型把整个 10 分钟窗口烧在 where / reg / winget 上。
"""

from __future__ import annotations

from pathlib import Path

from blank_api.services.tools import testing as testing_mod
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.testing import normalize_chained_interpreters

PRODUCTION_COMMAND = (
    'python -m pytest -q 2>nul || python -m unittest -v 2>nul || '
    'python -c "import units; print(units.convert(\' 42 \'))"'
)


def _venv(tmp_path: Path) -> Path:
    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_bytes(b"")
    return venv_python


async def _capture(monkeypatch):
    captured: dict = {}

    async def run(argv, root, timeout, **kwargs):
        captured["argv"] = argv
        captured["shell"] = kwargs.get("shell")
        return {
            "stdout": "no tests ran in 0.01s",
            "stderr": "",
            "exit_code": 0,
            "timed_out": False,
            "sandbox": {},
            "enforcement": {},
            "tier": {},
        }

    monkeypatch.setattr(testing_mod, "policy_gate", lambda *a, **k: None)
    monkeypatch.setattr(testing_mod, "execute_command", run)
    return captured


# ── 每一段都解析，分隔符与重定向原样保留 ────────────────────────────────────


def test_every_segment_resolves_to_the_same_interpreter(tmp_path: Path):
    venv_python = _venv(tmp_path)
    fixed = normalize_chained_interpreters(PRODUCTION_COMMAND, str(venv_python))
    assert fixed.count(str(venv_python)) == 3
    # 分隔符与 2>nul 都必须保留（重写命令不能改语义）
    assert fixed.count(" || ") == 2
    assert fixed.count("2>nul") == 2
    # 不再有裸解释器落在段首
    residue = fixed.replace(str(venv_python), "")
    assert " python -m" not in residue
    assert not residue.lstrip().startswith("python")
    # 第三段的参数完整保留
    assert "import units; print(units.convert(' 42 '))" in fixed


def test_pytest_heads_are_normalized_segment_by_segment(tmp_path: Path):
    venv_python = _venv(tmp_path)
    fixed = normalize_chained_interpreters("pytest -q || pytest -v", str(venv_python))
    assert fixed.count(f"{venv_python} -m pytest") == 2
    assert "pytest -q" not in fixed.replace(f"{venv_python} -m pytest", "")


# ── 反向守卫：不是「整条命令都是裸解释器」就一个字都不改 ────────────────────


def test_mixed_chain_with_other_programs_is_left_alone(tmp_path: Path):
    venv_python = _venv(tmp_path)
    command = "npm test && python -m pytest -q"
    assert normalize_chained_interpreters(command, str(venv_python)) == command


def test_set_path_chain_keeps_the_authors_interpreter_choice(tmp_path: Path):
    venv_python = _venv(tmp_path)
    command = 'set "PATH=C:\\Python312;%PATH%" && python -m pytest -q'
    assert normalize_chained_interpreters(command, str(venv_python)) == command


def test_explicit_interpreter_path_in_any_segment_is_left_alone(tmp_path: Path):
    venv_python = _venv(tmp_path)
    command = "python -m pytest -q || E:\\env\\python.exe -m unittest"
    assert normalize_chained_interpreters(command, str(venv_python)) == command


def test_semicolon_is_not_a_chain_separator_here(tmp_path: Path):
    # cmd.exe 不把 `;` 当分隔符（#410）；这里也不能把它当分隔符来改写。
    venv_python = _venv(tmp_path)
    command = 'python -c "print(1); print(2)"'
    assert normalize_chained_interpreters(command, str(venv_python)) == command


def test_pipe_inside_quotes_is_not_a_separator(tmp_path: Path):
    venv_python = _venv(tmp_path)
    command = 'python -c "print(\'a || b\')" || python -c "print(2)"'
    fixed = normalize_chained_interpreters(command, str(venv_python))
    assert fixed.count(str(venv_python)) == 2
    assert "print('a || b')" in fixed


# ── 接线：真机那条命令在 run_tests 里三段都用项目解释器 ─────────────────────


async def test_run_tests_chain_uses_project_interpreter_in_every_segment(tmp_path: Path, monkeypatch):
    venv_python = _venv(tmp_path)
    captured = await _capture(monkeypatch)
    context = ToolContext(root=tmp_path, task_id="t-chain-venv", session_factory=None)
    outcome = await testing_mod._run_tests(context, {"command": PRODUCTION_COMMAND})
    assert outcome.status == "ok"
    assert captured["argv"].count(str(venv_python)) == 3
    assert captured["shell"] is True


# ── 链尾没启动时不许诬告段首 ──────────────────────────────────────────────


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


async def test_chain_tail_9009_with_test_output_does_not_blame_the_head(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(testing_mod, "policy_gate", lambda *a, **k: None)
    monkeypatch.setattr(
        testing_mod,
        "execute_command",
        await _fake_execute({"stdout": "1 passed, 2 failed in 0.3s", "exit_code": 9009}),
    )
    context = ToolContext(root=tmp_path, task_id="t-chain-tail", session_factory=None)
    outcome = await testing_mod._run_tests(context, {"command": "npm test && python -m pytest -q"})
    assert outcome.status == "error"
    assert outcome.data["chain_tail_failed"] is True
    # 前半段确实跑过，回执不能再写「系统找不到 head」去把人引向找解释器
    assert "测试命令不存在" not in outcome.message
    assert "链尾" in outcome.message or "后面的某一段" in outcome.message
    assert "Python" in outcome.message
    assert "不要" in outcome.message


async def test_chain_9009_without_test_output_still_blames_the_missing_command(tmp_path: Path, monkeypatch):
    # 链式但前半段什么都没跑出来：就是命令不存在，别洗成「链尾没启动」。
    monkeypatch.setattr(testing_mod, "policy_gate", lambda *a, **k: None)
    monkeypatch.setattr(
        testing_mod,
        "execute_command",
        await _fake_execute({"stderr": "系统找不到指定的路径。", "exit_code": 9009}),
    )
    context = ToolContext(root=tmp_path, task_id="t-chain-silent", session_factory=None)
    outcome = await testing_mod._run_tests(context, {"command": "npm test && python -m pytest -q"})
    assert outcome.status == "error"
    assert "chain_tail_failed" not in outcome.data
    assert "测试命令不存在" in outcome.message


async def test_plain_missing_command_still_names_the_command(tmp_path: Path, monkeypatch):
    # 反向守卫：单条命令 9009 时语义没变，别把真缺失洗成链尾问题。
    monkeypatch.setattr(testing_mod, "policy_gate", lambda *a, **k: None)
    monkeypatch.setattr(
        testing_mod,
        "execute_command",
        await _fake_execute({"stderr": "'pytestrunner' 不是内部或外部命令", "exit_code": 9009}),
    )
    context = ToolContext(root=tmp_path, task_id="t-plain-missing", session_factory=None)
    outcome = await testing_mod._run_tests(context, {"command": "pytestrunner -q"})
    assert outcome.status == "error"
    assert outcome.data["command_not_found"] is True
    assert "chain_tail_failed" not in outcome.data
    assert "测试命令不存在" in outcome.message
