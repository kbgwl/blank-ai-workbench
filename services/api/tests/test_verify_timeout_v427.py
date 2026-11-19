"""验证超时不能写成「测试未通过」。

真机轨迹（2026-11-19，任务 36d86ed2「给我做一个3d智慧工厂」）：模型写完三份静态
产物，最后调 `run_tests` 想验一下，工具按"仓库根有 package.json 的 test 脚本"
自动跑了 `npm test`——那是**全量后端单测，约 25 分钟**，而单次上限是 180 秒。
结果写进交付文本的是「测试未通过：npm test」，时间线那张卡上只有一个 `{}`。

三处各自都能误导人，所以分开钉：

1. 收尾文案：必须说"超时、没拿到结论"，不能说"未通过"——用户会去翻一堆没问题的代码。
2. 修复指令：超时没有可修的失败证据，指令要指向"换一条跑得完的验证路径"，
   而不是"改最小范围、再跑一次"。
3. 计划改版：这一步的标题不该是"修复"，否则整个回路都在修一个不存在的 bug。
"""

from __future__ import annotations

from blank_api.services.agent.planner import repair_hint, revise_plan
from blank_api.services.agent.runtime import _failure_reason

TIMEOUT_VALIDATION = {
    "command": "npm test",
    "status": "timeout",
    "passed": False,
    "timeout_seconds": 180,
    "output_tail": "",
}
FAILURE = {"kind": "test_timeout", "detail": "验证命令超时未跑完：npm test"}


def test_a_timeout_is_not_written_up_as_a_failed_test():
    text = _failure_reason(TIMEOUT_VALIDATION)

    assert "超时" in text and "npm test" in text
    assert "180" in text, "要说清是多久的上限，否则用户以为是命令本身有错"
    assert "不是测试失败" in text
    assert "测试未通过" not in text


def test_a_real_failure_still_reads_as_a_failure():
    """别把超时改好之后顺手把所有失败都写成超时。"""
    text = _failure_reason({"command": "pytest tests/x.py", "status": "failed", "exit_code": 1})

    assert text.startswith("测试未通过：pytest tests/x.py")


def test_repair_hint_points_at_the_verification_path_not_the_code():
    hint = repair_hint(FAILURE, 1)

    assert "超时" in hint and "没有拿到任何结论" in hint
    assert "不要原样重跑" in hint
    assert "改最小范围" not in hint, "超时没有可修的失败证据，不该让模型去改代码"


def test_the_plan_revision_does_not_ask_for_a_code_fix():
    plan = {"revision": 0, "steps": [{"id": "step-1", "title": "写页面", "kind": "act"}]}

    revised = revise_plan(plan, failure=FAILURE, repair_round=1)
    titles = " ".join(str(step.get("title") or "") for step in revised["steps"])

    assert "验证路径" in titles, f"超时应当指向换验证方式，实际步骤：{titles}"
    assert "最小范围修复" not in titles
