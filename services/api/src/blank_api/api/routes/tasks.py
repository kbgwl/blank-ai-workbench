import asyncio
import hashlib
import json
import logging
import os
import time
import traceback
import uuid
from dataclasses import replace
from datetime import datetime

import anyio
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import case, delete, func, select

from blank_api.api.auth import require_token
from blank_api.api.errors import ApiError
from blank_api.api.routes.projects import load_project
from blank_api.db import db_retry, get_session_factory
from blank_api.deps import DbSession
from blank_api.models.model_provider import ModelProvider
from blank_api.models.checkpoint import AgentCheckpoint, AgentCheckpointWrite
from blank_api.models.model_provider import ModelUsage
from blank_api.models.plan_template import PlanTemplate
from blank_api.models.task import AgentEvent, FileChange, Task, ToolCall
from blank_api.models.thread import Thread
from blank_api.models.run import AgentRun
from blank_api.models.trigger import AgentTrigger
from blank_api.models.skill import Skill
from blank_api.schemas.task import (
    TaskClosureIn,
    EventOut,
    GoalUpdateIn,
    PlanProgressOut,
    PlanUpdateIn,
    TaskAnswerIn,
    TaskApprovalIn,
    TaskCreate,
    TaskForkIn,
    TaskOut,
    TaskPlanOut,
    TaskRunIn,
    TaskSteerIn,
)
from blank_api.services.agent.evidence import (
    DEFAULT_CHANGES_LIMIT,
    DEFAULT_TIMELINE_LIMIT,
    DEFAULT_TOOLS_LIMIT,
    MAX_CHANGES_LIMIT,
    MAX_TIMELINE_LIMIT,
    MAX_TOOLS_LIMIT,
    approval_is_expired,
    build_evidence,
    load_task,
    questions_can_default,
)
from blank_api.services.agent.artifacts import ArtifactStore
from blank_api.services.agent.background_policy import (
    budget_override,
    recovery_limit,
    resolve_background,
)
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.directives import listing_only
from blank_api.services.agent import closure
from blank_api.services.agent import notify
from blank_api.services.settings import SettingsStore, quiet_hours_active
from blank_api.services.agent.live import LiveFeed
from blank_api.services import metrics
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.agent.vision import supports_vision
from blank_api.services.commands_defs import expansion_payload, expand_user_input
from blank_api.services.tools.ask import parse_answers
from blank_api.services.fork import FORK_EVENT, fork_thread
from blank_api.services.agent.manager import RunAlreadyActive, run_manager
from blank_api.services.agent.queue import (
    TERMINAL_RUN_STATES,
    abandon_run,
    claim_run,
    enqueue_run,
    finish_run,
    renew_run,
)
from blank_api.services.agent.router import candidate_from_provider
from blank_api.services.agent.dispatch import (
    Budget,
    DISPATCH_DEFER,
    ExecutionSnapshot,
    INTENT_SCHEDULE,
    INTENT_WATCH,
    NOTIFY_APPROVAL_ONLY,
    explain,
    route,
)
from blank_api.services.features import is_enabled
from blank_api.services.models.registry import build_adapter
from blank_api.services.plan import (
    criteria_text,
    link_milestones,
    normalize_criteria,
    normalize_goal,
    normalize_milestones,
    normalize_steps,
    plan_fingerprint,
    plan_progress,
    resolve_milestone_status,
)
from blank_api.services.orchestration.runner import (
    MAX_RECOVERY_ATTEMPTS,
    _approval_locks,
    _defer_if_needed,
    _defer_intent,
    _enqueue_and_maybe_run,
    _execute_claimed_run,
    _factory_from,
    _graph_decision,
    _launch_resume,
    _record_dispatch,
    _record_notify_decision,
    _record_run_failure,
    _resolve_provider,
    _resume_decision_delivered,
    _routable,
    _run_request,
    _when_of,
    dispatch_of,
    inprocess_runner_enabled,
    launch_task,
    persist_task,
    recover_orphan_approvals,
    recover_pending_runs,
    recovery_attempt_limit,
)
from blank_api.services.uploads import UploadStore, load_attachments_for
from blank_api.services.models.routing import FallbackAdapter
from blank_api.services.sandbox import SnapshotNotRestorable, SnapshotStore

router = APIRouter(tags=["tasks"], dependencies=[Depends(require_token)])
log = logging.getLogger(__name__)

# 事件流在没有持久事件时等多久去数据库补一次。
# 实时增量会提前唤醒，所以这个值只影响"没有一场模型调用正在进行时"的延迟。
DELTA_WAIT_SECONDS = 0.5

# 空库查询的退避：0.25s 起，每空一轮 ×1.8，封顶 2s。
# 原来的固定 0.5s 是"每个连接每半秒查一次库"——一个跑 20 分钟的任务，界面开着就是
# 2400 次空查询，二十个连接就是四万多次。退避把这段压到十分之一左右，
# 而有事件时 `LiveFeed.wake()` 会立刻把循环叫回来，**时延不变**。
# 上限 2s 是照着"人看不出卡顿"的边界定的：再长，worker 在另一个进程里跑的任务
# 就会出现"点了没反应"的观感（跨进程收不到 wake，只能靠轮询）。
SSE_BACKOFF_MIN_SECONDS = 0.25
SSE_BACKOFF_MAX_SECONDS = 2.0
SSE_BACKOFF_FACTOR = 1.8
# 单条连接的最长寿命。到点就关，让客户端按 `after_seq` 重连——
# 无限长的流在代理后面迟早会被中间的某一层掐掉，而那种断开没有任何信号。
SSE_MAX_SECONDS = 30 * 60
# 心跳间隔：客户端 30s 收不到任何帧就重连；15s 一跳留出一整拍余量。
SSE_PING_SECONDS = 15.0


