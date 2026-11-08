"""v0.11.2 §7 发布阻塞：自动修复把文件改坏后不能误报成功。

真机任务 84be9cb9 的事件事实（.acceptance/v0112-autorepair-events.json）：
  seq 131  npm test 通过 → 之后 8 次改文件把 package.json 弄成非法 JSON
  seq 311  最后一次 run_tests ok=false
  seq 312  校验通道判 skipped「工作区没有可识别的测试配置」
  seq 322  交付文本写「完成：页面未修好…需要你：从 .blank_backups 恢复」
  seq 323  verdict=incomplete / passed=true / blocking=false
  seq 324  task.completed status=succeeded

两道闸都要钉住：校验通道不能把「本轮改坏配置」当成「环境没配」；
Verifier 不能对「交付文本自述未完成」视而不见。
保守原则不许破：本来就没有测试配置的任务仍然不算代码失败。
"""

from __future__ import annotations

import asyncio

from blank_api.services.agent.graph import _tests_were_working, run_validation_node
from blank_api.services.agent.runtime import resolve_status
from blank_api.services.agent.verifier import failure_reason, verify

REAL_DELIVERY = (
    "完成：页面未修好：`styles.css`、`package.json` 被补丁残留污染，"
    "`package.json` 已不是合法 JSON，`npm test` 跑不起来\n"
    "改动：4 个文件、4 处 Diff\n"
    "需要你：从工作区 `.blank_backups` 最近快照恢复这两个文件，再用带行号的 diff 重落改动\n"
    "\n未做自动化校验：工作区没有可识别的测试配置（pytest 配置或 "
    "package.json 的 test 脚本），本次未做自动化校验。"
)
REAL_SKIPPED = {
    "command": "run_tests",
    "passed": None,
    "status": "skipped",
    "exit_code": None,
    "output_tail": "未识别到测试命令：工作区中既没有 pytest 配置，也没有 package.json 的 test 脚本",
}


def _write(path: str) -> dict:
    return {
        "tool": "apply_patch",
        "status": "ok",
        "ok": True,
        "data": {"path": path, "change_type": "modified"},
    }


def _test(status: str, *, output: str = "") -> dict:
    return {
        "tool": "run_tests",
        "status": status,
        "ok": status == "ok",
        "output": output,
        "data": {"command": "npm test", "exit_code": 0 if status == "ok" else 1},
    }


REAL_RESULTS = [
    _write("package.json"),
    _test("ok", output="全部检查通过"),
    _write("package.json"),
    _test(
        "error",
        output="未识别到测试命令：工作区中既没有 pytest 配置，也没有 package.json 的 test 脚本",
    ),
]


# ── Verifier：交付文本自述未完成 ────────────────────────────────────────────


def test_delivery_that_admits_incomplete_is_blocking():
    verdict = verify(
        goal_spec={"objective": "修复这个页面并运行测试"},
        tool_results=REAL_RESULTS,
        validation=REAL_SKIPPED,
        changed_paths=["package.json", "styles.css"],
        report=REAL_DELIVERY,
        budget_exhausted=True,
    )
    assert verdict["blocking"] is True
    assert verdict["passed"] is False
    assert verdict["verdict"] == "failed"
    assert verdict["checks"]["delivery_says_done"] is False
    assert any("自述未完成" in item for item in verdict["findings"])
    # 状态机必须消费这个 blocking，而不是继续判 succeeded。
    assert resolve_status({"validation": REAL_SKIPPED, "verdict": verdict}) == "failed"
    assert "自述未完成" in failure_reason(verdict)


def test_a_normal_four_line_delivery_is_not_blocked():
    verdict = verify(
        goal_spec={"objective": "把按钮改成蓝色"},
        tool_results=[_write("button.css"), _test("ok")],
        validation={"status": "ok", "passed": True, "exit_code": 0},
        changed_paths=["button.css"],
        report="完成：已把按钮改成蓝色\n改动：1 个文件、1 处 Diff\n验证：npm test（退出码 0）",
    )
    assert verdict["verdict"] == "passed" and verdict["blocking"] is False
    assert verdict["checks"]["delivery_says_done"] is True
    assert verdict["checks"]["tests_still_runnable"] is True


def test_a_question_in_the_body_does_not_look_like_an_incomplete_delivery():
    """判据只认四行格式，别把正文里顺口一句「历史问题未修复」当成整轮没做完。"""
    verdict = verify(
        goal_spec={},
        tool_results=[_write("a.css"), _test("ok")],
        validation={"status": "ok", "passed": True, "exit_code": 0},
        changed_paths=["a.css"],
        report="完成：样式已调整\n改动：1 个文件\n备注：另一个历史遗留问题尚未修复，不在本轮范围",
    )
    assert verdict["blocking"] is False and verdict["checks"]["delivery_says_done"] is True


