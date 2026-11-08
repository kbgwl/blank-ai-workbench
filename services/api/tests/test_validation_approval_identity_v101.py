# -*- coding: utf-8 -*-
"""v0.10.1 §3：校验通道（run_tests）的审批卡也必须有完整身份。

2026-11-08 安装版真机抓到一张没有身份的卡：任务写完了文件、卡在随后的
``run_tests`` 上，``approval.required`` 的 payload 里只有 tool/approval_key/
source=validation，``pending_approvals`` 的 approval_id / tool_call_id /
arguments_hash / resource_path 全是空。审批 API 的精确匹配因此被整段跳过，
退回「任务当前在等的那张卡」——正是任务单 §3.2 明令禁止的做法。

主循环那条审批门早就有身份，两组 v101 审批测试也只测了主循环，所以一直全绿。
这里单独钉死校验通道：身份、有效期、事件链顺序、重放稳定、换 run 换卡、
驳回不执行。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from blank_api.services.agent import graph
from blank_api.services.agent.graph import approval_ttl_seconds, run_validation_node
from blank_api.services.permissions import ASK, PermissionPolicy

TASK_ID = "task-validation-approval"

IDENTITY_KEYS = (
    "approval_id",
    "tool_call_id",
    "tool",
    "tool_name",
    "arguments_hash",
    "resource_path",
)


class _Recorder:
    def __init__(self, run_id: str = "run-1", trace_id: str = "") -> None:
        self.run_id = run_id
        # 真实记录器没显式给 trace_id 时退回 task_id；这里留空是为了让老用例
        # 继续走 run_id 分支，新用例单独模拟「审批恢复」那次 run_id 变化。
        self.trace_id = trace_id
        self.events: list[tuple[str, str, dict]] = []

    async def add(self, kind: str, node: str, payload: dict | None = None) -> None:
        self.events.append((kind, node, dict(payload or {})))

    def payloads(self, kind: str) -> list[dict]:
        return [payload for event, _, payload in self.events if event == kind]


class _Gateway:
    def __init__(self) -> None:
        self.executed = 0

    def get(self, name: str):
        return object() if name == "run_tests" else None

    def decide(self, tool, arguments, context):
        return SimpleNamespace(
            outcome=ASK, risk="high", rule="policy:ask", memo="", prefix=""
        )

    async def execute(self, tool: str, args: dict, context, node: str) -> dict:
        self.executed += 1
        return {
            "status": "ok",
            "ok": True,
            "output": "1 passed",
            "data": {"command": "pytest -q", "exit_code": 0},
            "tool_call_id": "row-1",
        }


class _Session:
    def __init__(self, run_id: str = "run-1", trace_id: str = "") -> None:
        self.recorder = _Recorder(run_id, trace_id)
        self.gateway = _Gateway()
        self.context = SimpleNamespace(task_id=TASK_ID)
        self.permissions = PermissionPolicy()
        self.permission_mode = "default"
        self.readonly = False


def _state() -> dict:
    # 有一次成功的文件改动、没有跑过测试 → 校验通道会真的去问批不批。
    return {
        "tool_results": [
            {
                "tool": "apply_patch",
                "ok": True,
                "status": "ok",
                "data": {"path": "a.py", "change_type": "modified"},
            }
        ],
        "repair_rounds": 0,
        "plan_manifest": {},
        "goal_spec": {},
    }


def _run(monkeypatch, answer, session: _Session | None = None):
    """把 interrupt 换成「用户已经点了」，同时把卡面原样截下来。"""
    session = session or _Session()
    captured: list[dict] = []
    monkeypatch.setattr(
        graph,
        "interrupt",
        lambda payload: (captured.append(dict(payload)), answer)[1],
    )
    result = asyncio.run(run_validation_node(_state(), session))
    return result, session, captured


def test_validation_card_carries_full_identity(monkeypatch) -> None:
    _, session, captured = _run(monkeypatch, {"approve": True, "scope": "once"})

    assert len(captured) == 1, captured
    card = captured[0]
    assert card["source"] == "validation" and card["tool"] == "run_tests"
    for key in IDENTITY_KEYS:
        assert key in card, f"卡面缺 {key}：{sorted(card)}"
    assert card["approval_id"].startswith("apr-"), card
    assert card["tool_name"] == "run_tests", card
    assert card["arguments_hash"], card
    assert card["tool_call_id"].startswith("validation:"), card
    # run_tests 的参数是 {}，没有资源路径可写；空串是如实，不是漏字段。
    assert card["resource_path"] == "", card

    # 有效期必须走与主循环同一个可配置函数。之前这里写死 5 分钟，主循环早已
    # 改成默认 30 分钟——同一张界面上的两张卡寿命不同，人走开一会儿就 409。
    ttl = (
        datetime.fromisoformat(card["expires_at"]) - datetime.now(timezone.utc)
    ).total_seconds()
    assert approval_ttl_seconds() - 10 <= ttl <= approval_ttl_seconds() + 10, ttl
    assert session.gateway.executed == 1


def test_every_event_in_the_chain_carries_the_same_identity(monkeypatch) -> None:
    _, session, captured = _run(monkeypatch, {"approve": True, "scope": "once"})
    card = captured[0]

    granted = session.recorder.payloads("approval.granted")
    requested = session.recorder.payloads("tool.requested")
    completed = session.recorder.payloads("tool.completed")
    assert len(granted) == len(requested) == len(completed) == 1

    # §3.4：批准之后必须有「开始执行」这一半事件，不能只有 granted + completed。
    order = [kind for kind, _, _ in session.recorder.events]
    assert order.index("approval.granted") < order.index("tool.requested") < order.index(
        "tool.completed"
    ), order
    for payload in (granted[0], requested[0], completed[0]):
        for key in IDENTITY_KEYS:
            assert payload.get(key) == card[key], (key, payload, card)


def test_identity_is_stable_across_replays(monkeypatch) -> None:
    """同一轮重放必须算出同一个 approval_id：幂等判定靠它。"""
    session = _Session()
    _, _, first = _run(monkeypatch, {"approve": True, "scope": "once"}, session)
    _, _, second = _run(monkeypatch, {"approve": True, "scope": "once"}, session)
    assert first[0]["approval_id"] == second[0]["approval_id"]


def test_identity_survives_the_approval_resume(monkeypatch) -> None:
    """中断→用户点允许→恢复：run_id 会变，trace_id 不变，卡不能断成两截。

    真机教训：第一版拿 run_id 当定址键，required 是 AgentRun 的 UUID，恢复后
    runtime 改成 `task:<id>:attempt:1`，于是 granted / tool.requested /
    tool.completed 全换了 approval_id，审计按 id 对不上号。
    """
    trace = "task:t-1:attempt:1"
    _, _, before = _run(
        monkeypatch,
        {"approve": True, "scope": "once"},
        _Session("run-uuid-before", trace),
    )
    _, _, after = _run(
        monkeypatch,
        {"approve": True, "scope": "once"},
        _Session("task:t-1:attempt:1", trace),
    )
    assert before[0]["approval_id"] == after[0]["approval_id"]
    assert before[0]["tool_call_id"] == after[0]["tool_call_id"]


def test_a_new_attempt_generates_a_new_approval(monkeypatch) -> None:
    """同一任务重跑一轮（attempt 变了）：旧授权不能套在新卡上。"""
    _, _, first = _run(
        monkeypatch,
        {"approve": True, "scope": "once"},
        _Session("run-1", "task:t-1:attempt:1"),
    )
    _, _, second = _run(
        monkeypatch,
        {"approve": True, "scope": "once"},
        _Session("run-1", "task:t-1:attempt:2"),
    )
    assert first[0]["approval_id"] != second[0]["approval_id"]


def test_a_new_run_generates_a_new_approval(monkeypatch) -> None:
    """重试换了 run：旧授权不能套在新卡上（§3.3「任务重试会生成新审批」）。"""
    _, _, first = _run(monkeypatch, {"approve": True, "scope": "once"}, _Session("run-1"))
    _, _, second = _run(monkeypatch, {"approve": True, "scope": "once"}, _Session("run-2"))
    assert first[0]["approval_id"] != second[0]["approval_id"]
    assert first[0]["tool_call_id"] != second[0]["tool_call_id"]


def test_denied_carries_identity_and_never_executes(monkeypatch) -> None:
    result, session, captured = _run(monkeypatch, {"approve": False, "scope": "once"})
    card = captured[0]

    denied = session.recorder.payloads("approval.denied")
    assert len(denied) == 1
    for key in IDENTITY_KEYS:
        assert denied[0].get(key) == card[key], (key, denied[0], card)
    assert session.recorder.payloads("tool.requested") == []
    assert session.recorder.payloads("tool.completed") == []
    assert session.gateway.executed == 0
    assert result["validation"]["status"] == "skipped"
    assert "授权" in result["validation"]["skipped_reason"]