@router.post("/projects/{project_id}/tasks", response_model=TaskOut, status_code=201)
async def create_task(
    project_id: str, payload: TaskCreate, db: DbSession, request: Request
) -> TaskOut:
    project = await load_project(db, project_id)
    # 悬空的模型选择在这里就挡掉：等任务跑起来才报「模型供应商不存在」，
    # 用户看到的是一句没有出处的失败，而界面上那个选择器早就不显示它了。
    if payload.provider_id and await db.get(ModelProvider, payload.provider_id) is None:
        raise ApiError(
            code="provider_not_found",
            message="所选模型已不存在，请在输入框下方重新选择模型后再发送",
            status_code=400,
        )
    # 独立任务也必须有一条可追溯的会话（v164 §2.3）。
    #
    # 以前这条路径建出来的任务 thread_id 恒为 null：关掉窗口再从会话列表回来时，
    # 它在任何一条对话里都找不到，用户只能当它丢了。会话行和任务行在同一个事务里
    # 提交（persist_task 里面的 commit 会把它一起写下去），任务建失败就一起回滚，
    # 不会反过来留下一条点开是空的会话。
    thread = Thread(
        id=str(uuid.uuid4()),
        project_id=project.id,
        title=(payload.title or payload.goal or "新任务")[:80],
        status="active",
        turn_count=1,
    )
    db.add(thread)
    # 先把会话行落进同一个事务：tasks.thread_id 有外键，SQLAlchemy 不会仅凭
    # 一张裸 ForeignKey 保证「会话先于任务」的插入顺序；不 flush 时 SQLite
    # 打开 foreign_keys 会直接报 FOREIGN KEY constraint failed。
    await db.flush()
    # 用户输入以 `/名字 参数` 开头且命中命令/Skill 时换成正文字：这一步要在建行
    # 之前完成，落库的 goal 就是模型真正会读到的那段。标题保留用户原话。
    workspace = project.active_workspace
    # 项目一条目录都没绑时，这一轮只能纯聊天；显式 chat_only 时即使项目有目录，
    # 也不替用户沿用——那个目录没有被这个对话选过。
    chat_only = bool(payload.chat_only) or workspace is None
    no_tools = bool(payload.no_tools)
    read_only = bool(payload.read_only)
    list_only = bool(payload.list_only) or listing_only(payload.goal)
    command_root = (
        workspace.path
        if (workspace is not None and not chat_only and not no_tools)
        else None
    )
    expanded_goal, command_info = expand_user_input(
        payload.goal,
        root=command_root,
        session_id=thread.id,
    )
    updates: dict = {
        "chat_only": chat_only,
        "no_tools": no_tools,
        "read_only": read_only,
        "list_only": list_only,
    }
    if command_info is not None:
        updates["goal"] = expanded_goal
    payload = payload.model_copy(update=updates)
    task = await persist_task(
        db,
        project.id,
        payload,
        thread_id=thread.id,
        turn_index=1,
        command_info=command_info,
    )
    # v0.10.1 §2：与 threads.create_turn 同一条规矩——auto_start 由后端就地接管，
    # 前端不再二次调 /run。启动失败记 task.dispatch_failed，任务留在 draft/queued
    # 可重试的状态；失败不能把 201 带走，也不能静默。
    if payload.auto_start:
        # rollback 会过期 ORM 实例；id 先取成普通值，except 里才不会触发
        # 同步懒加载（MissingGreenlet）。
        auto_task_id = task.id
        run_payload = TaskRunIn(
            provider_id=payload.provider_id,
            scenario=payload.scenario or "",
            memory_opt_in=payload.memory_opt_in,
            permission_mode=payload.permission_mode,
        )
        try:
            deferred = await _defer_if_needed(db, task, run_payload)
            if deferred is not None:
                launched = deferred
            else:
                launched = await launch_task(db, task, run_payload, request)
            await EventRecorder(get_session_factory(), auto_task_id).add(
                "task.auto_started",
                "receive_task",
                {
                    "task_id": auto_task_id,
                    "run_id": str(launched.get("run_id") or ""),
                    "status": str(launched.get("status") or ""),
                    "source": "backend",
                },
            )
        except Exception as error:  # noqa: BLE001 - 派发失败降级为事件，不吞 201
            log.warning("任务自动下发失败 task=%s", auto_task_id, exc_info=True)
            await db.rollback()
            failure_payload = {
                "task_id": auto_task_id,
                "reason": str(error)[:400],
                "code": str(getattr(error, "code", "") or ""),
                "auto_start": True,
            }
            # 用独立会话查任务行现状：draft（没入队，可手动重试）还是 queued
            # （已入队，启动恢复/worker 会接管）。不猜，以库里的状态为准。
            async with get_session_factory()() as event_session:
                stored = (
                    await event_session.execute(select(Task).where(Task.id == auto_task_id))
                ).scalar_one_or_none()
                if stored is not None:
                    failure_payload["task_status"] = str(stored.status or "")
            await EventRecorder(get_session_factory(), auto_task_id).add(
                "task.dispatch_failed", "receive_task", failure_payload
            )
    # launch/rollback 会让 task 实例过期（expire_on_commit），直接序列化会
    # MissingGreenlet；refresh 一次拿到终态（draft/queued/deferred）再出去。
    await db.refresh(task)
    out = TaskOut.model_validate(task)
    # UP-04：创建就把「沙箱实际强制了什么」交给前端，别等第一条命令跑完才知道。
    # 解析失败（探针/后端异常）不挡创建：字段留空，运行时 evidence.boundaries 仍是事实源。
    try:
        from blank_api.services.sandbox.tiers import default_constraints

        out.sandbox_constraints = default_constraints()
    except Exception:  # noqa: BLE001 - 约束快照失败只是少一条提示
        pass
    return out


# 「关窗之后还在动」的那几种状态。界面用它决定要不要给用户一个回来的入口：
# 任务在后台继续跑，人却关掉窗口走了，重开时必须能重新找到它（v164 §2.3）。
ACTIVE_TASK_STATUSES = (
    "queued",
    "running",
    "waiting_approval",
    "paused",
    "failed",
    "interrupted",
)


@router.get("/tasks", response_model=list[TaskOut])
async def list_tasks(
    db: DbSession,
    project_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    active: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[TaskOut]:
    """任务列表。

    ``active=true`` 是给「后台任务」入口用的：只回还在进行或停下来等人处理的，
    不夹带一堆早就结束的历史任务。``status`` 支持逗号分隔多值——
    前端要一次问「running + waiting_approval + paused」，不该发三次请求。
    """
    stmt = select(Task).order_by(Task.created_at.desc()).limit(limit)
    if project_id:
        stmt = stmt.where(Task.project_id == project_id)
    if active:
        stmt = stmt.where(Task.status.in_(ACTIVE_TASK_STATUSES))
    elif status:
        wanted = [item.strip() for item in status.split(",") if item.strip()]
        if wanted:
            stmt = stmt.where(Task.status.in_(wanted))
    return [TaskOut.model_validate(task) for task in (await db.execute(stmt)).scalars()]


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(task_id: str, db: DbSession) -> TaskOut:
    return TaskOut.model_validate(await load_task(db, task_id))


@router.get("/tasks/{task_id}/plan", response_model=TaskPlanOut)
async def get_task_plan(task_id: str, db: DbSession) -> TaskPlanOut:
    """读计划。任务结束后仍然可读——这是"下一轮问做到哪了"能答上来的依据。"""
    task = await load_task(db, task_id)
    steps = list(task.plan_steps or [])
    milestones = list(task.milestones or [])
    return TaskPlanOut(
        task_id=task.id,
        goal=task.goal,
        goal_spec=dict(task.goal_spec or {}),
        steps=steps,
        milestones=milestones,
        progress=PlanProgressOut(**plan_progress(steps, milestones)),
        revision=int(task.plan_revision or 0),
    )


@router.patch("/tasks/{task_id}/plan", response_model=TaskPlanOut)
async def update_task_plan(task_id: str, payload: PlanUpdateIn, db: DbSession) -> TaskPlanOut:
    """手工改计划。

    存在的理由：模型给的计划不一定对。用户看着「第 3 步其实是两步」时，
    不该为了改一行字去重跑一整轮——那既费钱，又会让已经做完的步骤状态重来。
    """
    task = await load_task(db, task_id)
    previous = list(task.plan_steps or [])

    if payload.steps is not None:
        # 整份替换。传上来的项按 id / 文本认领上一版，状态与引用因此能保住。
        steps = normalize_steps([item.model_dump(exclude_none=True) for item in payload.steps], previous=previous)
    elif payload.patch:
        by_id = {str(step.get("id") or ""): dict(step) for step in previous}
        for item in payload.patch:
            target = by_id.get(item.id)
            if target is None:
                # 按 id 改却找不到：直接报错，不要静默新增一条——
                # 前端拿的是旧列表时静默新增会让计划越改越长。
                raise ApiError("plan_step_not_found", f"计划里没有这一步：{item.id}", 404)
            for key in ("step", "status", "milestone_id", "evidence"):
                value = getattr(item, key)
                if value is not None:
                    target[key] = value
        steps = normalize_steps(list(by_id.values()), previous=previous)
    else:
        steps = normalize_steps(previous, previous=previous)

    milestones = (
        normalize_milestones(payload.milestones)
        if payload.milestones is not None
        else list(task.milestones or [])
    )
    milestones = resolve_milestone_status(link_milestones(steps, milestones), steps)

    # 手改也算一次改版：下一次模型 update_plan 时能看出"人动过这份计划"。
    if plan_fingerprint(steps) != plan_fingerprint(previous):
        task.plan_revision = int(task.plan_revision or 0) + 1
    task.plan_steps = steps
    task.milestones = milestones
    await db.commit()
    await db.refresh(task)

    progress = plan_progress(steps, milestones)
    await EventRecorder(get_session_factory(), task.id).add(
        "plan.updated",
        "manual",
        {
            "steps": steps,
            "milestones": milestones,
            "progress": progress,
            "total": progress["total"],
            "completed": progress["completed"],
            "source": "user",
            "explanation": payload.explanation[:300],
        },
    )
    return TaskPlanOut(
        task_id=task.id,
        goal=task.goal,
        goal_spec=dict(task.goal_spec or {}),
        steps=steps,
        milestones=milestones,
        progress=PlanProgressOut(**progress),
        revision=int(task.plan_revision or 0),
    )


@router.patch("/tasks/{task_id}/goal", response_model=TaskPlanOut)
async def update_task_goal(task_id: str, payload: GoalUpdateIn, db: DbSession) -> TaskPlanOut:
    """改结构化目标。只覆盖传了的字段，没传的保持原样。"""
    task = await load_task(db, task_id)
    current = normalize_goal(task.goal_spec or {}, fallback=task.goal)
    merged = dict(current)
    for key in ("objective", "done_when", "constraints", "out_of_scope"):
        value = getattr(payload, key)
        if value is not None:
            merged[key] = value if key != "objective" else str(value)[:1000]
    spec = normalize_goal(merged, fallback=task.goal)

    task.goal_spec = spec
    await db.commit()
    await db.refresh(task)

    steps = list(task.plan_steps or [])
    milestones = list(task.milestones or [])
    await EventRecorder(get_session_factory(), task.id).add(
        "goal.updated", "manual", {**spec, "source": "user"}
    )
    return TaskPlanOut(
        task_id=task.id,
        goal=task.goal,
        goal_spec=spec,
        steps=steps,
        milestones=milestones,
        progress=PlanProgressOut(**plan_progress(steps, milestones)),
        revision=int(task.plan_revision or 0),
    )


async def _drop_unused_uploads(db: DbSession, project_id: str, ids: set[str]) -> None:
    """删掉只属于这个任务的附件。

    要再查一遍"还有谁引用"：同一份附件可能被重试的轮次、或被分叉出来的会话继续用，
    按 id 无条件删会把别人还在用的文件一起清掉。
    """
    if not ids:
        return
    rows = (
        await db.execute(select(Task.attachments).where(Task.project_id == project_id))
    ).scalars().all()
    keep: set[str] = set()
    for row in rows:
        for item in row or []:
            keep.add(str(item.get("id") or ""))
    leftover = ids - keep
    if not leftover:
        return
    await anyio.to_thread.run_sync(UploadStore().remove, project_id, leftover)


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: str, db: DbSession) -> Response:
    """删除一条会话（任务）及其事件、运行记录、检查点、工具调用与改动记录。

    运行中的会话不直接删：先取消，避免后台还在往已删除的任务上写事件。
    """
    task = await load_task(db, task_id)
    # 只拦真正在跑的：历史遗留的 queued（比如旧版崩在启动阶段）要允许删掉，
    # 否则列表里永远留着删不掉的僵尸会话。
    if task.status == "running":
        raise ApiError(
            code="task_running", message="任务正在执行中，先取消再删除", status_code=409
        )
    threads = (
        await db.execute(select(AgentCheckpoint.thread_id).where(AgentCheckpoint.task_id == task_id))
    ).scalars().all()
    if threads:
        await db.execute(delete(AgentCheckpointWrite).where(AgentCheckpointWrite.thread_id.in_(threads)))
    await db.execute(delete(AgentCheckpoint).where(AgentCheckpoint.task_id == task_id))
    await db.execute(delete(AgentEvent).where(AgentEvent.task_id == task_id))
    await db.execute(delete(ToolCall).where(ToolCall.task_id == task_id))
    await db.execute(delete(FileChange).where(FileChange.task_id == task_id))
    await db.execute(delete(AgentRun).where(AgentRun.task_id == task_id))
    await db.execute(delete(ModelUsage).where(ModelUsage.task_id == task_id))
    attached_ids = {str(item.get("id") or "") for item in (task.attachments or [])} - {""}
    project_id = task.project_id
    await db.delete(task)
    await db.commit()
    # 附件跟着任务走：删了任务还留着它的截图，就是一份没人认领的隐私文件。
    await _drop_unused_uploads(db, project_id, attached_ids)
    return Response(status_code=204)


