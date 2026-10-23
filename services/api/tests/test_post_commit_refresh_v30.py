"""POST /skills/{id}/disable 在真 HTTP 层上是 500：提交后没把行读回来就出参。

`Skill.updated_at` 声明的是 `onupdate=func.now()`（models/skill.py:33-35）。SQLAlchemy 在
UPDATE 之后不知道数据库算出来的那个值，于是把它标成过期；`expire_on_commit=False` 挡不住这一条
（它管的是"提交后不回收别的属性"，onupdate 那一列是 flush 时才失效的）。所以
`await db.commit()` 之后直接 `SkillOut.model_validate(skill)` 会去触发一次惰性加载，
而这次加载发生在同步上下文里 → `MissingGreenlet` → 500。

这个缺陷在同一条链上的四个口都会出现，而且离线用例一个都抓不到——因为 `test_models_v03.py`
那一类只走 service 层，从没按 HTTP 把这几个路由穿过。实测的红来自两处：

    #50 的 SQLite 驱动（E:/tmp/m50/drive-sqlite.log）：[FAIL] 37 技能停用 AssertionError: HTTP 500
    服务日志 api.log：pydantic_core…ValidationError: 1 validation error for SkillOut /
                      updated_at / Error extracting attribute: MissingGreenlet…  （skills.py:115）

下面每条断言都能失败：状态码、返回体里的字段值、以及"返回值和库里那一行是同一个值"。
只断言 200 是不够的——把 refresh 换成"手工塞一个假时间戳"照样 200。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from blank_api.db import get_engine, get_session_factory
from blank_api.models.skill import Skill
from blank_api.models.task import Task

API = "/api/v1"


async def _project(client) -> str:
    resp = await client.post(f"{API}/projects", json={"name": "提交后读回", "description": "d"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _skill(client) -> str:
    """走真实的产品入口造一个技能：候选 → 接受。和界面上一条路。"""
    resp = await client.post(f"{API}/skills/memory-candidates", json={"text": "先看目录再动手", "source": "e2e"})
    assert resp.status_code == 201, resp.text
    candidate_id = resp.json()["id"]
    resp = await client.post(f"{API}/skills/memory-candidates/{candidate_id}/accept", json={})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _db_updated_at(skill_id: str) -> datetime:
    async with get_session_factory()() as session:
        return (
            await session.execute(select(Skill.updated_at).where(Skill.id == skill_id))
        ).scalar_one()


async def test_disable_skill_returns_the_row_that_the_database_has(client) -> None:
    skill_id = await _skill(client)
    resp = await client.post(f"{API}/skills/{skill_id}/disable", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "disabled"
    # 出参里那一列必须等于库里的值：这一条同时挡住 500 和"拿旧值冒充新值"。
    assert datetime.fromisoformat(body["updated_at"]) == await _db_updated_at(skill_id)


async def test_enable_skill_returns_200(client) -> None:
    skill_id = await _skill(client)
    assert (await client.post(f"{API}/skills/{skill_id}/disable", json={})).status_code == 200
    resp = await client.post(f"{API}/skills/{skill_id}/enable", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"
    assert datetime.fromisoformat(resp.json()["updated_at"]) == await _db_updated_at(skill_id)


async def test_rollback_skill_returns_200_and_the_previous_text(client) -> None:
    skill_id = await _skill(client)
    original = (await client.get(f"{API}/skills")).json()
    before = next(row for row in original if row["id"] == skill_id)
    resp = await client.patch(f"{API}/skills/{skill_id}", json={"instructions": "改成别的做法"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == before["version"] + 1
    resp = await client.post(f"{API}/skills/{skill_id}/rollback", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["instructions"] == before["instructions"]
    assert body["version"] == before["version"] + 2


async def test_cancel_a_task_that_never_ran(client) -> None:
    """取消一条 draft 任务走的是 UPDATE + TaskOut，和 disable 同一个形状。

    界面只在"跑着的任务"上点取消，而跑着的任务会先被运行时置成终态，
    于是这条路径从没被真请求穿过。
    """
    project_id = await _project(client)
    resp = await client.post(f"{API}/projects/{project_id}/tasks", json={"title": "取消我", "goal": "g"})
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["id"]
    assert resp.json()["status"] == "draft"
    resp = await client.post(f"{API}/tasks/{task_id}/cancel", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    async with get_session_factory()() as session:
        row = (await session.execute(select(Task.status).where(Task.id == task_id))).scalar_one()
    assert row == "cancelled"


async def test_cancel_a_finished_task_is_still_409(client) -> None:
    """修复不许顺手改掉取消语义：终态任务仍然拒。"""
    project_id = await _project(client)
    resp = await client.post(f"{API}/projects/{project_id}/tasks", json={"title": "已完成的", "goal": "g"})
    task_id = resp.json()["id"]
    async with get_session_factory()() as session:
        task = await session.get(Task, task_id)
        task.status = "succeeded"
        await session.commit()
    resp = await client.post(f"{API}/tasks/{task_id}/cancel", json={})
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "task_not_cancellable"


# --------------------------------------------------------------------------- #
# 没有 RETURNING 的方言：MySQL 8.4 的 INSERT 带不回服务端生成的那一列
# --------------------------------------------------------------------------- #
# SQLite 靠 RETURNING 在插入语句里就把 created_at/updated_at 取了，所以这类缺陷在
# 我们平时跑的库上根本不出现；#50 的 MySQL 端到端驱动里，它是这样红的：
#
#     [FAIL] 16 任务创建  AssertionError: HTTP 500
#     api.log: MissingGreenlet … [SQL: SELECT tasks.created_at, tasks.updated_at FROM tasks WHERE tasks.id = %s]
#     [FAIL] 29 会话创建 / [FAIL] 35 Bot 创建  同一形状
#
# 把当前方言的 insert_returning 关掉，就能在 SQLite 上复现出 MySQL 的那一侧形状——
# 这样这条闸门可以进 CI，而不必每次真连一个 MySQL。


async def test_mutating_routes_survive_a_dialect_without_insert_returning(client) -> None:
    project_id = await _project(client)
    engine = get_engine()
    dialect = engine.sync_engine.dialect
    if not hasattr(dialect, "insert_returning"):  # pragma: no cover - 只在非 SQLite 夹具下发生
        pytest.skip(f"{type(dialect).__name__} 没有 insert_returning 这个开关")
    saved = dialect.insert_returning
    dialect.insert_returning = False
    try:
        resp = await client.post(f"{API}/projects/{project_id}/tasks", json={"title": "无 RETURNING 的任务", "goal": "g"})
        assert resp.status_code == 201, resp.text
        task = resp.json()
        assert task["created_at"] and task["updated_at"]
        assert _naive(task["updated_at"]) == await _db_task_updated_at(task["id"])

        resp = await client.post(f"{API}/projects/{project_id}/threads", json={"title": "无 RETURNING 的会话"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["created_at"]

        resp = await client.post(f"{API}/team/bots", json={
            "name": "无RETURNING探针Bot", "role": "检索", "instructions": "只读并汇报",
        })
        assert resp.status_code == 201, resp.text
        assert resp.json()["created_at"], "bot_payload 读的是服务端生成的那一列"
    finally:
        dialect.insert_returning = saved


def _naive(value: str) -> datetime:
    """TaskOut 的序列化器给的是带 UTC 偏移的串，列里存的是无时区那一半。"""
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed


async def _db_task_updated_at(task_id: str) -> datetime:
    async with get_session_factory()() as session:
        return (
            await session.execute(select(Task.updated_at).where(Task.id == task_id))
        ).scalar_one()
