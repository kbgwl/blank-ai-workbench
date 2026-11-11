"""Q 批第 5a 项：CronCreate/Delete/List 与 ScheduleWakeup 对齐 Claude。

判据分四层：

- 时间语义：5 段本地 cron、日/星期 OR、抖动确定性、7 天期限；
- 工具面：四个工具常驻、风险档、只读白名单、并行安全、中文别名可检索；
- 工具行为：登记/列出/取消、唤醒夹取、stop、noop 连击、会话内任务作废；
- 接线：到点懒建任务、执行路径仍只有 launch_task、重复任务重排后 job id 不变。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from blank_api import worker as worker_module
from blank_api.db import get_session_factory
from blank_api.models.run import AgentRun
from blank_api.models.task import Task
from blank_api.models.trigger import AgentTrigger
from blank_api.services.agent import cron as cron_module
from blank_api.services.agent import triggers as trg
from blank_api.services.permissions import READ_ONLY_TOOLS
from blank_api.services.subagent import READ_ONLY_TOOLS as SUBAGENT_READ_ONLY_TOOLS
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.cron import WAKEUP_DEDUPE_PREFIX, cron_tools
from blank_api.services.tools.registry import PARALLEL_SAFE_TOOLS, ToolRegistry

from tests.test_agent_v04 import (  # noqa: F401  (env 是端到端夹具)
    API,
    create_project,
    create_provider,
    env,
)

TOOLS = {tool.name: tool for tool in cron_tools()}


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def add(self, event: str, phase: str, payload: dict) -> None:
        self.events.append((event, phase, payload))


async def _make_task(env, project_id: str, provider_id: str, *, scenario: str = "dev") -> str:
    resp = await env.client.post(
        f"{API}/projects/{project_id}/tasks",
        json={
            "title": "定时任务宿主",
            "goal": "占位：本用例只验证定时工具",
            "provider_id": provider_id,
            "scenario": scenario,
            "max_steps": 3,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _context(task_id: str, root: Path) -> tuple[ToolContext, _Recorder]:
    recorder = _Recorder()
    context = ToolContext(
        root=root,
        task_id=task_id,
        session_factory=get_session_factory(),
        recorder=recorder,
    )
    return context, recorder


async def _row_by_dedupe(key: str) -> AgentTrigger:
    async with get_session_factory()() as session:
        return (
            await session.execute(
                select(AgentTrigger).where(AgentTrigger.dedupe_key == key)
            )
        ).scalar_one()


async def _pending_cron_rows() -> list[AgentTrigger]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(AgentTrigger).where(
                    AgentTrigger.event_type == trg.EVENT_SCHEDULE,
                    AgentTrigger.status == trg.STATUS_PENDING,
                )
            )
        ).scalars().all()
    return [row for row in rows if dict(row.payload or {}).get("kind") == "cron"]


async def _run_count() -> int:
    async with get_session_factory()() as session:
        return len((await session.execute(select(AgentRun))).scalars().all())


# --------------------------------------------------------------------------- #
# 1. 纯函数：解析、下一次、抖动
# --------------------------------------------------------------------------- #
def test_parse_accepts_standard_five_fields_and_maps_sunday_7_to_0() -> None:
    spec = cron_module.parse("0 9 * * 7")
    assert spec.minutes == frozenset({0})
    assert spec.hours == frozenset({9})
    assert spec.weekdays == frozenset({0})
    assert spec.weekday_restricted is True


@pytest.mark.parametrize(
    "expr, needle",
    [
        ("", "不能为空"),
        ("0 9 * *", "5 段"),
        ("0 9 * * * *", "5 段"),
        ("60 9 * * *", "分钟段超出范围"),
        ("0 24 * * *", "小时段超出范围"),
        ("*/0 * * * *", "步长"),
        ("1,,2 * * * *", "空的取值"),
        ("5-1 * * * *", "起点大于终点"),
    ],
)
def test_parse_rejects_broken_expressions_with_a_specific_reason(expr, needle) -> None:
    with pytest.raises(cron_module.CronError) as error:
        cron_module.parse(expr)
    assert needle in str(error.value)


def test_day_of_month_and_day_of_week_are_ored_when_both_restricted() -> None:
    spec = cron_module.parse("0 0 13 * 5")  # 13 号 或 星期五
    assert spec.day_matches(datetime(2026, 11, 13).date()) is True  # 周五
    assert spec.day_matches(datetime(2026, 12, 13).date()) is True  # 周日但 13 号
    assert spec.day_matches(datetime(2026, 11, 12).date()) is False


def test_next_local_is_strictly_after_and_walks_months() -> None:
    spec = cron_module.parse("30 14 28 2 *")
    start = datetime(2026, 11, 11, 10, 0)
    assert cron_module.next_local(spec, start) == datetime(2027, 2, 28, 14, 30)
    # 正好落在匹配点上也必须再往后找：重排时用它算"下一跳"，返回自己会无限自燃。
    assert cron_module.next_local(spec, datetime(2027, 2, 28, 14, 30)) == datetime(2028, 2, 28, 14, 30)


def test_next_local_gives_up_instead_of_looping_forever() -> None:
    with pytest.raises(cron_module.CronError):
        cron_module.next_local(cron_module.parse("0 0 30 2 *"), datetime(2026, 1, 1))


def test_recurring_jitter_is_deterministic_and_bounded() -> None:
    spec = cron_module.parse("*/5 * * * *")
    fire = datetime(2026, 11, 11, 10, 0)
    first = cron_module.jittered(fire, job_id="job-1", recurring=True, spec=spec)
    again = cron_module.jittered(fire, job_id="job-1", recurring=True, spec=spec)
    assert first == again, "同一 (job, 到点) 必须得到同一时间，否则测试与用户都不可复现"
    delay = (first - fire).total_seconds()
    assert 0 <= delay <= 30  # 5 分钟周期的 10%
    # 另一个 job 不该恰好永远落在同一个偏移上（抖动真的由 id 决定）
    assert cron_module.jittered(fire, job_id="job-2", recurring=True, spec=spec) != first


def test_oneshot_jitter_only_moves_half_hour_marks_earlier() -> None:
    spec = cron_module.parse("0 9 * * *")
    top_of_hour = cron_module.jittered(
        datetime(2026, 11, 11, 9, 0), job_id="j1", recurring=False, spec=spec
    )
    early = (datetime(2026, 11, 11, 9, 0) - top_of_hour).total_seconds()
    assert 0 <= early <= cron_module.MAX_ONESHOT_EARLY_SECONDS
    odd = cron_module.jittered(
        datetime(2026, 11, 11, 9, 7), job_id="j1", recurring=False, spec=spec
    )
    assert odd == datetime(2026, 11, 11, 9, 7)


# --------------------------------------------------------------------------- #
# 2. 工具面
# --------------------------------------------------------------------------- #
def test_all_four_tools_are_on_the_default_surface() -> None:
    tools = {tool.name: tool for tool in build_default_tools()}
    assert {"cron_create", "cron_delete", "cron_list", "schedule_wakeup"} <= set(tools)
    assert tools["cron_create"].risk_level == "medium"
    assert tools["schedule_wakeup"].risk_level == "medium"
    assert tools["cron_list"].requires_approval is False
    assert tools["cron_create"].input_schema["required"] == ["cron", "prompt"]
    assert tools["schedule_wakeup"].input_schema["required"] == []


def test_read_only_and_parallel_and_search_views_agree() -> None:
    assert "cron_list" in READ_ONLY_TOOLS
    assert "cron_list" in PARALLEL_SAFE_TOOLS
    assert "cron_create" not in PARALLEL_SAFE_TOOLS
    # 子代理是只读白名单：定时登记会改库，不该出现在那里。
    assert "cron_create" not in SUBAGENT_READ_ONLY_TOOLS
    assert "schedule_wakeup" not in SUBAGENT_READ_ONLY_TOOLS
    registry = ToolRegistry(build_default_tools())
    matched, _ = registry.search("提醒")
    assert "cron_create" in [tool.name for tool in matched]


# --------------------------------------------------------------------------- #
# 3. 工具行为
# --------------------------------------------------------------------------- #
async def test_cron_create_stores_a_recurring_schedule_with_a_seven_day_deadline(env, workspace):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await _make_task(env, project_id, provider_id)
    context, recorder = _context(task_id, workspace)

    outcome = await TOOLS["cron_create"].handler(
        context, {"cron": "*/5 * * * *", "prompt": "检查部署状态", "recurring": True}
    )
    assert outcome.ok, outcome.message
    job_id = outcome.data["job_id"]
    row = await _row_by_dedupe(job_id)

    now = trg.utc_now()
    assert row.fire_at is not None and row.fire_at > now
    assert row.fire_at <= now + timedelta(minutes=6)
    assert row.status == trg.STATUS_PENDING
    body = dict(row.payload)
    assert body["kind"] == "cron"
    assert body["job_id"] == job_id
    assert body["prompt"] == "检查部署状态"
    assert body["repeat"] == {"kind": "cron", "expr": "*/5 * * * *"}
    assert body["durable"] is False
    assert row.expires_at == row.fire_at + cron_module.RECURRING_LIFETIME + cron_module.EXPIRY_GRACE
    assert datetime.fromisoformat(body["ends_at"]) == row.fire_at + cron_module.RECURRING_LIFETIME
    assert recorder.events[-1][0] == "cron.created"


async def test_cron_create_oneshot_has_a_one_day_catchup_window(env, workspace):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await _make_task(env, project_id, provider_id)
    context, _ = _context(task_id, workspace)

    outcome = await TOOLS["cron_create"].handler(
        context, {"cron": "0 3 1 1 *", "prompt": "元旦提醒", "recurring": False}
    )
    assert outcome.ok, outcome.message
    row = await _row_by_dedupe(outcome.data["job_id"])
    assert row.expires_at == row.fire_at + cron_module.ONESHOT_CATCHUP
    assert "repeat" not in row.payload
    assert "只触发一次" in outcome.output


async def test_cron_create_rejects_bad_arguments_without_writing(env, workspace):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await _make_task(env, project_id, provider_id)
    context, _ = _context(task_id, workspace)

    assert not (await TOOLS["cron_create"].handler(context, {"cron": "nope", "prompt": "x"})).ok
    assert not (await TOOLS["cron_create"].handler(context, {"cron": "* * * * *", "prompt": "  "})).ok
    async with get_session_factory()() as session:
        rows = (await session.execute(select(AgentTrigger))).scalars().all()
    assert list(rows) == []


async def test_cron_list_and_delete_use_the_stable_job_id(env, workspace):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await _make_task(env, project_id, provider_id)
    context, _ = _context(task_id, workspace)

    created = await TOOLS["cron_create"].handler(
        context, {"cron": "0 9 * * 1-5", "prompt": "长" * 200, "durable": True}
    )
    assert created.ok, created.message
    job_id = created.data["job_id"]

    listed = await TOOLS["cron_list"].handler(context, {})
    assert listed.ok
    assert listed.data["count"] == 1
    job = listed.data["jobs"][0]
    assert job["job_id"] == job_id
    assert job["cron"] == "0 9 * * 1-5"
    assert job["durable"] is True
    assert len(job["prompt"]) == 121 and job["prompt"].endswith("…")

    deleted = await TOOLS["cron_delete"].handler(context, {"id": job_id})
    assert deleted.ok, deleted.message
    row = await _row_by_dedupe(job_id)
    assert row.status == trg.STATUS_CANCELLED
    assert (await TOOLS["cron_list"].handler(context, {})).data["count"] == 0
    assert not (await TOOLS["cron_delete"].handler(context, {"id": job_id})).ok


async def test_wakeup_clamps_delay_and_reuses_the_same_row(env, workspace):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await _make_task(env, project_id, provider_id)
    context, recorder = _context(task_id, workspace)

    low = await TOOLS["schedule_wakeup"].handler(
        context, {"delaySeconds": 1, "reason": "等部署", "prompt": "再看一次"}
    )
    assert low.ok and low.data["delay_seconds"] == 60
    row = await _row_by_dedupe(f"{WAKEUP_DEDUPE_PREFIX}{task_id}")
    assert row.payload["noop_streak"] == 0
    remaining = (row.fire_at - trg.utc_now()).total_seconds()
    assert 55 <= remaining <= 65

    noop = await TOOLS["schedule_wakeup"].handler(
        context, {"delaySeconds": 99999, "reason": "没变化", "prompt": "再看一次", "noop": True}
    )
    assert noop.ok and noop.data["delay_seconds"] == 3600
    assert noop.data["job_id"] == low.data["job_id"], "唤醒是一轮循环，不该每次新建一行"
    again = await TOOLS["schedule_wakeup"].handler(
        context, {"delaySeconds": 120, "reason": "还没变化", "prompt": "再看一次", "noop": True}
    )
    assert again.data["job_id"] == low.data["job_id"]
    assert again.data["noop_streak"] == 2

    stopped = await TOOLS["schedule_wakeup"].handler(context, {"stop": True})
    assert stopped.ok and stopped.data["stopped"] is True
    row = await _row_by_dedupe(f"{WAKEUP_DEDUPE_PREFIX}{task_id}")
    assert row.status == trg.STATUS_CANCELLED
    second = await TOOLS["schedule_wakeup"].handler(context, {"stop": True})
    assert second.ok and second.data["stopped"] is False
    assert recorder.events[-1][0] == "wakeup.stopped"


async def test_wakeup_after_a_fired_tick_starts_the_next_tick(env, workspace):
    """上一拍已经 fired 但行还没过期：下一次调度必须真正写进去。

    ingest 对同 key 的未过期行会原样返回，唤醒循环若直接沿用它，第二拍会被
    静默吞掉——界面上是「设了下一拍，然后什么也没发生」。
    """
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await _make_task(env, project_id, provider_id)
    context, _ = _context(task_id, workspace)

    first = await TOOLS["schedule_wakeup"].handler(
        context, {"delaySeconds": 300, "reason": "等第一轮", "prompt": "继续"}
    )
    assert first.ok
    async with get_session_factory()() as session:
        row = await session.get(AgentTrigger, first.data["job_id"])
        row.status = trg.STATUS_FIRED
        await session.commit()

    second = await TOOLS["schedule_wakeup"].handler(
        context, {"delaySeconds": 600, "reason": "等第二轮", "prompt": "继续"}
    )
    assert second.ok
    assert second.data["job_id"] == first.data["job_id"]
    row = await _row_by_dedupe(f"{WAKEUP_DEDUPE_PREFIX}{task_id}")
    assert row.status == trg.STATUS_PENDING, "fired 的行被原样复用，下一拍等于没排"
    assert row.payload["reason"] == "等第二轮"
    assert row.payload["delay_seconds"] == 600
    remaining = (row.fire_at - trg.utc_now()).total_seconds()
    assert 590 <= remaining <= 610


async def test_wakeup_requires_reason_and_prompt(env, workspace):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await _make_task(env, project_id, provider_id)
    context, _ = _context(task_id, workspace)

    missing_reason = await TOOLS["schedule_wakeup"].handler(
        context, {"delaySeconds": 300, "prompt": "继续"}
    )
    assert not missing_reason.ok and "reason" in missing_reason.message
    missing_prompt = await TOOLS["schedule_wakeup"].handler(
        context, {"delaySeconds": 300, "reason": "等结果"}
    )
    assert not missing_prompt.ok and "prompt" in missing_prompt.message


# --------------------------------------------------------------------------- #
# 4. worker 接线：到点懒建任务、重排、忙时顺延、会话作废
# --------------------------------------------------------------------------- #
async def _make_cron(env, workspace, **arguments) -> tuple[str, str, str]:
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await _make_task(env, project_id, provider_id)
    context, _ = _context(task_id, workspace)
    outcome = await TOOLS["cron_create"].handler(context, {"cron": "*/5 * * * *", **arguments})
    assert outcome.ok, outcome.message
    return project_id, task_id, outcome.data["job_id"]


async def _make_due(job_id: str, *, deadline: datetime | None = None) -> str:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(AgentTrigger).where(AgentTrigger.dedupe_key == job_id)
            )
        ).scalar_one()
        row.fire_at = trg.utc_now() - timedelta(seconds=5)
        if deadline is not None:
            row.payload = {**dict(row.payload or {}), "ends_at": deadline.isoformat()}
        await session.commit()
        return row.id


async def test_recurring_fire_creates_the_task_lazily_and_reschedules(env, workspace):
    project_id, host_task, job_id = await _make_cron(env, workspace, prompt="每周检查依赖")
    trigger_id = await _make_due(job_id)

    assert await worker_module.release_one(trigger_id, start_runner=False) is True
    row = await _row_by_dedupe(job_id)
    assert row.status == trg.STATUS_FIRED
    assert row.task_id, "第一次到点必须把任务建出来"

    async with get_session_factory()() as session:
        task = await session.get(Task, row.task_id)
    assert task is not None
    assert task.goal == "每周检查依赖" and task.title == "每周检查依赖"
    assert task.id != host_task
    assert await _run_count() == 1

    pending = await _pending_cron_rows()
    assert len(pending) == 1, "重复任务放行后必须登记下一次"
    nxt = pending[0]
    assert nxt.fire_at > trg.utc_now()
    assert nxt.payload["job_id"] == job_id, "重排后的行必须还挂在同一个 job id 上"
    assert nxt.dedupe_key.startswith(f"{job_id}:")
    assert nxt.task_id == row.task_id, "下一跳沿同一条任务继续跑"

    # 重排之后仍能用最初那个 job id 取消——行主键变了也不影响。
    context, _ = _context(host_task, workspace)
    deleted = await TOOLS["cron_delete"].handler(context, {"id": job_id})
    assert deleted.ok, deleted.message
    assert await _pending_cron_rows() == []


async def test_oneshot_fire_does_not_reschedule(env, workspace):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await _make_task(env, project_id, provider_id)
    context, _ = _context(task_id, workspace)
    created = await TOOLS["cron_create"].handler(
        context, {"cron": "0 3 1 1 *", "prompt": "元旦提醒", "recurring": False}
    )
    assert created.ok
    trigger_id = await _make_due(created.data["job_id"])

    assert await worker_module.release_one(trigger_id, start_runner=False) is True
    assert await _pending_cron_rows() == []


async def test_busy_bound_task_defers_instead_of_swallowing_the_reminder(env, workspace):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await _make_task(env, project_id, provider_id)
    context, _ = _context(task_id, workspace)
    wake = await TOOLS["schedule_wakeup"].handler(
        context, {"delaySeconds": 120, "reason": "等 build", "prompt": "检查 build"}
    )
    assert wake.ok
    trigger_id = wake.data["job_id"]

    async with get_session_factory()() as session:
        row = await session.get(AgentTrigger, trigger_id)
        row.fire_at = trg.utc_now() - timedelta(seconds=5)
        task = await session.get(Task, task_id)
        task.status = "running"
        await session.commit()

    assert await worker_module.release_one(trigger_id, start_runner=False) is False
    async with get_session_factory()() as session:
        row = await session.get(AgentTrigger, trigger_id)
    assert row.status == trg.STATUS_PENDING, "任务忙不是'这次不提醒'，不能标记 fired"
    remaining = (row.fire_at - trg.utc_now()).total_seconds()
    assert cron_module.BUSY_DEFER_SECONDS - 5 <= remaining <= cron_module.BUSY_DEFER_SECONDS + 5
    assert await _run_count() == 0


async def test_recurring_stops_rescheduling_past_the_creation_deadline(env, workspace):
    project_id, host_task, job_id = await _make_cron(env, workspace, prompt="快到期")
    trigger_id = await _make_due(job_id, deadline=trg.utc_now() - timedelta(minutes=1))

    assert await worker_module.release_one(trigger_id, start_runner=False) is True
    assert await _pending_cron_rows() == [], "越过 7 天期限后不再登记，最后一跳照常跑完"


async def test_discard_session_scoped_jobs_keeps_durable_and_unrelated_triggers(env, workspace):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await _make_task(env, project_id, provider_id)
    context, _ = _context(task_id, workspace)

    session_cron = await TOOLS["cron_create"].handler(
        context, {"cron": "0 9 * * *", "prompt": "会话内提醒"}
    )
    durable_cron = await TOOLS["cron_create"].handler(
        context, {"cron": "0 10 * * *", "prompt": "跨重启提醒", "durable": True}
    )
    wake = await TOOLS["schedule_wakeup"].handler(
        context, {"delaySeconds": 300, "reason": "等结果", "prompt": "继续"}
    )
    assert session_cron.ok and durable_cron.ok and wake.ok
    async with get_session_factory()() as session:
        await trg.ingest(
            session,
            project_id,
            trg.normalize(
                event_type="task_failed",
                source="watchdog",
                payload={"task_id": "t-x", "status": "failed"},
                dedupe_key="unrelated-1",
            ),
            task_id="",
        )
        await session.commit()

    dropped = await worker_module.discard_session_scoped_jobs()
    assert dropped == 2  # 非持久 cron + 唤醒
    assert (await _row_by_dedupe(session_cron.data["job_id"])).status == trg.STATUS_CANCELLED
    assert (await _row_by_dedupe(f"{WAKEUP_DEDUPE_PREFIX}{task_id}")).status == trg.STATUS_CANCELLED
    assert (await _row_by_dedupe(durable_cron.data["job_id"])).status == trg.STATUS_PENDING
    assert (await _row_by_dedupe("unrelated-1")).status == trg.STATUS_PENDING