@router.get("/tasks/{task_id}/run")
async def get_task_run(task_id: str, db: DbSession) -> dict:
    await load_task(db, task_id)
    run = (
        await db.execute(select(AgentRun).where(AgentRun.task_id == task_id))
    ).scalar_one_or_none()
    if run is None:
        return {"task_id": task_id, "status": "not_started", "attempts": 0}
    lease = run.lease_expires_at
    return {
        "id": run.id,
        "task_id": run.task_id,
        "status": run.status,
        "worker_id": run.worker_id,
        "attempts": run.attempts,
        "lease_expires_at": lease.isoformat() if lease else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
    }


@router.get("/tasks/{task_id}/snapshots")
async def list_task_snapshots(task_id: str, db: DbSession) -> list[dict]:
    await load_task(db, task_id)
    store = SnapshotStore()
    items = store.list(task_id)
    # 「能不能恢复」是列表上就该看见的属性：等用户点下去才报错，等于把
    # 一次可行的操作伪装成失败。文件清单型快照在按钮上直接标出来。
    for item in items:
        ok, reason = store.restorable(str(item.get("id") or ""))
        item["restorable"] = ok
        if not ok and not item.get("skipped_reason"):
            item["skipped_reason"] = reason
    return items


@router.post("/tasks/{task_id}/snapshots/{snapshot_id}/restore")
async def restore_task_snapshot(task_id: str, snapshot_id: str, db: DbSession) -> dict:
    task = await load_task(db, task_id)
    if task.status in ("queued", "running", "waiting_approval"):
        raise ApiError(code="task_active", message="任务仍在执行，完成后才能恢复快照", status_code=409)
    store = SnapshotStore()
    snapshot = next((item for item in store.list(task_id) if item.get("id") == snapshot_id), None)
    if snapshot is None:
        raise ApiError(code="snapshot_not_found", message="该任务没有找到对应快照", status_code=404)
    project = await load_project(db, task.project_id)
    workspace = project.active_workspace
    if workspace is None:
        raise ApiError(code="workspace_not_bound", message="该项目尚未绑定工作区目录", status_code=409)
    try:
        report = store.restore_report(snapshot_id, workspace.path, None)
    except SnapshotNotRestorable as exc:
        # 409 + 原因：这份快照当初就没复制（工作区过大/复制失败），恢复不了是
        # 一个已知结果，不是服务端故障。500 会让用户以为重试有用。
        raise ApiError(
            code="snapshot_not_restorable",
            message=f"这份快照无法恢复：{exc.reason}",
            status_code=409,
        ) from exc
    await EventRecorder(get_session_factory(), task_id).add(
        "snapshot.restored",
        "complete",
        {
            "snapshot_id": snapshot_id,
            "files": report["restored"],
            "skipped": report["skipped"],
        },
    )
    return {
        "task_id": task_id,
        "snapshot_id": snapshot_id,
        "restored": report["restored"],
        "skipped": report["skipped"],
        "skipped_reason": report["skipped_reason"],
    }


@router.post("/tasks/{task_id}/run")
async def run_task(task_id: str, payload: TaskRunIn, request: Request, db: DbSession) -> dict:
    task = await load_task(db, task_id)
    if task.status == "running":
        raise ApiError(code="task_running", message="任务正在执行中", status_code=409)
    run_request = await _run_request(db, task, payload, request)
    # 工作与请求解耦：客户端断开只该停止**等待**，不该停止**干活**。
    #
    # 实测（2026-11-20 08:50）：夜班用 curl 跑一个长任务，客户端 600 秒超时断开，
    # 服务端抛 CancelledError、回 500，已经跑掉的十分钟连同 token 一起作废，任务
    # 永远停在 running——再点运行就是 409「任务正在执行中」，谁也捡不回来。
    # 所以这里 detach：请求被取消时工作继续跑完，状态照常落库，客户端回来
    # GET /tasks/{id} 看结果。
    work = asyncio.create_task(
        AgentRuntime(get_session_factory()).run(run_request),
        name=f"blank-run-{task_id}",
    )
    evidence = await _run_detached(work, task_id)
    async with get_session_factory()() as session:
        finished = await load_task(session, task_id)
        return {"task": TaskOut.model_validate(finished).model_dump(), "evidence": evidence}


async def _run_detached(work: asyncio.Task, task_id: str) -> dict:
    """等一个任务跑完；等的人被取消时，跑的人继续跑。

    这条边界值得单独写出来：`await task` 在调用方被取消时会**连带取消被等待的任务**，
    于是"客户端断开"等于"工作作废"。shield 把两者拆开——断开的只是等，工作照旧，
    状态照旧落库。
    """
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        work.add_done_callback(lambda done: _log_detached(done, task_id))
        raise


