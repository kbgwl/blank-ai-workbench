# -*- coding: utf-8 -*-
"""v163：低打扰通知、固定四行交付、后台安全上限、暂停/恢复/重试、结构化状态。

这些用例全部不依赖模型与网络：判断是纯函数，状态机走库。这样"策略对不对"
和"模型这次表现如何"才能分开看——否则一个用例红了，永远要先排除是不是模型抽风。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from blank_api.api.routes import tasks as task_routes
from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.project import Project
from blank_api.models.run import AgentRun
from blank_api.models.task import AgentEvent, FileChange, Task
from blank_api.services.agent import delivery, notify
from blank_api.services.settings import normalize, quiet_hours_active

# ── 通知门（任务书 §5）────────────────────────────────────────────────────


def test_notify_silent_only_lets_approval_through():
    assert notify.should_disturb("silent", notify.EVENT_APPROVAL) is True
    assert notify.should_disturb("silent", notify.EVENT_DELIVERABLE) is False
    assert notify.should_disturb("silent", notify.EVENT_PROGRESS) is False


def test_notify_approval_only_is_the_default_low_disturbance():
    assert notify.should_disturb("approval_only", notify.EVENT_APPROVAL) is True
    assert notify.should_disturb("approval_only", notify.EVENT_DELIVERABLE) is False
    assert notify.should_disturb("approval_only", notify.EVENT_FAILURE) is False


def test_notify_milestone_reports_outcomes_but_not_progress():
    assert notify.should_disturb("milestone", notify.EVENT_FAILURE) is True
    assert notify.should_disturb("milestone", notify.EVENT_DELIVERABLE) is True
    assert notify.should_disturb("milestone", notify.EVENT_MILESTONE) is True
    assert notify.should_disturb("milestone", notify.EVENT_PROGRESS) is False


def test_notify_always_and_quiet_hours():
    assert notify.should_disturb("always", notify.EVENT_PROGRESS) is True
    # 免打扰时段只放审批过去：别的都能等到早上。
    assert notify.should_disturb("always", notify.EVENT_DELIVERABLE, quiet_hours=True) is False
    assert notify.should_disturb("always", notify.EVENT_APPROVAL, quiet_hours=True) is True


def test_notify_unknown_policy_is_quiet_by_default():
    assert notify.should_disturb("nonsense", notify.EVENT_DELIVERABLE) is False
    assert notify.should_disturb("nonsense", notify.EVENT_APPROVAL) is True
    # 认不出的事件类型一律不打扰。
    assert notify.should_disturb("always", "made_up_event") is False


def test_notify_explain_says_why():
    hit = notify.explain("milestone", notify.EVENT_DELIVERABLE)
    assert hit["disturb"] is True and "milestone" in hit["reason"]
    quiet = notify.explain("always", notify.EVENT_DELIVERABLE, quiet_hours=True)
    assert quiet["disturb"] is False and "免打扰" in quiet["reason"]


# ── 固定四行交付（任务书 §5）──────────────────────────────────────────────


def test_delivery_renders_four_fixed_lines():
    text = delivery.render(
        delivery.DeliveryFacts(
            completed="很高兴为您服务。修好了登录接口。",
            changed_files=2,
            diff_count=3,
            verify_command="pytest -q",
            verify_exit=0,
            needs_user="确认是否合并到 main",
        )
    )
    lines = text.splitlines()
    assert lines[0] == "完成：修好了登录接口"  # 空泛开场被丢掉
    assert lines[1] == "改动：2 个文件、3 处 Diff"
    assert lines[2] == "验证：pytest -q（退出码 0）"
    assert lines[3] == "需要你：确认是否合并到 main"


def test_delivery_hides_empty_sections_instead_of_placeholders():
    text = delivery.render(delivery.DeliveryFacts(completed="只读调研完成", changed_files=0))
    assert text == "完成：只读调研完成"
    assert "（无）" not in text


def test_delivery_never_claims_verification_without_a_real_exit_code():
    # 没有真跑（exit 为 None）就不许出现"验证"这一行，更不许写"已验证"。
    text = delivery.render(
        delivery.DeliveryFacts(completed="改了配置", changed_files=1, verify_command="pytest -q", verify_exit=None)
    )
    assert "验证" not in text


def test_delivery_as_dict_is_structured():
    payload = delivery.as_dict(
        delivery.DeliveryFacts(completed="完成 A", changed_files=1, verify_command="pytest", verify_exit=1)
    )
    assert payload["changed_files"] == 1
    assert payload["verify"] == {"command": "pytest", "exit": 1}
    assert "完成：完成 A" in payload["text"]


# ── 后台设置安全上限（任务书 §5）──────────────────────────────────────────


def test_background_settings_are_clamped_to_hard_caps():
    payload = normalize(
        {
            "background": {
                "max_steps": 9999,
                "max_seconds": 999999,
                "token_budget": 99_999_999,
                "auto_recoveries": 99,
                "notify_policy": "bogus",
                "default_model_policy": "bogus",
                "quiet_hours": "2222-0800",
            }
        }
    )["background"]
    assert payload["max_steps"] == 200
    assert payload["max_seconds"] == 6 * 3600
    assert payload["token_budget"] == 1_500_000
    assert payload["auto_recoveries"] == 3
    assert payload["notify_policy"] == "approval_only"
    assert payload["default_model_policy"] == "default"
    # 写坏的时间段整段丢掉，等于不设，不会变成"全天免打扰"。
    assert payload["quiet_hours"] == ""


def test_quiet_hours_crosses_midnight():
    late = datetime(2026, 1, 1, 23, 0)
    noon = datetime(2026, 1, 1, 12, 0)
    early = datetime(2026, 1, 1, 7, 0)
    assert quiet_hours_active("22:00-08:00", now=late) is True
    assert quiet_hours_active("22:00-08:00", now=early) is True
    assert quiet_hours_active("22:00-08:00", now=noon) is False
    assert quiet_hours_active("", now=late) is False


# ── 暂停 / 恢复 / 重试 与 结构化状态（任务书 §4.2 / §6）──────────────────


@pytest.fixture(autouse=True)
async def schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


async def _seed(status: str = "running") -> tuple[str, str]:
    stamp = uuid.uuid4().hex[:8]
    async with get_session_factory()() as session:
        project = Project(id=f"p-{stamp}", name="p", description="")
        session.add(project)
        await session.flush()
        task = Task(
            id=f"t-{stamp}",
            project_id=project.id,
            title="t",
            goal="g",
            status=status,
            max_steps=10,
            dispatch_snapshot={"snapshot_id": "dsp-test", "intent": "execute", "budget": {"max_seconds": 60}},
        )
        session.add(task)
        await session.flush()
        run = AgentRun(
            id=f"r-{stamp}",
            task_id=task.id,
            status="running",
            worker_id="w-1",
            attempts=0,
            lease_expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=60),
        )
        session.add(run)
        await session.commit()
        return task.id, run.id


async def test_pause_sets_paused_and_releases_lease():
    task_id, run_id = await _seed()
    async with get_session_factory()() as session:
        out = await task_routes.pause_task(task_id, session)
    assert out.status == "paused"

    async with get_session_factory()() as session:
        run = await session.get(AgentRun, run_id)
        events = list(
            (await session.execute(select(AgentEvent).where(AgentEvent.task_id == task_id))).scalars()
        )
    assert run.status == "paused"
    assert run.worker_id == ""
    assert run.lease_expires_at is None
    assert any(event.type == "task.paused" for event in events)


async def test_resume_rejects_non_paused_and_requeues_paused(monkeypatch):
    task_id, _ = await _seed(status="running")
    async with get_session_factory()() as session:
        with pytest.raises(Exception):
            await task_routes.resume_task(task_id, None, session)  # type: ignore[arg-type]

    paused_id, _ = await _seed(status="paused")

    async def fake_launch(db, task, payload, request, **kwargs):
        task.status = "queued"
        await db.commit()
        return {"task_id": task.id, "run_id": "r", "status": "queued"}

    monkeypatch.setattr(task_routes, "launch_task", fake_launch)
    async with get_session_factory()() as session:
        out = await task_routes.resume_task(paused_id, None, session)  # type: ignore[arg-type]
    assert out.status == "queued"


async def test_retry_rejects_running_and_requeues_failed(monkeypatch):
    running_id, _ = await _seed(status="running")
    async with get_session_factory()() as session:
        with pytest.raises(Exception):
            await task_routes.retry_task(running_id, None, session)  # type: ignore[arg-type]

    failed_id, _ = await _seed(status="failed")

    async def fake_launch(db, task, payload, request, **kwargs):
        task.status = "queued"
        await db.commit()
        return {"task_id": task.id, "run_id": "r", "status": "queued"}

    monkeypatch.setattr(task_routes, "launch_task", fake_launch)
    async with get_session_factory()() as session:
        out = await task_routes.retry_task(failed_id, None, session)  # type: ignore[arg-type]
    assert out.status == "queued"


async def test_task_status_exposes_structured_fields():
    task_id, run_id = await _seed()
    async with get_session_factory()() as session:
        session.add(FileChange(id="fc-1", task_id=task_id, path="a.py", change_type="modified"))
        session.add(AgentEvent(id="ev-1", task_id=task_id, run_id=run_id, seq=1, node="execute_action", type="node.started", payload={}))
        await session.commit()

    async with get_session_factory()() as session:
        payload = await task_routes.task_status(task_id, session)

    assert payload["task_id"] == task_id
    assert payload["run_id"] == run_id
    assert payload["worker_id"] == "w-1"
    assert payload["snapshot_id"] == "dsp-test"
    assert payload["status"] == "running"
    assert payload["changes"]["count"] == 1
    assert payload["changes"]["diff_fingerprint"]
    assert payload["last_event"]["type"] == "node.started"
    assert payload["approval"] is None
    assert payload["delivery"]["present"] is False


# ── 四行交付真的接进了收尾（任务书 §5）────────────────────────────────────
# 这一节钉的是「delivery.render 不是死代码」：交付文本必须由事实拼出来，
# 改动数、退出码来自工具结果与校验记录，模型那句「已验证」在这里不起作用。


def test_delivery_text_is_rendered_from_facts_not_model_prose():
    from blank_api.services.agent import graph

    state = {
        "validation": {"status": "ok", "passed": True, "command": "pytest -q", "exit_code": 0},
        "tool_results": [
            {"ok": True, "data": {"path": "auth.py", "diff": "@@ -1 +1 @@\n-a\n+b"}},
            {"ok": True, "data": {"path": "config.py", "diff": "@@ -3 +3 @@\n-x\n+y"}},
        ],
    }
    report = "结论\n- 登录令牌改成 30 天有效\n下一步\n- 重启服务生效"
    text = graph._delivery_text(report, state, None)
    assert text.splitlines() == [
        "完成：登录令牌改成 30 天有效",
        "改动：2 个文件、2 处 Diff",
        "验证：pytest -q（退出码 0）",
        "需要你：重启服务生效",
    ]


def test_delivery_text_hides_the_verify_line_when_nothing_ran():
    from blank_api.services.agent import graph

    state = {
        "validation": {"status": "skipped", "skipped_reason": "no test command"},
        "tool_results": [{"ok": True, "data": {"path": "a.py", "diff": "@@ -1 +1 @@\n-a\n+b"}}],
    }
    text = graph._delivery_text("结论\n- 改好了", state, None)
    lines = text.splitlines()
    assert "验证：" not in text, "没跑过就绝不能出现验证行"
    assert lines[0] == "完成：改好了"
    assert lines[1] == "改动：1 个文件、1 处 Diff"


def test_delivery_text_strips_empty_praise_and_keeps_one_line():
    from blank_api.services.agent import graph

    state = {"validation": {"status": "ok", "command": "pytest -q", "exit_code": 0}, "tool_results": []}
    text = graph._delivery_text("很高兴为您服务！配置项改成 30 天。感谢使用。", state, None)
    assert text.splitlines() == ["完成：配置项改成 30 天", "验证：pytest -q（退出码 0）"]


def test_delivery_text_leaves_analysis_answers_alone():
    """没改动、也没跑校验的交付（只读分析）不进四行格式——压成一句等于截掉答案。"""
    from blank_api.services.agent import graph

    report = "结论\n- 入口文件是 app/main.py，路由集中在 api/routes 下\n- 鉴权在 middleware 里"
    assert graph._delivery_text(report, {"validation": {}, "tool_results": []}, None) == report


def test_delivery_payload_carries_structured_facts():
    from blank_api.services.agent import graph

    state = {"validation": {"status": "ok", "command": "pytest -q", "exit_code": 1}, "tool_results": []}
    report = "结论\n- 改好了"
    text = graph._delivery_text(report, state, None)
    payload = graph._delivery_payload(report, state, "report", text)
    assert payload["mode"] == "report"
    assert payload["verify"]["command"] == "pytest -q"
    assert payload["verify"]["exit"] == 1
    assert payload["text"].startswith("完成：改好了")


def test_delivery_text_hides_needs_user_when_model_writes_none():
    """「下一步：无」不是待办——照抄会让每份交付都多一行「需要你：无」。"""
    from blank_api.services.agent import graph

    state = {
        "validation": {"status": "ok", "command": "pytest -q", "exit_code": 0},
        "tool_results": [{"ok": True, "data": {"path": "a.py", "diff": "@@ -1 +1 @@\n-a\n+b"}}],
    }
    text = graph._delivery_text("结论\n- 改好了\n下一步\n- 无", state, None)
    assert "需要你" not in text
    assert text.splitlines() == ["完成：改好了", "改动：1 个文件、1 处 Diff", "验证：pytest -q（退出码 0）"]


# ── 暂停与取消是两种语义（执行节点）────────────────────────────────────────
# 只看一个 is_cancelled 布尔时，暂停只能被冒充成取消，用户会以为活儿作废了。


class _Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def add(self, kind: str, node: str, payload: dict | None = None) -> None:
        self.events.append(kind)


def _fake_session(status: str, *, cancelled: bool = False):
    from types import SimpleNamespace

    async def current_status() -> str:
        return status

    async def is_cancelled() -> bool:
        return cancelled

    return SimpleNamespace(
        recorder=_Recorder(), current_status=current_status, is_cancelled=is_cancelled
    )


async def test_execute_action_stops_on_pause_without_treating_it_as_cancel():
    from blank_api.services.agent import graph

    session = _fake_session("paused")
    out = await graph.execute_action_node({"steps": 3, "pending": [{"call": {}}]}, session)
    assert out["status"] == "paused"
    assert out["pending"] == []
    assert session.recorder.events == ["task.paused"]


async def test_execute_action_stops_on_cancel():
    from blank_api.services.agent import graph

    session = _fake_session("running", cancelled=True)
    out = await graph.execute_action_node({"steps": 1, "pending": []}, session)
    assert out["status"] == "cancelled"
    assert session.recorder.events == ["task.cancelled"]


# ── 审批卡有效期（GUI 验收发现的真缺陷）──────────────────────────────────────
# 原先写死 5 分钟：人离开工位回来，卡片还在、按钮还能点，点下去 409，
# 任务永远停在 waiting_approval，界面一直显示「进行中」且没有重试入口。


def test_approval_ttl_is_configurable_and_clamped(monkeypatch):
    from blank_api.services.agent.graph import approval_ttl_seconds

    monkeypatch.delenv("BLANK_APPROVAL_TTL_SECONDS", raising=False)
    assert approval_ttl_seconds() == 1800, "默认给够半小时，别逼着人守在屏幕前"
    monkeypatch.setenv("BLANK_APPROVAL_TTL_SECONDS", "60")
    assert approval_ttl_seconds() == 60
    monkeypatch.setenv("BLANK_APPROVAL_TTL_SECONDS", "999999")
    assert approval_ttl_seconds() == 86400, "再长也不能超过一天"
    monkeypatch.setenv("BLANK_APPROVAL_TTL_SECONDS", "不是数字")
    assert approval_ttl_seconds() == 1800, "读不懂就回默认，不能抛"


async def test_expire_stale_approvals_fails_only_the_expired(monkeypatch):
    from sqlalchemy import update as sa_update

    from blank_api import worker

    monkeypatch.setenv("BLANK_APPROVAL_TTL_SECONDS", "1800")
    monkeypatch.setattr(worker, "is_enabled", lambda name: True)

    stale_id, _ = await _seed(status="waiting_approval")
    fresh_id, _ = await _seed(status="waiting_approval")
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1800 + 120)
    async with get_session_factory()() as session:
        # 用 Core update 显式给值：ORM flush 会被 onupdate 覆盖回 now
        await session.execute(sa_update(Task).where(Task.id == stale_id).values(updated_at=past))
        await session.commit()

    assert await worker.expire_stale_approvals() == 1

    async with get_session_factory()() as session:
        stale = await session.get(Task, stale_id)
        fresh = await session.get(Task, fresh_id)
        events = list(
            (await session.execute(select(AgentEvent).where(AgentEvent.task_id == stale_id))).scalars()
        )
    assert stale.status == "failed"
    assert "审批已过期" in stale.failure_reason
    assert "重试" in stale.failure_reason, "错误话术要告诉用户下一步能做什么"
    assert fresh.status == "waiting_approval", "没过期的一个都不许动"
    assert any(event.type == "approval.expired" for event in events)


async def test_expire_stale_approvals_respects_the_switch(monkeypatch):
    from blank_api import worker

    monkeypatch.setattr(worker, "is_enabled", lambda name: False)
    monkeypatch.setenv("BLANK_APPROVAL_TTL_SECONDS", "60")
    stale_id, _ = await _seed(status="waiting_approval")
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=3600)
    from sqlalchemy import update as sa_update

    async with get_session_factory()() as session:
        await session.execute(sa_update(Task).where(Task.id == stale_id).values(updated_at=past))
        await session.commit()
    assert await worker.expire_stale_approvals() == 0
    async with get_session_factory()() as session:
        assert (await session.get(Task, stale_id)).status == "waiting_approval"
