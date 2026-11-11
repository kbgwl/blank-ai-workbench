"""定时工具：cron_create / cron_delete / cron_list / schedule_wakeup。

对齐 Claude 的 CronCreate / CronDelete / CronList / ScheduleWakeup。本地没有
「REPL 会话」这层对象，所以把语义落在既有的触发器链路上：

* **存一行 ``AgentTrigger``（event_type=schedule）**，而不是新造一张表。到点放行
  仍然走 worker 的 ``release_due_triggers``，执行路径始终只有 ``launch_task`` 一条；
  这一层只负责"什么时候、跑什么"，不碰执行。
* **第一次到点才建任务**：prompt 就是任务的 goal。提前建会让任务列表里多出一条
  从没跑过的草稿；到点再建，用户看到的第一条记录就是真的跑过的那次。
* **重复任务放行后登记下一条**（``worker._reschedule_repeat`` 已支持 daily/weekly，
  这里补 cron 分支）：同一条记录反复改回 pending 会让「第几次」不可考。
* **非持久任务在进程重启时作废**：Claude 的 durable=false 只活在当前会话里。
  本地用「API 进程启动时取消 pending 的非持久 cron」近似，理由与边界写在
  ``worker.discard_session_cron_jobs``。
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from blank_api.models.task import Task
from blank_api.models.trigger import AgentTrigger
from blank_api.services.agent import cron as cron_module
from blank_api.services.agent import triggers as triggers_module
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok

# 列表里每条 prompt 只带开头这么多字符：这是给模型看的索引，不是任务正文。
PROMPT_PREVIEW_CHARS = 120
WAKEUP_MIN_SECONDS = 60
WAKEUP_MAX_SECONDS = 3600
WAKEUP_DEDUPE_PREFIX = "wakeup:"


def _flag(arguments: dict, key: str, default: bool) -> bool:
    value = arguments.get(key)
    if value is None:
        return default
    return bool(value)


def _text(arguments: dict, key: str) -> str:
    value = arguments.get(key)
    return value.strip() if isinstance(value, str) else ""


async def _record(context: ToolContext, event: str, payload: dict[str, Any]) -> None:
    recorder = getattr(context, "recorder", None)
    if recorder is not None:
        await recorder.add(event, "cron", payload)


async def _current_task(session, context: ToolContext) -> Task | None:
    if not context.task_id:
        return None
    return await session.get(Task, context.task_id)


def _run_payload(task: Task) -> dict[str, str]:
    body: dict[str, str] = {}
    if task.provider_id:
        body["provider_id"] = task.provider_id
    if task.scenario:
        body["scenario"] = task.scenario
    return body


def _cron_payload(job_id: str, arguments: dict, task: Task) -> dict[str, Any]:
    expr = _text(arguments, "cron")
    recurring = _flag(arguments, "recurring", True)
    body: dict[str, Any] = {
        "kind": cron_module.KIND_CRON,
        "job_id": job_id,
        "cron": expr,
        "prompt": _text(arguments, "prompt"),
        "recurring": recurring,
        "durable": _flag(arguments, "durable", False),
        "scenario": task.scenario or "",
        "created_by_task": task.id,
    }
    if recurring:
        # worker 只看 repeat 决定是否登记下一次；cron 的下一跳由本模块的纯函数算。
        body["repeat"] = {"kind": cron_module.KIND_CRON, "expr": expr}
    return body

async def _cron_create(context: ToolContext, arguments: dict) -> ToolOutcome:
    if context.session_factory is None:
        return fail("error", "定时任务需要数据库支持：当前会话没有可用的会话工厂")
    expr = _text(arguments, "cron")
    prompt = _text(arguments, "prompt")
    if not prompt:
        return fail("error", "prompt 不能为空：到点要执行的内容必须写清楚")
    try:
        spec = cron_module.parse(expr)
    except cron_module.CronError as error:
        return fail("error", str(error))

    recurring = _flag(arguments, "recurring", True)
    durable = _flag(arguments, "durable", False)
    job_id = f"cron-{uuid.uuid4().hex[:10]}"
    offset = cron_module.local_offset()
    now = triggers_module.utc_now()
    try:
        first_local = cron_module.next_local(spec, now + offset)
    except cron_module.CronError as error:
        return fail("error", str(error))
    fire_at = cron_module.jittered(
        first_local - offset,
        job_id=job_id,
        recurring=recurring,
        spec=spec,
        offset=offset,
    )
    # 一次性任务只留一天补跑窗口：进程停机错过还能补一次，但一条几周前的提醒
    # 突然开跑比不跑更吓人。重复任务的过期窗口覆盖到下一次到点之后。
    if recurring:
        expires_at = fire_at + cron_module.RECURRING_LIFETIME + cron_module.EXPIRY_GRACE
    else:
        expires_at = fire_at + cron_module.ONESHOT_CATCHUP
    # 抖动种子必须是排定出去的那串 id，后续每次重排也沿用同一串，才不会每次重启
    # 都把整条任务的时间挪一遍。

    async with context.session_factory() as session:
        task = await _current_task(session, context)
        if task is None:
            return fail("error", "找不到当前任务，无法确定要挂到哪个项目下")
        payload = _cron_payload(job_id, arguments, task)
        if recurring:
            # 7 天期限从"排定这一刻"算，不能跟着每次重排往后滚；worker 重排时
            # 拿这份 payload 判断有没有越过期限。
            payload["ends_at"] = (fire_at + cron_module.RECURRING_LIFETIME).isoformat()
        event = triggers_module.normalize(
            event_type=triggers_module.EVENT_SCHEDULE,
            source="cron",
            payload=payload,
            dedupe_key=job_id,
            expires_at=expires_at,
        )
        row, _ = await triggers_module.ingest(
            session,
            task.project_id,
            event,
            task_id="",
            provider_id=task.provider_id or "",
            run_payload=_run_payload(task),
            fire_at=fire_at,
        )
        # 对外只认 job_id（排定那一刻生成的稳定串）：重排出来的行 id 每次都不一样，
        # 拿行 id 当"任务编号"会让 cron_delete 在第二跳之后找不到人。
        await session.commit()

    await _record(
        context,
        "cron.created",
        {
            "job_id": job_id,
            "cron": expr,
            "recurring": recurring,
            "durable": durable,
            "fire_at": fire_at.isoformat(),
            "local_time": cron_module.format_local(fire_at, offset),
        },
    )
    when = cron_module.format_local(fire_at, offset)
    repeat_note = "（重复，7 天后自动结束）" if recurring else "（只触发一次）"
    return ok(
        f"已排定定时任务 {job_id}：下一次 {when}{repeat_note}。",
        job_id=job_id,
        cron=expr,
        fire_at=fire_at.isoformat(),
        local_time=when,
        recurring=recurring,
        durable=durable,
    )


async def _pending_cron_rows(session, project_id: str, job_id: str | None = None):
    """本项目里仍可能被取消的 cron 行（按 payload.job_id 过滤）。

    ``job_id`` 是 payload 里那串稳定 id，不是行主键：重复任务每跳都新登记一行，
    行 id 每跳都变。payload 是 JSON，跨 SQLite/MySQL 的 JSON 查询写法不一致，
    所以先用事件类型/状态收窄，再在 Python 里按 payload 过滤。
    """
    rows = (
        await session.execute(
            select(AgentTrigger)
            .where(AgentTrigger.project_id == project_id)
            .where(AgentTrigger.event_type == triggers_module.EVENT_SCHEDULE)
            .where(
                AgentTrigger.status.in_(
                    (triggers_module.STATUS_PENDING, triggers_module.STATUS_PAUSED)
                )
            )
            .order_by(AgentTrigger.fire_at)
        )
    ).scalars().all()
    found = []
    for row in rows:
        body = dict(row.payload or {})
        if body.get("kind") != cron_module.KIND_CRON:
            continue
        if job_id is not None and str(body.get("job_id") or "") != job_id:
            continue
        found.append(row)
    return found


async def _cron_delete(context: ToolContext, arguments: dict) -> ToolOutcome:
    if context.session_factory is None:
        return fail("error", "定时任务需要数据库支持：当前会话没有可用的会话工厂")
    job_id = _text(arguments, "id")
    if not job_id:
        return fail("error", "id 不能为空：用 cron_list 里的任务 id")
    async with context.session_factory() as session:
        task = await _current_task(session, context)
        if task is None:
            return fail("error", "找不到当前任务，无法确定要操作哪个项目")
        rows = await _pending_cron_rows(session, task.project_id, job_id)
        if not rows:
            return fail("error", f"找不到仍有效的定时任务 {job_id}：先用 cron_list 确认 id")
        previous = rows[0].status
        for row in rows:
            row.status = triggers_module.STATUS_CANCELLED
            row.payload = {**dict(row.payload or {}), "cancelled_by_task": task.id}
        await session.commit()

    await _record(context, "cron.deleted", {"job_id": job_id, "previous_status": previous})
    return ok(f"已取消定时任务 {job_id}。", job_id=job_id, previous_status=previous)


async def _cron_list(context: ToolContext, arguments: dict) -> ToolOutcome:
    if context.session_factory is None:
        return fail("error", "定时任务需要数据库支持：当前会话没有可用的会话工厂")
    async with context.session_factory() as session:
        task = await _current_task(session, context)
        if task is None:
            return fail("error", "找不到当前任务，无法确定要列出哪个项目的定时任务")
        rows = await _pending_cron_rows(session, task.project_id)

    offset = cron_module.local_offset()
    jobs: list[dict[str, Any]] = []
    for row in rows:
        body = dict(row.payload or {})
        prompt = str(body.get("prompt") or "")
        preview = prompt if len(prompt) <= PROMPT_PREVIEW_CHARS else prompt[:PROMPT_PREVIEW_CHARS] + "…"
        fire_at = row.fire_at
        jobs.append(
            {
                "job_id": str(body.get("job_id") or row.id),
                "cron": body.get("cron") or "",
                "prompt": preview,
                "recurring": bool(body.get("recurring", True)),
                "durable": bool(body.get("durable", False)),
                "status": row.status,
                "next_fire": cron_module.format_local(fire_at, offset) if fire_at else "",
            }
        )

    if not jobs:
        return ok("当前没有定时任务。", count=0, jobs=[])
    lines = [f"当前有 {len(jobs)} 个定时任务："]
    for job in jobs:
        kind = "重复" if job["recurring"] else "一次"
        scope = "持久" if job["durable"] else "本次会话"
        lines.append(
            f"- {job['job_id']}｜{job['cron']}｜{kind}/{scope}｜下一次 {job['next_fire']}"
            f"｜{job['prompt']}"
        )
    return ok("\n".join(lines), count=len(jobs), jobs=jobs)


def _wakeup_seconds(arguments: dict) -> tuple[int | None, str | None]:
    value = arguments.get("delaySeconds", arguments.get("delay_seconds"))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "delaySeconds 必须是数字（秒）"
    clamped = int(min(WAKEUP_MAX_SECONDS, max(WAKEUP_MIN_SECONDS, int(value))))
    return clamped, None


async def _schedule_wakeup(context: ToolContext, arguments: dict) -> ToolOutcome:
    if context.session_factory is None:
        return fail("error", "唤醒需要数据库支持：当前会话没有可用的会话工厂")
    async with context.session_factory() as session:
        task = await _current_task(session, context)
        if task is None:
            return fail("error", "找不到当前任务，无法安排唤醒")

        dedupe_key = f"{WAKEUP_DEDUPE_PREFIX}{task.id}"
        existing = (
            await session.execute(
                select(AgentTrigger)
                .where(AgentTrigger.project_id == task.project_id)
                .where(AgentTrigger.dedupe_key == dedupe_key)
                .where(
                    AgentTrigger.status.in_(
                        (triggers_module.STATUS_PENDING, triggers_module.STATUS_PAUSED)
                    )
                )
                .limit(1)
            )
        ).scalars().first()

        if _flag(arguments, "stop", False):
            if existing is None:
                return ok("当前没有待触发的唤醒，循环已经结束。", stopped=False)
            row = existing
            row.status = triggers_module.STATUS_CANCELLED
            row.payload = {**dict(row.payload or {}), "stopped_by_task": task.id}
            await session.commit()
            await _record(context, "wakeup.stopped", {"job_id": row.id})
            return ok("已结束动态循环，不再安排唤醒。", stopped=True, job_id=row.id)

        seconds, error = _wakeup_seconds(arguments)
        if error is not None:
            return fail("error", error)
        reason = _text(arguments, "reason")
        prompt = _text(arguments, "prompt")
        if not reason:
            return fail("error", "reason 不能为空：用一句话说明为什么等这么久")
        if not prompt:
            return fail("error", "prompt 不能为空：下一次唤醒要执行的输入必须写清楚")
        noop = _flag(arguments, "noop", False)

        previous = dict(existing.payload or {}) if existing is not None else {}
        streak = int(previous.get("noop_streak") or 0) + 1 if noop else 0
        now = triggers_module.utc_now()
        fire_at = now + timedelta(seconds=seconds)
        payload = {
            "kind": cron_module.KIND_WAKEUP,
            "prompt": prompt,
            "reason": reason,
            "noop": noop,
            "noop_streak": streak,
            "delay_seconds": seconds,
            "creator_task_id": task.id,
        }
        if existing is not None:
            row = existing
            row.status = triggers_module.STATUS_PENDING
            row.fire_at = fire_at
            row.expires_at = fire_at + timedelta(hours=1)
            row.payload = payload
            job_id = row.id
        else:
            event = triggers_module.normalize(
                event_type=triggers_module.EVENT_SCHEDULE,
                source="wakeup",
                payload=payload,
                dedupe_key=dedupe_key,
                expires_at=fire_at + timedelta(hours=1),
            )
            row, _ = await triggers_module.ingest(
                session,
                task.project_id,
                event,
                task_id=task.id,
                provider_id=task.provider_id or "",
                run_payload=_run_payload(task),
                fire_at=fire_at,
            )
            # 上一轮唤醒已经 fired 但行还没过期时，ingest 会原样返回旧行（同 key 去重）。
            # 唤醒是"同一循环的下一拍"，必须把新一拍真正写进去，否则第二次调度被静默吞掉。
            row.status = triggers_module.STATUS_PENDING
            row.fire_at = fire_at
            row.expires_at = fire_at + timedelta(hours=1)
            row.payload = payload
            job_id = row.id
        await session.commit()

    await _record(
        context,
        "wakeup.scheduled",
        {
            "job_id": job_id,
            "delay_seconds": seconds,
            "fire_at": fire_at.isoformat(),
            "noop": noop,
            "noop_streak": streak,
            "reason": reason,
        },
    )
    when = cron_module.format_local(fire_at, cron_module.local_offset())
    quiet = "（无变化）" if noop else ""
    return ok(
        f"已安排在 {when} 唤醒（{seconds} 秒后）{quiet}：{reason}",
        job_id=job_id,
        delay_seconds=seconds,
        fire_at=fire_at.isoformat(),
        noop=noop,
        noop_streak=streak,
    )


def cron_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="cron_create",
            description=(
                "排定一个未来要执行的提示词（对齐 Claude CronCreate）：既能做周期任务，"
                "也能做一次性提醒。用本地时区的标准 5 段 cron：分 时 日 月 周"
                "（\"30 14 28 2 *\" = 本地 2 月 28 日 14:30 一次）。"
                "一次性任务（recurring=false）要把分/时/日/月钉成具体值，到点触发后自动结束。"
                "用户的时间是约数时避开 :00 与 :30（例如「早上 9 点左右」用 3 9 * * *，"
                "「每小时」用 7 * * * *）——整点集中触发只会让所有用户挤在同一秒。"
                "durable=false（默认）只活到本次服务进程结束；只有用户明确要求跨会话保留时才传 true。"
                "重复任务 7 天后自动结束（最后一次照常触发）；排定后要告诉用户这个期限。"
                "不要用它代替用户当下就要执行的事——那直接做。返回 job id，可交给 cron_delete。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "cron": {
                        "type": "string",
                        "description": "本地时区的 5 段 cron：\"分 时 日 月 周\"",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "每次到点要执行的内容（就是任务的 goal）",
                    },
                    "recurring": {
                        "type": "boolean",
                        "description": "true（默认）= 每次匹配都触发，7 天后结束；false = 只触发一次",
                    },
                    "durable": {
                        "type": "boolean",
                        "description": "true = 跨服务重启保留；false（默认）= 只活在本次会话",
                    },
                },
                "required": ["cron", "prompt"],
            },
            risk_level="medium",
            handler=_cron_create,
        ),
        ToolDefinition(
            name="cron_delete",
            description=(
                "取消一个用 cron_create 排定的定时任务（对齐 Claude CronDelete）。"
                "id 用 cron_list 里的 job id；已经触发或已取消的任务会明确拒绝。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "cron_create 返回的 job id"},
                },
                "required": ["id"],
            },
            risk_level="low",
            handler=_cron_delete,
        ),
        ToolDefinition(
            name="cron_list",
            description=(
                "列出当前项目里仍有效的定时任务（对齐 Claude CronList）："
                "持久与非持久任务都列出，含 cron、下一次触发时间与 prompt 预览。"
            ),
            input_schema={"type": "object", "properties": {}},
            risk_level="low",
            handler=_cron_list,
        ),
        ToolDefinition(
            name="schedule_wakeup",
            description=(
                "安排下一次继续工作的时刻（对齐 Claude ScheduleWakeup，用于动态 /loop）。"
                "delaySeconds 会被夹到 [60, 3600]；不要把服务自己跟踪的后台工作当轮询对象"
                "——它结束时会自动唤醒你，这里只留一个长的兜底（1200 秒以上）。"
                "每轮传回同一个 prompt，下一次触发才会继续同一件事。"
                "stop=true 立刻结束循环，其余字段全部忽略。"
                "noop=true 表示这一轮检查过、没有新变化；连续 noop 会被折叠展示。"
                "reason 用一句话说明为什么等这么久。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "delaySeconds": {
                        "type": "number",
                        "description": "多少秒后唤醒；运行时夹到 [60, 3600]",
                    },
                    "reason": {"type": "string", "description": "一句话说明为什么等这么久"},
                    "prompt": {"type": "string", "description": "唤醒时要执行的输入"},
                    "stop": {"type": "boolean", "description": "true = 结束循环，不再唤醒"},
                    "noop": {
                        "type": "boolean",
                        "description": "true = 这一轮没有新变化；false = 有值得保留的进展",
                    },
                },
                "required": [],
            },
            risk_level="medium",
            handler=_schedule_wakeup,
        ),
    ]


__all__ = [
    "PROMPT_PREVIEW_CHARS",
    "WAKEUP_MAX_SECONDS",
    "WAKEUP_MIN_SECONDS",
    "cron_tools",
]
