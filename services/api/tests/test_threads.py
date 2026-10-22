"""多轮会话：上一轮说过的话，这一轮还算数。

要证明四件事：

1. **串起来**。同一会话的第二轮能看见第一轮要了什么、交付了什么；
   不同会话、以及没有会话的独立任务，一个字都不该串。
2. **装不下时先丢远的**。上下文是有预算的，先丢的必须是最久的那一轮，
   而不是随便丢——丢错顺序等于让模型记得开场白、忘了刚才那句话。
3. **分叉继承上下文、不复制任务**。复制任务等于复制它的审计记录；
   而"带着刚才聊的内容另开一条线"只需要沿 forked_from 回溯。
4. **不加会话时行为完全不变**。没有 thread_id 的任务，提示词与之前逐字节一致。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.project import Project, Workspace
from blank_api.models.task import Task
from blank_api.models.thread import Thread
from blank_api.services.agent.graph import initial_messages
from blank_api.services.threads import (
    FORK_DEPTH,
    HISTORY_MAX_CHARS,
    HISTORY_TURNS,
    history_for,
    summarize_history,
)


async def _project() -> Project:
    async with get_session_factory()() as session:
        project = Project(id=str(uuid.uuid4()), name="会话测试")
        session.add(project)
        await session.commit()
        return project


async def _thread(project_id: str, *, title: str = "大会话", forked_from: str | None = None) -> Thread:
    async with get_session_factory()() as session:
        thread = Thread(
            id=str(uuid.uuid4()),
            project_id=project_id,
            title=title,
            forked_from=forked_from,
            turn_count=0,
        )
        session.add(thread)
        await session.commit()
        return thread


async def _turn(
    thread_id: str, project_id: str, *, goal: str, summary: str = "", failure: str = "", index: int = 0
) -> Task:
    async with get_session_factory()() as session:
        task = Task(
            id=str(uuid.uuid4()),
            project_id=project_id,
            thread_id=thread_id,
            turn_index=index,
            title=goal[:40],
            goal=goal,
            status="succeeded" if summary else "draft",
            result_summary=summary,
            failure_reason=failure,
        )
        session.add(task)
        await session.commit()
        return task


# ------------------------------------------------------------------ 串起来


async def test_second_turn_sees_the_first(client):
    project = await _project()
    thread = await _thread(project.id)
    await _turn(thread.id, project.id, goal="把登录页改成深色", summary="改好了，涉及 3 个文件")

    history = await history_for(get_session_factory(), thread.id)

    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert "把登录页改成深色" in history[0]["content"]
    assert "第 1 轮" in history[0]["content"]
    assert history[1]["role"] == "assistant"
    assert "3 个文件" in history[1]["content"]


async def test_current_turn_is_not_its_own_history(client):
    project = await _project()
    thread = await _thread(project.id)
    first = await _turn(thread.id, project.id, goal="第一步", summary="做完了")
    second = await _turn(thread.id, project.id, goal="第二步")

    # 正在跑的这一轮如果被当成历史带进去，模型会看到两个"当前目标"
    history = await history_for(get_session_factory(), thread.id, exclude_task_id=second.id)

    assert len(history) == 2
    assert any("第一步" in item["content"] for item in history)
    assert not any("第二步" in item["content"] for item in history)
    assert first.id


async def test_a_task_without_a_thread_gets_no_history(client):
    # 独立任务：thread_id 为空时必须返回空，连一次查询都不该发
    assert await history_for(get_session_factory(), "") == []
    assert await history_for(get_session_factory(), None) == []


async def test_two_threads_do_not_leak_into_each_other(client):
    project = await _project()
    left = await _thread(project.id, title="左")
    right = await _thread(project.id, title="右")
    await _turn(left.id, project.id, goal="左线的内容", summary="左线的结论")
    await _turn(right.id, project.id, goal="右线的内容", summary="右线的结论")

    history = await history_for(get_session_factory(), right.id)

    assert any("右线" in item["content"] for item in history)
    assert not any("左线" in item["content"] for item in history)


async def test_failed_turn_still_carries_why(client):
    project = await _project()
    thread = await _thread(project.id)
    await _turn(thread.id, project.id, goal="部署到测试环境", failure="端口 8080 已被占用")

    history = await history_for(get_session_factory(), thread.id)

    # 不知道上一轮为什么失败，下一轮多半会照着同样的方式再失败一次
    assert "端口 8080 已被占用" in history[1]["content"]


# ------------------------------------------------------------------ 预算


async def test_history_keeps_the_newest_turns(client):
    project = await _project()
    thread = await _thread(project.id)
    for index in range(HISTORY_TURNS + 4):
        await _turn(
            thread.id, project.id, goal=f"第{index}步", summary=f"第{index}步的结论", index=index + 1
        )

    history = await history_for(get_session_factory(), thread.id)

    assert len(history) == HISTORY_TURNS * 2
    # 丢的必须是最久的那几轮：模型需要的是刚才那句话，不是开场白
    assert not any("第0步" in item["content"] for item in history)
    assert any(f"第{HISTORY_TURNS + 3}步" in item["content"] for item in history)


async def test_char_budget_drops_far_turns_not_the_latest(client):
    project = await _project()
    thread = await _thread(project.id)
    await _turn(thread.id, project.id, goal="旧的一轮", summary="旧" * 4_000)
    await _turn(thread.id, project.id, goal="新的一轮", summary="新的结论")

    history = await history_for(get_session_factory(), thread.id)

    assert any("新的结论" in item["content"] for item in history)
    assert summarize_history(history)["chars"] <= HISTORY_MAX_CHARS + 2_100
    assert any("旧的一轮" not in item["content"] for item in history if item["role"] == "assistant")


async def test_a_single_huge_turn_is_clipped_not_dropped(client):
    project = await _project()
    thread = await _thread(project.id)
    await _turn(thread.id, project.id, goal="很长的一轮", summary="结" * 20_000)

    history = await history_for(get_session_factory(), thread.id)

    # 整轮丢弃会让"上一轮做了什么"完全消失，截断的损失小得多
    assert len(history) == 2
    assert len(history[1]["content"]) < 20_000


# ------------------------------------------------------------------ 分叉


async def test_fork_inherits_context_without_copying_tasks(client):
    project = await _project()
    source = await _thread(project.id, title="原线")
    await _turn(source.id, project.id, goal="原来的话题", summary="原来的结论")
    branch = await _thread(project.id, title="分叉", forked_from=source.id)

    history = await history_for(get_session_factory(), branch.id)
    async with get_session_factory()() as session:
        copied = (await session.execute(select(Task).where(Task.thread_id == branch.id))).scalars().all()

    assert any("原来的话题" in item["content"] for item in history)
    # 只记来源，不复制任务：任务带着它自己的事件与审计，复制一份就是两份真相
    assert copied == []


async def test_fork_chain_adds_its_own_turns_first(client):
    project = await _project()
    source = await _thread(project.id, title="原线")
    await _turn(source.id, project.id, goal="上一代的话题", summary="上一代的结论")
    branch = await _thread(project.id, title="分叉", forked_from=source.id)
    await _turn(branch.id, project.id, goal="分叉后的话题", summary="分叉后的结论")

    history = await history_for(get_session_factory(), branch.id)

    texts = [item["content"] for item in history]
    assert any("分叉后的话题" in text for text in texts)
    assert any("上一代的话题" in text for text in texts)
    # 顺序：先老后新，否则模型读到的"最近发生了什么"是反的
    assert texts.index(next(t for t in texts if "上一代" in t)) < texts.index(
        next(t for t in texts if "分叉后" in t)
    )


async def test_fork_chain_is_bounded(client):
    project = await _project()
    head = await _thread(project.id, title="第0代")
    await _turn(head.id, project.id, goal="最老的话题", summary="最老的结论")
    cursor = head
    for index in range(FORK_DEPTH + 2):
        cursor = await _thread(project.id, title=f"第{index + 1}代", forked_from=cursor.id)

    history = await history_for(get_session_factory(), cursor.id)

    # 分叉的分叉再往上，和最初那条线已经没什么关系了
    assert not any("最老的话题" in item["content"] for item in history)


# ------------------------------------------------------------------ 提示词


def test_history_sits_between_system_and_the_current_goal():
    messages = initial_messages(
        "这次要做什么",
        [],
        history=[
            {"role": "user", "content": "上次要做什么"},
            {"role": "assistant", "content": "上次做完了"},
        ],
    )

    assert messages[0]["role"] == "system"
    assert messages[1]["content"] == "上次要做什么"
    assert messages[2]["role"] == "assistant"
    assert messages[3]["role"] == "user"
    # 当前目标必须还在最后：历史放在后面的话，模型会把旧要求当成这一轮的任务
    assert "这次要做什么" in messages[-1]["content"]
    # 系统提示仍然第一段，前缀缓存靠它
    assert "空白格" in messages[0]["content"] or messages[0]["content"]


def test_without_history_the_prompt_is_unchanged():
    plain = initial_messages("做点什么", ["能跑"])
    assert [item["role"] for item in plain] == ["system", "user"]
    assert plain == initial_messages("做点什么", ["能跑"], history=[])


def test_history_drops_empty_entries():
    messages = initial_messages("做事", [], history=[{"role": "user", "content": ""}])
    assert [item["role"] for item in messages] == ["system", "user"]


async def test_summarize_history_counts_turns_and_chars():
    report = summarize_history(
        [
            {"role": "user", "content": "一二三"},
            {"role": "assistant", "content": "四五六"},
        ]
    )
    assert report == {"turns": 1, "chars": 6}


# ------------------------------------------------------------------ 接口


API = "/api/v1"


async def _api_project(client) -> str:
    project = (await client.post(f"{API}/projects", json={"name": f"会话-{uuid.uuid4().hex[:6]}"})).json()
    return project["id"]


async def test_create_list_and_read_a_thread(client):
    project_id = await _api_project(client)

    created = await client.post(f"{API}/projects/{project_id}/threads", json={"title": "改登录页"})
    assert created.status_code == 201, created.text
    thread = created.json()
    assert thread["title"] == "改登录页"
    assert thread["turn_count"] == 0

    listed = (await client.get(f"{API}/projects/{project_id}/threads")).json()
    assert [item["id"] for item in listed] == [thread["id"]]

    detail = (await client.get(f"{API}/threads/{thread['id']}")).json()
    assert detail["turns"] == []


async def test_turns_are_numbered_and_listed_in_order(client):
    project_id = await _api_project(client)
    thread = (await client.post(f"{API}/projects/{project_id}/threads", json={})).json()

    first = await client.post(f"{API}/threads/{thread['id']}/turns", json={"title": "第一步", "goal": "看一下项目"})
    second = await client.post(f"{API}/threads/{thread['id']}/turns", json={"title": "第二步", "goal": "改一下首页"})
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text

    detail = (await client.get(f"{API}/threads/{thread['id']}")).json()
    assert [turn["title"] for turn in detail["turns"]] == ["第一步", "第二步"]
    assert detail["turn_count"] == 2
    # 轮次必须真的挂回会话：否则历史接口读不到，多轮就只是"名字上多轮"
    assert first.json()["thread_id"] == thread["id"]

    async with get_session_factory()() as session:
        rows = (
            await session.execute(select(Task).where(Task.thread_id == thread["id"]).order_by(Task.turn_index))
        ).scalars().all()
    assert [row.turn_index for row in rows] == [1, 2]


async def test_fork_records_its_source_and_refuses_cross_project(client):
    project_id = await _api_project(client)
    thread = (await client.post(f"{API}/projects/{project_id}/threads", json={"title": "主线"})).json()
    await client.post(f"{API}/threads/{thread['id']}/turns", json={"title": "铺垫", "goal": "先看看"})

    forked = await client.post(f"{API}/threads/{thread['id']}/fork", json={"title": "另一个方向"})
    assert forked.status_code == 201, forked.text
    assert forked.json()["forked_from"] == thread["id"]
    assert forked.json()["project_id"] == project_id

    other = await _api_project(client)
    crossed = await client.post(
        f"{API}/projects/{other}/threads", json={"title": "跨项目", "forked_from": thread["id"]}
    )
    # 跨项目分叉会把两条不同工作区的上下文拼在一起，错误会以"改错文件"的形式出现
    assert crossed.status_code == 409


async def test_archived_thread_refuses_new_turns(client):
    project_id = await _api_project(client)
    thread = (await client.post(f"{API}/projects/{project_id}/threads", json={})).json()

    assert (await client.delete(f"{API}/threads/{thread['id']}")).status_code == 204
    blocked = await client.post(f"{API}/threads/{thread['id']}/turns", json={"title": "还能聊吗"})

    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "thread_archived"
    # 归档不是删除：那条线还在，只是不再往下走
    assert (await client.get(f"{API}/threads/{thread['id']}")).json()["status"] == "archived"


async def test_missing_thread_is_404(client):
    assert (await client.get(f"{API}/threads/{uuid.uuid4()}")).status_code == 404


async def test_deleting_a_project_takes_its_threads_with_it(client):
    project_id = await _api_project(client)
    thread = (await client.post(f"{API}/projects/{project_id}/threads", json={})).json()
    await client.post(f"{API}/threads/{thread['id']}/turns", json={"title": "一轮", "goal": "做点事"})

    assert (await client.delete(f"{API}/projects/{project_id}")).status_code == 204

    # 留下孤儿会话行会让列表接口去取一个不存在的项目，而前端只能显示空白
    async with get_session_factory()() as session:
        assert (await session.execute(select(Thread).where(Thread.id == thread["id"]))).scalars().all() == []


async def test_turn_keeps_its_images_for_replay(client, workspace):
    """一轮贴的图要能回放出来。

    只存在前端内存里是不够的：刷新页面、隔天再打开，用户要能看到"我当时发的是哪张图"。
    所以 attachments 跟着 tasks 行走，会话详情里也带出来。
    """
    import base64

    created = await client.post("/api/v1/projects", json={"name": "带图会话", "description": ""})
    pid = created.json()["id"]
    await client.post(f"/api/v1/projects/{pid}/workspaces", json={"path": str(workspace)})
    thread = (await client.post(f"/api/v1/projects/{pid}/threads", json={"title": "看图改样式"})).json()

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    upload = (
        await client.post(
            f"/api/v1/projects/{pid}/uploads",
            json={"name": "改前.png", "mime": "image/png", "data": base64.b64encode(png).decode()},
        )
    ).json()

    turn = await client.post(
        f"/api/v1/threads/{thread['id']}/turns",
        json={"title": "照这张图改", "goal": "照这张图改", "attachments": [{"id": upload["id"], "name": "改前.png"}]},
    )
    assert turn.status_code == 201, turn.text
    assert [item["id"] for item in turn.json()["attachments"]] == [upload["id"]]

    detail = (await client.get(f"/api/v1/threads/{thread['id']}")).json()
    assert detail["turns"][0]["attachments"][0]["name"] == "改前.png"
    assert detail["turns"][0]["attachments"][0]["mime"] == "image/png"
