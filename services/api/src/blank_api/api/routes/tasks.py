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
from fastapi import APIRouter, Depends, Query, Request, Response
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
from blank_api.services.agent import notify
from blank_api.services.settings import SettingsStore, quiet_hours_active
from blank_api.services.agent.live import LiveFeed
from blank_api.services import metrics
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.agent.vision import supports_vision
from blank_api.services.fork import FORK_EVENT, fork_thread
from blank_api.services.agent.manager import RunAlreadyActive, run_manager
from blank_api.services.agent.queue import abandon_run, claim_run, enqueue_run, finish_run, renew_run
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
from blank_api.services.uploads import UploadStore, load_attachments_for
from blank_api.services.models.routing import FallbackAdapter
from blank_api.services.sandbox import SnapshotNotRestorable, SnapshotStore

router = APIRouter(tags=["tasks"], dependencies=[Depends(require_token)])
log = logging.getLogger(__name__)

# 自动重排队的上限：超过就不再无限重试，避免一条坏 run 在后台反复空转。
# 这是发布时钉死的**硬上限**；设置页的 background.auto_recoveries 只能把它调小。
MAX_RECOVERY_ATTEMPTS = 3
# 租约 90 秒，心跳 30 秒：允许一次心跳没写成，不让过期 worker 继续干活。
LEASE_HEARTBEAT_SECONDS = 30


def recovery_attempt_limit() -> int:
    """本轮实际生效的自动恢复次数（v165 §14 让设置真正被消费）。

    读设置失败按硬上限走：宁可多重试一次，也不要因为配置文件坏了就判死一条
    本来能恢复的任务。设置只能收紧，不能放宽——否则一份手改的 settings.json
    就能绕过发布闸门，把一条坏 run 重排到天荒地老。
    """
    try:
        policy = resolve_background(SettingsStore().read())
    except Exception:  # noqa: BLE001 - 设置读不出来时按硬上限
        return MAX_RECOVERY_ATTEMPTS
    return min(MAX_RECOVERY_ATTEMPTS, recovery_limit(policy))


