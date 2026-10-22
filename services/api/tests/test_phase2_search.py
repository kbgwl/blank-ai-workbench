"""第二阶段：跨群、跨任务的全文检索。"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.search import SearchDoc
from blank_api.models.task import Task
from blank_api.models.team import TeamBot, TeamGroup, TeamMessage
from blank_api.models.thread import Thread
from blank_api.services.chat_index import (
    backfill,
    delete_group_docs,
    index_task_summary,
    index_team_message,
    search_chat,
)
from tests.test_agent_v04 import API, create_project, create_provider, env  # noqa: F401
from tests.test_team import make_group


async def _add_message(group_id: str, content: str) -> int:
    async with get_session_factory()() as session:
        group = await session.get(TeamGroup, group_id)
        message = TeamMessage(
            group_id=group_id, role="user", content=content, status="sent", meta={}
        )
        session.add(message)
        await session.flush()
        await index_team_message(session, message, group)
        message_id = message.id
        await session.commit()
        return message_id


async def test_group_state_around_id_returns_the_anchor_window(env, workspace: Path) -> None:  # noqa: F811
    _, _, group_id = await make_group(env, workspace)
    async with get_session_factory()() as session:
        rows = [
            TeamMessage(
                group_id=group_id,
                role="user",
                content=f"历史消息 {index}",
                status="sent",
                meta={},
            )
            for index in range(150)
        ]
        session.add_all(rows)
        await session.flush()
        first_id, last_id = rows[0].id, rows[-1].id
        await session.commit()

    # 默认只看最近 120 条，最早那条不在里面
    latest = (await env.client.get(f"{API}/team/groups/{group_id}/state")).json()
    latest_ids = [row["id"] for row in latest["messages"]]
    assert last_id in latest_ids and first_id not in latest_ids

    # 带着命中消息的 id 取窗口，锚点必须真的在返回集里，供前端滚动高亮
    around = (
        await env.client.get(
            f"{API}/team/groups/{group_id}/state", params={"around_id": first_id}
        )
    ).json()
    around_ids = [row["id"] for row in around["messages"]]
    assert first_id in around_ids
    assert last_id not in around_ids
    assert len(around_ids) <= 121


async def test_search_across_groups_and_filter(env, workspace: Path) -> None:  # noqa: F811
    _, _, group_a = await make_group(env, workspace, members=("后端",))
    async with get_session_factory()() as session:
        # 第二个群直接建，避免复用 make_group 时同名 Bot 冲突
        project_id = (await session.get(TeamGroup, group_a)).project_id
        admin = (
            await session.execute(select(TeamBot).where(TeamBot.is_admin == True))  # noqa: E712
        ).scalar_one()
        group = TeamGroup(
            id=uuid.uuid4().hex,
            project_id=project_id,
            name="支付群",
            topic="支付回调",
            admin_bot_id=admin.id,
        )
        session.add(group)
        await session.flush()
        group_b = group.id
        await session.commit()
    await _add_message(group_a, "登录接口联调完成，结论是缓存穿透要加布隆过滤器")
    await _add_message(group_b, "支付回调必须做强制幂等，重复通知只记一次账")

    resp = await env.client.get(f"{API}/team/search", params={"q": "缓存穿透"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"] and body["results"][0]["group_id"] == group_a
    assert "布隆" in body["results"][0]["snippet"]

    only_b = (await env.client.get(f"{API}/team/search", params={"q": "幂等", "group_id": group_b})).json()
    assert only_b["results"] and all(row["group_id"] == group_b for row in only_b["results"])

    both = (await env.client.get(f"{API}/team/search", params={"q": "登录 支付"})).json()
    assert {row["group_id"] for row in both["results"]} == {group_a, group_b}

    # 删除群必须把索引一起清掉，否则搜索会跳到一个不存在的群
    deleted = await env.client.delete(f"{API}/team/groups/{group_b}")
    assert deleted.status_code == 204, deleted.text
    after = (await env.client.get(f"{API}/team/search", params={"q": "幂等"})).json()
    assert after["results"] == []


async def test_task_summary_is_searchable(env, workspace: Path) -> None:  # noqa: F811
    project_id = await create_project(env, workspace)
    await create_provider(env)
    thread = Thread(
        id=uuid.uuid4().hex, project_id=project_id, title="登录改造", status="active", kind="chat"
    )
    task = Task(
        id=uuid.uuid4().hex,
        project_id=project_id,
        thread_id=thread.id,
        turn_index=1,
        title="登录改造",
        goal="改造登录",
        status="succeeded",
        result_summary="结论：登录页改用验证码登录，旧密码登录保留一个版本",
    )
    async with get_session_factory()() as session:
        session.add(thread)
        session.add(task)
        await session.flush()
        await index_task_summary(session, task, thread)
        task_id = task.id
        await session.commit()

    body = (await env.client.get(f"{API}/team/search", params={"q": "验证码登录"})).json()
    assert any(row["source"] == "task" and row["task_id"] == task_id for row in body["results"])
    hit = next(row for row in body["results"] if row["task_id"] == task_id)
    assert hit["speaker"] == "助手" and "验证码" in hit["content"]


async def test_group_delete_cleans_docs_and_backfill_restores_old_messages(env, workspace: Path) -> None:  # noqa: F811
    _, _, group_id = await make_group(env, workspace)
    async with get_session_factory()() as session:
        group = await session.get(TeamGroup, group_id)
        message = TeamMessage(
            group_id=group_id, role="bot", bot_name="后端", content="历史消息：灰度发布先放 5%", status="succeeded"
        )
        session.add(message)
        await session.flush()
        message_id = message.id
        await session.commit()

    async with get_session_factory()() as session:
        assert (
            await session.execute(select(SearchDoc).where(SearchDoc.source_id == str(message_id)))
        ).scalar_one_or_none() is None
    added = await backfill()
    assert added >= 1
    body = (await env.client.get(f"{API}/team/search", params={"q": "灰度发布"})).json()
    assert body["results"] and body["results"][0]["message_id"] == message_id

    async with get_session_factory()() as session:
        await delete_group_docs(session, group_id)
        await session.commit()
        left = (
            await session.execute(select(SearchDoc).where(SearchDoc.group_id == group_id))
        ).scalars().all()
        assert left == []
