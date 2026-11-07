"""v165 Phase 2 接线层：计划 / 验证 / 修复 / 终态判定真的串起来了。

纯函数单测在 test_v165_phase2_units.py；这里只验「接进 4000 行执行链之后」
还成不成立，全部用鸭子类型替身，不调模型、不连网络。
"""

from __future__ import annotations

import json

import pytest

from blank_api.services.agent.graph import (
    create_plan_node,
    route_after_validation,
    run_validation_node,
)
from blank_api.services.agent.runtime import resolve_status


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def add(self, kind: str, node: str = "", payload: dict | None = None) -> int:
        self.events.append((kind, dict(payload or {})))
        return len(self.events)

    def has(self, kind: str) -> bool:
        return any(item[0] == kind for item in self.events)

    def payload(self, kind: str) -> dict:
        for item_kind, payload in reversed(self.events):
            if item_kind == kind:
                return payload
        return {}


class _PlanSession:
    """create_plan_node 用到的最小替身。"""

    def __init__(self, *, goal_spec: dict | None = None, intent_text: str | None = None) -> None:
        self.recorder = _Recorder()
        self.goal_spec = goal_spec or {}
        self.goal = "把登录页的按钮改成蓝色"
        self.scenario = "dev"
        self.max_steps = 200
        self.token_budget = 1_500_000
        self.hook_context: list[dict] = []
        self.recorded_questions = False
        self._intent_text = intent_text or json.dumps(
            {
                "intent": "act",
                "steps": ["读取入口文件", "修改按钮颜色", "跑测试"],
                "objective": "把登录页的按钮改成蓝色",
                "answer": "",
            },
            ensure_ascii=False,
        )

    async def ask(self, state, prompt, node, *, messages=None):
        # 断言「系统预判」不会把用户原话改掉：prompt 是追加的一条新消息。
        self.last_prompt = prompt
        return {
            "text": self._intent_text,
            "tool_calls": [],
            "usage": {},
            "messages": [{"role": "user", "content": self.goal}, {"role": "assistant", "content": self._intent_text}],
        }

    async def ensure_snapshot(self, node):
        self.snapshot_taken = True


class _ValidationSession:
    def __init__(self) -> None:
        self.recorder = _Recorder()
        self.readonly = False
        self.hook_context: list[dict] = []


_ROUTE_SESSION = _ValidationSession()  # route_after_validation 只读 session.readonly


def _failed_tool_results() -> list[dict]:
    return [
        {
            "tool": "apply_patch",
            "ok": True,
            "status": "ok",
            "data": {"path": "web/button.css", "change_type": "modified"},
        },
        {
            "tool": "run_tests",
            "ok": False,
            "status": "error",
            "message": "pytest: 1 failed",
            "data": {"command": "pytest -q"},
        },
    ]


# --------------------------------------------------------------------------- Planner 接线


@pytest.mark.asyncio
async def test_create_plan_builds_a_manifest_and_keeps_the_goal_id():
    spec = {"goal_id": "goal-1", "supervisor": {"intent": "execute"}, "dispatch": {"auto_start": True}}
    session = _PlanSession(goal_spec=spec)
    state = {
        "task_id": "t1",
        "messages": [{"role": "user", "content": "把登录页的按钮改成蓝色"}],
        "usage": {},
    }
    result = await create_plan_node(state, session)

    manifest = result["plan_manifest"]
    assert manifest["goal_id"] == "goal-1"
    assert manifest["plan_id"]
    assert [item["kind"] for item in manifest["steps"]] == ["inspect", "edit", "verify"]
    assert manifest["token_budget"] == 1_500_000
    assert session.recorder.has("plan.created")
    event = session.recorder.payload("plan.created")
    assert event["plan_id"] == manifest["plan_id"] and event["goal_id"] == "goal-1"
    assert event["rollback_strategy"] == "workspace_diff"

    # 计划节点不能把会话层写的 supervisor / goal_id / dispatch 覆盖掉。
    assert result["goal_spec"]["goal_id"] == "goal-1"
    assert result["goal_spec"]["supervisor"]["intent"] == "execute"
    assert result["goal_spec"]["dispatch"]["auto_start"] is True
    assert result["goal_spec"]["objective"] == "把登录页的按钮改成蓝色"
    # 追加的是系统提示，用户原话没有拼前缀。
    assert "执行规则" not in str(getattr(session, "last_prompt", "")) or "固定前缀" in str(session.last_prompt)