def _log_detached(done: asyncio.Task, task_id: str) -> None:
    """后台跑完的回调：异常必须有人取，否则会被 asyncio 静默丢掉。"""
    if done.cancelled():
        log.warning("客户端断开后后台执行被取消 task=%s", task_id)
        return
    error = done.exception()
    if error is not None:
        log.error("客户端断开后后台执行失败 task=%s: %r", task_id, error)


@router.post("/tasks/{task_id}/start", status_code=202)
async def start_task(task_id: str, payload: TaskRunIn, request: Request, db: DbSession) -> dict:
    """Start a run without holding the HTTP request open."""
    task = await load_task(db, task_id)
    if task.status in ("running", "queued"):
        raise ApiError(code="task_running", message="任务正在执行中", status_code=409)
    # 派发方式为「等触发」时不要当场跑（v162）：定时/监控类请求一旦按原路径立即执行，
    # 「每天跑一次」就变成了「现在跑一次」。特性开关关着时照旧立即执行。
    deferred = await _defer_if_needed(db, task, payload)
    if deferred is not None:
        return deferred
    return await launch_task(db, task, payload, request)


@router.get("/tasks/{task_id}/events", response_model=list[EventOut])
async def task_events(
    task_id: str,
    db: DbSession,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=2000),
) -> list[EventOut]:
    await load_task(db, task_id)
    rows = (
        await db.execute(
            select(AgentEvent)
            .where(AgentEvent.task_id == task_id, AgentEvent.seq > after_seq)
            .order_by(AgentEvent.seq)
            .limit(limit)
        )
    ).scalars()
    return [EventOut.model_validate(row) for row in rows]


@router.get("/tasks/{task_id}/artifacts/{artifact_id}")
async def get_task_artifact(task_id: str, artifact_id: str, db: DbSession) -> dict:
    await load_task(db, task_id)
    call = (
        await db.execute(
            select(ToolCall).where(ToolCall.task_id == task_id, ToolCall.id == artifact_id)
        )
    ).scalar_one_or_none()
    if call is None or not call.artifact_path:
        raise ApiError(code="artifact_not_found", message="工具完整输出不存在", status_code=404)
    try:
        return ArtifactStore().load(task_id, call.id)
    except (FileNotFoundError, json.JSONDecodeError):
        raise ApiError(code="artifact_not_found", message="工具完整输出不存在", status_code=404) from None


