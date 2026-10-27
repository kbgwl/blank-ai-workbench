"""#113：模型自己发起的计划模式进出（enter_plan_mode / exit_plan_mode）。

对标 Claude Code 的 EnterPlanMode / ExitPlanMode：

* **进入**只收紧（从任何模式进去都不会更危险），策略层直接放行；
* **退出**会放宽，计划模式下必须人工批准（`mode:switch`），审查员不能代批；
* 切换落一条 `permission.mode_changed`，`to` 取策略真值；
* 模式活过审批中断与后续轮次（`task.started` 只记第一次，后续读最近一次切换）；
* 同一条消息里先进入计划模式再写文件：写操作留到下一步，不能搭"已批过"的顺风车。

判据分两层：判定/工具单测钉规则，端到端钉"事件 + 工作区实物"。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.services.approvals import approval_key
from blank_api.services.permissions import (
    ALLOW,
    ASK,
    DENY,
    MODE_ACCEPT_EDITS,
    MODE_BYPASS,
    MODE_DEFAULT,
    MODE_PLAN,
    READ_ONLY_TOOLS,
    PermissionPolicy,
)
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.plan import _enter_plan_mode, _exit_plan_mode
from blank_api.services.tools.registry import LEAN_TOOLS

from tests.test_agent_v04 import (  # noqa: F401  (复用阶段3 的夹具)
    API,
    PATCH,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)


def _tool(name: str):
    return next(tool for tool in build_default_tools() if tool.name == name)


def _decide(policy: PermissionPolicy, name: str, arguments: dict):
    return policy.decide(_tool(name), arguments, "low")


# --------------------------------------------------------------------------- #
# 1. 判定：进入免费、退出要批、批过的口径照常生效
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "mode", [MODE_DEFAULT, MODE_ACCEPT_EDITS, MODE_PLAN, MODE_BYPASS]
)
def test_enter_is_allowed_in_every_mode(mode: str) -> None:
    policy = PermissionPolicy(mode=mode)
    verdict = _decide(policy, "enter_plan_mode", {})
    assert verdict.outcome == ALLOW
    expected = "mode:already_plan" if mode == MODE_PLAN else "mode:enter_plan"
    assert verdict.rule == expected


def test_exit_asks_only_in_plan_mode_and_is_a_noop_elsewhere() -> None:
    for mode in (MODE_DEFAULT, MODE_ACCEPT_EDITS, MODE_BYPASS):
        verdict = _decide(PermissionPolicy(mode=mode), "exit_plan_mode", {"plan": "p"})
        assert verdict.outcome == ALLOW, mode
        assert verdict.rule == "mode:exit_noop"

    verdict = _decide(PermissionPolicy(mode=MODE_PLAN), "exit_plan_mode", {"plan": "p"})
    assert verdict.outcome == ASK
    assert verdict.rule == "mode:switch"
    # 退出不该给"本会话总是允许某条命令"的前缀档
    assert verdict.memo == ""


def test_exit_keeps_deny_rules_and_session_memory() -> None:
    from blank_api.services.permission_rules import parse_rules

    ruleset = parse_rules("deny exit_plan_mode\n")
    assert ruleset.error == ""
    policy = PermissionPolicy(mode=MODE_PLAN)
    policy.ruleset = ruleset
    verdict = _decide(policy, "exit_plan_mode", {"plan": "p"})
    assert verdict.outcome == DENY
    assert verdict.rule == "rule:deny"

    policy = PermissionPolicy(mode=MODE_PLAN)
    policy.forget("exit_plan_mode", {"plan": "p"})
    verdict = _decide(policy, "exit_plan_mode", {"plan": "p"})
    assert verdict.outcome == DENY
    assert verdict.rule == "action:denied"

    policy = PermissionPolicy(mode=MODE_PLAN)
    policy.approved_actions.add(approval_key("exit_plan_mode", {"plan": "p"}))
    assert _decide(policy, "exit_plan_mode", {"plan": "p"}).rule == "action:approved"

    policy = PermissionPolicy(mode=MODE_PLAN)
    policy.approved_tools.add((policy.sandbox_tier, "exit_plan_mode", ""))
    verdict = _decide(policy, "exit_plan_mode", {"plan": "p"})
    assert verdict.outcome == ALLOW
    assert verdict.rule == f"tool:{policy.sandbox_tier}:exit_plan_mode"


# --------------------------------------------------------------------------- #
# 2. 工具：真的改状态，并如实上报 before/after
# --------------------------------------------------------------------------- #
async def test_handlers_flip_and_report_mode() -> None:
    policy = PermissionPolicy()
    context = SimpleNamespace(permissions=policy)

    entered = await _enter_plan_mode(context, {})
    assert entered.ok and entered.data["changed"] is True
    assert entered.data["previous"] == MODE_DEFAULT
    assert policy.mode == MODE_PLAN

    again = await _enter_plan_mode(context, {})
    assert again.ok and again.data["changed"] is False
    assert policy.mode == MODE_PLAN

    exited = await _exit_plan_mode(context, {"plan": "1. 改 main.py\n2. 跑测试"})
    assert exited.ok and exited.data["changed"] is True
    assert exited.data["previous"] == MODE_PLAN
    assert exited.data["mode"] == MODE_DEFAULT
    assert exited.data["plan"] == "1. 改 main.py\n2. 跑测试"
    assert policy.mode == MODE_DEFAULT
    assert "计划已提交" in exited.output

    idle = await _exit_plan_mode(context, {})
    assert idle.ok and idle.data["changed"] is False
    assert policy.mode == MODE_DEFAULT


async def test_handlers_fail_without_a_policy() -> None:
    context = SimpleNamespace(permissions=None)
    assert (await _enter_plan_mode(context, {})).ok is False
    assert (await _exit_plan_mode(context, {})).ok is False


def test_mode_tools_are_registered_in_plan_and_lean_views() -> None:
    names = {tool.name for tool in build_default_tools()}
    assert {"enter_plan_mode", "exit_plan_mode"} <= names
    # 计划模式要能退出；lean 视图必须成对给，否则弱模型会卡在计划模式里。
    assert {"enter_plan_mode", "exit_plan_mode"} <= READ_ONLY_TOOLS
    assert {"enter_plan_mode", "exit_plan_mode"} <= LEAN_TOOLS


# --------------------------------------------------------------------------- #
# 3. 端到端
# --------------------------------------------------------------------------- #
def _payloads(events: list[dict], kind: str) -> list[dict]:
    return [event["payload"] for event in events if event["type"] == kind]


async def test_exit_plan_mode_asks_then_unlocks_writes(env, workspace: Path) -> None:  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["先计划再动手"])
    plan_text = "1. 修改 main.py\n2. 跑测试"
    env.mock.reset(
        [
            {"content": "先只读调研，不改文件。"},
            {"content": "计划好了", "tool_calls": [call("exit_plan_mode", {"plan": plan_text})]},
            {"content": "动手", "tool_calls": [call("apply_patch", {"path": "main.py", "patch": PATCH})]},
            {"content": "已完成"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run",
        json={"provider_id": provider_id, "permission_mode": MODE_PLAN},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "waiting_approval"

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    required = _payloads(events, "approval.required")
    assert len(required) == 1, events
    card = required[0]
    assert card["tool"] == "exit_plan_mode"
    assert card["permission_rule"] == "mode:switch"
    assert card["permission_mode"] == MODE_PLAN
    assert card["arguments"]["plan"] == plan_text
    assert (workspace / "main.py").read_text(encoding="utf-8") == "print('hi')\n"

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={"approve": True, "provider_id": provider_id, "scope": "once"},
    )
    assert resp.status_code == 200, resp.text
    # 批准后计划文本进了模型上下文，写操作按默认模式执行
    assert "patched" in (workspace / "main.py").read_text(encoding="utf-8")

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    changed = _payloads(events, "permission.mode_changed")
    assert len(changed) == 1, events
    assert changed[0]["tool"] == "exit_plan_mode"
    assert changed[0]["from"] == MODE_PLAN
    assert changed[0]["to"] == MODE_DEFAULT
    assert changed[0]["plan"] == plan_text
    patched = [
        payload
        for payload in _payloads(events, "tool.completed")
        if payload.get("tool") == "apply_patch"
    ]
    assert len(patched) == 1 and patched[0]["ok"] is True, patched


async def test_guardian_cannot_approve_the_plan_mode_exit(env, workspace: Path, monkeypatch) -> None:  # noqa: F811
    """退出计划模式是权限开关，不是普通动作：审查员说 allow 也不能替用户按这个钮。"""
    monkeypatch.setenv("BLANK_GUARDIAN", "auto")
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["审查员别替我退出"])
    env.mock.reset(
        [
            {"content": "先只读调研。"},
            {"content": "计划好了", "tool_calls": [call("exit_plan_mode", {"plan": "1. 改 main.py"})]},
            # 审查消耗下一个 mock 步骤；它说可以也不能生效
            {"content": '{"verdict": "allow", "reasons": ["低风险"]}'},
            {"content": "已完成"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run",
        json={"provider_id": provider_id, "permission_mode": MODE_PLAN},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "waiting_approval", resp.json()["task"]

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    reviewed = _payloads(events, "guardian.reviewed")
    assert reviewed and reviewed[-1]["outcome"] == "asked", reviewed
    assert _payloads(events, "guardian.auto_approved") == []
    card = _payloads(events, "approval.required")[0]
    assert card["tool"] == "exit_plan_mode"
    assert card["permission_rule"] == "mode:switch"


async def test_enter_plan_mode_persists_and_blocks_later_writes(env, workspace: Path) -> None:  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["先调研"])

    env.mock.reset(
        [
            {"content": "先只读调研。"},
            {"content": "进入计划模式", "tool_calls": [call("enter_plan_mode", {})]},
            {"content": "试着改文件", "tool_calls": [call("apply_patch", {"path": "main.py", "patch": PATCH})]},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    # 进入计划模式不需要审批卡
    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    assert _payloads(events, "approval.required") == []
    changed = _payloads(events, "permission.mode_changed")
    assert len(changed) == 1
    assert changed[0]["from"] == MODE_DEFAULT and changed[0]["to"] == MODE_PLAN

    patched = [
        payload
        for payload in _payloads(events, "tool.completed")
        if payload.get("tool") == "apply_patch"
    ]
    assert len(patched) == 1, events
    assert patched[0]["ok"] is False
    assert patched[0]["permission"]["rule"] == "mode:plan"
    assert (workspace / "main.py").read_text(encoding="utf-8") == "print('hi')\n"

    # 下一轮不带 permission_mode：模型拨过的模式要活过这一轮（task.started 只记第一次）。
    env.mock.reset([{"content": "继续"}, {"content": "完成"}])
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    started = _payloads(events, "task.started")
    assert started[-1]["permission_mode"] == MODE_PLAN, [item["permission_mode"] for item in started]


async def test_same_message_write_after_enter_is_refused(env, workspace: Path) -> None:  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["一条消息里先进入再写"])
    env.mock.reset(
        [
            {"content": "计划"},
            {
                "content": "进入并顺手改",
                "tool_calls": [
                    call("enter_plan_mode", {}),
                    call("apply_patch", {"path": "main.py", "patch": PATCH}),
                ],
            },
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    patched = [
        payload
        for payload in _payloads(events, "tool.completed")
        if payload.get("tool") == "apply_patch"
    ]
    assert len(patched) == 1, events
    assert patched[0]["ok"] is False
    assert "留到下一步" in (patched[0].get("summary") or patched[0].get("message") or ""), patched[0]
    assert (workspace / "main.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert len(_payloads(events, "permission.mode_changed")) == 1
