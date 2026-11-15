"""Claude「Doing tasks」段里没移植的六条，以及校验节点空跑审批。

上游 2.1.271 的系统提示（偏移 101028812 / 101029148 / 101030000 / 101031196 /
101031816 / 197932215）里有六件事我们的系统提示一条都没写：网页改动要真跑、
不要为假设需求加东西、不要为不可能的场景兜底、不要留兼容垫片与半成品、
收尾不能停在「接下来我会……」、报告要分清验证过的和推断的。

另一条来自装置实物：UI 对比那一轮我们的任务 10 分钟超时在最后一个
`run_tests` 审批上（runs/ui-ours-events.json seq 160-161），交付正文为空。
工作区里既没有 pytest 配置也没有 package.json 的 test 脚本时，这次审批
注定只会换回一句「未识别到测试命令」——不该为它打断收尾。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from blank_api.services.agent.graph import (
    SYSTEM_PROMPT,
    compose_system_prompt,
    run_validation_node,
)


def _prompt() -> str:
    return SYSTEM_PROMPT


# ── 1. 上游六条：默认提示里必须有，且能按输出样式语义被替换 ─────────────────
def test_frontend_changes_must_be_actually_exercised_not_just_tested() -> None:
    text = _prompt()
    assert "浏览器" in text
    # 单测/类型检查只证明代码正确，不证明页面能用——这是上游原话的落点。
    assert "不证明页面能用" in text
    # 做不到时必须明说，而不是把「能打开」写成「功能对」。
    assert "没验过" in text


def test_no_features_beyond_the_request_and_no_half_finished_code() -> None:
    text = _prompt()
    assert "不为假设的未来需求设计" in text
    assert "半成品" in text


def test_no_defensive_code_for_scenarios_that_cannot_happen() -> None:
    text = _prompt()
    assert "不会发生的场景" in text
    assert "只在系统边界校验" in text


def test_no_backwards_compatibility_shims() -> None:
    text = _prompt()
    assert "向后兼容垫片" in text
    assert "旧路径该删就删" in text


def test_turn_does_not_end_on_a_plan() -> None:
    # 上游原话：只宣布下一步而不调用工具，等于把没做完的活交回用户。
    text = _prompt()
    assert "最后一段" in text
    assert "把控制权交回去" in text


def test_reports_separate_verified_from_assumed() -> None:
    text = _prompt()
    assert "验证过的" in text
    assert "推断的" in text


def test_six_rules_follow_output_style_semantics() -> None:
    # 编程类三条随 keep-coding-instructions 走（与上游 code editor 段一致）；
    # 收尾与如实报告属于永久段，换样式也不能丢。
    replaced = compose_system_prompt(
        {"name": "review", "prompt": "只做评审。", "keep_coding_instructions": False}
    )
    kept = compose_system_prompt(
        {"name": "review", "prompt": "只做评审。", "keep_coding_instructions": True}
    )
    for needle in ("浏览器", "半成品", "向后兼容垫片", "不会发生的场景"):
        assert needle in kept
        assert needle not in replaced
    for needle in ("最后一段", "验证过的"):
        assert needle in replaced


# ── 2. 没有测试配置：不弹审批、不执行 run_tests、直接 skipped ───────────────
class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def add(self, kind: str, node: str, payload: dict | None = None) -> None:
        self.events.append((kind, node, payload or {}))


class _AllowGateway:
    """完全访问：若校验节点仍决定执行，就真的走 execute（用 calls 计数抓出来）。"""

    def __init__(self, payload: dict | None = None) -> None:
        self.calls = 0
        self.payload = payload

    def get(self, name: str):
        return {"name": name}

    def decide(self, tool, arguments, context):
        return SimpleNamespace(outcome="allow", rule="mode:bypass", risk="medium")

    async def execute(self, tool: str, args: dict, context: object, node: str) -> dict:
        self.calls += 1
        if self.payload is not None:
            return self.payload
        return {
            "tool": tool,
            "status": "ok",
            "ok": True,
            "output": "2 passed",
            "data": {"command": "pytest -q", "exit_code": 0},
        }


class _Session:
    def __init__(self, root) -> None:
        self.recorder = _Recorder()
        self.gateway = _AllowGateway()
        self.readonly = False
        self.context = SimpleNamespace(root=root)


def _run(state: dict, root) -> tuple[dict, _Session]:
    session = _Session(root)
    update = asyncio.run(run_validation_node(state, session))
    return update, session


def _created_html() -> dict:
    return {
        "tool": "write_file",
        "status": "ok",
        "ok": True,
        "output": "已写入 index.html",
        "data": {"path": "index.html", "change_type": "created"},
    }


def test_no_test_config_does_not_trigger_a_doomed_approval(tmp_path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")

    update, session = _run({"tool_results": [_created_html()]}, tmp_path)

    # 关键：一行都不该执行。执行了就是又一张审批卡，用户点完才发现没测试可跑。
    assert session.gateway.calls == 0
    validation = update["validation"]
    assert validation["status"] == "skipped"
    assert validation["passed"] is None
    # 理由要和 run_tests 自己那条口径一致，交付文本才能说清"没测"的原因。
    assert "没有可识别的测试配置" in validation["skipped_reason"]
    assert "repair_rounds" not in update
    recorded = [p for k, _, p in session.recorder.events if k == "validation.completed"]
    assert len(recorded) == 1 and recorded[0]["passed"] is None


def test_a_working_test_config_that_this_run_broke_still_runs_and_fails(tmp_path) -> None:
    """反向守卫：本轮先跑通过测试、后来把配置删了，这不是「环境本来没测试」。

    预检如果用「现在检测不到」直接 skipped，就会把「把配置改坏了」洗成成功
    （v0.11.2 §7 真机原样发生过）。这种情形必须照旧执行并判失败进修复回路。
    """
    session = _Session(tmp_path)
    session.gateway = _AllowGateway(
        {
            "tool": "run_tests",
            "status": "error",
            "ok": False,
            "message": "未识别到测试命令：工作区中既没有 pytest 配置，也没有 package.json 的 test 脚本",
            "data": {"command": "run_tests"},
        }
    )
    state = {
        "tool_results": [
            {
                "tool": "run_tests",
                "status": "ok",
                "ok": True,
                "output": "3 passed",
                "data": {"command": "pytest -q", "exit_code": 0},
            },
            _created_html(),
        ]
    }
    update = asyncio.run(run_validation_node(state, session))

    assert session.gateway.calls == 1
    assert update["validation"]["passed"] is False
    assert update["repair_rounds"] == 1


def test_an_existing_test_config_still_goes_through_the_gate(tmp_path) -> None:
    # 反向守卫：别把预检写成"凡静态页面都跳过"。
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    update, session = _run({"tool_results": [_created_html()]}, tmp_path)

    assert session.gateway.calls == 1
    assert update["validation"]["passed"] is True
