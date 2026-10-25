"""证据包按发起轮汇总：跨成员任务的一整轮花销要能一行读出来。

#47 把 `origin_run_id` 记进账本、#65 让群读模型按它合计、#66 把它写进事件；但证据包
（脱离数据库之后唯一还活着的那份交付证明）的 `usage` 只有 `turns`（按 run_id，每人一行）
与 `branches`（按线程）。同一个任务里如果有多条 run 属于同一轮，"这一轮花了多少"
又得由读者自己在 `turns` 上加。这一批补 `origins`，并给未挂发起轮的老账单独计数。
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.model_provider import ModelUsage
from blank_api.models.project import Project
from blank_api.models.task import Task
from blank_api.services.agent.evidence import build_evidence, origin_rollup

TASK_ID = "task-origin-evidence"


def _row(**overrides) -> SimpleNamespace:
    base = dict(
        prompt_tokens=0,
        completion_tokens=0,
        cached_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
        trace_id="",
        task_id="",
        run_id="",
        origin_run_id="",
        node="execute_action",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# 纯函数：分组的键只有一个真源
# --------------------------------------------------------------------------- #


def test_rollup_groups_by_origin_and_does_not_invent_a_zero_round():
    rows = [
        _row(origin_run_id="team-round-1", run_id="team-r1-a", task_id="t1", trace_id="th-a", prompt_tokens=100),
        _row(
            origin_run_id="team-round-1",
            run_id="team-r1-b",
            task_id="t1",
            trace_id="th-b",
            node="subagent",
            prompt_tokens=200,
            completion_tokens=20,
        ),
        _row(origin_run_id="team-round-2", run_id="team-r2-a", task_id="t2", trace_id="th-c", prompt_tokens=50),
        _row(origin_run_id="", run_id="", task_id="t1", trace_id="th-old", prompt_tokens=30),
    ]
    origins = origin_rollup(rows)
    assert [item["origin_run_id"] for item in origins] == ["team-round-1", "team-round-2"], (
        "空串不许并成第 0 轮，它根本不该出现在表里"
    )
    first, second = origins
    assert first["run_ids"] == ["team-r1-a", "team-r1-b"], "一轮里几条 run 要能对出来"
    assert first["tasks"] == 1 and second["tasks"] == 1
    assert first["subagent_calls"] == 1
    assert first["total_tokens"] == 320 and second["total_tokens"] == 50
    assert sum(item["total_tokens"] for item in origins) == 370, "老账那 30 不许被算进任何一轮"


# --------------------------------------------------------------------------- #
# 真链路：证据包把同一组行同时端成 turns 与 origins
# --------------------------------------------------------------------------- #


async def _seed() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="project-origin-evidence", name="发起轮证据"))
        session.add(
            Task(
                id=TASK_ID,
                project_id="project-origin-evidence",
                title="发起轮证据",
                goal="发起轮证据",
                max_steps=5,
            )
        )
        session.add_all(
            [
                ModelUsage(
                    id="u1",
                    provider_id="p-gone",
                    task_id=TASK_ID,
                    trace_id="th-a",
                    model="m",
                    prompt_tokens=100,
                    completion_tokens=10,
                    run_id="team-r1-a",
                    origin_run_id="team-round-1",
                ),
                ModelUsage(
                    id="u2",
                    provider_id="p-gone",
                    task_id=TASK_ID,
                    trace_id="th-b",
                    node="subagent",
                    model="m",
                    prompt_tokens=200,
                    completion_tokens=20,
                    run_id="team-r1-b",
                    origin_run_id="team-round-1",
                ),
                ModelUsage(
                    id="u3",
                    provider_id="p-gone",
                    task_id=TASK_ID,
                    trace_id="th-c",
                    model="m",
                    prompt_tokens=50,
                    completion_tokens=5,
                    run_id="team-r2-a",
                    origin_run_id="team-round-2",
                ),
                ModelUsage(
                    id="u4",
                    provider_id="p-gone",
                    task_id=TASK_ID,
                    trace_id="th-old",
                    model="m",
                    prompt_tokens=30,
                    completion_tokens=3,
                    run_id="",
                    origin_run_id="",
                ),
            ]
        )
        await session.commit()


async def test_evidence_exposes_origin_rollup_alongside_turns():
    await _seed()
    evidence = await build_evidence(get_session_factory(), TASK_ID)
    usage = evidence["usage"]

    origins = usage["origins"]
    assert [item["origin_run_id"] for item in origins] == ["team-round-1", "team-round-2"]
    assert origins[0]["run_ids"] == ["team-r1-a", "team-r1-b"]
    assert origins[0]["threads"] == 2 and origins[0]["subagent_calls"] == 1
    assert origins[0]["total_tokens"] == 330
    assert origins[1]["total_tokens"] == 55
    assert usage["origin_unattributed_calls"] == 1, "老账没有发起轮这件事要单独说出来"

    # 两张表各说各的：turns 按人/按 run 三行，origins 合成两行。
    # 共同点只有一个：认过的钱两边加起来必须相等——期望值按库里的行现算，不写死。
    async with get_session_factory()() as session:
        rows = (
            (await session.execute(select(ModelUsage).where(ModelUsage.task_id == TASK_ID)))
            .scalars()
            .all()
        )
    attributed = sum(
        row.prompt_tokens + row.completion_tokens for row in rows if str(row.run_id or "")
    )
    legacy = sum(
        row.prompt_tokens + row.completion_tokens for row in rows if not str(row.run_id or "")
    )
    assert len(usage["turns"]) == 3
    assert sum(item["total_tokens"] for item in usage["turns"]) == attributed
    assert sum(item["total_tokens"] for item in origins) == attributed
    assert usage["total_tokens"] == attributed + legacy
    assert sum(item["calls"] for item in origins) == usage["calls"] - 1


async def test_origin_rollup_sees_every_ledger_row_of_the_task():
    """装置自检：直接按库里的行重算，和走 build_evidence 的结果对上。"""
    await _seed()
    async with get_session_factory()() as session:
        rows = (
            (await session.execute(select(ModelUsage).where(ModelUsage.task_id == TASK_ID)))
            .scalars()
            .all()
        )
    evidence = await build_evidence(get_session_factory(), TASK_ID)
    assert origin_rollup(rows) == evidence["usage"]["origins"]
    assert len(rows) == 4