@router.get("/tasks/{task_id}/events/stream")
async def task_event_stream(
    task_id: str,
    db: DbSession,
    after_seq: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Replay persisted events and wait for new ones from a sequence cursor.

    同一个连接上也带实时增量（``event: delta``）：持久事件是「发生了什么」，
    增量是「模型正在说什么」。两者分开是因为前者要能跳读、要能重放，后者允许丢。

    两次查库之间是**退避 + 事件唤醒**，不再是固定半秒：

    * 空转一轮就把等待拉长（``SSE_BACKOFF_*``，上限 2 秒）——一个 200 步的任务
      原来要查几千次库，其中绝大多数是空的；
    * 本进程每次写持久事件都会 ``LiveFeed.wake()`` 叫醒这个循环，时延不涨；
    * 退避只是"少查空库"，**断线补偿一刻没减**：``after_seq`` 仍然从库里的 seq
      接着补，跨进程（worker）写的事件照样靠轮询落地，只是间隔更长。
    """
    await load_task(db, task_id)
    # SSE 重连时客户端把最后一个事件 id 放在 `Last-Event-ID` 头里；与 query 取较大值。
    # 解不出数字就按 0，交给 query 的 after_seq，不猜"大概是第几条"。
    try:
        header_cursor = int(str(last_event_id or "").strip() or 0)
    except ValueError:
        header_cursor = 0
    resume_from = max(after_seq, header_cursor)

    async def events():
        cursor = resume_from
        delay = SSE_BACKOFF_MIN_SECONDS
        started = time.monotonic()
        last_sent = started
        terminal = {"succeeded", "failed", "cancelled", "paused", "waiting_approval"}
        queue = LiveFeed.subscribe(task_id)
        metrics.inc(metrics.SSE_CONNECTED)
        metrics.gauge("sse.connections", LiveFeed.total_subscribers())
        try:
            while time.monotonic() - started < SSE_MAX_SECONDS:
                # 断流会取消这个生成器：如果取消正好落在连接 close 上，池子里会留下一条
                # 半关闭连接，后面所有请求都可能撞上 no active connection。每次读取都
                # 屏蔽取消，等这一小段数据库操作跑完再说。
                with anyio.CancelScope(shield=True):
                    metrics.inc(metrics.SSE_POLL)
                    async with get_session_factory()() as session:
                        rows = (
                            await session.execute(
                                select(AgentEvent)
                                .where(AgentEvent.task_id == task_id, AgentEvent.seq > cursor)
                                .order_by(AgentEvent.seq)
                            )
                        ).scalars().all()
                        status = (await session.execute(select(Task.status).where(Task.id == task_id))).scalar_one_or_none()
                if rows:
                    for row in rows:
                        cursor = row.seq
                        last_sent = time.monotonic()
                        yield f"id: {row.seq}\ndata: {json.dumps(EventOut.model_validate(row).model_dump(), ensure_ascii=False)}\n\n"
                    # 有新东西就把退避重置回最短：刚活跃过的任务下一秒多半还会活跃。
                    delay = SSE_BACKOFF_MIN_SECONDS
                else:
                    if status in terminal:
                        break
                    delay = min(SSE_BACKOFF_MAX_SECONDS, delay * SSE_BACKOFF_FACTOR)
                # 有增量就立刻醒，没有就按当前退避等一次。
                # 用 wait_for 而不是 sleep：sleep 会让文字比模型慢半拍，
                # 而实时预览的全部价值就在这半拍上。
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=delay)
                except asyncio.TimeoutError:
                    # 空转到期：满 15s 没有出过任何帧就发一次命名心跳。
                    # 客户端据此区分"任务安静"与"连接死了"，30s 收不到就重连。
                    if not rows and time.monotonic() - last_sent >= SSE_PING_SECONDS:
                        last_sent = time.monotonic()
                        yield "event: ping\ndata: \n\n"
                    continue
                if frame.get("wake"):
                    # 被人叫醒：不推帧，直接回去查库（这次查到的多半就是刚写进去的那条）。
                    metrics.inc(metrics.SSE_WAKE)
                    delay = SSE_BACKOFF_MIN_SECONDS
                    continue
                last_sent = time.monotonic()
                yield _delta_frame(frame)
                while not queue.empty():
                    extra = queue.get_nowait()
                    if extra.get("wake"):
                        metrics.inc(metrics.SSE_WAKE)
                        continue
                    yield _delta_frame(extra)
        finally:
            LiveFeed.unsubscribe(task_id, queue)
            metrics.inc(metrics.SSE_DISCONNECTED)
            metrics.gauge("sse.connections", LiveFeed.total_subscribers())

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _delta_frame(payload: dict) -> str:
    """实时增量帧。用独立的事件名：前端不必把它当成"发生过的事"塞进时间线。"""
    return f"event: delta\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.get("/tasks/{task_id}/evidence")
async def task_evidence(
    task_id: str,
    db: DbSession,
    timeline_limit: int = Query(default=DEFAULT_TIMELINE_LIMIT, ge=0, le=MAX_TIMELINE_LIMIT),
    timeline_before_seq: int | None = Query(default=None, ge=1),
    include_timeline: bool = Query(default=True),
    tools_limit: int = Query(default=DEFAULT_TOOLS_LIMIT, ge=0, le=MAX_TOOLS_LIMIT),
    tools_offset: int = Query(default=0, ge=0),
    changes_limit: int = Query(default=DEFAULT_CHANGES_LIMIT, ge=0, le=MAX_CHANGES_LIMIT),
    changes_offset: int = Query(default=0, ge=0),
) -> dict:
    """交付证据包。

    长任务的时间线默认只给最近 200 条并带翻页游标（``timeline_meta``）；工具调用与文件变更
    同样默认各取最近 500 条（``tools_meta`` / ``changes_meta`` 给出总数与 ``has_more``）。
    这是一次**行为收紧**：以前一次请求会把全部事件、全部 ToolCall、全部文件 diff
    （含一个可能数 MB 的拼接 ``diff`` 字段）塞进响应，长任务单是 JSON 就有几 MB，前端渲染
    卡住而用户真正要看的就是最后一段。要全量仍然可以显式要大上限（``*_limit=2000``），
    但默认路径不再无界。
    """
    await load_task(db, task_id)
    return await build_evidence(
        get_session_factory(),
        task_id,
        timeline_limit=timeline_limit,
        timeline_before_seq=timeline_before_seq,
        include_timeline=include_timeline,
        tools_limit=tools_limit,
        tools_offset=tools_offset,
        changes_limit=changes_limit,
        changes_offset=changes_offset,
    )


@router.get("/tasks/{task_id}/evidence/summary")
async def task_evidence_summary(task_id: str, db: DbSession) -> dict:
    """只要汇总，不要时间线。

    验收、审批、用量、边界这些字段全部由服务端算好；界面首屏读这一份就够，
    时间线用 ``/events`` 增量取。少读一遍全量事件是这条路径存在的全部理由。
    """
    await load_task(db, task_id)
    return await build_evidence(get_session_factory(), task_id, include_timeline=False)


@router.get("/tasks/{task_id}/diff")
async def task_diff(task_id: str, db: DbSession) -> dict:
    await load_task(db, task_id)
    evidence = await build_evidence(get_session_factory(), task_id)
    return {
        "task_id": task_id,
        "files": evidence["changes"]["files"],
        "diff": evidence["changes"]["diff"],
        "reversible": evidence["changes"]["reversible"],
    }


@router.get("/tasks/{task_id}/closure")
async def read_task_closure(task_id: str, db: DbSession) -> dict:
    """读闭合记录：改了什么、校验结论、验收位签了没有。

    返回里带 ``revision`` 与 ``closed``：界面上"改完了"和"被验收了"必须是两个状态，
    合成一句话就又回到了"模型自评即验收"。
    """
    task = await load_task(db, task_id)
    project = await load_project(db, task.project_id)
    record = closure.read(project.path, task_id)
    rows = [row for row in closure.ledger(project.path) if str(row.get("task_id") or "") == task_id]
    return {
        "task_id": task_id,
        "closure": record,
        "decisions": rows,
        "revision_now": closure.revision_of(project.path),
        "closed": bool(record.get("closed")),
    }


@router.post("/tasks/{task_id}/closure")
async def decide_task_closure(task_id: str, payload: TaskClosureIn, db: DbSession) -> dict:
    """记一次验收决定，绑当前 revision。revision 变了返回 409。

    不做"顺手更新到最新版"：验收针对的是一份具体结果，把旧决定挂到新提交上会让
    台账显示"这一版被验收过"，而被看的其实是上一版。
    """
    task = await load_task(db, task_id)
    project = await load_project(db, task.project_id)
    try:
        record = closure.decide(
            project.path,
            task_id,
            decision=payload.decision,
            decided_by=payload.decided_by,
            note=payload.note,
        )
    except ValueError as exc:
        raise ApiError(
            code="closure_not_bindable",
            message=str(exc),
            status_code=409,
        ) from exc
    return {"task_id": task_id, "closure": record}


@router.post("/tasks/{task_id}/approval", status_code=202)
async def decide_approval(
    task_id: str,
    payload: TaskApprovalIn,
    request: Request,
    response: Response,
    db: DbSession,
) -> dict:
    # 同一张卡连点两下、或两个审批卡并发提交：决策+入队必须串行，否则两次
    # 决定会按各自读到的状态各入队一次，第二张卡被静默跳过。
    lock = _approval_locks.setdefault(task_id, asyncio.Lock())
    async with lock:
        return await _decide_approval_locked(task_id, payload, request, response, db)





async def _decide_approval_locked(
    task_id: str,
    payload: TaskApprovalIn,
    request: Request,
    response: Response,
    db: DbSession,
) -> dict:
    task = await load_task(db, task_id)
    if task.status != "waiting_approval":
        # v0.10.1 §3.4 幂等：同一张卡已经批过了（重复点击），返回当时的决定，
        # 不再入队、也不报错——「重复点击允许不能重复执行」的温和实现。
        if payload.approval_id:
            prior = await _prior_decision(task_id, payload.approval_id)
            if prior is not None:
                response.status_code = 200
                return {
                    "status": "already_decided",
                    "approval_id": payload.approval_id,
                    "decision": prior,
                    "task": TaskOut.model_validate(task).model_dump(),
                }
        raise ApiError(
            code="task_not_waiting_approval",
            message=f"任务当前不在待审批状态（当前状态：{task.status}），无需重复审批",
            status_code=409,
        )
    evidence = await build_evidence(get_session_factory(), task_id)
    pending = [str(item.get("tool") or "") for item in evidence.get("pending_approvals") or []]
    # v0.10.1 §3.2/§3.3：审批按身份精确匹配。当前等待的是哪张卡，以最近一次
    # approval.required 事件为准；提交的身份对不上就明确拒绝，绝不按文件名、
    # 工具名或"任务里第一张 pending 卡"猜。
    identity = await _current_approval_identity(task_id)
    provided = {
        key: value
        for key, value in (
            ("approval_id", payload.approval_id),
            ("tool_call_id", payload.tool_call_id),
            ("arguments_hash", payload.arguments_hash),
            ("tool_name", payload.tool_name),
        )
        if value
    }
    if provided and identity:
        mismatched = [
            key for key, value in provided.items() if str(identity.get(key) or "") != value
        ]
        # tool_call_id 有两种真实形态：中断事件里的模型调用 id，和网关形态留下的
        # ToolCall 行 id。两个都算匹配——审批身份的唯一定址键是 approval_id
        # （它就是从 task+调用 id+参数哈希派生的），行 id 只是展示层的账。
        if "tool_call_id" in mismatched:
            row_ids = {
                str(item.get("tool_call_id") or "")
                for item in evidence.get("pending_approvals") or []
            }
            if provided["tool_call_id"] in row_ids or provided["tool_call_id"] == str(
                identity.get("tool_call_id") or ""
            ):
                mismatched.remove("tool_call_id")
        if mismatched:
            raise ApiError(
                code="approval_mismatch",
                message="审批对象已变化，请重新生成审批请求",
                status_code=409,
            )
    for approval in evidence.get("pending_approvals") or []:
        expires_at = approval.get("expires_at")
        if expires_at:
            try:
                datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            except ValueError:
                raise ApiError(code="approval_invalid", message="审批请求时间无效，无法继续执行", status_code=409) from None
            # TTL 交给读侧同一个函数：这条链路曾经有三份各自实现的时间比较，
            # 读侧说「待审批」、写侧 409，界面就只能给一个必然失败的按钮。
            if approval_is_expired(expires_at):
                raise ApiError(code="approval_expired", message="审批请求已过期，请重新运行任务生成新的审批请求", status_code=409)
    recorder = EventRecorder(get_session_factory(), task_id)
    # 审批身份随决定落账：幂等判定与事后审计都靠它对号。当前等待的卡没有身份
    # （老中断事件）时，如实记提交上来的值，不编空号。
    decided_identity = (
        {
            key: str(identity.get(key) or "")
            for key in (
                "approval_id",
                "tool_call_id",
                "tool",
                "tool_name",
                "arguments_hash",
                "resource_path",
            )
        }
        if identity
        else {"approval_id": payload.approval_id}
    )
    # 决定本身单独一份：恢复入队与「启动补偿扫描」都从它重建，所以 feedback /
    # send_back_step_id 必须一起落账，不能只活在这次 HTTP 请求的内存里。
    decision = {
        "approve": payload.approve,
        "scope": payload.scope,
        # 空串不传：老客户端不会带这两个字段，图里按"普通驳回"处理。
        **({"feedback": payload.feedback} if payload.feedback else {}),
        **({"step_id": payload.send_back_step_id} if payload.send_back_step_id else {}),
    }
    decided_seq = await recorder.add(
        "approval.decided",
        "execute_action",
        {
            **decision,
            "tools": pending,
            **decided_identity,
        },
    )
    approval_id = str(decided_identity.get("approval_id") or payload.approval_id)
    resume_decision = {
        **decision,
        "approval_id": approval_id,
        # 是否已经投递只看这个序号之后的 task.resumed：worker 崩溃重投时
        # 不会把决定再消费一遍。
        "decided_seq": decided_seq,
    }
    # 重建恢复入参：沿用这条 run 原来的请求快照（provider / 模式 / 场景都在里面），
    # 只把审批决定换成这一次的。普通 run 的 payload 绝不会带上旧决定。
    existing_run = (
        await db.execute(select(AgentRun).where(AgentRun.task_id == task_id))
    ).scalar_one_or_none()
    base_payload = dict(existing_run.request_payload or {}) if existing_run is not None else {}
    base_payload.pop("resume_decision", None)
    if payload.provider_id:
        base_payload["provider_id"] = payload.provider_id
    try:
        run_payload = TaskRunIn.model_validate(base_payload)
    except Exception:  # noqa: BLE001 - 旧入参形状不认识时按最小入参恢复，不阻断审批
        run_payload = TaskRunIn(provider_id=payload.provider_id)
    # 决定只通过 Command(resume=...) 交回图里：批准时中断返回带作用域的批准，
    # 图据此写入会话记忆并补记 approval.granted。在这里预先塞 approved_tools
    # 会让中断被跳过，审批轨迹反而丢失。
    run_request = await _run_request(
        db, task, run_payload, request, resume_decision=resume_decision
    )
    # 校验与入队之间任务可能已被取消：入队前再读一次状态，取消后不再恢复。
    fresh = await db.get(Task, task_id)
    if fresh is not None:
        await db.refresh(fresh)
    if fresh is None or fresh.status != "waiting_approval":
        raise ApiError(
            code="task_not_waiting_approval",
            message=(
                f"任务状态已变化（当前状态：{fresh.status if fresh else '不存在'}），"
                "这次审批没有入队；如果任务仍在等待审批，可以重新提交"
            ),
            status_code=409,
        )
    try:
        queued = await _enqueue_and_maybe_run(
            db,
            task_id,
            fresh,
            run_request,
            request_payload=base_payload,
            resume_decision=resume_decision,
        )
    except ApiError as error:
        if error.code != "task_running":
            raise
        # 补偿扫描（另一个进程）刚好先入了队：决定已经生效，按已提交返回，
        # 不能再报一次错让用户以为没批上。
        async with get_session_factory()() as check_session:
            existing = (
                await check_session.execute(
                    select(AgentRun).where(AgentRun.task_id == task_id)
                )
            ).scalar_one_or_none()
        if existing is None or existing.status not in ("queued", "running"):
            raise
        queued = {
            "task_id": task_id,
            "run_id": existing.id,
            "status": str(existing.status),
        }
    response.status_code = 202
    return {
        "task_id": task_id,
        # 任务进入 queued，用户点完就能走：恢复在后台/独立 worker 里继续。
        "run_id": str(queued.get("run_id") or ""),
        "status": str(queued.get("status") or "queued"),
        "approval_id": approval_id,
    }


async def _current_approval_identity(task_id: str) -> dict:
    """任务此刻在等的那张审批卡的身份（最近一次 approval.required 事件）。"""
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(AgentEvent)
                .where(AgentEvent.task_id == task_id, AgentEvent.type == "approval.required")
                .order_by(AgentEvent.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if row is None or not isinstance(row.payload, dict):
        return {}
    identity = {
        key: str(row.payload.get(key) or "")
        for key in (
            "approval_id",
            "tool_call_id",
            "tool",
            "tool_name",
            "arguments_hash",
            "resource_path",
        )
    }
    # 老中断事件只写了 tool（v0.11.1 之前没有 tool_name 这个别名）。不带身份的
    # 老卡不该因为多一个字段名就批不动：用 tool 补齐，语义完全一致。
    identity["tool_name"] = identity["tool_name"] or identity["tool"]
    return identity


async def _prior_decision(task_id: str, approval_id: str) -> dict | None:
    """这张卡之前批过吗？批过就返回当时的决定（幂等回放）。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(AgentEvent)
                .where(AgentEvent.task_id == task_id, AgentEvent.type == "approval.decided")
                .order_by(AgentEvent.seq)
            )
        ).scalars().all()
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        if str(payload.get("approval_id") or "") == approval_id:
            return {"approve": bool(payload.get("approve")), "scope": str(payload.get("scope") or "")}
    return None


