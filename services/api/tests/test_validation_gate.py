"""验证节点对「等待审批」的判定。

这类缺陷的代价特别大，因为它不出现在报错里：工具只是在等用户点批准，
却被当成测试失败，于是任务进修复回路——重试同一个被拦下的工具、再等、
再判失败，直到步数预算耗尽，最后整个任务被记成「测试未通过」。
前面几十步读文件、改代码、写测试全都作废，用户看到的是"烧了一堆 token 什么都没做出来"。
"""

from __future__ import annotations

import asyncio

from blank_api.services.agent.graph import run_validation_node
from blank_api.services.agent.runtime import resolve_status


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def add(self, kind: str, node: str, payload: dict | None = None) -> None:
        self.events.append((kind, node, payload or {}))


class _Gateway:
    """没被调用过就说明验证节点用了已有的工具结果，而不是又跑一遍。"""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, tool: str, args: dict, context: object, node: str) -> dict:
        self.calls += 1
        raise AssertionError("不该在已有 run_tests 结果时再执行一次")


class _Session:
    def __init__(self) -> None:
        self.recorder = _Recorder()
        self.gateway = _Gateway()
        # 只读调研开关：真实 AgentSession 上是个字段，这里跟着补齐，
        # 否则验证节点一读它就 AttributeError——那不是被测逻辑的问题。
        self.readonly = False


def _run(state: dict) -> tuple[dict, _Session]:
    session = _Session()
    update = asyncio.run(run_validation_node(state, session))
    return update, session


def _test_result(status: str, *, exit_code: int | None = None, output: str = "") -> dict:
    return {
        "tool": "run_tests",
        "status": status,
        "ok": status == "ok",
        "output": output,
        "data": {"command": "pytest -q", "exit_code": exit_code},
    }


def test_pending_approval_is_not_a_failure() -> None:
    update, _ = _run(
        {"tool_results": [_test_result("awaiting_approval")], "repair_rounds": 2}
    )
    validation = update["validation"]
    assert validation["status"] == "skipped"
    assert validation["passed"] is True
    # 关键：不把修复轮次加一。加了就会回 execute_action，模型只会重试同一个
    # 需要审批的工具，再等一次、再判一次失败，直到步数烧完。
    assert "repair_rounds" not in update
    assert resolve_status({"validation": validation}) == "succeeded"


def test_pending_approval_says_so_instead_of_claiming_success() -> None:
    update, _ = _run({"tool_results": [_test_result("awaiting_approval")]})
    reason = update["validation"]["skipped_reason"]
    # 不能写成「无需测试」：那是"没有产物"的意思，而这里是有产物、只是还没跑。
    assert "批准" in reason
    assert "未执行" in reason


def test_a_real_test_failure_still_fails() -> None:
    # 反向守卫：别为了少烧 token 把真失败也说成通过。
    update, _ = _run(
        {"tool_results": [_test_result("failed", exit_code=1, output="1 failed")]}
    )
    assert update["validation"]["passed"] is False
    assert update["repair_rounds"] == 1
    assert resolve_status({"validation": update["validation"]}) == "failed"


def test_denied_still_skips_without_failing() -> None:
    # 已有行为，一起守住：权限不足是配置问题，不是代码问题。
    update, _ = _run({"tool_results": [_test_result("denied")]})
    assert update["validation"]["status"] == "skipped"
    assert "repair_rounds" not in update


def test_no_changes_and_no_tests_is_skipped_not_failed() -> None:
    update, _ = _run({"tool_results": [], "repair_rounds": 0})
    assert update["validation"]["status"] == "skipped"
    assert resolve_status({"validation": update["validation"]}) == "succeeded"
