"""每轮回退点：把对话截到某一轮，同时把代码放回那一轮结束时的样子。

四条边界，每条都写成一条能失败的判据：

1. **对话真的截断了**。回退之后，后面几轮不再进历史、不再出现在会话详情里，
   ``turn_count`` 也退回去——三处任何一处没退，用户下一轮就会看到一段
   "已经说不要了"的上下文。
2. **代码回到的是「第 M 轮结束时」，不是「第 M 轮开始时」**。第 M+1 轮的快照
   拍在它自己写入之前，所以那份快照正好是第 M 轮写完后的样子。选错会悄悄丢掉
   用户想保留的那一轮改动。
3. **没有可用快照时不许假装恢复**。对话照样截断，但 ``restored`` 必须为空、
   ``skipped_reason`` 必须说明为什么，并且 ``atomic`` 为 false——这是一次
   只完成了一半的回退，响应得自己说清楚。
4. **有一轮还在跑就拒绝**。一边截断对话、一边让正在写文件的轮次继续，等于
   制造两份互相矛盾的真相。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.task import AgentEvent, Task
from blank_api.services.sandbox import SnapshotStore
from blank_api.services.threads import history_for

API = "/api/v1"


async def _project(client, root) -> str:
    project = (await client.post(f"{API}/projects", json={"name": f"回退-{uuid.uuid4().hex[:6]}"})).json()
    bound = await client.post(f"{API}/projects/{project['id']}/workspaces", json={"path": str(root)})
    assert bound.status_code == 201, bound.text
    return project["id"]


async def _thread(client, project_id: str) -> dict:
    response = await client.post(f"{API}/projects/{project_id}/threads", json={"title": "回退测试"})
    assert response.status_code == 201, response.text
    return response.json()


async def _turn(client, thread_id: str, title: str) -> dict:
    response = await client.post(
        f"{API}/threads/{thread_id}/turns",
        json={"title": title, "goal": title},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _capture(workspace, task_id: str) -> dict:
    """模拟运行时：这一轮动手之前拍一份。"""
    return SnapshotStore().capture(str(workspace), reason="轮次开始前快照", task_id=task_id)


async def _events(task_id: str) -> list[AgentEvent]:
    async with get_session_factory()() as session:
        return list(
            (
                await session.execute(
                    select(AgentEvent).where(AgentEvent.task_id == task_id).order_by(AgentEvent.seq)
                )
            ).scalars()
        )


async def _set_status(task_id: str, status: str) -> None:
    async with get_session_factory()() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        task.status = status
        await session.commit()


async def _read(task_id: str) -> Task:
    async with get_session_factory()() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        return task


async def test_rewind_truncates_conversation_and_puts_code_back_to_that_turn(client, workspace):
    project_id = await _project(client, workspace)
    thread = await _thread(client, project_id)
    first = await _turn(client, thread["id"], "第一轮")
    _capture(workspace, first["id"])  # 第 1 轮动手前 = v0
    (workspace / "main.py").write_text("print('one')\n", encoding="utf-8")
    second = await _turn(client, thread["id"], "第二轮")
    _capture(workspace, second["id"])  # 第 2 轮动手前 = 第 1 轮结束后（v1）
    (workspace / "main.py").write_text("print('two')\n", encoding="utf-8")
    third = await _turn(client, thread["id"], "第三轮")
    (workspace / "main.py").write_text("print('three')\n", encoding="utf-8")

    response = await client.post(f"{API}/threads/{thread['id']}/rewind", json={"turn": 1})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["atomic"] is False
    assert sorted(item["turn"] for item in body["rewound"]) == [2, 3]
    assert {item["id"] for item in body["rewound"]} == {second["id"], third["id"]}
    assert "main.py" in body["restored"]
    assert body["skipped"] == []
    assert body["snapshot_id"], "代码恢复到第 1 轮结束时必须有据可查是哪份快照"
    # 第 2 轮快照拍在它写入之前 → 内容应是第 1 轮写下的 one
    assert (workspace / "main.py").read_text(encoding="utf-8") == "print('one')\n"

    detail = (await client.get(f"{API}/threads/{thread['id']}")).json()
    assert detail["turn_count"] == 1
    assert [turn["id"] for turn in detail["turns"]] == [first["id"]]

    history = await history_for(get_session_factory(), thread["id"])
    brought = " ".join(item["content"] for item in history)
    assert "第一轮" in brought
    assert "第二轮" not in brought and "第三轮" not in brought


async def test_rewind_without_a_snapshot_still_truncates_but_says_code_was_not_restored(client, workspace):
    project_id = await _project(client, workspace)
    thread = await _thread(client, project_id)
    first = await _turn(client, thread["id"], "第一轮")
    second = await _turn(client, thread["id"], "第二轮")
    (workspace / "main.py").write_text("print('later')\n", encoding="utf-8")

    response = await client.post(f"{API}/threads/{thread['id']}/rewind", json={"turn": 1})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["atomic"] is False
    assert body["restored"] == [], "没有快照就不能声称文件回来了"
    assert body["snapshot_id"] == ""
    assert body["skipped_reason"], "回退只完成一半时必须说明代码为什么没恢复"
    assert (workspace / "main.py").read_text(encoding="utf-8") == "print('later')\n"
    assert [item["id"] for item in body["rewound"]] == [second["id"]]

    detail = (await client.get(f"{API}/threads/{thread['id']}")).json()
    assert [turn["id"] for turn in detail["turns"]] == [first["id"]]


async def test_rewind_refuses_while_a_turn_is_still_running(client, workspace):
    project_id = await _project(client, workspace)
    thread = await _thread(client, project_id)
    await _turn(client, thread["id"], "第一轮")
    second = await _turn(client, thread["id"], "第二轮")
    await _set_status(second["id"], "running")

    response = await client.post(f"{API}/threads/{thread['id']}/rewind", json={"turn": 1})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "task_active"

    detail = (await client.get(f"{API}/threads/{thread['id']}")).json()
    assert detail["turn_count"] == 2, "被拒绝的回退不能留下半截状态"
    assert len(detail["turns"]) == 2
    assert await _read(second["id"]) is not None
    async with get_session_factory()() as session:
        rows = (
            await session.execute(select(Task).where(Task.thread_id == thread["id"]))
        ).scalars().all()
    assert all(row.rewound_at is None for row in rows)


async def test_rewind_to_a_missing_turn_is_404_not_a_silent_truncation(client, workspace):
    project_id = await _project(client, workspace)
    thread = await _thread(client, project_id)
    first = await _turn(client, thread["id"], "第一轮")

    response = await client.post(f"{API}/threads/{thread['id']}/rewind", json={"turn": 9})
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "turn_not_found"

    detail = (await client.get(f"{API}/threads/{thread['id']}")).json()
    assert detail["turn_count"] == 1
    assert [turn["id"] for turn in detail["turns"]] == [first["id"]]


async def test_rewind_points_say_which_turns_can_restore_code(client, workspace):
    project_id = await _project(client, workspace)
    thread = await _thread(client, project_id)
    first = await _turn(client, thread["id"], "第一轮")
    _capture(workspace, first["id"])
    second = await _turn(client, thread["id"], "第二轮")
    second_snapshot = _capture(workspace, second["id"])
    third = await _turn(client, thread["id"], "第三轮")

    response = await client.get(f"{API}/threads/{thread['id']}/rewind-points")
    assert response.status_code == 200, response.text
    points = {item["turn"]: item for item in response.json()}

    assert set(points) == {1, 2, 3}
    # 第 1 轮结束后的样子 = 第 2 轮动手前的快照
    assert points[1]["restorable"] is True
    assert points[1]["snapshot_id"] == second_snapshot["id"]
    # 第 2 轮之后没有任何快照，代码回不去
    assert points[2]["restorable"] is False
    assert points[2]["skipped_reason"]
    # 最后一轮没有"更晚的快照"可用
    assert points[3]["restorable"] is False
    assert points[3]["skipped_reason"]


async def test_rewind_writes_one_event_on_the_target_turn(client, workspace):
    project_id = await _project(client, workspace)
    thread = await _thread(client, project_id)
    first = await _turn(client, thread["id"], "第一轮")
    second = await _turn(client, thread["id"], "第二轮")

    response = await client.post(f"{API}/threads/{thread['id']}/rewind", json={"turn": 1})
    assert response.status_code == 200, response.text

    events = [item for item in await _events(first["id"]) if item.type == "thread.rewound"]
    assert len(events) == 1
    payload = events[0].payload
    assert payload["thread"] == thread["id"]
    assert payload["turn"] == 1
    assert payload["rewound"] == [second["id"]]


async def test_next_turn_after_rewind_reuses_the_number_but_not_the_old_row(client, workspace):
    project_id = await _project(client, workspace)
    thread = await _thread(client, project_id)
    first = await _turn(client, thread["id"], "第一轮")
    second = await _turn(client, thread["id"], "第二轮")
    await _turn(client, thread["id"], "第三轮")

    response = await client.post(f"{API}/threads/{thread['id']}/rewind", json={"turn": 1})
    assert response.status_code == 200, response.text

    fresh = await _turn(client, thread["id"], "重来一轮")
    assert fresh["id"] not in (second["id"],)
    row = await _read(fresh["id"])
    assert row.turn_index == 2, "被回退的旧第 2 轮不能再占住序号"

    detail = (await client.get(f"{API}/threads/{thread['id']}")).json()
    assert [turn["id"] for turn in detail["turns"]] == [first["id"], fresh["id"]]
    assert detail["turn_count"] == 2


async def test_rewind_on_an_archived_thread_is_rejected(client, workspace):
    project_id = await _project(client, workspace)
    thread = await _thread(client, project_id)
    await _turn(client, thread["id"], "第一轮")
    archived = await client.delete(f"{API}/threads/{thread['id']}")
    assert archived.status_code == 204

    response = await client.post(f"{API}/threads/{thread['id']}/rewind", json={"turn": 1})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "thread_archived"
