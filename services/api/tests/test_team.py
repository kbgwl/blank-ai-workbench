"""团队群聊：名册、编排顺序、静默、预算与审批恢复。

模型全部走脚本化的 MockTransport：这里验证的是团队层的排队与状态翻译，
不是模型能力。每次 Bot 发言仍是普通 Task，所以事件、用量与审批轨迹
可以直接用现有任务接口反查。
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from sqlalchemy import select

from tests.test_agent_v04 import (  # noqa: F401
    API,
    call,
    create_project,
    create_provider,
    env,
)

from blank_api.api.routes.team import _BACKGROUND
from blank_api.db import dispose_engine, get_session_factory
from blank_api.models.thread import Thread
from blank_api.services.team import ADMIN_NAME, is_pass, mentioned_names


@pytest.fixture(autouse=True)
async def _isolate_team_engine(env):  # noqa: F811
    """内存 SQLite 的共享连接绑定在事件循环上，跨测试复用会串库。

    每个测试结束后先停掉后台推进器与恢复任务，再释放引擎；下一个测试会拿到
    与自己的事件循环绑定的新连接。
    """
    yield
    app = getattr(getattr(env.client, "_transport", None), "app", None)
    orchestrator = getattr(getattr(app, "state", None), "team_orchestrator", None) if app else None
    if orchestrator is not None:
        await orchestrator.shutdown()
    pending = list(_BACKGROUND)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await dispose_engine()


def _model_response(content: str, *, prompt: int = 120, completion: int = 30, tool_calls=None):
    message: dict = {"content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": message,
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
            "model": "mock-team",
        },
    )


def install_team_model(env, *, member_tool: bool = False, prompt: int = 120, completion: int = 30):
    """按系统提示里的身份回话；需要审批时让「后端」发一条联网命令。"""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        messages = payload.get("messages") or []
        system = "\n".join(
            str(item.get("content") or "")
            for item in messages
            if item.get("role") == "system"
        )
        has_tool_result = any(item.get("role") == "tool" for item in messages)
        tool_calls = None
        if "你的身份：后端" in system:
            if member_tool and not has_tool_result:
                content = "需要先联网确认。"
                tool_calls = [call("run_shell", {"command": "curl https://example.com"})]
            else:
                content = "后端处理完成。"
        elif "你的身份：评审" in system:
            content = "[PASS]"
        else:
            content = "收到，@后端 你先处理。"
        return _model_response(content, prompt=prompt, completion=completion, tool_calls=tool_calls)

    env.mock._handler = handler


async def make_group(env, workspace, *, members=("后端",), budget: int = 60_000, max_speakers: int = 5):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    created: dict[str, dict] = {}
    for name in members:
        resp = await env.client.post(
            f"{API}/team/bots",
            json={"name": name, "role": f"{name}角色", "provider_id": provider_id},
        )
        assert resp.status_code == 201, resp.text
        created[name] = resp.json()
    resp = await env.client.post(
        f"{API}/team/groups",
        json={
            "project_id": project_id,
            "name": "交付群",
            "topic": "把登录页做出来",
            "member_ids": [created[name]["id"] for name in members],
            "max_speakers": max_speakers,
            "token_budget": budget,
        },
    )
    assert resp.status_code == 201, resp.text
    return project_id, provider_id, resp.json()["group"]["id"]


async def wait_round(env, group_id: str, statuses: tuple[str, ...], timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = (await env.client.get(f"{API}/team/groups/{group_id}/state")).json()
        status = (last.get("round") or {}).get("status")
        if status in statuses:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"等待轮次状态 {statuses} 超时，最后状态：{last}")


def messages_of(state: dict, role: str = "") -> list[dict]:
    return [row for row in state.get("messages") or [] if not role or row["role"] == role]


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
def test_pass_marker_is_not_a_reply() -> None:
    assert is_pass("[PASS]")
    assert is_pass(" [pass] ")
    assert is_pass("（PASS）")
    assert not is_pass("PASS 之后建议继续")
    assert not is_pass("通过了")


def test_mentions_follow_text_order_and_ignore_unknown() -> None:
    names = ["后端", "评审", "前端"]
    assert mentioned_names("请 @评审 复核，再让 @后端 改一下", names) == ["评审", "后端"]
    assert mentioned_names("＠后端 看一下", names) == ["后端"]
    assert mentioned_names("没有点名，只是普通文本", names) == []
    assert mentioned_names("@后端 @后端 重复点名", names) == ["后端"]


# --------------------------------------------------------------------------- #
# 名册
# --------------------------------------------------------------------------- #
async def test_bootstrap_creates_admin_and_protects_it(env):  # noqa: F811
    bootstrap = (await env.client.get(f"{API}/team/bootstrap")).json()
    admin = next(bot for bot in bootstrap["bots"] if bot["is_admin"])
    assert admin["name"] == ADMIN_NAME
    assert bootstrap["admin_name"] == ADMIN_NAME

    created = await env.client.post(
        f"{API}/team/bots",
        json={"name": "后端", "role": "服务端", "permission_mode": "accept_edits", "max_steps": 6},
    )
    assert created.status_code == 201, created.text
    bot = created.json()
    assert bot["name"] == "后端" and bot["is_admin"] is False
    assert bot["permission_mode"] == "accept_edits" and bot["max_steps"] == 6

    duplicate = await env.client.post(f"{API}/team/bots", json={"name": "后端"})
    assert duplicate.status_code == 409, duplicate.text

    bad_name = await env.client.post(f"{API}/team/bots", json={"name": "后 端"})
    assert bad_name.status_code == 422

    rename_admin = await env.client.patch(
        f"{API}/team/bots/{admin['id']}", json={"name": "大老板"}
    )
    assert rename_admin.status_code == 409
    disable_admin = await env.client.patch(
        f"{API}/team/bots/{admin['id']}", json={"enabled": False}
    )
    assert disable_admin.status_code == 409
    delete_admin = await env.client.delete(f"{API}/team/bots/{admin['id']}")
    assert delete_admin.status_code == 409

    renamed = await env.client.patch(f"{API}/team/bots/{bot['id']}", json={"name": "服务端"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "服务端"

    removed = await env.client.delete(f"{API}/team/bots/{bot['id']}")
    assert removed.status_code == 204
    names = [row["name"] for row in (await env.client.get(f"{API}/team/bots")).json()]
    assert "服务端" not in names


async def test_workspace_must_match_group_project(env, workspace, tmp_path):  # noqa: F811
    project_a = await create_project(env, workspace)
    other_root = tmp_path / "other"
    other_root.mkdir()
    project_b = await create_project(env, other_root)
    project = (await env.client.get(f"{API}/projects/{project_a}")).json()
    workspace_a = project["workspaces"][0]["id"]

    bot = (await env.client.post(f"{API}/team/bots", json={"name": "后端", "workspace_id": workspace_a})).json()
    resp = await env.client.post(
        f"{API}/team/groups",
        json={"project_id": project_b, "name": "错项目群", "member_ids": [bot["id"]]},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bot_workspace_mismatch"

    # 已经在 A 项目群里之后，不能再把 Bot 绑到 B 项目的工作区
    ok_group = await env.client.post(
        f"{API}/team/groups",
        json={"project_id": project_a, "name": "正确项目群", "member_ids": [bot["id"]]},
    )
    assert ok_group.status_code == 201, ok_group.text
    project_b_payload = (await env.client.get(f"{API}/projects/{project_b}")).json()
    workspace_b = project_b_payload["workspaces"][0]["id"]
    patched = await env.client.patch(f"{API}/team/bots/{bot['id']}", json={"workspace_id": workspace_b})
    assert patched.status_code == 409
    assert patched.json()["error"]["code"] == "bot_workspace_mismatch"


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #
async def test_group_turn_order_pass_and_wrap_up(env, workspace):  # noqa: F811
    install_team_model(env)
    project_id, _, group_id = await make_group(env, workspace, members=("后端", "评审"))

    posted = await env.client.post(
        f"{API}/team/groups/{group_id}/messages", json={"content": "做一个登录页"}
    )
    assert posted.status_code == 201, posted.text
    state = await wait_round(env, group_id, ("completed",))

    bots = messages_of(state, "bot")
    assert [row["bot_name"] for row in bots] == ["小白管家", "后端", "小白管家"], json.dumps(
        {"round": state["round"], "messages": state["messages"]}, ensure_ascii=False
    )
    assert bots[-1]["meta"]["wrap_up"] is True
    assert "后端处理完成" in bots[1]["content"]
    assert state["round"]["spoken"] == [
        state["members"][0]["bot_id"],
        next(row["bot_id"] for row in bots if row["bot_name"] == "后端"),
        state["members"][0]["bot_id"],
    ]
    assert state["round"]["tokens"] > 0
    assert {row["bot_name"] for row in state["usage_by_bot"]} >= {"小白管家", "后端"}

    # 每个 Bot 在群里有一条隐藏会话，用户对话列表不出现这些线程
    async with get_session_factory()() as session:
        threads = (await session.execute(select(Thread).where(Thread.kind == "team"))).scalars().all()
        assert len(threads) == 3
        assert all(thread.title.startswith("团队·交付群·") for thread in threads)
    visible = (await env.client.get(f"{API}/projects/{project_id}/threads")).json()
    assert visible == []


async def test_silent_member_is_recorded_as_pass(env, workspace):  # noqa: F811
    install_team_model(env)
    _, _, group_id = await make_group(env, workspace, members=("评审",))
    await env.client.post(f"{API}/team/groups/{group_id}/messages", json={"content": "@评审 需要评审吗？"})
    state = await wait_round(env, group_id, ("completed",))
    reviewer = next(row for row in messages_of(state, "bot") if row["bot_name"] == "评审")
    assert reviewer["status"] == "pass"
    assert reviewer["content"] == ""


async def test_token_budget_stops_the_round(env, workspace):  # noqa: F811
    install_team_model(env, prompt=800, completion=400)
    _, _, group_id = await make_group(env, workspace, members=("后端",), budget=5_000)
    await env.client.post(f"{API}/team/groups/{group_id}/messages", json={"content": "做一个登录页"})
    state = await wait_round(env, group_id, ("completed",))
    assert state["round"]["tokens"] >= 5_000
    system_rows = messages_of(state, "system")
    assert any("预算上限" in row["content"] for row in system_rows)


async def test_approval_waits_then_resumes_the_same_turn(env, workspace):  # noqa: F811
    install_team_model(env, member_tool=True)
    _, _, group_id = await make_group(env, workspace, members=("后端",))
    await env.client.post(f"{API}/team/groups/{group_id}/messages", json={"content": "确认公网可达"})

    waiting = await wait_round(env, group_id, ("waiting_approval",))
    pending = waiting["pending"]
    assert pending and pending[0]["approvals"], pending
    assert pending[0]["approvals"][0]["tool"] == "run_shell"
    message_id = pending[0]["message_id"]
    task_id = pending[0]["task_id"]

    rejected = await env.client.post(
        f"{API}/team/messages/{message_id}/approval", json={"approve": False}
    )
    assert rejected.status_code == 202, rejected.text

    finished = await wait_round(env, group_id, ("completed", "failed"))
    assert finished["round"]["status"] == "completed", finished["round"]
    resumed = next(row for row in finished["messages"] if row["id"] == message_id)
    # 驳回高风险操作会让任务停在安全闸门（paused），不是失败；群聊里如实标注，
    # 随后由主理人收尾。
    assert resumed["status"] == "paused", json.dumps(
        {"resumed": resumed, "round": finished["round"], "messages": finished["messages"]},
        ensure_ascii=False,
    )
    assert "驳回" in resumed["content"]
    assert messages_of(finished, "bot")[-1]["meta"]["wrap_up"] is True

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    types = [event["type"] for event in events]
    assert "approval.required" in types and "approval.decided" in types
