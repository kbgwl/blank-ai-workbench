# -*- coding: utf-8 -*-
"""v162 Watchdog：纯策略与落库去重。

钉三件事：
- 判断顺序（先看"没人跑"，再看"没在动"，最后看预算），口径错会把死掉的 run 报成超预算；
- 动作分级（诊断 < 通知 < 放行 < 暂停 < 终止），调用方不必自己组合；
- 同一条诊断只写一条事件——巡检是周期性的，不设门槛就是一晚上几百条通知。
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.project import Project
from blank_api.models.run import AgentRun
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent import watchdog

NOW = watchdog.now_utc()


def _pulse(**kwargs) -> watchdog.Pulse:
    base = dict(
        run_id="r-1",
        task_id="t-1",
        status="running",
        attempts=0,
        max_recoveries=2,
        max_steps=10,
        steps_used=1,
        lease_expires_at=NOW + timedelta(seconds=60),
        last_event_at=NOW - timedelta(seconds=5),
        started_at=NOW - timedelta(minutes=3),
        now=NOW,
    )
    base.update(kwargs)
    return watchdog.Pulse(**base)


# ── 纯策略 ──────────────────────────────────────────────────────────────────


def test_healthy_run_is_ok():
    verdict = watchdog.inspect(_pulse())
    assert verdict.action == watchdog.ACTION_OK
    assert verdict.notify is False


def test_expired_lease_requests_release_within_recovery_budget():
    verdict = watchdog.inspect(_pulse(lease_expires_at=NOW - timedelta(seconds=1), attempts=1))
    assert verdict.action == watchdog.ACTION_RELEASE
    assert verdict.notify is True
    assert any("租约已过期" in reason for reason in verdict.reasons)


def test_expired_lease_without_budget_terminates():
    verdict = watchdog.inspect(_pulse(lease_expires_at=NOW - timedelta(seconds=1), attempts=2))
    assert verdict.action == watchdog.ACTION_TERMINATE


def test_too_many_attempts_terminates_before_budget_checks():
    # 已经超过恢复上限：不该再被报成"预算用尽"——它压根没在跑了。
    verdict = watchdog.inspect(
        _pulse(attempts=5, lease_expires_at=NOW - timedelta(minutes=10), steps_used=99)
    )
    assert verdict.action == watchdog.ACTION_TERMINATE
    assert any("超过上限" in reason for reason in verdict.reasons)


def test_long_silence_requests_pause_but_short_silence_only_diagnoses():
    short = watchdog.inspect(_pulse(last_event_at=NOW - timedelta(seconds=watchdog.SILENCE_SECONDS + 5)))
    assert short.action == watchdog.ACTION_DIAGNOSE
    long = watchdog.inspect(_pulse(last_event_at=NOW - timedelta(seconds=watchdog.STUCK_SECONDS + 5)))
    assert long.action == watchdog.ACTION_PAUSE


def test_waiting_for_approval_is_not_treated_as_stuck():
    # 等审批时"没有新事件"是正常的：这时候报卡死会把用户引去重试，而只是他没点。
    verdict = watchdog.inspect(
        _pulse(
            last_event_at=NOW - timedelta(seconds=watchdog.STUCK_SECONDS + 5),
            approval_waiting_since=NOW - timedelta(minutes=2),
        )
    )
    assert verdict.action == watchdog.ACTION_OK


def test_stale_approval_notifies():
    verdict = watchdog.inspect(
        _pulse(approval_waiting_since=NOW - timedelta(seconds=watchdog.APPROVAL_STALE_SECONDS + 10))
    )
    assert verdict.action == watchdog.ACTION_NOTIFY
    assert any("审批已等待" in reason for reason in verdict.reasons)


def test_budget_exhaustion_asks_to_wrap_up_not_fail():
    steps = watchdog.inspect(_pulse(steps_used=10, max_steps=10))
    assert steps.action == watchdog.ACTION_PAUSE
    tokens = watchdog.inspect(_pulse(tokens_used=1000, token_budget=1000))
    assert tokens.action == watchdog.ACTION_PAUSE
    # 动作是"要求收尾"，不是判死：已经做过的改动还在。
    assert "终止" not in "；".join(tokens.reasons)


def test_time_budget_exhaustion_asks_to_wrap_up():
    # 任务书 §4.4 的「耗时」一项：跑了 20 分钟、上限 10 分钟 → 要求收尾，不是判死。
    exhausted = watchdog.inspect(_pulse(started_at=NOW - timedelta(minutes=20), max_seconds=600))
    assert exhausted.action == watchdog.ACTION_PAUSE
    assert any("耗时预算" in reason for reason in exhausted.reasons)

    # 没声明上限（老数据/未派发）时这一项不参与判定，不能凭空判定超时。
    unset = watchdog.inspect(_pulse(started_at=NOW - timedelta(minutes=20), max_seconds=0))
    assert unset.action == watchdog.ACTION_OK


def test_elapsed_and_time_exhausted_properties():
    pulse = _pulse(started_at=NOW - timedelta(seconds=90), max_seconds=60)
    assert pulse.elapsed_seconds == 90
    assert pulse.time_exhausted is True
    # 起点缺失时读作 0 秒，不猜一个"很久"。
    assert _pulse(started_at=None, max_seconds=60).elapsed_seconds == 0
    assert _pulse(started_at=None, max_seconds=60).time_exhausted is False


def test_success_without_delivery_evidence_is_flagged():
    verdict = watchdog.inspect(_pulse(status="succeeded", has_delivery=False))
    assert verdict.action == watchdog.ACTION_DIAGNOSE
    assert verdict.notify is True


def test_success_with_evidence_is_ok():
    assert watchdog.inspect(_pulse(status="succeeded", has_delivery=True)).action == watchdog.ACTION_OK


def test_terminated_status_is_not_inspected_further():
    verdict = watchdog.inspect(_pulse(status="failed", attempts=9, lease_expires_at=NOW - timedelta(hours=1)))
    assert verdict.action == watchdog.ACTION_OK


# ── 落库与去重 ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


async def _seed(*, lease_offset: int = -60, attempts: int = 0) -> tuple[str, str]:
    stamp = uuid.uuid4().hex[:8]
    async with get_session_factory()() as session:
        project = Project(id=f"p-{stamp}", name="p", description="")
        session.add(project)
        await session.flush()
        task = Task(id=f"t-{stamp}", project_id=project.id, title="t", goal="g", status="running", max_steps=10)
        session.add(task)
        await session.flush()
        run = AgentRun(
            id=f"r-{stamp}",
            task_id=task.id,
            status="running",
            worker_id="w",
            attempts=attempts,
            # 用「当前时钟」算租约，不能用模块导入时固定的 NOW：sweep 拿的是调用时真实时钟，
            # 全集里这个测试跑得晚，NOW+offset 会变成过去式，把健康 run 误判成租约过期。
            lease_expires_at=watchdog.now_utc() + timedelta(seconds=lease_offset),
        )
        session.add(run)
        await session.commit()
        return run.id, task.id


async def test_sweep_records_alert_once_and_does_not_change_run_state():
    run_id, task_id = await _seed()
    async with get_session_factory()() as session:
        first = await watchdog.sweep(session)
    assert first, "租约过期的 run 应该被诊断出来"
    assert all(item["run_id"] == run_id for item in first)

    # 再扫一轮：同一条诊断不再重复落库。
    async with get_session_factory()() as session:
        second = await watchdog.sweep(session)
    assert second == []

    async with get_session_factory()() as session:
        events = list(
            (
                await session.execute(
                    select(AgentEvent).where(AgentEvent.type == "watchdog.alert")
                )
            ).scalars()
        )
        run = await session.get(AgentRun, run_id)
    assert events and all(event.task_id == task_id for event in events)
    assert events[0].payload["enforced"] is False
    # 观察者不改状态：run 还是 running，等着正常的认领路径处理。
    assert run.status == "running"


async def test_healthy_run_produces_no_alert():
    await _seed(lease_offset=120)
    async with get_session_factory()() as session:
        assert await watchdog.sweep(session) == []
