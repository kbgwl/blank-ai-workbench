"""侧栏状态点的数据源：会话列表要能直接说出"这条最后跑成什么样"。

四个要点：

1. 最后一轮按 ``turn_index`` 判，不按创建时间——同一秒里连开两轮时时间戳分不出先后。
2. 被回退掉的轮不算。拿它当"最后状态"，一条明明跑完的会话会永远显示失败。
3. 空会话给空串，不能给 ``draft``：前者是"还没有任何一轮"，后者是"有一轮在草稿"，
   界面上一个是没点、一个是有个灰点。
4. 详情接口与列表接口同一口径。两处算法一旦分叉，用户点进去看到的点和列表上的点不一样。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from blank_api.db import get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import Task
from blank_api.models.thread import Thread


def _now() -> datetime:
    """库里存的是 naive UTC，测试也照这个口径给，才能和真实数据比先后。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _project() -> Project:
    async with get_session_factory()() as session:
        project = Project(id=str(uuid.uuid4()), name="状态点测试")
        session.add(project)
        await session.commit()
        return project


async def _thread(project_id: str, title: str = "会话") -> Thread:
    async with get_session_factory()() as session:
        thread = Thread(id=str(uuid.uuid4()), project_id=project_id, title=title, turn_count=0)
        session.add(thread)
        await session.commit()
        return thread


async def _turn(
    project_id: str,
    thread_id: str | None,
    *,
    index: int,
    status: str,
    rewound: bool = False,
    created_at: datetime | None = None,
) -> Task:
    async with get_session_factory()() as session:
        task = Task(
            id=str(uuid.uuid4()),
            project_id=project_id,
            thread_id=thread_id,
            turn_index=index,
            title=f"第 {index} 轮",
            goal=f"第 {index} 轮",
            status=status,
            created_at=created_at or _now(),
            rewound_at=_now() if rewound else None,
        )
        session.add(task)
        await session.commit()
        return task


async def test_last_turn_status_follows_turn_index(client):
    project = await _project()
    thread = await _thread(project.id)
    await _turn(project.id, thread.id, index=1, status="succeeded")
    await _turn(project.id, thread.id, index=2, status="succeeded")
    # 第三轮的创建时间故意让得更早：按时间挑就会挑错，按 turn_index 才对。
    await _turn(project.id, thread.id, index=3, status="failed", created_at=_now() - timedelta(days=1))

    response = await client.get(f"/api/v1/projects/{project.id}/threads")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["last_turn_status"] == "failed"


async def test_rewound_turn_does_not_count(client):
    project = await _project()
    thread = await _thread(project.id)
    await _turn(project.id, thread.id, index=1, status="succeeded")
    await _turn(project.id, thread.id, index=2, status="succeeded")
    # 第 3 轮被回退：它已经不属于这条对话，状态点该退回第 2 轮。
    await _turn(project.id, thread.id, index=3, status="failed", rewound=True)

    response = await client.get(f"/api/v1/projects/{project.id}/threads")
    assert response.status_code == 200
    assert response.json()[0]["last_turn_status"] == "succeeded"


async def test_empty_thread_reports_empty_string(client):
    project = await _project()
    await _thread(project.id, title="还没说过话")

    response = await client.get(f"/api/v1/projects/{project.id}/threads")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["last_turn_status"] == ""


async def test_independent_task_does_not_leak_into_threads(client):
    project = await _project()
    thread = await _thread(project.id)
    await _turn(project.id, thread.id, index=1, status="draft")
    await _turn(project.id, None, index=0, status="failed")

    response = await client.get(f"/api/v1/projects/{project.id}/threads")
    assert response.status_code == 200
    assert response.json()[0]["last_turn_status"] == "draft"


async def test_detail_uses_same_status(client):
    project = await _project()
    thread = await _thread(project.id)
    await _turn(project.id, thread.id, index=1, status="running")

    response = await client.get(f"/api/v1/threads/{thread.id}")
    assert response.status_code == 200
    assert response.json()["last_turn_status"] == "running"


async def test_threads_are_queried_once_for_status(client):
    """列表不能逐条查状态。

    首屏就是这条列表，N+1 在这里等于"打开首页慢一秒"。这里直接数 SQL：
    会话数与查询数无关，多一条会话不该多一条查询。
    """
    from sqlalchemy import event

    from blank_api.db import get_engine

    project = await _project()
    ids = []
    for index in range(4):
        thread = await _thread(project.id, title=f"会话 {index}")
        ids.append(thread.id)
        await _turn(project.id, thread.id, index=1, status="succeeded")

    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    engine = get_engine()
    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        response = await client.get(f"/api/v1/projects/{project.id}/threads")
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)

    assert response.status_code == 200
    assert len(response.json()) == 4
    assert len([row for row in statements if "FROM tasks" in row]) == 1
