"""回归测试：0.4.4「输入之后没有任何返回」相关的几处缺陷。

这些 bug 单独看都不像崩溃，叠在一起的表现是「任务卡在排队中，界面上什么都没有」，
所以每条都用一个最小的断言钉住，避免以后改 prompt / 改路由时又悄悄退回去。
"""

from __future__ import annotations

from blank_api.services.agent.graph import (
    _extract_json,
    _last_assistant_text,
    _parse_plan,
    route_after_plan,
    route_after_validation,
)


class _StubSession:
    """路由函数只读 state 和很少的 session 字段，这里给最小替身。"""

    acceptance_criteria: list[str] = []
    max_steps = 200
    steer_metrics: dict = {"count": 0, "steps": []}


# ── 1. 计划解析不能把散文当成执行计划 ──────────────────────────────


def test_parse_plan_ignores_prose_answer() -> None:
    # 这句是模型对「你好」的真实回答，曾被解析成 4 步计划，
    # 于是纯问答也被拉进执行回合，最后交出来的是元描述而不是答案。
    answer = "你好！👋\n这只是打个招呼，不需要动工作区。\n我是空白格的操作台。\n可以帮你读代码、改文件。"
    assert _parse_plan(answer) == []


def test_parse_plan_keeps_numbered_and_bulleted_steps() -> None:
    plan = "1. 读取入口文件\n2. 补充单元测试\n- 跑一遍测试\n• 汇总结果"
    assert _parse_plan(plan) == ["读取入口文件", "补充单元测试", "跑一遍测试", "汇总结果"]


# ── 2. 纯问答走短路，不进执行/校验/审查 ─────────────────────────────


def test_route_after_plan_short_circuits_direct_answer() -> None:
    assert route_after_plan({"direct_answer": True}, _StubSession()) == "summarize_result"
    assert route_after_plan({"direct_answer": False}, _StubSession()) == "execute_action"


def test_route_after_validation_skips_review_when_no_changes() -> None:
    # skipped = 没改文件，校验与审查都不适用；硬拉模型去审查只会拿到一段散文
    state = {"validation": {"status": "skipped"}, "repair_rounds": 0}
    assert route_after_validation(state, _StubSession()) == "summarize_result"


# ── 3. 摘要兜底：模型的原话不能被丢掉 ─────────────────────────────


def test_last_assistant_text_prefers_latest_reply() -> None:
    messages = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "第一条"},
        {"role": "tool", "content": "工具输出"},
        {"role": "assistant", "content": "你好！有什么可以帮你的吗？"},
    ]
    assert _last_assistant_text(messages) == "你好！有什么可以帮你的吗？"


def test_last_assistant_text_from_block_content() -> None:
    messages = [
        {"role": "assistant", "content": [{"type": "text", "text": "分块回复"}, {"type": "tool_use"}]},
    ]
    assert _last_assistant_text(messages) == "分块回复"


# ── 4. JSON 提取要容忍混排文字与代码围栏 ────────────────────────────


def test_extract_json_tolerates_surrounding_text() -> None:
    text = '审查结论如下：\n```json\n{"approved": true, "findings": []}\n```\n以上。'
    assert _extract_json(text) == {"approved": True, "findings": []}


def test_extract_json_skips_unbalanced_braces() -> None:
    # 解释文字里先出现一个孤立的 {，不能因此放弃后面真正的 JSON
    text = '说明 {不是 JSON}\n然后才是结果 {"approved": false}'
    assert _extract_json(text) == {"approved": False}
