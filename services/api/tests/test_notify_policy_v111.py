# -*- coding: utf-8 -*-
"""v0.11.1 通知策略的来源顺序：快照 > 设置 > 兜底。

这组用例来自一次真机验收踩出来的坑：在安装版上把设置里的「通知时机」改成
里程碑，跑完一个任务，`notify.decided` 里仍然是 ``policy=approval_only``、
``disturb=false``——**下拉框选什么都不生效**。

根因是 ``_record_notify_decision`` 只认任务快照里的 ``notify_policy``，没有就
硬兜 ``approval_only``；而写快照的那段代码被 ``super_agent_dispatch`` 挡着，
那个开关默认是**关**的（BETA），于是快照恒为 ``{}``。
「设置项存在但没人消费」比「没有这个设置」更糟：用户以为自己开了，实际没有。

同一个根因还让 ``origin`` 恒为空，桌面通知永远写"做完了"，永远写不出"到点了"。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select

from blank_api.api.routes.tasks import _record_notify_decision
from blank_api.db import get_session_factory
from blank_api.models.task import AgentEvent, Task
from blank_api.models.trigger import AgentTrigger

from tests.test_agent_v04 import (  # noqa: F401  (复用端到端夹具)
    API,
    create_project,
    create_provider,
    env,
)

SETTINGS_FILE = "BLANK_SETTINGS_FILE"


async def _make_task(project_id: str, *, snapshot: dict | None = None, status: str = "succeeded") -> str:
    async with get_session_factory()() as session:
        task = Task(
            id=str(uuid4()),
            project_id=project_id,
            title="检查部署",
            goal="检查一下部署",
            status=status,
            dispatch_snapshot=snapshot or {},
        )
        session.add(task)
        await session.commit()
        return task.id


async def _with_trigger(task_id: str, project_id: str, intent: str) -> None:
    async with get_session_factory()() as session:
        session.add(
            AgentTrigger(
                id=str(uuid4()),
                project_id=project_id,
                task_id=task_id,
                event_type="schedule" if intent == "schedule" else "workspace_changed",
                source="task.start",
                payload={"goal": "检查一下部署", "intent": intent},
                dedupe_key=f"k-{uuid4().hex[:8]}",
                status="fired",
            )
        )
        await session.commit()


async def _decided(task_id: str) -> dict:
    """取这条任务的 notify.decided 判定。"""
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(AgentEvent)
                .where(AgentEvent.type == "notify.decided", AgentEvent.task_id == task_id)
                .order_by(AgentEvent.seq.desc())
                .limit(1)
            )
        ).scalars().first()
    assert row is not None, "没有 notify.decided，判定根本没发生"
    return dict(row.payload or {})


def _write_settings(monkeypatch, tmp_path, background: dict) -> None:
    from blank_api.services.settings import SettingsStore

    monkeypatch.setenv(SETTINGS_FILE, str(tmp_path / "settings.json"))
    SettingsStore().write({"background": background})


# ── 策略来源 ────────────────────────────────────────────────────────────────


async def test_settings_policy_applies_when_snapshot_is_empty(env, workspace, monkeypatch, tmp_path):
    """快照空的时候，必须用设置里选的「通知时机」，不能硬兜 approval_only。"""
    _write_settings(monkeypatch, tmp_path, {"notify_policy": "milestone"})
    project_id = await create_project(env, workspace)
    task_id = await _make_task(project_id)

    await _record_notify_decision(task_id, "succeeded")
    payload = await _decided(task_id)

    assert payload["policy"] == "milestone", "设置里选了里程碑，判定就得按里程碑走"
    assert payload["disturb"] is True, "milestone 策略下，跑完有交付物是要通知的"


async def test_silent_setting_actually_silences(env, workspace, monkeypatch, tmp_path):
    """反过来也要成立：选了「安静」，完成类通知就真的不出声。"""
    _write_settings(monkeypatch, tmp_path, {"notify_policy": "silent"})
    project_id = await create_project(env, workspace)
    task_id = await _make_task(project_id)

    await _record_notify_decision(task_id, "succeeded")
    payload = await _decided(task_id)

    assert payload["policy"] == "silent"
    assert payload["disturb"] is False


async def test_snapshot_policy_wins_over_settings(env, workspace, monkeypatch, tmp_path):
    """开了 Router 之后，这一次派发的判定比全局设置更具体，不能被设置顶掉。"""
    _write_settings(monkeypatch, tmp_path, {"notify_policy": "always"})
    project_id = await create_project(env, workspace)
    task_id = await _make_task(project_id, snapshot={"intent": "answer", "notify_policy": "silent"})

    await _record_notify_decision(task_id, "succeeded")
    payload = await _decided(task_id)

    assert payload["policy"] == "silent", "任务快照说这一轮别打扰，全局 always 也不该推翻它"


async def test_approval_is_announced_even_when_settings_are_silent(env, workspace, monkeypatch, tmp_path):
    """审批压着任务动不了，静默策略也必须放行——这一条不能因为回落逻辑被改坏。"""
    _write_settings(monkeypatch, tmp_path, {"notify_policy": "silent"})
    project_id = await create_project(env, workspace)
    task_id = await _make_task(project_id, status="waiting_approval")

    await _record_notify_decision(task_id, "waiting_approval")
    payload = await _decided(task_id)

    assert payload["event"] == "approval"
    assert payload["disturb"] is True


async def test_quiet_hours_from_settings_still_apply(env, workspace, monkeypatch, tmp_path):
    """免打扰时段也要继续生效：回落路径不能顺手把 quiet_hours 丢了。"""
    # 覆盖全天的免打扰，现在必然处在时段内。
    _write_settings(monkeypatch, tmp_path, {"notify_policy": "milestone", "quiet_hours": "00:00-23:59"})
    project_id = await create_project(env, workspace)
    task_id = await _make_task(project_id)

    await _record_notify_decision(task_id, "succeeded")
    payload = await _decided(task_id)

    assert payload["quiet_hours"] is True
    assert payload["disturb"] is False, "免打扰时段内，完成类通知要压住"


# ── origin（文案分档）────────────────────────────────────────────────────────


async def test_origin_comes_from_the_trigger_when_snapshot_is_empty(env, workspace, monkeypatch, tmp_path):
    """定时起的任务，就算快照是空的，也要能认出「这是到点了」。"""
    _write_settings(monkeypatch, tmp_path, {"notify_policy": "milestone"})
    project_id = await create_project(env, workspace)
    task_id = await _make_task(project_id)
    await _with_trigger(task_id, project_id, "schedule")

    await _record_notify_decision(task_id, "succeeded")
    payload = await _decided(task_id)

    assert payload["origin"] == "schedule", "没有这个值，桌面只能弹「做完了」，弹不出「到点了」"


async def test_watch_origin_is_recognized_too(env, workspace, monkeypatch, tmp_path):
    _write_settings(monkeypatch, tmp_path, {"notify_policy": "milestone"})
    project_id = await create_project(env, workspace)
    task_id = await _make_task(project_id)
    await _with_trigger(task_id, project_id, "watch")

    await _record_notify_decision(task_id, "succeeded")
    payload = await _decided(task_id)

    assert payload["origin"] == "watch"


async def test_origin_stays_empty_for_a_task_a_person_started(env, workspace, monkeypatch, tmp_path):
    """没有触发器的任务是人自己发的，origin 就该是空的——不能瞎认成定时。"""
    _write_settings(monkeypatch, tmp_path, {"notify_policy": "milestone"})
    project_id = await create_project(env, workspace)
    task_id = await _make_task(project_id)

    await _record_notify_decision(task_id, "succeeded")
    payload = await _decided(task_id)

    assert payload["origin"] == ""


async def test_snapshot_intent_wins_over_the_trigger(env, workspace, monkeypatch, tmp_path):
    """快照里写了 intent 就以它为准，不去翻触发器表。"""
    _write_settings(monkeypatch, tmp_path, {"notify_policy": "milestone"})
    project_id = await create_project(env, workspace)
    task_id = await _make_task(project_id, snapshot={"intent": "execute"})
    await _with_trigger(task_id, project_id, "schedule")

    await _record_notify_decision(task_id, "succeeded")
    payload = await _decided(task_id)

    assert payload["origin"] == "execute"