# ── Verifier：本轮把测试配置改坏 ────────────────────────────────────────────


def test_broken_test_config_is_a_hard_fact_even_without_an_explicit_admission():
    verdict = verify(
        goal_spec={"objective": "修复这个页面并运行测试"},
        tool_results=REAL_RESULTS,
        validation=REAL_SKIPPED,
        changed_paths=["package.json"],
        report="完成：已调整样式\n改动：1 个文件",
    )
    assert verdict["blocking"] is True
    assert verdict["checks"]["tests_still_runnable"] is False
    assert any("配置被改坏" in item for item in verdict["findings"])
    assert "配置弄坏" in failure_reason(verdict)


def test_a_workspace_without_tests_from_the_start_is_still_not_a_code_failure():
    """v0.9.1 教训：环境缺配置不能改判成代码失败，否则用户会去翻本来没问题的代码。"""
    verdict = verify(
        goal_spec={"objective": "改样式"},
        tool_results=[
            _write("a.css"),
            _test("error", output="未识别到测试命令：工作区中没有 pytest 配置"),
        ],
        validation={"status": "skipped"},
        changed_paths=["a.css"],
        report="完成：样式已调整\n改动：1 个文件",
    )
    assert verdict["blocking"] is False
    assert verdict["checks"]["tests_still_runnable"] is True
    assert _tests_were_working(
        [_write("a.css"), _test("error", output="未识别到测试命令")]
    ) is False


# ── 校验通道：不能再把「本轮改坏」写成 skipped ──────────────────────────────


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def add(self, kind: str, node: str, payload: dict | None = None) -> None:
        self.events.append((kind, payload or {}))


class _Gateway:
    def get(self, name: str):
        return None

    async def execute(self, tool: str, args: dict, context: object, node: str) -> dict:
        raise AssertionError("已有 run_tests 结果时不该再跑一次")


class _Session:
    def __init__(self) -> None:
        self.recorder = _Recorder()
        self.gateway = _Gateway()
        self.readonly = False
        self.hook_context: list[dict] = []


def _run(state: dict) -> tuple[dict, _Session]:
    session = _Session()
    return asyncio.run(run_validation_node(state, session)), session


def test_validation_gate_records_a_broken_config_as_a_real_error():
    update, session = _run({"tool_results": REAL_RESULTS})
    validation = update["validation"]
    assert validation["status"] == "error"
    assert validation["passed"] is False
    assert validation["broken_by_this_run"] is True
    assert "改动前曾跑通过测试" in validation["output_tail"]
    assert resolve_status({"validation": validation}) == "failed"
    # 预算没耗尽时要真的进修复回路，不能静默 summarize。
    assert update.get("repair_rounds") == 1
    recorded = [p for kind, p in session.recorder.events if kind == "validation.completed"]
    assert len(recorded) == 1 and recorded[0]["status"] == "error"


def test_a_passing_run_never_gets_the_broken_config_marker():
    """真机复跑抓到的回归：本轮有过成功测试 + 有改动 ≠ 这次报错。

    「改坏配置」只能在*本次确实报未识别到测试命令*时打标；否则一个正常跑通的
    任务会在 validation.completed 里收到「本轮改动后测试命令无法识别」的前缀，
    正好又制造一处事实污染。
    """
    update, session = _run(
        {
            "tool_results": [
                _write("package.json"),
                _test("ok", output="全部检查通过"),
                _write("check-page.mjs"),
                _test("ok", output="全部检查通过"),
            ]
        }
    )
    validation = update["validation"]
    assert validation["status"] == "ok" and validation["passed"] is True
    assert "broken_by_this_run" not in validation
    assert "本轮改动后测试命令无法识别" not in validation["output_tail"]
    assert "repair_rounds" not in update
    recorded = [p for kind, p in session.recorder.events if kind == "validation.completed"]
    assert len(recorded) == 1 and "broken_by_this_run" not in recorded[0]


def test_validation_gate_still_skips_when_the_config_never_existed():
    update, _ = _run(
        {
            "tool_results": [
                _write("a.css"),
                _test("error", output="未识别到测试命令：工作区中没有 pytest 配置"),
            ]
        }
    )
    assert update["validation"]["status"] == "skipped"
    assert update["validation"]["passed"] is None
    assert resolve_status({"validation": update["validation"]}) == "succeeded"
    assert "repair_rounds" not in update