# --------------------------------------------------------------------------- 修复回路接线


@pytest.mark.asyncio
async def test_a_test_failure_dispatches_an_automatic_repair_round():
    session = _ValidationSession()
    state = {
        "tool_results": _failed_tool_results(),
        "repair_rounds": 0,
        "plan_manifest": {
            "plan_id": "p1",
            "goal_id": "g1",
            "steps": [],
            "approval_points": [],
            "revision": 0,
        },
        "goal_spec": {"goal_id": "g1"},
    }
    update = await run_validation_node(state, session)

    assert update["validation"]["passed"] is False
    assert update["repair_rounds"] == 1
    assert session.recorder.has("verification.started")
    assert session.recorder.has("verification.failed")
    assert session.recorder.has("repair.started")
    assert update["plan_manifest"]["revision"] == 1
    assert update["plan_manifest"]["steps"][-1]["kind"] == "repair"
    assert "第 1 轮自动修复" in update["repair_hint"]
    # 修复指令只进下一次模型调用（hook_context 会被 drain 一次），不写进用户历史。
    assert len(session.hook_context) == 1
    assert session.hook_context[0]["event"] == "repair.started"
    assert "messages" not in state  # 没有往历史里塞任何东西


@pytest.mark.asyncio
async def test_a_repeated_failure_stops_the_loop_instead_of_burning_budget():
    session = _ValidationSession()
    first = await run_validation_node(
        {
            "tool_results": _failed_tool_results(),
            "repair_rounds": 0,
            "plan_manifest": {"plan_id": "p", "steps": [], "approval_points": [], "revision": 0},
            "goal_spec": {},
        },
        session,
    )
    session2 = _ValidationSession()
    second = await run_validation_node(
        {
            "tool_results": _failed_tool_results(),
            "repair_rounds": 1,
            "plan_manifest": first["plan_manifest"],
            "goal_spec": {},
        },
        session2,
    )
    assert second["plan_manifest"]["no_new_evidence"] is True
    assert second["plan_manifest"]["exhausted"] is True
    assert session2.recorder.has("repair.stopped")
    assert not session2.recorder.has("repair.started")
    assert session2.hook_context == []
    assert route_after_validation(
        {**second, "validation": {"status": "error"}}, _ROUTE_SESSION
    ) == "review_result"


def test_route_after_validation_now_repairs_error_status_too():
    base = {"repair_rounds": 1, "plan_manifest": {}, "validation": {"status": "error"}}
    assert route_after_validation(base, _ROUTE_SESSION) == "execute_action"
    assert route_after_validation({"validation": {"status": "ok"}}, _ROUTE_SESSION) == "review_result"
    assert route_after_validation({"validation": {"status": "skipped"}}, _ROUTE_SESSION) == "summarize_result"
    assert route_after_validation(
        {"validation": {"status": "error"}, "budget_exhausted": True}, _ROUTE_SESSION
    ) == "review_result"


# --------------------------------------------------------------------------- Verifier 接线


def test_verifier_verdict_overrides_a_false_success():
    assert resolve_status({"status": "running", "validation": {"status": "skipped"}}) == "succeeded"
    assert (
        resolve_status(
            {
                "status": "running",
                "validation": {"status": "skipped"},
                "verdict": {"blocking": True, "verdict": "failed"},
            }
        )
        == "failed"
    )
    # 纯问答的 not_applicable 不能把回答判成失败。
    assert (
        resolve_status(
            {"status": "running", "validation": {}, "verdict": {"blocking": False, "verdict": "not_applicable"}}
        )
        == "succeeded"
    )
    # 取消/暂停优先于独立判定。
    assert (
        resolve_status({"status": "cancelled", "verdict": {"blocking": True}})
        == "cancelled"
    )
