# -*- coding: utf-8 -*-
"""v0.10.1 §3：审批对象与授权状态统一。

「我同意了却说没有同意 Clipboard_Screenshot.png」的根子是审批没有身份：
前端只能提交 approve/scope，后端按"任务里第一张 pending 卡"处理，多张卡并存、
审批过期后界面仍可点、重试换了参数——全都对不上号还都看不出来。

这里钉死四条：

1. 待审批卡带完整身份（approval_id / tool_call_id / arguments_hash /
   resource_path），与中断事件、审批事件三处一致；
2. 提交的身份对不上 → 409 ``approval_mismatch``「审批对象已变化」，
   绝不静默换卡，也绝不只回一句"没有同意"；
3. 同一张卡重复提交 → 200 ``already_decided``，不重复执行；
4. approval.granted / approval.denied 事件带同一组身份字段，审计可对号。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.task import AgentEvent

from tests.test_agent_v04 import (  # noqa: F401  (复用阶段3 的夹具)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)

ENV_PATCH = "--- a/.env\n+++ b/.env\n@@ -0,0 +1,1 @@\n+TOKEN=plain\n"


async def _events(env, task_id: str, kind: str) -> list[dict]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(AgentEvent)
                .where(AgentEvent.task_id == task_id, AgentEvent.type == kind)
                .order_by(AgentEvent.seq)
            )
        ).scalars().all()
        return [dict(row.payload or {}) for row in rows]


async def _waiting_task(env, workspace: Path) -> str:
    """走到「写 .env 需审批」的 waiting_approval 任务（与 test_approval_v06 同一条路）。"""
    provider_id = await create_provider(env)
    project_id = await create_project(env, workspace)
    task_id = await create_task(env, project_id, ["写入 .env 需人工审批"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 写入 .env"},
            {"content": "写入 .env", "tool_calls": [call("apply_patch", {"path": ".env", "patch": ENV_PATCH})]},
            {"content": ".env 已写入"},
            {"content": "已完成 .env 写入"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "waiting_approval", resp.text
    return task_id


async def _pending_card(env, task_id: str) -> dict:
    resp = await env.client.get(f"{API}/tasks/{task_id}/evidence/summary")
    assert resp.status_code == 200, resp.text
    cards = resp.json().get("pending_approvals") or []
    assert cards, "证据包里没有待审批卡"
    return cards[0]


# ------------------------------------------------------------------ #
# 1：审批卡带完整身份
# ------------------------------------------------------------------ #


async def test_pending_card_carries_full_identity(env, workspace):  # noqa: F811
    task_id = await _waiting_task(env, workspace)
    card = await _pending_card(env, task_id)

    assert card["approval_id"].startswith("apr-"), card
    assert card["tool_call_id"], card
    assert card["arguments_hash"], card
    assert card["resource_path"] == ".env", card

    # 中断事件与卡片是同一组字段：前端从哪边取都能对上。
    required = await _events(env, task_id, "approval.required")
    assert required, "没有 approval.required 事件"
    payload = required[-1]
    assert payload["approval_id"] == card["approval_id"]
    assert payload["arguments_hash"] == card["arguments_hash"]


async def test_identity_is_stable_across_replays(env, workspace):  # noqa: F811
    """同一张卡重放（重新读证据包）必须算出同一个 approval_id：幂等判定靠它。"""
    task_id = await _waiting_task(env, workspace)
    first = await _pending_card(env, task_id)
    second = await _pending_card(env, task_id)
    assert first["approval_id"] == second["approval_id"]


# ------------------------------------------------------------------ #
# 2：身份对不上 → 明确拒绝
# ------------------------------------------------------------------ #


async def test_wrong_approval_id_is_rejected_with_specific_error(env, workspace):  # noqa: F811
    task_id = await _waiting_task(env, workspace)
    card = await _pending_card(env, task_id)

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={
            "approve": True,
            "approval_id": "apr-doesnotexist",
            "tool_call_id": card["tool_call_id"],
            "arguments_hash": card["arguments_hash"],
        },
    )
    assert resp.status_code == 409, resp.text
    body = resp.json()["error"]
    assert body["code"] == "approval_mismatch", body
    assert "审批对象已变化" in body["message"], body
    # 拒绝就是拒绝：文件没有落地。
    assert not (workspace / ".env").exists()


async def test_wrong_arguments_hash_is_rejected(env, workspace):  # noqa: F811
    """重试/重新生成后参数变了：旧授权不能再套在新载荷上。"""
    task_id = await _waiting_task(env, workspace)
    card = await _pending_card(env, task_id)

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={
            "approve": True,
            "approval_id": card["approval_id"],
            "tool_call_id": card["tool_call_id"],
            "arguments_hash": "deadbeef",
        },
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "approval_mismatch", resp.text
    assert not (workspace / ".env").exists()


# ------------------------------------------------------------------ #
# 3：身份匹配才执行；重复点击幂等
# ------------------------------------------------------------------ #


async def test_matching_identity_executes_and_grants(env, workspace):  # noqa: F811
    task_id = await _waiting_task(env, workspace)
    card = await _pending_card(env, task_id)

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={
            "approve": True,
            "approval_id": card["approval_id"],
            "tool_call_id": card["tool_call_id"],
            "arguments_hash": card["arguments_hash"],
        },
    )
    assert resp.status_code == 200, resp.text
    assert (workspace / ".env").read_text(encoding="utf-8") == "TOKEN=plain"

    granted = await _events(env, task_id, "approval.granted")
    assert granted, "没有 approval.granted 事件"
    assert granted[-1]["approval_id"] == card["approval_id"]
    assert granted[-1]["resource_path"] == ".env"


async def test_repeat_click_returns_already_decided_not_error(env, workspace):  # noqa: F811
    """重复点击「允许」：返回 200 already_decided，不重复执行、不 409 吓人。"""
    task_id = await _waiting_task(env, workspace)
    card = await _pending_card(env, task_id)
    body = {
        "approve": True,
        "approval_id": card["approval_id"],
        "tool_call_id": card["tool_call_id"],
        "arguments_hash": card["arguments_hash"],
    }

    first = await env.client.post(f"{API}/tasks/{task_id}/approval", json=body)
    assert first.status_code == 200, first.text
    content_after_first = (workspace / ".env").read_text(encoding="utf-8")

    second = await env.client.post(f"{API}/tasks/{task_id}/approval", json=body)
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "already_decided", second.text
    # 文件没有被第二次执行改坏/追加。
    assert (workspace / ".env").read_text(encoding="utf-8") == content_after_first
    # 决定事件只落了一次。
    decided = await _events(env, task_id, "approval.decided")
    assert len([e for e in decided if e.get("approval_id") == card["approval_id"]]) == 1


async def test_decided_event_carries_identity(env, workspace):  # noqa: F811
    task_id = await _waiting_task(env, workspace)
    card = await _pending_card(env, task_id)
    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={"approve": True, "approval_id": card["approval_id"]},
    )
    assert resp.status_code == 200, resp.text
    decided = await _events(env, task_id, "approval.decided")
    assert decided, "没有 approval.decided 事件"
    assert decided[-1]["approval_id"] == card["approval_id"]
    assert decided[-1]["arguments_hash"] == card["arguments_hash"]