@router.post("/tasks/{task_id}/answer")
async def answer_question(
    task_id: str, payload: TaskAnswerIn, request: Request, db: DbSession
) -> dict:
    """回答 Agent 主动提出的问题：与审批共用同一条中断通道，恢复值换成答案。"""
    task = await load_task(db, task_id)
    if task.status != "waiting_approval":
        raise ApiError(
            code="task_not_waiting_answer",
            message=f"任务当前不在等待输入状态（当前状态：{task.status}）",
            status_code=409,
        )
    evidence = await build_evidence(get_session_factory(), task_id)
    questions = evidence.get("pending_questions") or []
    if not questions:
        raise ApiError(
            code="no_pending_question",
            message="当前没有待回答的问题，这是一次审批请求，请走审批接口",
            status_code=409,
        )
    known = {str(item.get("id") or "") for item in questions}
    # 三种键都认：问题 id、问题原文、header（Claude 的回执按问题文本做键）。
    # 多选答案按 ", " 拼成一段——与 ask._value_text 的落账形状一致。
    answers = {
        key: str(value)[:4000]
        for key, value in parse_answers({"answers": payload.answers}, questions).items()
        if str(value).strip()
    }
    annotation_payload = {
        name: {field: text for field, text in item.model_dump().items() if text}
        for name, item in payload.annotations.items()
    }
    annotation_payload = {name: item for name, item in annotation_payload.items() if item}
    if payload.answer.strip():
        first = str(questions[0].get("id") or "")
        answers.setdefault(first, payload.answer.strip()[:4000])
    if not answers:
        # 空答案有两种含义，方向相反：偏好题是「你看着办」，事实题是「这处留空」。
        # 只有每道题都有推荐项时才放行——默认值由执行图补齐并记账，账只记在一处。
        if not questions_can_default(questions):
            raise ApiError(code="answer_empty", message="答案为空，至少回答一个问题", status_code=422)

    expires_at = evidence.get("question_expires_at") or ""
    if expires_at:
        try:
            datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except ValueError:
            raise ApiError(code="question_invalid", message="问题时间无效", status_code=409) from None
        if approval_is_expired(expires_at):
            raise ApiError(
                code="question_expired",
                message="问题已过期，请重新运行任务",
                status_code=409,
            )

    decided: dict = {"answers": answers, "questions": sorted(known)}
    response = payload.response.strip()[:4000]
    if response:
        decided["response"] = response
    if annotation_payload:
        decided["annotations"] = annotation_payload
    await EventRecorder(get_session_factory(), task_id).add(
        "question.decided", "execute_action", decided
    )
    run_request = await _run_request(
        db, task, TaskRunIn(provider_id=payload.provider_id), request
    )
    decision: dict = {"answers": answers}
    if response:
        decision["response"] = response
    if annotation_payload:
        decision["annotations"] = annotation_payload
    evidence = await AgentRuntime(get_session_factory()).resume(run_request, decision=decision)
    async with get_session_factory()() as session:
        finished = await load_task(session, task_id)
        return {"task": TaskOut.model_validate(finished).model_dump(), "evidence": evidence}


