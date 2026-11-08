"""事件触发接口（v162 §5）。

对外只有三件事：上报、查询、点火。**不做周期调度**——cron 表达式解析与重复
触发规则属于 P1，先把它写进来再声称支持，等于给用户一个会静默不跑的功能。

为什么上报接口自己会建任务：触发源（文件监听、失败回调、外部 webhook）只知道
"发生了什么"，不知道"空白格要做什么"。``goal`` 由调用方给，Router 负责判派发方式，
Worker 负责执行——三段各管一件事，中间不互相猜。
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select

from blank_api.api.auth import require_token
from blank_api.api.errors import ApiError
from blank_api.api.routes.projects import load_project
from blank_api.api.routes.tasks import launch_task, persist_task
from blank_api.deps import DbSession
from blank_api.models.trigger import AgentTrigger
from blank_api.schemas.task import TaskCreate, TaskRunIn
from blank_api.schemas.trigger import TriggerAck, TriggerIn
from blank_api.services.agent import triggers
from blank_api.services.agent.events import EventRecorder
from blank_api.db import get_session_factory
from blank_api.services.features import is_enabled

router = APIRouter(tags=["triggers"], dependencies=[Depends(require_token)])

FEATURE = "background_execution"


def _require_feature() -> None:
    """开关关着时明确拒绝，而不是安静地记一条永远不会被消费的触发器。

    安静接受是最坏的一种：上报方看到 201 以为接通了，实际队列里没有任何人读它。
    """
    if not is_enabled(FEATURE):
        raise ApiError(
            code="feature_disabled",
            message="后台执行未开启：请在设置里打开「后台主动执行」（或设 BLANK_FEATURE_BACKGROUND_EXECUTION=1）",
            status_code=409,
        )


async def _ingest(db, project_id: str, payload: TriggerIn) -> dict:
    _require_feature()
    try:
        event = triggers.normalize(
            event_type=payload.event_type,
            source=payload.source,
            payload=payload.payload,
            dedupe_key=payload.dedupe_key,
            expires_at=payload.expires_at,
            window=timedelta(seconds=payload.window_seconds) if payload.window_seconds else None,
        )
    except ValueError as error:
        raise ApiError(code="bad_event", message=str(error), status_code=400) from None
    if event.expired:
        raise ApiError(
            code="event_expired",
            message="这条事件已经过期，不补做（过期的失败通知现在补跑，用户会以为是现在失败）",
            status_code=400,
        )
    if payload.provider_id:
        from blank_api.models.model_provider import ModelProvider

        if await db.get(ModelProvider, payload.provider_id) is None:
            raise ApiError(code="provider_not_found", message="所选模型已不存在", status_code=400)

    existing = await triggers.find_active(db, project_id, event.dedupe_key)
    if existing is not None:
        # 去重命中：不建新任务、不改动已有任务，只把"我已收到"回给上报方。
        return {
            "created": False,
            "trigger": triggers.as_dict(existing),
            "task_id": existing.task_id or None,
            "launched": False,
            "run_id": None,
        }

    task = None
    status = triggers.STATUS_PENDING
    if payload.goal.strip():
        task = await persist_task(
            db,
            project_id,
            TaskCreate(
                title=(payload.title or payload.goal[:60]).strip() or "事件触发",
                goal=payload.goal,
                acceptance_criteria=list(payload.acceptance_criteria),
                provider_id=payload.provider_id,
                scenario=payload.scenario or "",
                max_steps=payload.max_steps,
            ),
        )
    else:
        # 只登记事件。没有目标就没有"任务"可言，硬建一条只会多一条零产出的记录。
        status = triggers.STATUS_SKIPPED

    row, created = await triggers.ingest(
        db,
        project_id,
        event,
        task_id=task.id if task is not None else "",
        provider_id=payload.provider_id or "",
        run_payload=TaskRunIn(
            provider_id=payload.provider_id,
            memory_opt_in=payload.memory_opt_in,
        ).model_dump(mode="json"),
        fire_at=payload.fire_at,
        status=status,
    )
    await db.commit()

    launched, run_id = False, None
    due_now = row.fire_at is None or row.fire_at <= triggers.utc_now()
    if payload.autostart and task is not None and due_now:
        run = await launch_task(
            db,
            task,
            TaskRunIn(provider_id=payload.provider_id, memory_opt_in=payload.memory_opt_in),
            None,
        )
        await triggers.mark_fired(db, row)
        await db.commit()
        launched, run_id = True, run.get("run_id")

    if task is not None:
        # 没有任务就没有时间线可写（事件挂在任务上）。这种情况"收到过什么"由
        # agent_triggers 那一行承担，不硬造一条孤儿事件。
        await EventRecorder(get_session_factory(), task.id).add(
            "trigger.received",
            "receive_event",
            {
                "trigger_id": row.id,
                "event_type": event.event_type,
                "source": event.source,
                "dedupe_key": event.dedupe_key,
                "status": row.status,
                "autostart": bool(payload.autostart),
                "launched": launched,
                "task_id": task.id,
            },
        )
    return {
        "created": created,
        "trigger": triggers.as_dict(row),
        "task_id": row.task_id or None,
        "launched": launched,
        "run_id": run_id,
    }


@router.post("/projects/{project_id}/triggers", response_model=TriggerAck)
async def create_trigger(
    project_id: str, payload: TriggerIn, db: DbSession, response: Response
) -> TriggerAck:
    project = await load_project(db, project_id)
    result = await _ingest(db, project.id, payload)
    # 去重命中是 200（什么都没新建），真建了才是 201——上报方据此判断要不要重试/记日志。
    response.status_code = 201 if result["created"] else 200
    return TriggerAck(**result)


@router.get("/projects/{project_id}/triggers", response_model=list[dict])
async def list_triggers(
    project_id: str,
    db: DbSession,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    _require_feature()
    project = await load_project(db, project_id)
    stmt = (
        select(AgentTrigger)
        .where(AgentTrigger.project_id == project.id)
        .order_by(AgentTrigger.created_at.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(AgentTrigger.status == status)
    rows = (await db.execute(stmt)).scalars().all()
    return [triggers.as_dict(row) for row in rows]


@router.post("/triggers/{trigger_id}/fire", response_model=dict)
async def fire_trigger(trigger_id: str, db: DbSession) -> dict:
    """现在放行一条待触发项。定时到点走的是同一段逻辑（worker 里的 release_due）。"""
    _require_feature()
    row = await db.get(AgentTrigger, trigger_id)
    if row is None:
        raise ApiError(code="trigger_not_found", message="触发器不存在", status_code=404)
    if row.status != triggers.STATUS_PENDING:
        raise ApiError(
            code="trigger_not_pending",
            message=f"这条触发器当前状态是 {row.status}，不再放行（已经跑过或已过期）",
            status_code=409,
        )
    if row.expires_at is not None and row.expires_at <= triggers.utc_now():
        row.status = triggers.STATUS_EXPIRED
        await db.commit()
        raise ApiError(
            code="trigger_expired",
            message="这条触发器已过期，不补做",
            status_code=410,
        )
    if not (row.task_id or ""):
        raise ApiError(code="trigger_without_task", message="这条触发器没有关联任务，无法执行", status_code=409)
    from blank_api.models.task import Task

    task = await db.get(Task, row.task_id)
    if task is None:
        raise ApiError(code="task_not_found", message="关联任务已不存在", status_code=409)
    if task.status in ("running", "queued"):
        raise ApiError(code="task_running", message="关联任务正在执行中", status_code=409)
    run = await launch_task(db, task, TaskRunIn.model_validate(row.run_payload or {}), None)
    await triggers.mark_fired(db, row)
    await db.commit()
    await EventRecorder(get_session_factory(), task.id).add(
        "trigger.fired",
        "receive_event",
        {"trigger_id": row.id, "run_id": run.get("run_id"), "source": "manual"},
    )
    return {"trigger_id": row.id, "task_id": task.id, "run_id": run.get("run_id"), "status": row.status}


# --------------------------------------------------------------------------- #
# v0.11.2 §8：取消 / 暂停 / 恢复
#
# 以前触发器只有「等着」和「跑过了」两种活法：用户交代错了「每天 9 点」，
# 除了把项目删掉没有任何办法让它停下来，而它每天都会真的开一个任务。
# 行不删（还要能查「后来怎样了」），只把状态挪出 due()/watch_once 的扫描面。
# --------------------------------------------------------------------------- #


async def _set_trigger_status(
    trigger_id: str,
    db,
    *,
    target: str,
    allowed: tuple[str, ...],
    action: str,
) -> dict:
    row = await db.get(AgentTrigger, trigger_id)
    if row is None:
        raise ApiError(code="trigger_not_found", message="触发器不存在", status_code=404)
    if row.status not in allowed:
        raise ApiError(
            code="trigger_status_unchanged",
            message=f"这条触发器当前状态是 {row.status}，不能{action}",
            status_code=409,
        )
    previous = row.status
    row.status = target
    await db.commit()
    if row.task_id:
        await EventRecorder(get_session_factory(), row.task_id).add(
            f"trigger.{action}",
            "receive_event",
            {"trigger_id": row.id, "previous_status": previous, "status": target},
        )
    return {
        "trigger_id": row.id,
        "task_id": row.task_id or None,
        "previous_status": previous,
        "status": row.status,
        "action": action,
    }


@router.post("/triggers/{trigger_id}/pause", response_model=dict)
async def pause_trigger(trigger_id: str, db: DbSession) -> dict:
    """暂停：不再到点、不再被 watch 唤醒，但随时能恢复。"""
    return await _set_trigger_status(
        trigger_id, db, target=triggers.STATUS_PAUSED, allowed=(triggers.STATUS_PENDING,), action="paused"
    )


@router.post("/triggers/{trigger_id}/resume", response_model=dict)
async def resume_trigger(trigger_id: str, db: DbSession) -> dict:
    """恢复：回到 pending，原来的 fire_at / watch_since 原样保留。"""
    return await _set_trigger_status(
        trigger_id, db, target=triggers.STATUS_PENDING, allowed=(triggers.STATUS_PAUSED,), action="resumed"
    )


@router.post("/triggers/{trigger_id}/cancel", response_model=dict)
async def cancel_trigger(trigger_id: str, db: DbSession) -> dict:
    """取消：终态，不恢复；同名事件再说一次会重新登记（不挡去重）。"""
    return await _set_trigger_status(
        trigger_id,
        db,
        target=triggers.STATUS_CANCELLED,
        allowed=(triggers.STATUS_PENDING, triggers.STATUS_PAUSED),
        action="cancelled",
    )
