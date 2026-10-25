"""发起轮进事件时间线：每个事件自己说得清「我是哪一轮引起的」。

#47 把发起轮写进了账本（`model_usage.origin_run_id`），事件时间线只留了 `run_id`——
那是"每人一个"的键：团队一轮里每个发言成员各有自己的 Task 和自己的 run。
于是拿事件回答"这条事件属于哪一轮群聊"必须再去 join 任务或账本；而证据包端给用户的
就是事件本身，时间线一旦离开数据库就再也说不清归属。

这批判定的是四件事，每条都能失败：

① 记录器每次 `add` 都把发起轮落进那一行，和账本是同一个值；
② 顶层运行的发起轮就是它自己；两个都没给时留空串（"当时没记"），不许拿 task_id 顶上；
③ 子代理共用父记录器，事件里的发起轮跟着父辈走；
④ 迁移 0024 真能跑：老库加上列与索引、老行保持空串、降级来回不丢行。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.test_agent_v04 import API, env  # noqa: F401

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.evidence import build_evidence
from blank_api.services.models.base import (
    AdapterConfig,
    ModelAdapter,
    ModelResponse,
    Usage,
)
from blank_api.services.subagent import SubAgentRequest, run_subagent
from blank_api.services.team import round_origin

TASK_ID = "task-event-origin"


class _OneShot(ModelAdapter):
    def __init__(self, name: str = "kid") -> None:
        super().__init__(
            AdapterConfig(name=name, kind="openai_compatible", base_url="", api_key="", model=name)
        )

    async def invoke(self, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def stream(self, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def bind_tools(self, tools, messages, **kwargs):
        return ModelResponse(
            text="已完成", usage=Usage(prompt_tokens=10, completion_tokens=1), model=self.config.name
        )


@pytest.fixture
async def seeded():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="project-event-origin", name="事件归属"))
        session.add(
            Task(id=TASK_ID, project_id="project-event-origin", title="事件归属", goal="事件归属", max_steps=5)
        )
        await session.commit()


async def _events() -> list[AgentEvent]:
    async with get_session_factory()() as session:
        return list(
            (await session.execute(select(AgentEvent).order_by(AgentEvent.seq))).scalars().all()
        )


# --------------------------------------------------------------------------- #
# 写入侧：发起轮和 run 各管一级
# --------------------------------------------------------------------------- #


async def test_every_event_carries_the_origin_from_the_recorder(seeded):
    """记录器上只有一个真源：改了它，之后每条事件都跟着变。"""
    recorder = EventRecorder(
        get_session_factory(), TASK_ID, run_id="team-round-9-2", origin_run_id=round_origin("round-9")
    )
    await recorder.add("tool.completed", "execute_action", {"name": "read_file"})
    await recorder.add("model.usage", "execute_action", {"prompt_tokens": 1})
    rows = await _events()
    assert len(rows) == 2
    assert {row.origin_run_id for row in rows} == {round_origin("round-9")}
    assert {row.run_id for row in rows} == {"team-round-9-2"}, "run 仍是派它那次发言的"

    recorder.origin_run_id = round_origin("round-10")
    await recorder.add("task.failed", "", {"reason": "换轮"})
    rows = await _events()
    assert rows[-1].origin_run_id == round_origin("round-10"), "事件跟着记录器走，不在会话里另存一份"
    assert rows[-1].run_id == "team-round-9-2"


async def test_a_top_level_run_is_its_own_origin_and_nothing_stays_unrecorded(seeded):
    """顶层运行的发起轮就是它自己；两个都没给时留空，不许拿 task_id 冒充。"""
    await EventRecorder(get_session_factory(), TASK_ID, run_id="run-a").add("task.started", "", {})
    await EventRecorder(get_session_factory(), TASK_ID).add("task.started", "", {})
    rows = {row.seq: row for row in await _events()}
    assert rows[1].origin_run_id == "run-a" == rows[1].run_id
    assert rows[2].origin_run_id == "" and rows[2].run_id == "", "没记过就是没记过"
    assert TASK_ID not in (rows[2].origin_run_id, rows[2].run_id)


async def test_a_subagent_event_inherits_the_origin_of_its_tree(seeded, tmp_path):
    """子代理不认识团队：父辈的发起轮从记录器一路传下去，它的每条事件照抄。"""
    origin = round_origin("round-sub")
    result = await run_subagent(
        adapter=_OneShot(),
        tools=[],
        root=tmp_path,
        task_id=TASK_ID,
        session_factory=get_session_factory(),
        recorder=EventRecorder(
            get_session_factory(), TASK_ID, run_id="team-round-sub-1", origin_run_id=origin
        ),
        request=SubAgentRequest(goal="读 README 并复述", max_steps=2),
        agent_path="root/sub-1",
        depth=1,
        parent_thread_id="thread-parent",
        root_thread_id="thread-parent",
    )
    assert result.ok, result.error
    rows = await _events()
    assert rows, "子代理一条事件都没写，下面的断言会变成空对空"
    assert {row.origin_run_id for row in rows} == {origin}
    assert {row.run_id for row in rows} == {"team-round-sub-1"}


# --------------------------------------------------------------------------- #
# 读侧：接口与证据包带同一列
# --------------------------------------------------------------------------- #


async def test_task_events_api_exposes_the_origin(env):  # noqa: F811
    """/tasks/{id}/events 是前端时间线读的那条路；字段不出去，库里记了也白记。"""
    async with get_session_factory()() as session:
        session.add(Project(id="project-api-origin", name="接口归属"))
        session.add(
            Task(id=TASK_ID, project_id="project-api-origin", title="接口归属", goal="接口归属", max_steps=5)
        )
        await session.commit()
    await EventRecorder(
        get_session_factory(), TASK_ID, run_id="team-round-api-1", origin_run_id=round_origin("api")
    ).add("tool.completed", "", {"name": "read_file"})
    await EventRecorder(get_session_factory(), TASK_ID).add("task.failed", "", {"reason": "老行"})

    resp = await env.client.get(f"{API}/tasks/{TASK_ID}/events")
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert len(items) == 2
    assert items[0]["origin_run_id"] == round_origin("api")
    assert items[0]["run_id"] == "team-round-api-1"
    assert items[1]["origin_run_id"] == "", "没记过的事件在接口上必须是空串，不是借来的值"


async def test_evidence_timeline_carries_origin_next_to_run_id(seeded):
    """证据包离开数据库后只剩这份 timeline：两个键都要在，才回答得了归属。"""
    recorder = EventRecorder(
        get_session_factory(), TASK_ID, run_id="team-round-ev-1", origin_run_id=round_origin("ev")
    )
    await recorder.add("tool.completed", "execute_action", {"name": "read_file"})
    await recorder.add("subagent.finished", "subagent", {"ok": True})

    evidence = await build_evidence(get_session_factory(), TASK_ID)
    timeline = evidence["timeline"]
    assert len(timeline) == 2
    assert {item["origin_run_id"] for item in timeline} == {round_origin("ev")}
    assert {item["run_id"] for item in timeline} == {"team-round-ev-1"}


# --------------------------------------------------------------------------- #
# 迁移：老库加上列，不替老行编归属
# --------------------------------------------------------------------------- #


def _alembic(args: list[str], db_path: Path, cwd: Path) -> None:
    env_vars = dict(os.environ)
    env_vars["BLANK_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    env_vars["PYTHONIOENCODING"] = "utf-8"
    done = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "alembic", *args],
        cwd=str(cwd),
        env=env_vars,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=240,
    )
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]


def _column_ddl(db: sqlite3.Connection, table: str, column: str) -> str:
    sql = db.execute("select sql from sqlite_master where type='table' and name=?", (table,)).fetchone()[0]
    for part in sql.replace("\n", " ").split(","):
        if part.strip().startswith(f"{column} "):
            return part.strip()
    return ""


def test_migration_0024_adds_the_column_without_backfilling_rows(tmp_path):
    """这是唯一能失败在「改了模型忘了写迁移」上的判定：DDL 没跟上，真实库上就是 no such column。"""
    cwd = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "mig-event.db"
    _alembic(["upgrade", "0023_timestamps_not_null"], db_path, cwd)
    db = sqlite3.connect(db_path)
    assert _column_ddl(db, "agent_events", "origin_run_id") == "", "0023 上不该有这一列"
    db.execute(
        "insert into agent_events (id, task_id, run_id, step_id, parent_event_id, seq, node, type, payload)"
        " values ('e1','t-old','run-old','e1','',1,'execute_action','tool.completed','{}')"
    )
    db.commit()
    db.close()

    _alembic(["upgrade", "head"], db_path, cwd)
    db = sqlite3.connect(db_path)
    ddl = _column_ddl(db, "agent_events", "origin_run_id").upper()
    assert "VARCHAR(120)" in ddl and "NOT NULL" in ddl, ddl
    assert (
        db.execute(
            "select name from sqlite_master where type='index' and name='ix_agent_events_origin_run_id'"
        ).fetchone()
        is not None
    ), "加了列不建索引，按轮查事件就是全表扫"
    run_id, origin = db.execute(
        "select run_id, origin_run_id from agent_events where id='e1'"
    ).fetchone()
    assert run_id == "run-old", "加列不许动老行"
    assert origin == "", "老行当时没记，不许拿 run_id 回填成归属"
    assert db.execute("select count(*) from agent_events").fetchone()[0] == 1
    db.close()

    _alembic(["downgrade", "0023_timestamps_not_null"], db_path, cwd)
    db = sqlite3.connect(db_path)
    assert _column_ddl(db, "agent_events", "origin_run_id") == ""
    assert db.execute("select count(*) from agent_events").fetchone()[0] == 1, "往返不许丢行"
    db.close()
