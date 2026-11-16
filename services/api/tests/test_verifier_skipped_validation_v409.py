"""#409：隐藏测试被移出工作区后 pytest 必然 exit 5，校验结论是 skipped。

Verifier 的 ⑦ 把「曾有 run_tests ok + 有文件改动 + 最后 skipped」判成本轮把测试
配置改坏。可严格评测里隐藏测试先移出、判分才装回，pytest 收集不到任何测试就是
exit 5 → skipped——这不是改动造成的，也不代表配置被改坏。

实测（2026-11-16 严格评测 coding-008 t2，后端任务 d190a3f9）：只改了 fib.py，
run_tests 曾用自选命令 `python -c "import fib; ..."` 跑通（exit 0），随后校验通道
自动跑 pytest 拿到 exit 5；verdict 打出「本轮改动前有测试可用，改完后测试命令
无法识别（配置被改坏）」并把任务判失败——代码其实早就改对了。

判据：skipped 本身不是证据，只有本轮真的动过测试文件/测试配置才拦；
error 那条老路径（命令还在但跑不起来了）保持不放宽。
"""

from __future__ import annotations

from blank_api.services.agent.verifier import failure_reason, verify

SKIPPED_NO_TESTS = {
    "command": "E:/projects/kbgai/kbgai/services/api/.venv/Scripts/python.exe -m pytest -q",
    "passed": None,
    "status": "skipped",
    "exit_code": None,
    "output_tail": "no tests ran in 0.00s",
    "skipped_reason": "pytest 没收集到测试（exit 5），这不是测试未通过",
    "skip_code": "no_tests_collected",
}

REPORT = (
    "完成：fib 已改成标准斐波那契数列（fib(0)=0、fib(1)=1）\n"
    "改动：1 个文件、1 处 Diff\n"
    "验证：python -c \"import fib; print(fib.fib(10))\"（退出码 0）"
)


def _write(path: str) -> dict:
    return {
        "tool": "apply_patch",
        "status": "ok",
        "ok": True,
        "data": {"path": path, "change_type": "modified"},
    }


def _test(status: str, *, command: str = 'python -c "import fib"') -> dict:
    return {
        "tool": "run_tests",
        "status": status,
        "ok": status == "ok",
        "output": "",
        "data": {"command": command, "exit_code": 0 if status == "ok" else 1},
    }


def _trial(validation: dict, paths: list[str], *, rows: list[dict] | None = None) -> dict:
    return verify(
        goal_spec={"objective": "把 fib 修成标准斐波那契"},
        tool_results=rows if rows is not None else [_write("fib.py"), _test("ok")],
        validation=validation,
        changed_paths=paths,
        report=REPORT,
    )


def test_skipped_because_no_tests_collected_with_only_source_edits_is_not_a_failure():
    """生产形状：只改了源码，pytest 收集不到测试（exit 5）——不能算配置被改坏。"""
    verdict = _trial(SKIPPED_NO_TESTS, ["fib.py"])
    assert verdict["checks"]["tests_still_runnable"] is True
    assert verdict["checks"]["tests_executed"] is False
    assert verdict["blocking"] is False, verdict["findings"]
    assert failure_reason(verdict) == ""


def test_skipped_after_touching_a_test_file_is_still_blocking():
    """反向守卫：本轮确实动了测试文件、之后一条测试都没收集到 —— 仍然要拦。"""
    verdict = _trial(SKIPPED_NO_TESTS, ["fib.py", "tests/test_fib.py"])
    assert verdict["checks"]["tests_still_runnable"] is False
    assert verdict["blocking"] is True
    assert any("配置被改坏" in item for item in verdict["findings"])
    assert "配置弄坏" in failure_reason(verdict)


def test_skipped_after_touching_test_config_is_still_blocking():
    """package.json / pyproject.toml / conftest.py 这类测试配置同样算证据。"""
    for path in ("pyproject.toml", "conftest.py", "package.json"):
        verdict = _trial(SKIPPED_NO_TESTS, ["fib.py", path])
        assert verdict["checks"]["tests_still_runnable"] is False, path
        assert verdict["blocking"] is True, path


def test_skipped_for_approval_is_not_evidence_of_a_broken_config():
    """审批没走完（甚至模型动过测试文件）不等于配置被改坏——那是没跑，不是跑坏。"""
    validation = dict(SKIPPED_NO_TESTS, skipped_reason="测试在等待你批准，本次未执行",
                      skip_code="awaiting_approval")
    verdict = _trial(validation, ["fib.py", "tests/test_fib.py"])
    assert verdict["checks"]["tests_still_runnable"] is True
    assert verdict["blocking"] is False, verdict["findings"]


def test_error_with_a_prior_successful_run_is_still_blocking():
    """error 那条老路径不放宽：命令还在、之前跑通过、最后跑不起来了。"""
    validation = {"status": "error", "passed": False, "exit_code": 1}
    verdict = _trial(validation, ["fib.py"])
    assert verdict["checks"]["tests_still_runnable"] is False
    assert verdict["blocking"] is True