@router.post("/tasks/{task_id}/fork")
async def fork_task(task_id: str, payload: TaskForkIn, db: DbSession) -> dict:
    """从某个线程的最新检查点分叉出一条新线程。

    分叉不会改动源线程：`trunk_reuse` 复制检查点（可从中断处继续），
    `trunk_new` 只留血缘记录。两者都写 thread.forked 事件，线程号也不会撞车。
    """
    task = await load_task(db, task_id)
    if task.status == "running":
        raise ApiError(
            code="task_running",
            message="任务正在运行，请先等待当前步骤结束或暂停后再分叉",
            status_code=409,
        )
    result = await fork_thread(
        get_session_factory(),
        task_id,
        from_thread_id=payload.from_thread_id,
        strategy=payload.strategy,
    )
    await EventRecorder(get_session_factory(), task_id).add(
        FORK_EVENT, "fork", {**result.as_dict(), "status": task.status}
    )
    return {
        "task_id": task_id,
        "thread_id": result.thread_id,
        "forked_from_thread_id": result.forked_from_thread_id,
        "strategy": result.strategy,
        "checkpoints_copied": result.checkpoints_copied,
        "writes_copied": result.writes_copied,
    }


@router.post("/tasks/{task_id}/steer")
async def steer_task(task_id: str, payload: TaskSteerIn, db: DbSession) -> dict:
    """运行中插话：立刻入队，从下一次模型调用开始生效。

    这里只负责排队与审计，不假装能撤回已经执行的动作——插话的语义是
    "改变接下来的方向"，不是"撤销刚才的结果"。
    """
    task = await load_task(db, task_id)
    if task.status not in ("running", "waiting_approval", "paused", "queued"):
        raise ApiError(
            code="task_not_steerable",
            message=f"任务已结束，无法插话（当前状态：{task.status}）",
            status_code=409,
        )
    queued_at = int(time.time() * 1000)
    # 插话同样接受 `/名字 参数`：`message` 留给界面显示用户原话，模型读 `expanded`。
    project = await load_project(db, task.project_id)
    workspace = project.active_workspace
    expanded, command_info = expand_user_input(
        payload.message,
        root=workspace.path if workspace is not None else None,
        session_id=str(task.thread_id or task.id),
    )
    recorder = EventRecorder(get_session_factory(), task_id)
    if command_info is not None:
        await recorder.add(
            "command.expanded",
            "receive_task",
            {"task_id": task_id, "channel": "steer", **command_info},
        )
    await recorder.add(
        "steer.queued",
        "user",
        {
            "message": payload.message.strip(),
            **({"expanded": expanded} if command_info is not None else {}),
            # 展开载荷随插话一起入队：抽干的那边拿它跑**同一份** user_prompt_expansion
            # 钩子，载荷与提交路径逐字一致。与任务行上的 `expansion` 列不同，这里走的是
            # 审计事件，落库前过 mask_deep，密钥形态的参数会被抹掉（插话没有可落的列）。
            **expansion_payload(command_info),
            "queued_at": queued_at,
            "step": int(task.steps_used or 0),
        },
    )
    return {
        "task_id": task_id,
        "queued": True,
        "queued_at": queued_at,
        "status": task.status,
        "applies": "下一次模型调用生效",
    }


@router.post("/tasks/{task_id}/cancel", response_model=TaskOut)
async def cancel_task(task_id: str, db: DbSession) -> TaskOut:
    task = await load_task(db, task_id)
    if task.status in ("succeeded", "failed", "cancelled"):
        raise ApiError(code="task_not_cancellable", message=f"任务已结束，无法取消（当前状态：{task.status}）", status_code=409)
    task.status = "cancelled"
    await db.commit()
    # 与 skills.py 同一形状：updated_at 带 onupdate，提交后不读回来就出参会在
    # 同步上下文里触发惰性加载（MissingGreenlet → 500）。
    await db.refresh(task)
    recorder = EventRecorder(get_session_factory(), task_id)
    await recorder.add(
        "task.cancel_requested", "receive_task", {"status": "cancelled"}
    )
    # 取消是终态，必须当场落一条终态事件。
    #
    # 以前的形状只写 cancel_requested，真正的 task.cancelled 留给运行时收尾去写。
    # 可运行时不一定还能走到收尾：模型调用挂在那儿、任务已经被别处标记结束、
    # 或者进程已经退出，就没有人再补那一条。界面靠终态事件判断"这一轮结束了"，
    # 于是一直显示"进行中"、一直把用户的输入当成"插话"发给一个已结束的任务，
    # 全部被打回 409"任务已结束，无法插话"——用户看到的就是彻底不能用。
    if not await recorder.has_terminal():
        await recorder.add(
            "task.cancelled",
            "receive_task",
            {"status": "cancelled", "reason": "用户取消了这一轮"},
        )
    return TaskOut.model_validate(task)


async def _stored_run_payload(db: DbSession, task_id: str) -> TaskRunIn:
    """恢复/重试要沿用这条任务当时的运行参数，不能用今天设置页里的默认值。"""
    run = (
        await db.execute(select(AgentRun).where(AgentRun.task_id == task_id))
    ).scalar_one_or_none()
    raw = dict(run.request_payload or {}) if run is not None else {}
    try:
        return TaskRunIn.model_validate(raw)
    except Exception:  # noqa: BLE001 - 旧入参形状不认识时按默认跑，不阻断恢复
        return TaskRunIn()


@router.post("/tasks/{task_id}/pause", response_model=TaskOut)
async def pause_task(task_id: str, db: DbSession) -> TaskOut:
    """暂停后台任务：状态落 paused、租约释放，执行节点在下一步停下。

    暂停不是取消——任务还在，恢复后接着跑。所以这里不写 cancelled 终态，
    也不删事件。执行侧每一步都会读一次任务状态（graph.execute_action_node），
    因此暂停在「下一次模型调用之前」生效，不会打断正在进行的单次调用。
    """
    task = await load_task(db, task_id)
    if task.status in ("succeeded", "failed", "cancelled"):
        raise ApiError(
            code="task_not_pausable",
            message=f"任务已结束，无法暂停（当前状态：{task.status}）",
            status_code=409,
        )
    if task.status == "paused":
        return TaskOut.model_validate(task)
    task.status = "paused"
    run = (
        await db.execute(select(AgentRun).where(AgentRun.task_id == task_id))
    ).scalar_one_or_none()
    if run is not None and run.status not in ("succeeded", "failed", "cancelled"):
        # 释放租约：暂停期间不允许别的 worker 把它领走继续跑。
        run.status = "paused"
        run.worker_id = ""
        run.lease_expires_at = None
    await db.commit()
    await db.refresh(task)
    recorder = EventRecorder(get_session_factory(), task_id)
    await recorder.add("task.pause_requested", "receive_task", {"status": "paused"})
    if not await recorder.has_terminal(paused_counts=True):
        await recorder.add(
            "task.paused", "receive_task", {"status": "paused", "reason": "用户暂停了任务"}
        )
    return TaskOut.model_validate(task)


@router.post("/tasks/{task_id}/resume", response_model=TaskOut)
async def resume_task(task_id: str, request: Request, db: DbSession) -> TaskOut:
    """恢复一条已暂停的任务：从最近检查点继续，不重跑已经完成的工具。"""
    task = await load_task(db, task_id)
    if task.status != "paused":
        raise ApiError(
            code="task_not_paused",
            message=f"只有已暂停的任务可以恢复（当前状态：{task.status}）",
            status_code=409,
        )
    payload = await _stored_run_payload(db, task_id)
    await EventRecorder(get_session_factory(), task_id).add(
        "task.resume_requested", "receive_task", {"status": "queued"}
    )
    await launch_task(db, task, payload, request, crashed=True)
    await db.refresh(task)
    return TaskOut.model_validate(task)


@router.post("/tasks/{task_id}/retry", response_model=TaskOut)
async def retry_task(task_id: str, request: Request, db: DbSession) -> TaskOut:
    """重试：把一条失败/取消/暂停的任务重新排进队列。

    与"恢复"的区别：重试是从头再跑一遍（attempts 归零），恢复是从检查点接着跑。
    失败后先改代码再重试是常见用法，所以这里不沿用旧的工具结果。
    """
    task = await load_task(db, task_id)
    if task.status in ("running", "queued", "waiting_approval"):
        raise ApiError(code="task_running", message="任务仍在执行中，不能重试", status_code=409)
    if task.status == "paused":
        # 暂停态的重试语义就是"继续"，交给恢复路径，避免两套状态机。
        return await resume_task(task_id, request, db)
    payload = await _stored_run_payload(db, task_id)
    await EventRecorder(get_session_factory(), task_id).add(
        "task.retry_requested", "receive_task", {"from": task.status}
    )
    await launch_task(db, task, payload, request)
    await db.refresh(task)
    return TaskOut.model_validate(task)


