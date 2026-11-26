"""可量化验收：`file_min_chars` 不过时回执行节点补齐，而不是只把状态改成失败。

真机 e08fc1db（2026-11-21 大文件写入验证）：任务要求 9000 字符，实际 6291，
交付里写「约 8850 字」，模型自报的数字和磁盘事实差两千多。C1 让这种交付
不再判 succeeded；C2 把「字数」变成机器判据，并在没过时自动回到执行节点，
让模型拿着实测证据补齐——这是「自主完成」和「只会说没做完」的分界线。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from blank_api.services.agent.graph import (
    MAX_REPAIR_ROUNDS,
    route_after_validation,
    run_validation_node,
)
from blank_api.services.agent.planner import repair_hint
from blank_api.services.agent.verifier import run_done_checks
from blank_api.services.plan import normalize_goal


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def add(self, kind: str, node: str, payload: dict | None = None) -> None:
        self.events.append((kind, node, payload or {}))


class _Context:
    def __init__(self, root: Path) -> None:
        self.root = root


class _Session:
    def __init__(self, root: Path) -> None:
        self.recorder = _Recorder()
        self.context = _Context(root)
        self.readonly = False
        self.hook_context: list[dict] = []


def _write_result(path: str = "large-check.md") -> dict:
    return {
        "tool": "write_file",
        "status": "ok",
        "ok": True,
        "data": {"path": path, "change_type": "created"},
    }


def _state(goal_spec: dict, *, repair_rounds: int = 0) -> dict:
    return {
        "goal_spec": goal_spec,
        "tool_results": [_write_result()],
        "repair_rounds": repair_rounds,
    }


GOAL = {
    "objective": "写一篇足够长的文档",
    "done_when": ["large-check.md 正文不少于 20 字符"],
    "done_checks": [
        {"type": "file_min_chars", "path": "large-check.md", "min": 20},
    ],
}


def test_file_min_chars_counts_unicode_characters(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("字" * 10, encoding="utf-8")
    rows = run_done_checks(
        ["a.md 至少 10 字符"],
        [{"type": "file_min_chars", "path": "a.md", "min": 10}],
        [],
        tmp_path,
    )
    assert rows[0]["status"] == "passed"
    assert "10 字符" in rows[0]["evidence"]

    rows = run_done_checks(
        ["a.md 至少 11 字符"],
        [{"type": "file_min_chars", "path": "a.md", "min": 11}],
        [],
        tmp_path,
    )
    assert rows[0]["status"] == "failed"
    assert "要 ≥11" in rows[0]["evidence"]


def test_missing_file_fails_the_check(tmp_path: Path) -> None:
    rows = run_done_checks(
        ["a.md 至少 10 字符"],
        [{"type": "file_min_chars", "path": "a.md", "min": 10}],
        [],
        tmp_path,
    )
    assert rows[0]["status"] == "failed"
    assert "不存在" in rows[0]["evidence"]


def test_normalize_goal_keeps_file_min_chars() -> None:
    spec = normalize_goal(
        {
            "objective": "写文档",
            "done_when": [
                {
                    "text": "正文至少 9000 字",
                    "check": {"type": "file_min_chars", "path": "doc.md", "min": 9000},
                }
            ],
        }
    )
    assert spec["done_checks"] == [
        {"type": "file_min_chars", "path": "doc.md", "min": 9000}
    ]


def test_invalid_min_is_dropped_but_text_is_kept() -> None:
    spec = normalize_goal(
        {
            "objective": "写文档",
            "done_when": [
                {"text": "正文至少很多字", "check": {"type": "file_min_chars", "path": "doc.md"}}
            ],
        }
    )
    assert spec["done_when"] == ["正文至少很多字"]
    assert spec["done_checks"] == [{}]


def test_failed_goal_check_routes_back_to_execute_with_evidence(tmp_path: Path) -> None:
    (tmp_path / "large-check.md").write_text("短", encoding="utf-8")
    session = _Session(tmp_path)
    update = asyncio.run(run_validation_node(_state(GOAL), session))

    validation = update["validation"]
    assert validation["status"] == "failed"
    assert validation["passed"] is False
    assert validation["command"] == "goal_check"
    assert "large-check.md" in validation["output_tail"]
    assert "要 ≥20" in validation["output_tail"]
    assert update["repair_rounds"] == 1
    assert "wc -m" in update["repair_hint"]
    assert any(
        item.get("event") == "repair.started" and "wc -m" in item.get("text", "")
        for item in session.hook_context
    )
    kinds = [kind for kind, _, _ in session.recorder.events]
    assert "goal.check_failed" in kinds
    assert "repair.started" in kinds


def test_passing_goal_check_leaves_validation_untouched(tmp_path: Path) -> None:
    (tmp_path / "large-check.md").write_text("足够长的正文" * 10, encoding="utf-8")
    session = _Session(tmp_path)
    update = asyncio.run(run_validation_node(_state(GOAL, repair_rounds=1), session))

    validation = update["validation"]
    # 没有测试配置时既有结论是 skipped；判据全过就保持 skipped，不额外加修复轮。
    assert validation["status"] == "skipped"
    assert validation["goal_checks"][0]["status"] == "passed"
    assert "repair_rounds" not in update
    assert all(kind != "goal.check_failed" for kind, _, _ in session.recorder.events)


def test_failed_goal_check_is_routed_back_to_execute(tmp_path: Path) -> None:
    session = _Session(tmp_path)
    failed = {"validation": {"status": "failed"}, "repair_rounds": 1}
    assert route_after_validation(failed, session) == "execute_action"
    # 修复预算用尽后不再空转，交给审查/收尾；最终仍会按失败的 validation 判失败。
    exhausted = {
        "validation": {"status": "failed"},
        "repair_rounds": MAX_REPAIR_ROUNDS + 1,
    }
    assert route_after_validation(exhausted, session) == "review_result"


def test_goal_check_repair_hint_tells_the_model_to_measure() -> None:
    hint = repair_hint(
        {"kind": "goal_check_failed", "detail": "large-check.md 共 6291 字符（要 ≥9000）"}, 1
    )
    assert "验收判据" in hint
    assert "6291" in hint
    assert "wc -m" in hint
    # 不能把模型引去“改代码”或“重跑测试”。
    assert "改最小范围" not in hint
