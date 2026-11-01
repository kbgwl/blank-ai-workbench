# -*- coding: utf-8 -*-
"""证据包的时间线分页与"只要摘要"读法。

判据分两层：

1. **切得对**：长任务一次只给最近 N 条，游标能往前翻，翻到头时 `next_before_seq`
   必须为空（满页不等于还有下一页）。
2. **切了也不丢字段**：时间线是最容易被看见的那部分，而验收结论、审批状态、上下文
   水位、边界归属、用量明细全部来自事件。分页只许影响"显示多少条"，不许让任何一块
   汇总字段变空——所以这里既钉每条字段，也钉一份"证据包读了哪些事件类型"的清单。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, FileChange, Task, ToolCall
from blank_api.services.agent.evidence import (
    DEFAULT_CHANGES_LIMIT,
    DEFAULT_TIMELINE_LIMIT,
    DEFAULT_TOOLS_LIMIT,
    DIFF_CHAR_BUDGET,
    MAX_TIMELINE_LIMIT,
    SUMMARY_EVENT_TYPES,
    build_evidence,
)

EVIDENCE_SOURCE = Path(__file__).resolve().parents[1] / "src" / "blank_api" / "services" / "agent" / "evidence.py"


@pytest.fixture(autouse=True)
async def schema() -> AsyncIterator[None]:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


async def _task(events: list[tuple[str, dict]], *, markers: list[tuple[int, str, dict]] | None = None) -> str:
    """建一条任务并铺事件。``markers`` 用来在中间插入特定编号的事件（可重复 seq 之外的空洞）。"""
    task_id = f"t-{uuid.uuid4().hex[:8]}"
    async with get_session_factory()() as session:
        session.add(Project(id=f"p-{task_id}", name="p"))
        session.add(Task(id=task_id, project_id=f"p-{task_id}", title="t", goal="g"))
        await session.flush()
        seq = 0
        pinned = dict(markers or [])
        for index, (event_type, payload) in enumerate(events, start=1):
            seq = pinned.get(index, index)
            session.add(
                AgentEvent(
                    id=str(uuid.uuid4()),
                    task_id=task_id,
                    seq=seq,
                    type=event_type,
                    payload=payload,
                    node="execute_action",
                )
            )
        await session.commit()
    return task_id


async def _long_task(total: int = 300) -> str:
    """一条"跑了很久"的任务：前两条是汇总字段的来源，后面全是节点流水。"""
    events: list[tuple[str, dict]] = [
        ("validation.completed", {"status": "ok", "command": "pytest -q", "passed": True}),
        ("task.started", {"model": "demo"}),
    ]
    events += [("node.started", {"step": index}) for index in range(total - 2)]
    return await _task(events)


# --------------------------------------------------------------------------- #
# 1. 摘要类型清单必须覆盖 evidence.py 真正读的每一种事件
# --------------------------------------------------------------------------- #


def test_summary_types_cover_every_type_read_by_evidence():
    """新增一种 `event.type == "..."` 却忘了进清单，会让对应汇总字段静默变空。

    这条判据直接扫源码：比枚举"我记得有哪几种"可靠——人会忘，源码不会。
    """
    source = EVIDENCE_SOURCE.read_text(encoding="utf-8")
    used: set[str] = set()
    used |= set(re.findall(r'event\.type\s*==\s*"([a-z][a-z0-9_.]*)"', source))
    for group in re.findall(r'event\.type\s+(?:not\s+)?in\s*\(([^)]*)\)', source):
        used |= set(re.findall(r'"([a-z][a-z0-9_.]*)"', group))
    # 上面两处正则之外还可能写成 `if event.type not in (...)`，一并覆盖。
    for group in re.findall(r'event\.type\s+not\s+in\s*\(([^)]*)\)', source):
        used |= set(re.findall(r'"([a-z][a-z0-9_.]*)"', group))

    assert used, "没从源码里扫到任何事件类型——这条判据本身失效了"
    missing = sorted(used - SUMMARY_EVENT_TYPES)
    assert not missing, f"这些事件类型被证据包读取，却没进 SUMMARY_EVENT_TYPES：{missing}"


# --------------------------------------------------------------------------- #
# 2. 分页
# --------------------------------------------------------------------------- #


async def test_long_task_timeline_is_windowed_by_default():
    task_id = await _long_task(300)

    evidence = await build_evidence(get_session_factory(), task_id)

    meta = evidence["timeline_meta"]
    assert len(evidence["timeline"]) == DEFAULT_TIMELINE_LIMIT
    assert meta["total_events"] == 300
    assert meta["loaded"] == DEFAULT_TIMELINE_LIMIT
    assert meta["truncated"] is True
    assert meta["earlier"] == 300 - DEFAULT_TIMELINE_LIMIT
    # 给的是"最近那一段"：末条必须是最后一条。
    assert evidence["timeline"][-1]["seq"] == 300
    assert meta["next_before_seq"] == 300 - DEFAULT_TIMELINE_LIMIT + 1


async def test_timeline_pages_backwards_without_gaps_or_repeats():
    task_id = await _long_task(120)

    first = await build_evidence(get_session_factory(), task_id, timeline_limit=50)
    assert [row["seq"] for row in first["timeline"]] == list(range(71, 121))

    second = await build_evidence(
        get_session_factory(),
        task_id,
        timeline_limit=50,
        timeline_before_seq=first["timeline_meta"]["next_before_seq"],
    )
    assert [row["seq"] for row in second["timeline"]] == list(range(21, 71))

    third = await build_evidence(
        get_session_factory(),
        task_id,
        timeline_limit=50,
        timeline_before_seq=second["timeline_meta"]["next_before_seq"],
    )
    assert [row["seq"] for row in third["timeline"]] == list(range(1, 21))
    # 到头了：满页不等于还有下一页，否则界面会一直显示一个点了没反应的"更早"。
    assert third["timeline_meta"]["truncated"] is False
    assert third["timeline_meta"]["next_before_seq"] is None
    assert third["timeline_meta"]["earlier"] == 0


async def test_exact_boundary_page_does_not_promise_more():
    """总数正好等于一页时，不许给出"还有更早的"游标。"""
    task_id = await _long_task(40)

    evidence = await build_evidence(get_session_factory(), task_id, timeline_limit=40)

    assert len(evidence["timeline"]) == 40
    assert evidence["timeline_meta"]["truncated"] is False
    assert evidence["timeline_meta"]["next_before_seq"] is None


async def test_timeline_limit_is_clamped():
    task_id = await _long_task(10)

    evidence = await build_evidence(get_session_factory(), task_id, timeline_limit=999_999)
    assert evidence["timeline_meta"]["limit"] == MAX_TIMELINE_LIMIT


# --------------------------------------------------------------------------- #
# 3. 只要摘要
# --------------------------------------------------------------------------- #


async def test_summary_only_skips_timeline_but_keeps_every_derived_field():
    """时间线可以不要，验收结论不能丢——它就是靠一条 seq=1 的旧事件算出来的。"""
    task_id = await _long_task(300)

    evidence = await build_evidence(get_session_factory(), task_id, include_timeline=False)

    assert evidence["timeline"] == []
    assert evidence["timeline_meta"]["loaded"] == 0
    # 总数是聚合数出来的，与"这一页装了几条"无关。
    assert evidence["timeline_meta"]["total_events"] == 300
    assert evidence["validation"]["status"] == "ok"
    assert evidence["validation"]["command"] == "pytest -q"


async def test_derived_fields_survive_windowing():
    """窗口只覆盖尾部时，头部的审批/上下文事件仍要算进汇总。"""
    events: list[tuple[str, dict]] = [
        ("context.measured", {"active_context_tokens_after": 1234}),
        ("approval.required", {"tool": "run_shell", "expires_at": None}),
        ("validation.completed", {"status": "skipped", "skipped_reason": "沙箱不可用"}),
    ]
    events += [("node.started", {"step": index}) for index in range(50)]
    task_id = await _task(events)

    evidence = await build_evidence(get_session_factory(), task_id, timeline_limit=10)

    assert evidence["context"]["active_tokens"] == 1234
    assert evidence["validation"]["status"] == "skipped"
    assert any("沙箱不可用" in item for item in evidence["unverified_risks"])


# --------------------------------------------------------------------------- #
# 4. HTTP 层：默认值与参数都真的生效
# --------------------------------------------------------------------------- #


async def test_http_evidence_defaults_to_a_page_and_accepts_a_cursor(client):
    task_id = await _long_task(250)

    page = (await client.get(f"/api/v1/tasks/{task_id}/evidence")).json()
    assert len(page["timeline"]) == DEFAULT_TIMELINE_LIMIT
    assert page["timeline_meta"]["total_events"] == 250

    small = (
        await client.get(
            f"/api/v1/tasks/{task_id}/evidence",
            params={"timeline_limit": 5, "timeline_before_seq": 101},
        )
    ).json()
    assert [row["seq"] for row in small["timeline"]] == [96, 97, 98, 99, 100]

    summary = (await client.get(f"/api/v1/tasks/{task_id}/evidence/summary")).json()
    assert summary["timeline"] == []
    assert summary["validation"]["status"] == "ok"
    assert summary["task"]["id"] == task_id


async def test_http_rejects_absurd_limits(client):
    task_id = await _long_task(3)
    too_big = await client.get(
        f"/api/v1/tasks/{task_id}/evidence", params={"timeline_limit": 999_999}
    )
    assert too_big.status_code == 422


# --------------------------------------------------------------------------- #
# 5. 工具调用 / 文件变更 分页 + diff 预算
# --------------------------------------------------------------------------- #


async def _task_with_rows(tool_count: int, change_count: int, *, diff_chars: int = 0) -> str:
    """建一条任务并铺 ToolCall / FileChange 行，用来验证这两项也走分页与 diff 预算。"""
    task_id = f"t-{uuid.uuid4().hex[:8]}"
    async with get_session_factory()() as session:
        session.add(Project(id=f"p-{task_id}", name="p"))
        session.add(Task(id=task_id, project_id=f"p-{task_id}", title="t", goal="g"))
        await session.flush()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index in range(tool_count):
            session.add(
                ToolCall(
                    id=str(uuid.uuid4()),
                    task_id=task_id,
                    trace_id=f"tr-{index}",
                    node="execute_action",
                    name="run_shell",
                    arguments={"command": f"echo {index}"},
                    status="ok",
                    risk_level="low",
                    duration_ms=1,
                    result_summary="ok",
                    artifact_path="",
                    created_at=base + timedelta(seconds=index),
                )
            )
        for index in range(change_count):
            session.add(
                FileChange(
                    id=str(uuid.uuid4()),
                    task_id=task_id,
                    path=f"src/file_{index}.py",
                    change_type="modified",
                    diff=("x" * diff_chars) if diff_chars else "",
                    backup_path="",
                    created_at=base + timedelta(seconds=index),
                )
            )
        await session.commit()
    return task_id


async def test_tools_paginated_by_default_with_meta():
    task_id = await _task_with_rows(600, 0)

    evidence = await build_evidence(get_session_factory(), task_id)

    assert evidence["tools_meta"] == {"total": 600, "returned": DEFAULT_TOOLS_LIMIT, "has_more": True}
    assert len(evidence["tools"]) == DEFAULT_TOOLS_LIMIT


async def test_tools_offset_pages_and_unbounded_request():
    task_id = await _task_with_rows(600, 0)

    full = await build_evidence(get_session_factory(), task_id, tools_limit=2000)
    assert full["tools_meta"] == {"total": 600, "returned": 600, "has_more": False}
    assert len(full["tools"]) == 600

    second = await build_evidence(
        get_session_factory(), task_id, tools_limit=50, tools_offset=500
    )
    assert second["tools_meta"]["returned"] == 50
    assert second["tools_meta"]["has_more"] is True
    # offset 真的跳过前 500 条：第一条的 trace_id 应是 tr-500。
    assert second["tools"][0]["trace_id"] == "tr-500"


async def test_changes_paginated_and_diff_bounded():
    """10 个文件、每个 30000 字符 diff：拼接后 300k 超过 200k 预算，必须截断并如实报数。"""
    task_id = await _task_with_rows(0, 10, diff_chars=30_000)

    evidence = await build_evidence(get_session_factory(), task_id)

    assert evidence["changes_meta"]["total"] == 10
    assert evidence["changes_meta"]["diff_truncated"] > 0
    assert len(evidence["changes"]["diff"]) <= DIFF_CHAR_BUDGET


async def test_http_evidence_defaults_cap_tools_and_changes(client):
    task_id = await _task_with_rows(600, 600)

    page = (await client.get(f"/api/v1/tasks/{task_id}/evidence")).json()

    assert page["tools_meta"]["total"] == 600
    assert page["tools_meta"]["returned"] == DEFAULT_TOOLS_LIMIT
    assert page["changes_meta"]["total"] == 600
    assert page["changes_meta"]["returned"] == DEFAULT_CHANGES_LIMIT

async def test_evidence_sql_statements_are_all_bounded():
    """§7：证据包发出去的每一条事件/工具/改动 SELECT 都必须带 LIMIT。

    不看返回值看真实 SQL——用 before_cursor_execute 监听引擎抓语句，
    以后有人把某条查询改回无界 .all()，这里直接红，不依赖"恰好数据少"。
    """
    from sqlalchemy import event as sa_event

    seeded = 40
    base = datetime.now(timezone.utc)
    async with get_session_factory()() as session:
        session.add(Project(id="p-sqlbound", name="p"))
        await session.commit()
        task = Task(
            id="task-sqlbound",
            project_id="p-sqlbound",
            title="t",
            goal="g",
            status="succeeded",
            created_at=base,
            updated_at=base,
        )
        session.add(task)
        for seq in range(1, seeded + 1):
            session.add(
                AgentEvent(
                    id=f"ev-{seq:04d}",
                    task_id=task.id,
                    type="tool_result" if seq % 2 else "task.started",
                    payload={"seq": seq, "output": "x" * 200},
                    seq=seq,
                    created_at=base + timedelta(seconds=seq),
                )
            )
        await session.commit()

    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(str(statement).lower())

    engine = get_engine()
    sa_event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        evidence = await build_evidence(get_session_factory(), task.id)
    finally:
        sa_event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    assert evidence["tools_meta"]["returned"] <= DEFAULT_TOOLS_LIMIT
    assert evidence["changes_meta"]["returned"] <= DEFAULT_CHANGES_LIMIT

    checked = 0
    for sql in statements:
        if "select" not in sql:
            continue
        for table in ("agent_events", "tool_calls", "file_changes"):
            if table in sql:
                # count(*) 与 group by 统计不需要 LIMIT；取行数的必须带。
                is_count = "count(" in sql or "group by" in sql
                if not is_count:
                    assert "limit" in sql, f"{table} 的取行查询没有 LIMIT：\n{sql}"
                    checked += 1
                break
    assert checked >= 3, f"没抓到足够的取行查询，监听器可能没生效（抓到 {checked} 条）"