def inprocess_runner_enabled() -> bool:
    """API 进程是否自己执行 queued run。

    单机桌面形态保持默认开启；多进程部署把它关掉，run 由
    ``python -m blank_api.worker`` 领取，否则两边会抢同一条队列。
    """
    raw = (os.environ.get("BLANK_INPROCESS_RUNNER") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")

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


def _factory_from(request: Request | None):
    """Tests embed a mock transport here; production falls back to httpx.AsyncClient."""
    return getattr(request.app.state, "model_client_factory", None) if request is not None else None


async def _resolve_provider(db: DbSession, task: Task, requested: str | None) -> ModelProvider:
    provider_id = requested or task.provider_id
    if provider_id:
        provider = await db.get(ModelProvider, provider_id)
        if provider is None:
            raise ApiError(code="provider_not_found", message="模型供应商不存在", status_code=404)
        if not provider.enabled:
            raise ApiError(code="provider_disabled", message="该模型供应商已停用", status_code=409)
        return provider
    # 默认模型优先挑「配了 key 的」：只按名字取第一个启用项，很容易挑到模板里预置但
    # 没填 key 的供应商，用户第一次发消息就是 401，还得自己猜是哪一环出的问题。
    # 全都没配 key 时退回第一个启用项，报错仍然来自供应商，不会凭空成功。
    provider = (
        await db.execute(
            select(ModelProvider)
            .where(ModelProvider.enabled == True)  # noqa: E712
            .order_by(
                case((ModelProvider.api_key_encrypted != "", 0), else_=1),
                ModelProvider.name,
            )
        )
    ).scalars().first()
    if provider is None:
        raise ApiError(
            code="provider_not_configured",
            message="尚未配置可用的模型供应商，请先在设置中添加模型后运行任务",
            status_code=409,
        )
    return provider


async def persist_task(
    db: DbSession,
    project_id: str,
    payload: TaskCreate,
    *,
    thread_id: str | None = None,
    turn_index: int = 0,
) -> Task:
    """把一条任务写进库并发一条 task.created。

    独立任务和"会话里的一轮"共用它：``thread_id`` / ``turn_index`` 在建行时就带上，
    而不是建完再 UPDATE 一次——那次 UPDATE 会把服务端生成的 ``updated_at`` 置为过期，
    紧接着序列化它就会在异步会话里触发一次同步懒加载（MissingGreenlet）。

    ``created_at``/``updated_at`` 是服务端生成的，插入后 ORM 并不知道它们的值。
    SQLite 会用 RETURNING 顺手带回来，MySQL 8.4 的 INSERT 没有 RETURNING：不先读回，
    序列化时同样是一次同步懒加载。所以这里统一 refresh，两种方言走同一条路。
    """
    # 附件先过一遍存在性检查：前端传来的 id 可能是上一次上传后又被清理掉的，
    # 那种直接丢掉，不要带着一个指向空气的引用进任务。
    attachments = [
        ref.as_dict()
        for ref in UploadStore().resolve(project_id, [item.model_dump() for item in payload.attachments])
    ]
    # 验收条件与目标在这里归一化，而不是等 Agent 去猜：
    # 建行这一刻就把形状定下来，后面验收循环、证据页、前端读到的才是同一份东西。
    criteria = normalize_criteria(payload.acceptance_criteria)
    goal_spec = normalize_goal(payload.goal_spec, fallback=payload.goal or payload.title)
    # v0.10.1 §4：创建时用户选的权限档位钉进快照链。_run_request 的回落顺序是
    # 运行请求 → goal_spec.dispatch.permission_mode → 设置页默认；落在中间一层，
    # 之后重试/恢复不传模式也不会漂回全局默认。（threads 路径随后会整份覆盖
    # goal_spec.dispatch，两处口径一致，互不冲突。）
    if payload.permission_mode:
        goal_spec = {
            **goal_spec,
            "dispatch": {
                **(goal_spec.get("dispatch") or {}),
                "permission_mode": payload.permission_mode,
            },
        }

    # 套用计划模板：步骤原样带过来，但状态一律重置为 pending。
    # 模板是"怎么做事"，不是"做到哪了"——把完成态一起带过来会显示一份假进度。
    plan_steps: list[dict] = []
    milestones: list[dict] = []
    if payload.plan_template_id:
        template = await db.get(PlanTemplate, payload.plan_template_id)
        if template is None or template.project_id != project_id:
            raise ApiError("plan_template_not_found", "计划模板不存在", 404)
        plan_steps = [
            {**step, "status": "pending", "evidence": ""}
            for step in (template.steps or [])
            if isinstance(step, dict)
        ]
        milestones = [
            {**item, "status": "pending"}
            for item in (template.milestones or [])
            if isinstance(item, dict)
        ]
        if not payload.goal_spec and template.goal_spec:
            goal_spec = normalize_goal(template.goal_spec, fallback=payload.goal or payload.title)
        template.usage_count = int(template.usage_count or 0) + 1

    task = Task(
        id=str(uuid.uuid4()),
        project_id=project_id,
        provider_id=payload.provider_id,
        scenario=payload.scenario or "dev",
        title=payload.title,
        goal=payload.goal or payload.title,
        status="draft",
        acceptance_criteria=criteria,
        acceptance_results=[],
        goal_spec=goal_spec,
        plan_steps=plan_steps,
        milestones=milestones,
        attachments=attachments,
        max_steps=payload.max_steps,
        thread_id=thread_id,
        turn_index=turn_index,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    await EventRecorder(get_session_factory(), task.id).add(
        "task.created",
        "receive_task",
        {
            "title": task.title,
            "acceptance_criteria": list(task.acceptance_criteria or []),
            "attachments": len(attachments),
        },
    )
    await _record_dispatch(db, task)
    return task


async def _record_dispatch(db: DbSession, task: Task) -> None:
    """创建时算一次派发快照并落库（v162，特性开关 super_agent_dispatch）。

    为什么在**创建**这一刻算：派发方式（现在跑 / 排队 / 等事件 / 先问人）是用户
    点发送后立刻要给出的反应，等到模型判完意图才决定"这条该不该排队"已经晚了。
    为什么落库而不是留在内存：执行可能发生在另一个进程（独立 worker）或几小时后
    （断点恢复），那时内存里那份早没了，只剩库能回答「当时按什么权限派的」。

    开关关着时整段跳过：不写快照、不发事件，行为与引入 Router 之前一致。
    """
    if not is_enabled("super_agent_dispatch"):
        return
    if (task.dispatch_snapshot or {}).get("snapshot_id"):
        # 已有快照就不再覆盖：重复调用 persist 时重新路由会让"当时那份决定"漂移。
        return
    # 沙箱档位也一起钉进快照：它是"这件事跑在哪"的决定，事后传参改不了它（v164 §5.3）。
    # 读设置失败按默认档位走——比让整个任务建不出来更合理。
    try:
        default_tier = str(
            (SettingsStore().read().get("sandbox") or {}).get("default_tier") or "workspace"
        )
    except Exception:  # noqa: BLE001 - 设置读不出来时按默认档位，不影响建任务
        default_tier = "workspace"
    snapshot = route(
        task.goal or task.title,
        scenario=task.scenario or "dev",
        max_steps=int(task.max_steps or 0) or None,
        sandbox_tier=default_tier,
    )
    # v165 §14：设置页的 max_steps / max_seconds / token_budget / auto_recoveries
    # 折进这份快照。budget_override 逐项取 min，只能收紧；设置读不出来时不改。
    # route() 保持纯函数（同样的入参永远得到同样的快照），夹取只发生在落库这一层。
    try:
        background = resolve_background(SettingsStore().read())
        tightened = budget_override(background, snapshot.budget.as_dict())
        snapshot = replace(snapshot, budget=Budget(**tightened))
        snapshot.guard()
    except Exception:  # noqa: BLE001 - 设置读坏/夹取异常都不该让任务建不出来
        log.warning("后台预算夹取失败，按发布默认预算继续", exc_info=True)
    task.dispatch_snapshot = snapshot.as_dict()
    await db.commit()
    # commit 会让 ORM 实例过期，之后序列化 created_at/updated_at 会触发同步懒加载
    # （异步会话里直接 MissingGreenlet）。和 persist_task 里同一条规矩：写完读回。
    await db.refresh(task)
    await EventRecorder(get_session_factory(), task.id).add(
        "dispatch.decided",
        "receive_task",
        {
            "snapshot_id": snapshot.snapshot_id,
            "intent": snapshot.intent,
            "dispatch": snapshot.dispatch,
            "tool_policy": snapshot.tool_policy,
            "notify_policy": snapshot.notify_policy,
            "memory_policy": snapshot.memory_policy,
            "priority": snapshot.priority,
            "high_risk": snapshot.high_risk,
            "budget": snapshot.budget.as_dict(),
            "why": explain(snapshot),
        },
    )


def dispatch_of(task: Task) -> ExecutionSnapshot | None:
    """读回任务上的快照。开关关着、或老数据没有快照时返回 None。"""
    if not is_enabled("super_agent_dispatch"):
        return None
    raw = task.dispatch_snapshot or {}
    if not isinstance(raw, dict) or not raw:
        return None
    return ExecutionSnapshot.from_dict(raw)


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
    task = await persist_task(db, project.id, payload, thread_id=thread.id, turn_index=1)
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
    return TaskOut.model_validate(task)


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


async def _run_request(db: DbSession, task: Task, payload: TaskRunIn, request: Request) -> RunRequest:
    project = await load_project(db, task.project_id)
    workspace = project.active_workspace
    if workspace is None:
        raise ApiError(code="workspace_not_bound", message="该项目尚未绑定工作区目录", status_code=409)
    provider = await _resolve_provider(db, task, payload.provider_id)
    task.provider_id = provider.id
    # 场景：运行请求显式指定（用户切了场景）时覆盖存档并写回；否则用创建时的。
    # 未知值在 resolve() 里回退开发场景，脏数据不改变默认行为。
    scenario_key = payload.scenario or task.scenario or "dev"
    task.scenario = scenario_key
    await db.commit()
    # 派发快照（v162）：执行侧的唯一权限来源。只往更严的方向收紧——
    # 步数取「任务上限」与「快照预算」的较小者，只读就整回合禁写。
    # 开关关着或是老数据时为 None，下面三行都不生效，行为与从前一致。
    snapshot_of_task = dispatch_of(task)
    max_steps = int(task.max_steps)
    readonly = False
    dispatch_payload: dict = {}
    if snapshot_of_task is not None:
        snapshot_of_task = snapshot_of_task.tightened(max_steps=max_steps)
        max_steps = snapshot_of_task.budget.max_steps
        readonly = snapshot_of_task.readonly
        dispatch_payload = snapshot_of_task.as_dict()
    client_factory = _factory_from(request)
    adapters = [build_adapter(provider, client_factory)]
    # 路由候选：本次任务已经选定的供应商（主用 + 兜底 + 分角色），不会扩到别处。
    # 只在这几个里面挑，是因为「换更便宜的模型」和「换个供应商跑」是两件事，
    # 后者必须由用户显式指定过。
    routing_rows: dict[str, ModelProvider] = {provider.id: provider}
    routing_adapters: dict = {provider.id: adapters[0]}
    if payload.fallback_provider_ids:
        fallback_rows = (
            await db.execute(
                select(ModelProvider).where(
                    ModelProvider.id.in_(payload.fallback_provider_ids),
                    ModelProvider.enabled == True,  # noqa: E712
                )
            )
        ).scalars().all()
        by_id = {row.id: row for row in fallback_rows}
        for fallback_id in payload.fallback_provider_ids:
            fallback = by_id.get(fallback_id)
            if fallback is not None and fallback.id != provider.id:
                adapters.append(build_adapter(fallback, client_factory))
                routing_rows.setdefault(fallback.id, fallback)
                routing_adapters.setdefault(fallback.id, adapters[-1])
    adapter = FallbackAdapter(adapters) if len(adapters) > 1 else adapters[0]
    role_adapters = {}
    role_provider_ids = {}
    if payload.role_provider_ids:
        role_rows = (
            await db.execute(
                select(ModelProvider).where(
                    ModelProvider.id.in_(list(payload.role_provider_ids.values())),
                    ModelProvider.enabled == True,  # noqa: E712
                )
            )
        ).scalars().all()
        by_id = {row.id: row for row in role_rows}
        for role, provider_id in payload.role_provider_ids.items():
            role_provider = by_id.get(provider_id)
            if role_provider is not None:
                role_adapters[role] = build_adapter(role_provider, client_factory)
                role_provider_ids[role] = role_provider.id
                routing_rows.setdefault(role_provider.id, role_provider)
                routing_adapters.setdefault(role_provider.id, role_adapters[role])
    active_skills = (
        await db.execute(select(Skill.instructions).where(Skill.status == "active").order_by(Skill.updated_at))
    ).scalars().all()
    # rollback 会让本事务里的 ORM 实例全部过期，之后再读属性会触发同步懒加载，
    # 在异步会话里直接抛 MissingGreenlet。所以先把要用的字段取成普通值。
    attachment_refs = [dict(item) for item in (task.attachments or [])]
    # v0.10.1 §4.3：权限快照链的「任务」一环。发送栏选的档在创建时刻写进了
    # 下发快照（goal_spec.dispatch.permission_mode），运行请求没显式带模式时
    # 优先用它，其次才是运行时自己的回落（同任务历史模式 → 设置页默认）。
    # 没有这一环，「任务创建后改全局默认档」会悄悄改变正在跑的任务的审批行为。
    task_permission_mode = str(
        ((task.goal_spec or {}).get("dispatch") or {}).get("permission_mode") or ""
    )
    # 读附件放到线程里：图片最多 20MB、PDF 还要解析，同步读会把事件循环按住，
    # 这期间连 /health 都不回。
    bundle = await anyio.to_thread.run_sync(load_attachments_for, project.id, attachment_refs)
    if bundle.dropped:
        # 少给了就必须说出来。用户看到 5 个附件、模型只用上 3 个而界面一个字不提，
        # 比直接报错更糟：他会以为模型没看图，而不是"这轮塞不下"。
        await EventRecorder(get_session_factory(), task.id).add(
            "attachment.trimmed",
            "receive_task",
            bundle.summarize(),
        )
    snapshot = {
        "task_id": task.id,
        "provider_id": provider.id,
        "provider_name": provider.name,
        "root": workspace.path,
        "title": task.title,
        "goal": task.goal,
        "acceptance_criteria": criteria_text(task.acceptance_criteria),
        # 结构化约束（constraints/out_of_scope）随任务带进运行时：它们要进首轮提示，
        # 光存库不进提示等于模型从没见过用户声明的边界。
        "goal_spec": dict(task.goal_spec or {}) or None,
        # 结构化的一份单独带过去：会话主体只吃字符串，但收尾判定要知道
        # 每条验收对应哪几步——这个映射不能靠再回表读一次。
        "acceptance_links": normalize_criteria(task.acceptance_criteria),
        "max_steps": max_steps,
        "images": bundle.images,
        "texts": bundle.texts,
        # 能不能看图取决于模型名，不只看那个开关：把模型换成 glm-4.5v 之后，
        # 开关不会自己变，图会被静默降级。见 services/agent/vision.py。
        "supports_vision": supports_vision(bool(provider.supports_vision), provider.default_model),
        "skill_instructions": [str(item) for item in active_skills],
        # 会话上下文由运行时自己读：这里只告诉它"这一轮属于哪条会话"。
        "thread_ref": task.thread_id or "",
        # 场景决定系统提示附加段、工具白名单与记忆权限；只走系统提示与工具层，
        # 绝不向用户 prompt 拼前缀（见 scenarios.py 的设计红线）。
        "scenario": scenario_key,
        # 空串＝沿用设置页默认档；运行时负责解析、降级与拒绝，路由层不预先猜。
        "sandbox_tier": payload.sandbox_tier,
        # 派发快照与它决定的只读边界。空 dict / False = 这条 run 不按快照跑。
        "dispatch": dispatch_payload,
        "readonly": readonly,
    }
    # 候选必须在这里算：下面 rollback 会让 ORM 实例过期，之后读 api_key_encrypted
    # 会触发同步懒加载，在异步会话里直接抛 MissingGreenlet。
    routing_pool = [
        candidate_from_provider(row) for row in routing_rows.values() if _routable(row)
    ]
    # The runtime owns its own sessions; end this request transaction first so a
    # single-connection SQLite backend never interleaves them mid-run.
    await db.rollback()
    return RunRequest(
        **snapshot,
        adapter=adapter,
        approve_high_risk=payload.approve_high_risk,
        approved_tools=set(payload.approved_tools),
        approved_actions=set(payload.approved_actions),
        approved_command_prefixes=tuple(payload.approved_command_prefixes),
        # 任务快照优先于全局默认：runtime.run 里 `if not request.permission_mode`
        # 的回落链仍在其后兜底（同任务历史 → 设置页默认）。
        permission_mode=payload.permission_mode or task_permission_mode,
        memory_opt_in=payload.memory_opt_in,
        role_adapters=role_adapters,
        role_provider_ids=role_provider_ids,
        routing_pool=routing_pool,
        routing_adapters=routing_adapters,
    )


def _routable(provider: ModelProvider) -> bool:
    """没配 Key 的供应商不参与路由。

    它留在列表里只是占位配置（用户加了还没填密钥），把它选进来等于把任务
    交给一个必然 401 的端点——而路由失败不会像兜底那样静默降级，是直接报错。
    Ollama 是本机端点，本来就不需要 Key。
    """
    if provider.kind == "ollama":
        return True
    return bool(provider.api_key_encrypted)


@router.post("/tasks/{task_id}/run")
async def run_task(task_id: str, payload: TaskRunIn, request: Request, db: DbSession) -> dict:
    task = await load_task(db, task_id)
    if task.status == "running":
        raise ApiError(code="task_running", message="任务正在执行中", status_code=409)
    run_request = await _run_request(db, task, payload, request)
    evidence = await AgentRuntime(get_session_factory()).run(run_request)
    async with get_session_factory()() as session:
        finished = await load_task(session, task_id)
        return {"task": TaskOut.model_validate(finished).model_dump(), "evidence": evidence}


async def _record_run_failure(task_id: str, run_id: str, error: BaseException) -> None:
    """把「跑不起来」写进事件流和任务状态。

    之前这里只把 agent_runs 标成 failed，任务自己停在 queued、事件流里一条都没有——
    界面上就是「输入之后没有任何返回」，用户既看不到原因也点不动。凡是异常都必须
    变成一条 task.failed 事件 + failed 状态。
    """
    message = str(error) or error.__class__.__name__
    detail = "".join(traceback.format_exception(type(error), error, error.__traceback__))[-4000:]
    try:
        # runtime 正常收尾时自己就会写 task.failed（带 reason），别写第二条。
        async with get_session_factory()() as session:
            already = (
                await session.execute(
                    select(AgentEvent.id).where(
                        AgentEvent.task_id == task_id,
                        AgentEvent.run_id == run_id,
                        AgentEvent.type == "task.failed",
                    )
                )
            ).first()
        if already is None:
            await EventRecorder(get_session_factory(), task_id, run_id=run_id).add(
                "task.failed",
                "error",
                {
                    "error": message,
                    "reason": message,
                    "error_type": error.__class__.__name__,
                    "traceback": detail,
                },
            )
    except Exception:  # 记事件本身失败也不能把进程带崩
        log.exception("写入 task.failed 事件失败 task=%s", task_id)

    async def mark_failed() -> None:
        async with get_session_factory()() as session:
            task = await session.get(Task, task_id)
            if task is not None and task.status not in ("cancelled", "succeeded"):
                task.status = "failed"
                task.failure_reason = message[:2000]
                await session.commit()

    try:
        # 连接池里可能躺着一条被取消打断的旧连接，换新连接重试即可
        await db_retry(mark_failed)
    except Exception:
        log.exception("更新任务失败状态出错 task=%s", task_id)


async def _execute_claimed_run(
    run_id: str,
    worker_id: str,
    run_request: RunRequest,
    *,
    crashed: bool = False,
    lease_seconds: int = 90,
) -> None:
    """Run one claimed job and keep its database lease alive while it works.

    ``crashed``：这条 run 之前已经被认领过（attempts>0），上次执行没写终态。
    那种情况必须从检查点续跑，不能再开一个新 attempt 从头跑——工具已经执行过的
    那一步会因此再执行一遍。
    """
    runtime = AgentRuntime(get_session_factory())
    call = runtime.resume_crash(run_request) if crashed else runtime.run(run_request)
    execute = asyncio.create_task(call, name=f"blank-run-{run_id}")
    # 心跳跟着租约走：租约收紧（实测/短租约部署）时，30 秒一跳会让租约先过期，
    # 别人接管时我还在跑。上限仍是原来的 30 秒。
    heartbeat_seconds = min(LEASE_HEARTBEAT_SECONDS, max(0.5, lease_seconds / 3))

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(heartbeat_seconds)
            try:
                async with get_session_factory()() as session:
                    if not await renew_run(
                        session, run_id, worker_id, lease_seconds=lease_seconds
                    ):
                        # 租约不再属于我：接着跑下去就是和接管者同时往同一条 run 写账
                        execute.cancel()
                        return
            except Exception:  # 续租失败不该把执行任务带走，下一轮再试
                log.warning("续租失败 run=%s", run_id, exc_info=True)

    lease_task = asyncio.create_task(heartbeat(), name=f"blank-lease-{run_id}")
    try:
        evidence = await execute
        status = str((evidence.get("task") or {}).get("status") or "succeeded")
    except asyncio.CancelledError:
        # Keep the row running so startup recovery can safely reclaim it.
        raise
    except Exception as error:
        status = "failed"
        log.exception("任务执行失败 task=%s run=%s", run_request.task_id, run_id)
        await _record_run_failure(run_request.task_id, run_id, error)
    finally:
        lease_task.cancel()
        if not execute.done():
            execute.cancel()  # 外面中止这一路时，执行任务不会自己停
        await asyncio.gather(lease_task, execute, return_exceptions=True)

    async def finish() -> bool:
        async with get_session_factory()() as session:
            return await finish_run(session, run_id, status, worker_id)

    try:
        if not await db_retry(finish):
            # 收尾被拒=租约在这期间被人拿走了。这条 run 的账现在归接管者写，我闭嘴。
            log.warning("收尾被拒（租约已失去）run=%s worker=%s", run_id, worker_id)
        else:
            await _record_notify_decision(run_request.task_id, status)
    except Exception as error:
        # 收尾失败会让任务永远停在 queued（界面一直转圈），必须留下痕迹并强行置终态
        log.exception("收尾写库失败 run=%s", run_id)
        await _record_run_failure(run_request.task_id, run_id, error)


def _notify_kind(status: str) -> str:
    """任务终态对应哪一类通知事件。

    waiting_approval 走审批（永远放行），failed/cancelled 走失败，succeeded 走交付物。
    其余中间态算进度——策略配了 always 才会打扰。
    """
    if status == "waiting_approval":
        return notify.EVENT_APPROVAL
    if status in ("failed", "cancelled"):
        return notify.EVENT_FAILURE
    if status == "succeeded":
        return notify.EVENT_DELIVERABLE
    return notify.EVENT_PROGRESS


async def _origin_from_trigger(session, task_id: str) -> str:
    """快照里没有 ``intent`` 时，从触发器行反推"这一次是谁起的"。

    ``super_agent_dispatch`` 关着时快照是空的（见 ``_record_notify_decision``
    里那段注释），但"定时到点"和"盯着工作区"起的那一轮，触发器行里明明白白
    写着 intent。不补这一步，所有桌面通知都只能写"做完了"——用户设了九点的
    提醒，到点弹出来的却是一句"任务完成"，读起来根本不像提醒。

    ``release_one`` 要求触发器必须已关联 task_id 才放行，所以这个反查是可靠的：
    查不到的说明它不是触发器起的（人自己发的），返回空串即可。
    """
    try:
        row = (
            await session.execute(
                select(AgentTrigger)
                .where(AgentTrigger.task_id == task_id)
                .order_by(AgentTrigger.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
    except Exception:  # noqa: BLE001 - 反查失败只影响文案分档，不该影响通知本身
        return ""
    if row is None:
        return ""
    return str((row.payload or {}).get("intent") or "")


async def _record_notify_decision(task_id: str, status: str) -> None:
    """把「这一次该不该打扰用户」写成事件，并把它真的投递出去（任务书 §5）。

    判断与投递分开，但**都在这里收口**：``agent/notify.py`` 只回答该不该出声，
    ``channels/delivery.py`` 只负责发。任何一个通道发失败都不影响任务终态，
    也不会写坏判定——判定是事实（"策略说要打扰"），投递结果另记一条事件。

    v0.10.3 起这里多了一件事：IM 里问的那句话，结论要回到原会话。它不看
    notify 策略——用户在聊天里问了一句，答案就该出现在聊天里，那不叫打扰。
    """
    title = goal = detail = ""
    disturb = False
    kind = notify.EVENT_PROGRESS
    origin = ""
    try:
        async with get_session_factory()() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            snapshot = dict(task.dispatch_snapshot or {})
            background = SettingsStore().read().get("background") or {}
            window = str(background.get("quiet_hours") or "")
            quiet = quiet_hours_active(window)
            kind = _notify_kind(status)
            # 策略的来源顺序（2026-11-08 修）：任务快照 > 设置里的「通知时机」> approval_only。
            #
            # 以前只有第一档，没有就硬兜 approval_only——而 ``super_agent_dispatch``
            # 默认是**关**的（BETA），关着时快照恒为 ``{}``。于是设置页那个「通知时机」
            # 下拉框选了 milestone / always 也毫无作用，桌面通知永远按"只报审批"走，
            # 用户选了"全都说"却什么都收不到。这是"设置项没人消费"的典型，
            # 不是"默认保守"就能解释过去的。
            policy = str(snapshot.get("notify_policy") or "").strip().lower()
            if not policy:
                policy = str(background.get("notify_policy") or "").strip().lower() or NOTIFY_APPROVAL_ONLY
            decision = notify.explain(policy, kind, quiet_hours=quiet)
            # 先把要发的内容取出来：会话关掉之后还要用它拼消息，而 Task 实例
            # 在会话外读字段会触发一次同步懒加载。
            title = str(task.title or "")
            goal = str(task.goal or "")
            detail = str(task.failure_reason or task.result_summary or "")
            disturb = bool(decision["disturb"])
            # 「这一次是谁起的」——定时到点、工作区变了、还是人自己发的。
            # 桌面通知要靠它决定标题写「提醒：检查部署」还是「任务已完成」：
            # 同一句"任务完成"扣在定时提醒上，读起来就不像提醒了。
            origin = str(snapshot.get("intent") or "")
            if not origin:
                origin = await _origin_from_trigger(session, task_id)
            await EventRecorder(get_session_factory(), task_id).add(
                "notify.decided",
                "finish",
                {**decision, "status": status, "origin": origin},
            )
    except Exception:  # 通知判定是旁路，坏了也不能把任务的终态带走
        log.warning("记录通知判定失败 task=%s", task_id, exc_info=True)
        return

    from blank_api.services.channels import delivery

    # ── 1. 提醒推到开了推送的通道 ────────────────────────────────────────
    results: list[dict] = []
    if disturb:
        try:
            results = await delivery.fanout(
                delivery.notification_text(title=title, goal=goal, kind=kind, detail=detail)
            )
        except Exception:  # noqa: BLE001 - 投递失败只影响"有没有收到"，不影响任务
            log.warning("通道投递失败 task=%s", task_id, exc_info=True)
    if results:
        try:
            await EventRecorder(get_session_factory(), task_id).add(
                "notify.delivered",
                "finish",
                {
                    "delivered": results,
                    "ok": sum(1 for item in results if item.get("ok")),
                    "failed": sum(1 for item in results if not item.get("ok")),
                },
            )
        except Exception:
            log.warning("记录投递结果失败 task=%s", task_id, exc_info=True)

    # ── 2. 结论回到消息进来的那个会话 ────────────────────────────────────
    if status in ("succeeded", "failed", "cancelled", "waiting_approval"):
        try:
            sent = await delivery.reply_to_source(
                task_id,
                delivery.reply_text(
                    status=status, summary=detail if status == "succeeded" else "",
                    failure=detail if status != "succeeded" else "", task_id=task_id,
                ),
            )
            if sent is not None:
                await EventRecorder(get_session_factory(), task_id).add(
                    "channel.replied", "finish", sent
                )
        except Exception:  # noqa: BLE001
            log.warning("回话失败 task=%s", task_id, exc_info=True)


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


async def launch_task(
    db: DbSession,
    task: Task,
    payload: TaskRunIn,
    request: Request | None,
    *,
    start_runner: bool = True,
    crashed: bool = False,
) -> dict:
    """把一条任务推进 run 队列（可顺带在进程内起跑）。

    触发器、定时到点、以及用户点「开始」走的是同一条路：入参快照、队列行、状态
    三件事必须一起写，分开写就会出现「界面上在跑、队列里没有」这种对不上的状态。
    """
    # 先取出 id：``_run_request`` 收尾会 rollback（让运行时用自己的会话），
    # 之后任何属性访问都会触发同步懒加载，在异步会话里直接 MissingGreenlet。
    task_id = task.id
    run_request = await _run_request(db, task, payload, request)
    run = await enqueue_run(db, task_id, payload.model_dump(mode="json"))
    run_request.run_id = run.id
    task.status = "queued"
    await db.commit()
    # v0.10.1 §2：run 的两条生命周期事件。「入队」与「真正开跑」必须分开记——
    # 排队后无人认领（独立 worker 没部署）和执行中途失败是两种完全不同的病。
    recorder = EventRecorder(get_session_factory(), task_id)
    await recorder.add(
        "run.created",
        "receive_task",
        {"run_id": run.id, "task_id": task_id, "status": "queued"},
    )

    if not start_runner or not inprocess_runner_enabled():
        # 队列归独立 worker：这里只落库并返回，否则本进程会先抢走它。
        return {"task_id": task_id, "run_id": run.id, "status": "queued"}

    async def runner() -> None:
        worker_id = f"{os.environ.get('COMPUTERNAME', 'worker')}-{uuid.uuid4().hex[:8]}"
        try:
            async with get_session_factory()() as session:
                if not await claim_run(session, run.id, worker_id):
                    return
            await recorder.add(
                "run.started",
                "receive_task",
                {"run_id": run.id, "worker_id": worker_id, "crashed": crashed},
            )
            await _execute_claimed_run(run.id, worker_id, run_request, crashed=crashed)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # 后台任务的异常没人接，不接住就是“任务永远 queued、界面一直转圈”
            log.exception("启动执行失败 task=%s run=%s", task_id, run.id)
            await _record_run_failure(task_id, run.id, error)

    try:
        await run_manager.start(run.id, runner)
    except RunAlreadyActive:
        raise ApiError(code="task_running", message="任务正在执行中", status_code=409) from None
    return {"task_id": task_id, "run_id": run.id, "status": "queued"}


def _defer_intent(task: Task) -> str:
    """这条任务要不要「等事件再跑」。返回 ``watch`` / ``schedule`` / 空串。

    正常路径读 ``task.dispatch_snapshot``；但对话轮次（``create_turn``）只把派发
    快照写进 ``goal_spec.dispatch``，``dispatch_snapshot`` 字段一直是空的。于是
    「盯着 X，一旦有变化就告诉我」这句：判定对了（intent=watch），却在这里读到
    None 直接放行——任务当场跑起来，既没有登记待触发，也没有任何界面痕迹。

    兜底只认 watch / schedule 两个意图，不放宽其它类型：defer 的语义是「等一个
    现在还不存在的事件」，多认一种意图就多一条"点了没反应"的路径。
    """
    snapshot = dispatch_of(task)
    if snapshot is not None:
        return snapshot.intent if snapshot.dispatch == DISPATCH_DEFER else ""
    dispatch = (task.goal_spec or {}).get("dispatch") or {}
    intent = str(dispatch.get("intent") or "")
    return intent if intent in (INTENT_WATCH, INTENT_SCHEDULE) else ""


def _when_of(task: Task):
    """从任务原话里解析到点时间。解析不出返回 None。

    只对「定时」意图解析：「盯着构建结果」是等外部事件，它没有"几点"，
    硬塞一个时间会在到点时把一件本来该由变化触发的事按钟表跑一遍。
    """
    from blank_api.services.agent import schedule

    return schedule.parse_when(task.goal or "")


async def _defer_if_needed(db: DbSession, task: Task, payload: TaskRunIn) -> dict | None:
    """监控/定时类请求：登记为待触发，返回给调用方，而不是现在就跑。

    定时意图必须解析出 ``fire_at`` 才算登记成功。解析不出就**退化为立即执行**：
    一条 ``fire_at=None`` + ``event_type=schedule`` 的登记既不会被时间扫到、
    也没有事件源唤醒，等于在监控页上摆一条「等一个不会到来的事件」——用户看到
    的是"说好了提醒我，结果一声不响"，这比当场跑一次糟得多。

    **不看 ``background_execution``。** 那个 flag 的语义是「后台自己发起的事」
    （自动修复、自动继续），而「9 点提醒我」「盯着这个文件」是人明确下的指令。
    让一个默认关闭的 BETA 开关挡住它，表现就是：用户说设个提醒，程序当场做掉了
    一件本来该在九点做的事——而且没有任何地方告诉他"我没听你的时间"。
    同一条判断在 IM 入站那次已经定过（见 channels/inbound.py）。
    """
    intent = _defer_intent(task)
    if not intent:
        return None
    from blank_api.models.trigger import AgentTrigger  # 局部导入：路由与触发器互相引用
    from blank_api.services.agent import triggers

    when = _when_of(task) if intent == INTENT_SCHEDULE else None
    if intent == INTENT_SCHEDULE and when is None:
        # 说了"每天汇总一次"却没说几点、或整句读不出时间：现在就做一次，
        # 并在事件里留下"没定时"这个事实，别让它变成一条永远不响的登记。
        await EventRecorder(get_session_factory(), task.id).add(
            "dispatch.immediate",
            "receive_task",
            {
                "intent": intent,
                "reason": "定时意图但原话里读不出到点时间，改为立即执行一次",
                "goal": (task.goal or "")[:200],
            },
        )
        return None

    fire_at = when.fire_at if when is not None else None
    repeat = when.repeat_payload() if when is not None else {}
    from datetime import timedelta

    from blank_api.services.agent import triggers as _triggers  # 已导入，仅取 utc_now

    extra: dict = {}
    if intent == INTENT_WATCH:
        # 「盯着」靠文件变化唤醒：记下"从现在起"，巡检才有比较的起点。
        # 没有这个时刻，第一拍只能建基线，用户刚交代就改的那一下会被吃掉。
        extra["watch_since"] = _triggers.utc_now().isoformat()
        # 把**登记时**的工作区路径固化下来。巡检时再按 active_workspace 现查，
        # 用户中途切换活动目录就会让这条盯着悄悄换了地方——他明明说的是
        # 「盯着 README」，系统去盯另一个仓库，而且看不出哪里错了。
        from sqlalchemy import select as _select

        from blank_api.models.project import Workspace as _Workspace

        target = (
            await db.execute(
                _select(_Workspace)
                .where(_Workspace.project_id == task.project_id)
                .order_by(_Workspace.is_active.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if target is not None and (target.path or "").strip():
            extra["watch_root"] = target.path
    event = triggers.normalize(
        event_type=triggers.EVENT_SCHEDULE,
        source="task.start",
        payload={
            "task_id": task.id,
            "goal": task.goal[:200],
            # intent 写进 payload：监控页要能区分"等变化"（watch，合理）与
            # "等一个没定下来的时间"（schedule 缺 fire_at，是缺陷）。
            "intent": intent,
            **extra,
            **({"repeat": repeat} if repeat else {}),
            **({"shifted": True} if when is not None and when.shifted else {}),
        },
        # 盯着可能要等好几天，24 小时窗口会让「盯着」在第二天自己作废。
        expires_at=_triggers.utc_now() + timedelta(days=7) if intent == INTENT_WATCH else None,
        dedupe_key=f"task:{task.id}",
    )
    row, created = await triggers.ingest(
        db,
        task.project_id,
        event,
        task_id=task.id,
        provider_id=payload.provider_id or "",
        run_payload=payload.model_dump(mode="json"),
        fire_at=fire_at,
    )
    await db.commit()
    await EventRecorder(get_session_factory(), task.id).add(
        "dispatch.deferred",
        "receive_task",
        {
            "trigger_id": row.id,
            "created": created,
            "intent": intent,
            "fire_at": fire_at.isoformat() if fire_at else "",
            "repeat": repeat,
            "shifted": bool(when is not None and when.shifted),
            "reason": (
                "定时请求已登记，到点自动执行"
                if fire_at is not None
                else "监控请求已登记，等外部事件"
            ),
        },
    )
    return {
        "task_id": task.id,
        "trigger_id": row.id,
        "status": "deferred",
        "fire_at": fire_at.isoformat() if fire_at else "",
        "repeat": repeat,
        "reason": (
            "已登记定时，到点执行" if fire_at is not None else "已登记待触发，等外部事件"
        ),
    }


async def recover_pending_runs() -> int:
    """Requeue tasks left queued/running when the API process was restarted."""
    if not inprocess_runner_enabled():
        # 独立 worker 自己会扫这张表；API 再抢一次就是两个执行者。
        return 0
    attempt_limit = recovery_attempt_limit()

    async def load():
        items: list[tuple[str, object]] = []
        async with get_session_factory()() as session:
            runs = (await session.execute(select(AgentRun).where(AgentRun.status.in_(("queued", "running"))))).scalars().all()
        # 一次批量加载所有关联 task，避免循环内 N+1。
        task_ids = {run.task_id for run in runs}
        task_map: dict[str, Task] = {}
        if task_ids:
            async with get_session_factory()() as session:
                existing = await session.execute(select(Task).where(Task.id.in_(task_ids)))
                for t in existing.scalars():
                    task_map[t.id] = t
        for run in runs:
            # 扫描时 attempts>0 = 上一次认领后没写终态（进程被杀/断电）。
            crashed = int(run.attempts or 0) > 0
            if crashed:
                # 进程内恢复同样算一次崩溃恢复：这条 run 上次认领后没写终态。
                metrics.inc(metrics.WORKER_RECOVERED)
            task = task_map.get(run.task_id)
            if task is None:
                continue
            if run.attempts >= attempt_limit:
                # 反复重排同一个 run 只会无限循环（上次收尾没写成就是这种表现）。
                # 到这里就停手，把原因交给用户，让他决定是否重试。
                async with get_session_factory()() as failed_session:
                    if not await abandon_run(failed_session, run.id):
                        # 租约还新鲜=真有 worker 在跑（另一个进程），不能判它的死
                        log.warning("跳过判死：run=%s 仍被现役 worker 持有", run.id)
                        continue
                await _record_run_failure(
                    run.task_id,
                    run.id,
                    RuntimeError(
                        f"这条任务连续 {run.attempts} 次没能正常启动（上一次执行没有正常收尾），"
                        "已停止自动重试。请点「重试」重新发起。"
                    ),
                )
                continue
            try:
                request_payload = TaskRunIn.model_validate(run.request_payload or {})
                async with get_session_factory()() as req_session:
                    request = await _run_request(req_session, task, request_payload, None)
                request.run_id = run.id
            except Exception:
                async with get_session_factory()() as failed_session:
                    if not await abandon_run(failed_session, run.id):
                        log.warning("跳过判死：run=%s 仍被现役 worker 持有", run.id)
                        continue
                continue

            async def runner(run_id=run.id, request=request, crashed=crashed):
                worker_id = f"recovery-{uuid.uuid4().hex[:8]}"
                async with get_session_factory()() as claim_session:
                    if not await claim_run(claim_session, run_id, worker_id):
                        return
                await _execute_claimed_run(run_id, worker_id, request, crashed=crashed)

            items.append((run.id, runner))
        return items

    return await run_manager.recover(load)


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

    async def events():
        cursor = after_seq
        delay = SSE_BACKOFF_MIN_SECONDS
        started = time.monotonic()
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
                    if not rows:
                        yield ": keep-alive\n\n"
                    continue
                if frame.get("wake"):
                    # 被人叫醒：不推帧，直接回去查库（这次查到的多半就是刚写进去的那条）。
                    metrics.inc(metrics.SSE_WAKE)
                    delay = SSE_BACKOFF_MIN_SECONDS
                    continue
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


@router.post("/tasks/{task_id}/approval")
async def decide_approval(
    task_id: str, payload: TaskApprovalIn, request: Request, db: DbSession
) -> dict:
    # 同一张卡连点两下、或两个审批卡并发提交：决策+恢复必须串行，否则两次
    # resume 会按中断位置各消费一次，第二张卡被静默跳过。
    lock = _approval_locks.setdefault(task_id, asyncio.Lock())
    async with lock:
        return await _decide_approval_locked(task_id, payload, request, db)


_approval_locks: dict[str, asyncio.Lock] = {}


async def _decide_approval_locked(
    task_id: str, payload: TaskApprovalIn, request: Request, db: DbSession
) -> dict:
    task = await load_task(db, task_id)
    if task.status != "waiting_approval":
        # v0.10.1 §3.4 幂等：同一张卡已经批过了（重复点击），返回当时的决定，
        # 不再 resume、也不报错——「重复点击允许不能重复执行」的温和实现。
        if payload.approval_id:
            prior = await _prior_decision(task_id, payload.approval_id)
            if prior is not None:
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
            for key in ("approval_id", "tool_call_id", "arguments_hash", "resource_path")
        }
        if identity
        else {"approval_id": payload.approval_id}
    )
    await recorder.add(
        "approval.decided",
        "execute_action",
        {
            "approve": payload.approve,
            "scope": payload.scope,
            "tools": pending,
            **decided_identity,
        },
    )
    # 决定只通过 Command(resume=...) 交回图里：批准时中断返回带作用域的批准，
    # 图据此写入会话记忆并补记 approval.granted。在这里预先塞 approved_tools
    # 会让中断被跳过，审批轨迹反而丢失。
    run_request = await _run_request(
        db, task, TaskRunIn(provider_id=payload.provider_id), request
    )
    evidence = await AgentRuntime(get_session_factory()).resume(
        run_request, decision={"approve": payload.approve, "scope": payload.scope}
    )
    async with get_session_factory()() as session:
        finished = await load_task(session, task_id)
        return {"task": TaskOut.model_validate(finished).model_dump(), "evidence": evidence}


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
    return {
        key: str(row.payload.get(key) or "")
        for key in ("approval_id", "tool_call_id", "tool", "arguments_hash", "resource_path")
    }


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
    answers = {key: str(value)[:4000] for key, value in payload.answers.items() if key in known}
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

    await EventRecorder(get_session_factory(), task_id).add(
        "question.decided",
        "execute_action",
        {"answers": answers, "questions": sorted(known)},
    )
    run_request = await _run_request(
        db, task, TaskRunIn(provider_id=payload.provider_id), request
    )
    evidence = await AgentRuntime(get_session_factory()).resume(
        run_request, decision={"answers": answers}
    )
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
    await EventRecorder(get_session_factory(), task_id).add(
        "steer.queued",
        "user",
        {"message": payload.message.strip(), "queued_at": queued_at, "step": int(task.steps_used or 0)},
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
            "present": delivered is not None,
            "summary": task.result_summary,
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
