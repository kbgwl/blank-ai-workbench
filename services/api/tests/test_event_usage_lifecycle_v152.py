# -*- coding: utf-8 -*-
"""事件与用量的生命周期：超期事件只瘦身、账本只出读数。

这里的判据全部围绕两句话展开：

* **事件行不能删**。时间线的一行就是"那一步做过什么"，删掉之后界面上那一步
  凭空消失——所以对终态旧任务做的是裁 payload（大字符串留头一段 + 明确标记），
  行数、类型、序号、时间一个不动。
* **账本不能改**。``model_usage`` 每一行是"这一次调用花了多少 token"的历史事实，
  删行等于改账；所以用量这一侧只提供增长读数，不提供删除。

另外两条闸也各有判据：只碰**终态**任务（跑着/等审批/暂停的还能续跑，它们的
payload 是重放材料），以及默认 dry-run。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.model_provider import ModelUsage
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services import maintenance


@pytest.fixture(autouse=True)
async def schema() -> AsyncIterator[None]:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


def _days_ago(days: float) -> datetime:
    """库里列是 naive UTC：比账面时间时也按 naive 比，避免"差 8 小时"的假红。"""
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)


async def _seed_task(
    task_id: str,
    *,
    status: str = "succeeded",
    updated_days_ago: float = 200,
    events: list[tuple[str, dict]] | None = None,
) -> None:
    async with get_session_factory()() as session:
        session.add(Project(id=f"p-{task_id}", name="p"))
        session.add(
            Task(
                id=task_id,
                project_id=f"p-{task_id}",
                title="t",
                goal="g",
                status=status,
                updated_at=_days_ago(updated_days_ago),
            )
        )
        for index, (event_type, payload) in enumerate(events or [], start=1):
            session.add(
                AgentEvent(
                    id=f"{task_id}-e{index}",
                    task_id=task_id,
                    run_id=f"run-{task_id}",
                    seq=index,
                    node="execute_action",
                    type=event_type,
                    payload=payload,
                )
            )
        await session.commit()


async def _events(task_id: str) -> list[AgentEvent]:
    async with get_session_factory()() as session:
        return list(
            (
                await session.execute(
                    select(AgentEvent).where(AgentEvent.task_id == task_id).order_by(AgentEvent.seq)
                )
            ).scalars().all()
        )


LONG = "命令输出" * 400  # 1600 字，超过 value_limit


# ── 裁 payload 的纯函数：键一个不删、大值留标记 ────────────────────────────
def test_compaction_keeps_every_key_and_marks_the_cut():
    payload = {
        "call_id": "call_1",
        "tool_call_id": "tc_1",
        "output": LONG,
        "arguments": {"blob": "x" * 5000},
        "status": "ok",
    }
    compacted, changed = maintenance.compact_payload(payload)

    assert changed is True
    assert set(compacted) == set(payload), "键名不许少：少了就等于那一步的信息没了"
    # 身份键原样保留——重放靠它们认回这条调用
    assert compacted["call_id"] == "call_1"
    assert compacted["tool_call_id"] == "tc_1"
    # 大值被裁短并留下"原有多长"的痕迹
    assert len(compacted["output"]) < len(LONG)
    assert "已压缩" in compacted["output"] and "1600" in compacted["output"]
    assert compacted["arguments"] == {"compacted": True, "bytes": len(str({"blob": "x" * 5000}))} or compacted[
        "arguments"
    ].get("compacted") is True
    # 小值一个字节都别动
    assert compacted["status"] == "ok"


def test_small_payloads_are_left_alone():
    payload = {"call_id": "c", "output": "ok", "status": "ok"}
    compacted, changed = maintenance.compact_payload(payload)
    assert changed is False
    assert compacted == payload


# ── 报告：只读，且能说清"能省多少" ────────────────────────────────────────
async def test_report_counts_stale_events_and_reclaims_bytes():
    await _seed_task(
        "t-old",
        events=[
            ("tool.completed", {"call_id": "c1", "tool_call_id": "tc1", "output": LONG}),
            ("task.started", {"goal": "g"}),
        ],
    )
    report = await maintenance.event_report(get_session_factory(), retention_days=90)

    assert report["events"] == 2
    assert report["stale_tasks"] == 1
    assert report["sampled_events"] == 2
    assert report["compactable_events"] == 1, "只有那条带大输出的需要裁"
    assert report["reclaimable_bytes"] > 0


async def test_report_does_not_touch_events_that_are_still_resumable():
    """跑着 / 等审批 / 暂停的任务一个字都不许动，它们还能续跑。"""
    for status in ("running", "waiting_approval", "paused", "queued"):
        await _seed_task(
            f"t-{status}",
            status=status,
            events=[("tool.completed", {"call_id": "c1", "output": LONG})],
        )
    report = await maintenance.event_report(get_session_factory(), retention_days=90)
    assert report["stale_tasks"] == 0
    assert report["compactable_events"] == 0


async def test_recent_terminal_tasks_are_inside_the_retention_window():
    await _seed_task(
        "t-new",
        updated_days_ago=1,
        events=[("tool.completed", {"call_id": "c1", "output": LONG})],
    )
    report = await maintenance.event_report(get_session_factory(), retention_days=90)
    assert report["stale_tasks"] == 0


# ── 真正下手：行数不变、payload 变短 ──────────────────────────────────────
async def test_compact_defaults_to_dry_run_and_changes_nothing():
    await _seed_task(
        "t-old",
        events=[("tool.completed", {"call_id": "c1", "tool_call_id": "tc1", "output": LONG})],
    )
    result = await maintenance.compact_events(get_session_factory(), retention_days=90)

    assert result["dry_run"] is True
    assert result["would_free_bytes"] > 0
    assert result["compacted"] == 0
    events = await _events("t-old")
    assert events[0].payload["output"] == LONG, "dry-run 必须一个字节都不改"


async def test_compaction_shrinks_payload_without_deleting_rows():
    await _seed_task(
        "t-old",
        events=[
            ("tool.completed", {"call_id": "c1", "tool_call_id": "tc1", "output": LONG}),
            ("task.started", {"goal": "g"}),
            ("validation.completed", {"status": "ok"}),
        ],
    )
    result = await maintenance.compact_events(
        get_session_factory(), retention_days=90, dry_run=False
    )

    assert result["compacted"] == 1
    assert result["freed_bytes"] > 0
    events = await _events("t-old")
    assert [event.type for event in events] == [
        "tool.completed",
        "task.started",
        "validation.completed",
    ], "行不能少、类型不能变、顺序不能乱"
    assert [event.seq for event in events] == [1, 2, 3]
    # 身份键在，重放还认得出这条调用；内容被裁短了
    assert events[0].payload["call_id"] == "c1"
    assert events[0].payload["tool_call_id"] == "tc1"
    assert len(events[0].payload["output"]) < len(LONG)
    # 本来就短的载荷不动
    assert events[2].payload == {"status": "ok"}


async def test_compaction_is_idempotent():
    await _seed_task(
        "t-old",
        events=[("tool.completed", {"call_id": "c1", "output": LONG})],
    )
    first = await maintenance.compact_events(get_session_factory(), retention_days=90, dry_run=False)
    second = await maintenance.compact_events(get_session_factory(), retention_days=90, dry_run=False)
    assert first["compacted"] == 1
    assert second["compacted"] == 0, "已裁过的不该再记一次账"


async def test_compaction_never_crosses_into_a_running_task():
    await _seed_task(
        "t-run",
        status="running",
        events=[("tool.completed", {"call_id": "c1", "output": LONG})],
    )
    await maintenance.compact_events(get_session_factory(), retention_days=90, dry_run=False)
    events = await _events("t-run")
    assert events[0].payload["output"] == LONG


# ── 用量账本：只读数、不删行 ──────────────────────────────────────────────
async def test_usage_report_reads_growth_and_declares_the_policy():
    async with get_session_factory()() as session:
        for index in range(3):
            session.add(
                ModelUsage(
                    id=f"u-{index}",
                    provider_id="provider-1",
                    model="deepseek-chat",
                    prompt_tokens=100,
                    completion_tokens=10,
                )
            )
        await session.commit()
    report = await maintenance.usage_report(get_session_factory())

    assert report["rows"] == 3
    assert report["prompt_tokens"] == 300
    assert report["completion_tokens"] == 30
    assert report["top_models"][0]["model"] == "deepseek-chat"
    # 这句话必须跟着读数一起出现，否则读的人会以为"没清"是漏了
    assert "不自动清理" in report["policy"]


async def test_storage_report_covers_all_four_kinds():
    await _seed_task(
        "t-old",
        events=[("tool.completed", {"call_id": "c1", "output": LONG})],
    )
    async with get_session_factory()() as session:
        session.add(
            ModelUsage(id="u-1", provider_id="provider-1", model="m", prompt_tokens=1, completion_tokens=1)
        )
        await session.commit()
    report = await maintenance.storage_report(get_session_factory())

    for key in ("artifacts", "snapshots", "events", "usage", "database"):
        assert key in report, f"{key} 这一块没进总账就等于没人看得见它"
    assert report["events"]["events"] == 1
    assert report["usage"]["rows"] == 1


async def test_metrics_endpoint_reports_what_it_claims_to():
    """指标得有读出来的地方：记了没人能看，等于没记。"""
    from blank_api.api.routes import maintenance as maintenance_routes
    from blank_api.services import metrics

    metrics.reset()
    metrics.inc(metrics.MODEL_TIMEOUT)
    metrics.inc(metrics.MODEL_FALLBACK, provider="backup")
    metrics.gauge("sse.connections", 3)

    payload = await maintenance_routes.process_metrics()
    assert payload["counters"][metrics.MODEL_TIMEOUT] == 1
    # 带标签的计数按分组给，同时算进总数——否则"只有标签那部分"看起来像没记过
    assert payload["counters_by_label"][metrics.MODEL_FALLBACK] == {"provider=backup": 1}
    assert metrics.counter(metrics.MODEL_FALLBACK) == 1
    assert payload["gauges"]["sse.connections"] == 3
    assert payload["uptime_seconds"] >= 0
    metrics.reset()


# ── 真实环境场景：并发清理与失败恢复 ────────────────────────────────────────


async def test_concurrent_compaction_is_safe_and_complete():
    """两个 worker 同时跑清理：不重不漏、行数不变、终态收敛一致。

    对应真实部署里维护端点被多次触发（定时任务 + 人工点按钮）的情况。
    清理写的是同一条 UPDATE 语义（裁到同一目标值），并发结果应与单次等价。
    """
    import asyncio

    events = [("tool_result", {"output": LONG})] * 6
    await _seed_task("conc", events=events)

    results = await asyncio.gather(
        maintenance.compact_events(get_session_factory(), dry_run=False),
        maintenance.compact_events(get_session_factory(), dry_run=False),
        return_exceptions=True,
    )
    for result in results:
        assert not isinstance(result, BaseException), result

    rows = await _events("conc")
    # 行一个不删，且全部被裁到位（与单次清理等价）。
    assert len(rows) == 6
    for row in rows:
        assert maintenance._already_compacted(row.payload.get("output", ""))
        assert len(row.payload["output"]) < len(LONG)

    # 再跑一次：幂等，不再"释放"任何字节。
    third = await maintenance.compact_events(get_session_factory(), dry_run=False)
    assert third["freed_bytes"] == 0


async def test_compaction_recovers_after_midway_failure(monkeypatch):
    """清理中途失败（如进程被杀）：重跑一次即可补齐，不留半裁状态。

    模拟清理过程中崩溃：monkeypatch compact_payload 让首次调用抛错，
    然后原样重跑，最终所有 payload 都应收敛到与一次成功清理相同的状态。
    """
    real = maintenance.compact_payload
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash midway")
        return real(*args, **kwargs)

    monkeypatch.setattr(maintenance, "compact_payload", flaky)

    events = [("tool_result", {"output": LONG})] * 4
    await _seed_task("recov", events=events)

    with pytest.raises(RuntimeError):
        await maintenance.compact_events(get_session_factory(), dry_run=False)

    # 崩溃后数据完好：行数不变、没有任何 payload 被裁（第一次调用就抛了）。
    assert len(await _events("recov")) == 4
    rows = await _events("recov")
    assert all(row.payload["output"] == LONG for row in rows)

    # 重跑：全部补齐。
    done = await maintenance.compact_events(get_session_factory(), dry_run=False)
    assert done["compacted"] == 4
    for row in await _events("recov"):
        assert maintenance._already_compacted(row.payload.get("output", ""))