def _iso_or_none(value) -> str | None:
    return value.isoformat() if value else None


def _delivery_verify(payload: dict | None) -> dict:
    """把交付事件里的 ``verify`` 摊平成接口字段。

    以前只回 ``delivery.present``：只要有交付事件就算交付了，未验证交付与已验证交付
    在接口上完全一样——「未跑校验」只活在正文那一行文本里，机器读不到。这里把它
    提到结构化字段，下游（前端标记、通知决策、验收统计）才有判据可用。

    ``observed`` 说明「有没有一条真实交付记录」：没有记录时其余字段全是默认值，
    把默认值读成「无缺口」是错的。
    """
    verify = (payload or {}).get("verify")
    if not isinstance(verify, dict):
        return {"observed": False, "command": "", "exit": None, "missing": False, "stale": False, "covers": []}
    raw_exit = verify.get("exit")
    covers = verify.get("covers")
    return {
        "observed": True,
        "command": str(verify.get("command") or ""),
        "exit": raw_exit if isinstance(raw_exit, int) and not isinstance(raw_exit, bool) else None,
        "missing": verify.get("missing") is True,
        "stale": verify.get("stale") is True,
        # 这条校验覆盖的改动文件：交付时说得出「验的是什么」，不只一个退出码。
        "covers": [str(name) for name in covers] if isinstance(covers, list) else [],
    }


@router.get("/tasks/{task_id}/status")
async def task_status(task_id: str, db: DbSession) -> dict:
    """结构化任务状态（任务书 §6）。

    前端不许靠"最后一条文本"猜状态：这里把 task / run / trigger / 最后事件 /
    用量 / 审批 / 改动指纹读成一份结构化快照，缺哪项就显式给 null。
    """
    task = await load_task(db, task_id)
    run = (
        await db.execute(select(AgentRun).where(AgentRun.task_id == task_id))
    ).scalar_one_or_none()
    trigger = (
        await db.execute(
            select(AgentTrigger)
            .where(AgentTrigger.task_id == task_id)
            .order_by(AgentTrigger.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    last_event = (
        await db.execute(
            select(AgentEvent.type, AgentEvent.node, AgentEvent.created_at)
            .where(AgentEvent.task_id == task_id)
            .order_by(AgentEvent.seq.desc())
            .limit(1)
        )
    ).first()
    approval = (
        await db.execute(
            select(ToolCall.name, ToolCall.created_at)
            .where(ToolCall.task_id == task_id, ToolCall.status == "awaiting_approval")
            .order_by(ToolCall.created_at)
            .limit(1)
        )
    ).first()
    approval_payload = (
        {"tool": approval[0], "waiting_since": _iso_or_none(approval[1])}
        if approval is not None
        else None
    )
    if approval_payload is None and task.status == "waiting_approval":
        # 图在工具网关之前就 interrupt() 时不会留下 ToolCall 行（审批卡还没落到工具账上），
        # 只查 ToolCall 会让「正在等审批」的任务显示成「没有审批」——前端于是既不弹卡片、
        # 也不提示，用户只能看着任务挂着。退回最近一次审批事件，与 build_evidence 同口径。
        row = (
            await db.execute(
                select(AgentEvent.payload, AgentEvent.created_at)
                .where(
                    AgentEvent.task_id == task_id,
                    AgentEvent.type.in_(("approval.required", "approval.requested")),
                )
                .order_by(AgentEvent.seq.desc())
                .limit(1)
            )
        ).first()
        if row is not None:
            payload = dict(row[0] or {})
            approval_payload = {
                "tool": str(payload.get("tool") or ""),
                "waiting_since": _iso_or_none(row[1]),
                "risk_level": str(payload.get("risk_level") or "high"),
            }
    changes = (
        await db.execute(
            select(FileChange.path, FileChange.change_type)
            .where(FileChange.task_id == task_id)
            .order_by(FileChange.path)
        )
    ).all()
    usage = (
        await db.execute(
            select(
                func.coalesce(func.sum(ModelUsage.prompt_tokens + ModelUsage.completion_tokens), 0),
                func.count(),
            ).where(ModelUsage.task_id == task_id)
        )
    ).first()
    model_name = (
        await db.execute(
            select(ModelUsage.model)
            .where(ModelUsage.task_id == task_id)
            .order_by(ModelUsage.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    delivered = (
        await db.execute(
            select(AgentEvent.id)
            .where(
                AgentEvent.task_id == task_id,
                AgentEvent.type.in_(("task.completed", "validation.completed", "delivery.recorded")),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    delivery_payload = (
        await db.execute(
            select(AgentEvent.payload)
            .where(AgentEvent.task_id == task_id, AgentEvent.type == "delivery.recorded")
            .order_by(AgentEvent.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    snapshot = dict(task.dispatch_snapshot or {})
    notify_row = (
        await db.execute(
            select(AgentEvent.payload, AgentEvent.created_at)
            .where(AgentEvent.task_id == task_id, AgentEvent.type == "notify.decided")
            .order_by(AgentEvent.seq.desc())
            .limit(1)
        )
    ).first()
    _last_notify_decision = (
        {**dict(notify_row[0] or {}), "at": _iso_or_none(notify_row[1])}
        if notify_row is not None
        else None
    )
    fingerprint = (
        hashlib.sha256(
            "|".join(f"{path}:{kind}" for path, kind in changes).encode("utf-8")
        ).hexdigest()[:16]
        if changes
        else ""
    )
    return {
        "task_id": task.id,
        "title": task.title,
        "project_id": task.project_id,
        # 会话归属：关窗、换设备、从任务列表点回来都靠它把用户送回原来那条对话。
        # 历史任务可能没有（thread 被删过），显式给 null，前端走孤儿任务兜底。
        "conversation_id": task.thread_id or None,
        "turn_index": int(task.turn_index or 0),
        "updated_at": _iso_or_none(task.updated_at),
        "run_id": run.id if run is not None else None,
        "trigger_id": trigger.id if trigger is not None else None,
        "worker_id": (run.worker_id or None) if run is not None else None,
        "snapshot_id": snapshot.get("snapshot_id"),
        "status": task.status,
        "current_step": int(task.steps_used or 0),
        "last_heartbeat": _iso_or_none(run.updated_at) if run is not None else None,
        "last_event": (
            {"type": last_event[0], "node": last_event[1], "at": _iso_or_none(last_event[2])}
            if last_event is not None
            else None
        ),
        "model": model_name,
        "tokens": {"total": int((usage[0] if usage else 0) or 0), "calls": int((usage[1] if usage else 0) or 0)},
        "approval": approval_payload,
        "changes": {
            "count": len(changes),
            "paths": [path for path, _ in changes],
            "diff_fingerprint": fingerprint,
        },
        "delivery": {
            # 只认真正的交付事件。以前这里把 task.completed / validation.completed
            # 也算进来，于是「跑到校验就崩、从未交付」的任务显示成已交付且无缺口——
            # 新的 verify 读者就建在这个过宽的口径上，两个字段会互相矛盾。
            "present": delivery_payload is not None,
            "summary": task.result_summary,
            # 未验证交付要能被机器判出来，不能只靠正文那一行。
            "verify": _delivery_verify(delivery_payload),
            # 老任务没有 delivery.recorded，但确实跑完了：如实标出来，
            # 别让它看起来和"已交付且无缺口"一样。
            "legacy": delivered is not None and delivery_payload is None,
        },
        "dispatch": {
            "intent": snapshot.get("intent"),
            "scenario": snapshot.get("scenario"),
            "notify_policy": snapshot.get("notify_policy"),
            "memory_policy": snapshot.get("memory_policy"),
        },
        # 低打扰（§5）：前端读策略 + 最近一次 notify.decided 判定，不靠"有没有新文本"猜要不要提醒。
        "notify": {
            "policy": str(snapshot.get("notify_policy") or NOTIFY_APPROVAL_ONLY),
            "last": _last_notify_decision,
        },
    }
