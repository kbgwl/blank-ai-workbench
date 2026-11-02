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
    update, session = _run(
        {"tool_results": [_test_result("awaiting_approval")], "repair_rounds": 2}
    )
    validation = update["validation"]
    assert validation["status"] == "skipped"
    # 放行靠的是 status == "skipped"，不是 passed。passed 是给人看的证据，
    # 一行测试都没跑却写 True，等于对着用户谎报"测试通过"。
    assert validation["passed"] is None
    # 关键：不把修复轮次加一。加了就会回 execute_action，模型只会重试同一个
    # 需要审批的工具，再等一次、再判一次失败，直到步数烧完。
    assert "repair_rounds" not in update
    assert resolve_status({"validation": validation}) == "succeeded"
    # 事件里的每一份拷贝都得是同一个口径，否则前端时间线读到的仍是"通过"
    recorded = [payload for kind, _, payload in session.recorder.events if kind == "validation.completed"]
    assert len(recorded) == 1 and recorded[0]["passed"] is None


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
    assert update["validation"]["passed"] is None
    assert "repair_rounds" not in update


def test_no_changes_and_no_tests_is_skipped_not_failed() -> None:
    update, _ = _run({"tool_results": [], "repair_rounds": 0})
    assert update["validation"]["status"] == "skipped"
    assert update["validation"]["passed"] is None
    assert resolve_status({"validation": update["validation"]}) == "succeeded"


def test_skipped_never_enters_the_repair_loop() -> None:
    """跳过校验的四种原因都不能把任务拽回 execute_action。

    这套放行靠 status 判定，跟 passed 的口径改动无关；把 passed 从 True 改成 None
    一旦顺手动了路由，模型就会去"修复"一个不存在的问题，直到步数烧完。
    """
    from blank_api.services.agent.graph import route_after_validation

    cases = [
        _test_result("awaiting_approval"),
        _test_result("denied"),
        # error 只在"环境起不来"时才算跳过；空输出的 error 是真失败，会走修复回路
        _test_result("error", output="工具执行失败：SandboxUnavailable"),
    ]
    for result in cases:
        update, _ = _run({"tool_results": [result]})
        assert route_after_validation({"validation": update["validation"]}, _Session()) == "summarize_result"
    update, _ = _run({"tool_results": [], "repair_rounds": 0})
    assert route_after_validation({"validation": update["validation"]}, _Session()) == "summarize_result"


def test_a_real_pass_still_reports_passed() -> None:
    # 反向守卫：改成 None 只针对没跑成的情形。真跑过且退码 0 必须还是 True，
    # 否则"如实报告"会变成一律不说通过，证据就失去了区分度。
    update, _ = _run({"tool_results": [_test_result("ok", exit_code=0, output="3 passed")]})
    assert update["validation"]["passed"] is True
    assert update["validation"]["status"] == "ok"


# ── 交付正文必须说出"校验没跑"（v161 §2.2：没有验证证据不得输出成功）──
def _note(state: dict, report: str = "结论\n- 改好了") -> str:
    from blank_api.services.agent.graph import _validation_note

    return _validation_note(report, state)


def test_delivery_says_out_loud_when_validation_was_skipped() -> None:
    """这条以前只活在证据包里：卡片上写着"测试没执行"，交付正文却可以照写"改好了"。
    读正文的人不会去翻依据条，于是"没验过"就等于"验过了"。"""
    update, _ = _run({"tool_results": [_test_result("awaiting_approval")]})
    text = _note({"validation": update["validation"]})
    assert "未做自动化校验" in text
    assert "批准" in text


def test_delivery_is_not_polluted_when_there_was_nothing_to_validate() -> None:
    # 纯问答没有产物，硬加一句"未验证"是噪音。
    update, _ = _run({"tool_results": [], "repair_rounds": 0})
    assert _note({"validation": update["validation"]}) == "结论\n- 改好了"


def test_delivery_note_is_not_duplicated_when_the_model_already_said_it() -> None:
    update, _ = _run({"tool_results": [_test_result("denied")]})
    report = "结论\n- 改好了，但没验证（命令未获批）"
    assert _note({"validation": update["validation"]}, report) == report


def test_a_passed_validation_adds_no_note_and_no_prompt_hint() -> None:
    from blank_api.services.agent.graph import _validation_fact

    update, _ = _run({"tool_results": [_test_result("ok", exit_code=0)]})
    state = {"validation": update["validation"]}
    assert _note(state) == "结论\n- 改好了"
    assert _validation_fact(state) == ""


def test_a_skipped_validation_is_told_to_the_writer_explicitly() -> None:
    from blank_api.services.agent.graph import _validation_fact

    update, _ = _run({"tool_results": [_test_result("awaiting_approval")]})
    fact = _validation_fact({"validation": update["validation"]})
    # 光说"没跑"不够：要明确禁止它写成测试通过。
    assert "未执行" in fact
    assert "不许写测试通过" in fact
