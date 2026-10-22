"""第二阶段：主理人团队管理工具的可见性、审批与硬边界。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.task import ToolCall
from blank_api.models.team import TeamBot, TeamGroup, TeamMember, TeamMessage, TeamRound
from blank_api.services.permissions import PermissionPolicy
from blank_api.services.team import create_user_round
from blank_api.services.tools import TEAM_ADMIN_TOOL_NAMES, ToolContext, ToolGateway, build_default_tools
from tests.test_agent_v04 import API, Env, call, create_project, create_provider, create_task, env  # noqa: F401
from tests.test_team import _model_response, make_group, messages_of, wait_round


def _context(workspace: Path, scope: dict, *, task_id: str = "task-admin", mode: str = "bypass") -> ToolContext:
    return ToolContext(
        root=workspace,
        task_id=task_id,
        session_factory=get_session_factory(),
        permissions=PermissionPolicy.build(mode),
        team_scope=scope,
    )


async def _admin_id(env: Env) -> str:  # noqa: F811
    bots = (await env.client.get(f"{API}/team/bots")).json()
    return next(bot["id"] for bot in bots if bot["is_admin"])


async def test_non_team_task_never_sees_team_tools(env: Env, workspace: Path) -> None:  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, [])
    env.mock.reset([{"content": "完成"}])
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    started = next(event for event in events if event["type"] == "task.started")
    assert not (TEAM_ADMIN_TOOL_NAMES & set(started["payload"]["tools"]))


async def test_admin_turn_sees_team_status_and_can_call_it(env: Env, workspace: Path) -> None:  # noqa: F811
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        system = "\n".join(
            str(item.get("content") or "")
            for item in payload.get("messages") or []
            if item.get("role") == "system"
        )
        has_tool = any(item.get("role") == "tool" for item in payload.get("messages") or [])
        if "你的身份：小白管家" in system:
            if not has_tool:
                return _model_response(
                    "先看团队状态。", tool_calls=[call("team_status", {})]
                )
            return _model_response("团队状态已确认。")
        return _model_response("[PASS]")

    env.mock._handler = handler
    _, _, group_id = await make_group(env, workspace, members=("后端",))
    await env.client.post(f"{API}/team/groups/{group_id}/messages", json={"content": "报一下团队情况"})
    state = await wait_round(env, group_id, ("completed",))

    admin_message = next(row for row in messages_of(state, "bot") if row["bot_name"] == "小白管家")
    async with get_session_factory()() as session:
        calls = (
            await session.execute(
                select(ToolCall).where(ToolCall.task_id == admin_message["task_id"])
            )
        ).scalars().all()
        assert any(call_row.name == "team_status" for call_row in calls)
        status_call = next(call_row for call_row in calls if call_row.name == "team_status")
        assert status_call.result_summary and "后端" in status_call.result_summary

    events = (await env.client.get(f"{API}/tasks/{admin_message['task_id']}/events")).json()
    started = next(event for event in events if event["type"] == "task.started")
    assert "team_status" in started["payload"]["tools"]


async def test_high_risk_management_needs_approval(env: Env, workspace: Path) -> None:  # noqa: F811
    project_id, _, group_id = await make_group(env, workspace)
    admin_id = await _admin_id(env)
    scope = {"group_id": group_id, "bot_id": admin_id, "is_admin": True, "project_id": project_id}
    gateway = ToolGateway(build_default_tools())

    pending = await gateway.execute(
        "team_create_bot", {"name": "前端"}, _context(workspace, scope, mode="default"), "execute_action"
    )
    assert pending["status"] == "awaiting_approval", pending
    async with get_session_factory()() as session:
        found = (
            await session.execute(select(TeamBot).where(TeamBot.name == "前端"))
        ).scalar_one_or_none()
        assert found is None

    created = await gateway.execute(
        "team_create_bot", {"name": "前端"}, _context(workspace, scope), "execute_action"
    )
    assert created["ok"] is True, created
    assert created["data"]["permission_mode"] == "default"


async def test_duplicate_bot_name_is_actionable_not_a_crash(env: Env, workspace: Path) -> None:  # noqa: F811
    """重名必须回一句"该怎么办"，不能甩 IntegrityError。

    真模型端到端实测踩过这条：主理人被要求"把评审拉进群"，它选了 team_create_bot，
    而群里早已有个同名 Bot。`team_bots.name` 的 UNIQUE 约束在第一次 flush() 就炸，
    根本走不到包着 commit 的那个 try——工具回「工具执行失败：IntegrityError」。
    模型从中读不出"该改用 team_add_member"，于是原样重试同一个高风险调用，
    再挂一次审批，整轮永远停在 waiting_approval。
    """
    project_id, _, group_id = await make_group(env, workspace, members=("评审",))
    admin_id = await _admin_id(env)
    scope = {"group_id": group_id, "bot_id": admin_id, "is_admin": True, "project_id": project_id}
    from blank_api.services.tools.team_admin import _create_bot

    outcome = await _create_bot(_context(workspace, scope), {"name": "评审"})
    assert outcome.ok is False, outcome
    assert outcome.status == "error"
    assert "IntegrityError" not in outcome.message
    assert "team_add_member" in outcome.message, outcome.message
    async with get_session_factory()() as session:
        bots = (
            await session.execute(select(TeamBot).where(TeamBot.name == "评审"))
        ).scalars().all()
        # 成员里那个「评审」是 make_group 建的，不该再多出一个同名 Bot
        assert len(bots) == 1


async def test_tool_hard_boundaries(env: Env, workspace: Path) -> None:  # noqa: F811
    project_id, _, group_id = await make_group(env, workspace)
    admin_id = await _admin_id(env)
    scope = {"group_id": group_id, "bot_id": admin_id, "is_admin": True, "project_id": project_id}
    gateway = ToolGateway(build_default_tools())

    edit_admin = await gateway.execute(
        "team_update_bot",
        {"bot": admin_id, "name": "大老板"},
        _context(workspace, scope),
        "execute_action",
    )
    assert edit_admin["ok"] is False and edit_admin["status"] == "denied"
    assert "主理人" in edit_admin["message"]

    bypass = await gateway.execute(
        "team_create_bot",
        {"name": "全放行", "permission_mode": "bypass"},
        _context(workspace, scope),
        "execute_action",
    )
    # schema 的 enum 是第一条防线（校验阶段就拒绝）；即使绕过 schema，
    # handler 也必须给出明确的 denied，而不是靠参数校验兜底。
    assert bypass["ok"] is False
    from blank_api.services.tools.team_admin import _create_bot

    direct = await _create_bot(
        _context(workspace, scope), {"name": "全放行", "permission_mode": "bypass"}
    )
    assert direct.ok is False and direct.status == "denied", direct
    assert "bypass" in direct.message
    async with get_session_factory()() as session:
        found = (
            await session.execute(select(TeamBot).where(TeamBot.name == "全放行"))
        ).scalar_one_or_none()
        assert found is None

    non_admin = await gateway.execute(
        "team_create_bot",
        {"name": "越权"},
        _context(workspace, {**scope, "is_admin": False}),
        "execute_action",
    )
    assert non_admin["ok"] is False and non_admin["status"] == "denied"


async def test_member_limit_and_assign_task(env: Env, workspace: Path) -> None:  # noqa: F811
    project_id, _, group_id = await make_group(
        env, workspace, members=("后端", "前端", "评审", "测试")
    )
    admin_id = await _admin_id(env)
    scope = {"group_id": group_id, "bot_id": admin_id, "is_admin": True, "project_id": project_id}
    gateway = ToolGateway(build_default_tools())

    extra = (await env.client.post(f"{API}/team/bots", json={"name": "运维"})).json()
    too_many = await gateway.execute(
        "team_add_member", {"bot": extra["id"]}, _context(workspace, scope), "execute_action"
    )
    assert too_many["ok"] is False and "最多" in too_many["message"]

    async with get_session_factory()() as session:
        group = await session.get(TeamGroup, group_id)
        admin = await session.get(TeamBot, admin_id)
        target = (
            await session.execute(select(TeamBot).where(TeamBot.name == "后端"))
        ).scalar_one()
        message, round_row = await create_user_round(session, group, "开始做登录页", admin)
        round_id = round_row.id
        session.add(
            TeamMessage(
                group_id=group_id,
                round_id=round_id,
                role="bot",
                bot_id=admin_id,
                bot_name="小白管家",
                content="",
                status="running",
                task_id="task-admin-assign",
            )
        )
        await session.commit()
        target_id = target.id

    assigned = await gateway.execute(
        "team_assign_task",
        {"bot": "后端", "task": "检查登录页的输入校验"},
        _context(workspace, scope, task_id="task-admin-assign"),
        "execute_action",
    )
    assert assigned["ok"] is True, assigned
    async with get_session_factory()() as session:
        round_row = await session.get(TeamRound, round_id)
        assert target_id in list(round_row.queue or [])
        note = (
            await session.execute(
                select(TeamMessage).where(
                    TeamMessage.round_id == round_id,
                    TeamMessage.role == "system",
                )
            )
        ).scalars().all()
        assert any("检查登录页" in row.content for row in note)


async def test_member_changes_apply_inside_the_running_round(env: Env, workspace: Path) -> None:  # noqa: F811
    """主理人只可能在一轮之内调用管理工具：改动必须当轮生效，移出还要把队列里的名字摘掉。

    这条判定在"本轮进行中不许调整成员"的实现下会直接变红。
    """
    project_id, _, group_id = await make_group(env, workspace, members=("后端",))
    admin_id = await _admin_id(env)
    scope = {"group_id": group_id, "bot_id": admin_id, "is_admin": True, "project_id": project_id}
    gateway = ToolGateway(build_default_tools())
    extra = (await env.client.post(f"{API}/team/bots", json={"name": "运维"})).json()

    async with get_session_factory()() as session:
        group = await session.get(TeamGroup, group_id)
        admin = await session.get(TeamBot, admin_id)
        backend = (await session.execute(select(TeamBot).where(TeamBot.name == "后端"))).scalar_one()
        _, round_row = await create_user_round(session, group, "把运维拉进来一起干", admin)
        round_row.queue = [admin_id, backend.id]
        round_id = round_row.id
        backend_id = backend.id
        await session.commit()

    added = await gateway.execute(
        "team_add_member", {"bot": extra["id"]}, _context(workspace, scope), "execute_action"
    )
    assert added["ok"] is True, added
    async with get_session_factory()() as session:
        assert (
            await session.execute(
                select(TeamMember).where(TeamMember.group_id == group_id, TeamMember.bot_id == extra["id"])
            )
        ).scalar_one_or_none() is not None

    removed = await gateway.execute(
        "team_remove_member", {"bot": "后端"}, _context(workspace, scope), "execute_action"
    )
    assert removed["ok"] is True, removed
    async with get_session_factory()() as session:
        assert (
            await session.execute(
                select(TeamMember).where(TeamMember.group_id == group_id, TeamMember.bot_id == backend_id)
            )
        ).scalar_one_or_none() is None
        round_row = await session.get(TeamRound, round_id)
        assert backend_id not in list(round_row.queue or [])
        assert admin_id in list(round_row.queue or [])
