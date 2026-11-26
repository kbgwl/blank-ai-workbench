"""交付文本自己承认“没达到门槛”时，任务不能判 succeeded。

真机 e08fc1db（2026-11-21 大文件写入验证）：任务要求 `large-check.md` 正文不少于
9000 字符，实际只写了 6291 字符，交付文本自己写：

    完成：`large-check.md` 已成文，但正文约 6291 字，未达 9000 字门槛
    需要你：补足正文至 9000 字以上并实跑字数口径统计

旧词表只认「未完成 / 未实现 / 仍为空」这类叙事词，没认「未达…门槛」和
「补足…」；Verifier 的 `delivery_says_done` 仍为 true，任务被 `resolve_status`
判成 succeeded。这一组判据把两类词都钉住，同时保证正常的“做完了”不被误伤。
"""

from __future__ import annotations

from blank_api.services.agent.runtime import resolve_status
from blank_api.services.agent.verifier import verify

GOAL_SPEC = {"done_when": ["large-check.md 正文不少于 9000 字符"]}
PLAN = {"steps": [{"kind": "edit", "title": "写入 large-check.md"}]}
TOOL_RESULTS = [{"tool": "write_file", "status": "ok", "ok": True}]
VALIDATION = {"status": "skipped", "passed": None, "exit_code": None}


def _verify(report: str, *, budget_exhausted: bool = False) -> dict:
    return verify(
        goal_spec=GOAL_SPEC,
        plan=PLAN,
        tool_results=TOOL_RESULTS,
        validation=VALIDATION,
        changed_paths=["large-check.md"],
        report=report,
        budget_exhausted=budget_exhausted,
    )


def test_delivery_admits_missing_numeric_threshold_blocks_success():
    report = (
        "完成：`large-check.md` 已成文，但正文约 6291 字，未达 9000 字门槛\n"
        "改动：1 个文件（large-check.md）\n"
        "需要你：补足正文至 9000 字以上并实跑字数口径统计"
    )
    result = _verify(report, budget_exhausted=True)

    assert result["checks"]["delivery_says_done"] is False, result["findings"]
    assert result["blocking"] is True, result["findings"]
    assert result["verdict"] == "failed"
    assert resolve_status({"verdict": result, "validation": VALIDATION}) == "failed"


def test_completed_delivery_is_not_blocked_by_the_new_markers():
    report = (
        "完成：`large-check.md` 已写入 9200 字，并读回确认长度\n"
        "改动：1 个文件（large-check.md）"
    )
    result = _verify(report)

    assert result["checks"]["delivery_says_done"] is True, result["findings"]
    assert result["blocking"] is False, result["findings"]


def test_broken_frontend_with_handoff_is_not_success():
    """真机 7ce98397：18 项服务端测试全过，但前端导航点不动，交付还写着“重跑”。"""
    report = (
        "完成：服务端、模拟器与本地规则引擎可用，18 项测试全通过；"
        "前端导航点击无反应，五个视图进不去\n"
        "改动：11 个文件（data/product.json、lib/simulator.js、lib/ai.js、server.js、package.json…）、"
        "12 处 Diff，入口 public/index.html\n"
        "验证：npm test（退出码 0）\n"
        "需要你：给 switchView 的 history.replaceState 包 try/catch，重跑浏览器点击确认视图切换"
    )
    result = _verify(report)

    assert result["checks"]["delivery_says_done"] is False, result["findings"]
    assert result["blocking"] is True, result["findings"]
    assert result["verdict"] == "failed"
    assert resolve_status({"verdict": result, "validation": VALIDATION}) == "failed"


def test_cleanup_handoff_still_does_not_block():
    """「需要你：手动删除验证残留」是收尾交接，不是没做完——不能被新词误伤。"""
    report = (
        "完成：`large-check.md` 已写入 9200 字\n"
        "改动：1 个文件（large-check.md）\n"
        "需要你：手动删除验证残留"
    )
    result = _verify(report)

    assert result["checks"]["delivery_says_done"] is True, result["findings"]
    assert result["blocking"] is False, result["findings"]
