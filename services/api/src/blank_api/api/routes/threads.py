"""会话接口：一条会话 = 一串按顺序发生的轮次。

四个动作，对应四件真实的事：

* 开一条新会话（``POST /projects/{id}/threads``）
* 看一眼这个项目有哪些会话（``GET /projects/{id}/threads``）
* 继续聊下一轮（``POST /threads/{id}/turns``）——创建的是一个普通任务，
  只是多了 ``thread_id``，所以运行、审批、证据、回放全部复用既有路径
* 分叉（``POST /threads/{id}/fork``）——带着上下文另开一条线，不复制任务

刻意**不做**"会话级运行"接口：一轮怎么跑、怎么审批、怎么中断恢复，任务那边已经
有完整的一套。会话层如果自己再开一套执行入口，两条路径迟早会分叉，而出问题时
最难的恰恰是"这条 run 到底走的是哪条路"。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from blank_api.api.auth import require_token
from blank_api.api.errors import ApiError
from blank_api.api.routes.projects import load_project
from blank_api.api.routes.tasks import _defer_if_needed, _defer_intent, launch_task, persist_task
from blank_api.db import get_session_factory
from blank_api.deps import DbSession
from blank_api.models.goal import Goal
from blank_api.models.task import Task
from blank_api.models.thread import Thread
from blank_api.schemas.goal import GoalOut
from blank_api.schemas.task import TaskOut, TaskRunIn
from blank_api.schemas.thread import (
    ThreadCreate,
    ThreadDetailOut,
    ThreadOut,
    ThreadUpdate,
    ThreadRewindIn,
    ThreadRewindOut,
    ThreadRewindPointOut,
    ThreadRewoundTurnOut,
    ThreadTurnIn,
    ThreadTurnOut,
)
from blank_api.services.agent.automation import (
    effective_policy,
    resolve_mode as resolve_automation_mode,
)
from blank_api.services.agent.dispatcher import (
    decide_dispatch,
    dispatch_snapshot,
)
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.goals import GoalTracker
from blank_api.services.agent.supervisor import observe, wants_new_goal
from blank_api.services.sandbox import SnapshotNotRestorable, SnapshotStore
from blank_api.services.settings import SettingsStore
from blank_api.services.threads import rewind_to_turn, thread_or_404, thread_turns

router = APIRouter(tags=["threads"], dependencies=[Depends(require_token)])

log = logging.getLogger(__name__)


def _turn_out(task: Task) -> ThreadTurnOut:
    return ThreadTurnOut(
        id=task.id,
        title=task.title,
        goal=task.goal,
        status=task.status,
        result_summary=task.result_summary,
        attachments=list(task.attachments or []),
        created_at=task.created_at,
    )


async def _latest_turn_status(db: AsyncSession, thread_ids: list[str]) -> dict[str, str]:
    """每条会话最后一轮的状态，供侧栏画状态点。

    被回退掉的轮不算：``rewound_at`` 非空的那几轮已经不属于这条对话，
    拿它当"最后状态"会让一条明明跑完的会话永远显示"失败"。

    一次查询换一次遍历，不在列表里逐条查：会话列表是首屏，N+1 直接变成首屏卡顿。
    """
    if not thread_ids:
        return {}
    rows = await db.execute(
        select(Task.thread_id, Task.status)
        .where(Task.thread_id.in_(thread_ids), Task.rewound_at.is_(None))
        .order_by(Task.turn_index.asc(), Task.created_at.asc())
    )
    latest: dict[str, str] = {}
    for thread_id, status in rows.all():
        # 升序遍历，同一个 thread 最后写进去的就是 turn_index 最大的那一轮。
        if thread_id:
            latest[thread_id] = str(status or "")
    return latest


async def _detail(db: AsyncSession, thread: Thread) -> ThreadDetailOut:
    turns = await thread_turns(db, thread.id)
    latest = await _latest_turn_status(db, [thread.id])
    # 逐字段传，不先 model_dump 再解回来：那条路会把 datetime 序列化成字符串
    # 再解析一遍，绕一圈只为了丢掉时区信息。
    return ThreadDetailOut(
        id=thread.id,
        project_id=thread.project_id,
        title=thread.title,
        status=thread.status,
        forked_from=thread.forked_from,
        turn_count=int(thread.turn_count or 0),
        last_turn_status=latest.get(thread.id, ""),
        created_at=thread.created_at,
        updated_at=thread.updated_at,
        turns=[_turn_out(task) for task in turns],
    )


@router.post("/projects/{project_id}/threads", response_model=ThreadOut, status_code=201)
async def create_thread(project_id: str, payload: ThreadCreate, db: DbSession) -> ThreadOut:
    project = await load_project(db, project_id)
    if payload.forked_from:
        source = await thread_or_404(db, payload.forked_from)
        if source.project_id != project.id:
            # 跨项目分叉会把两条不同工作区里的上下文拼在一起，模型分不清哪些路径
            # 属于哪个仓库，而错误会以"改错文件"的形式出现。
            raise ApiError(code="thread_project_mismatch", message="不能跨项目分叉会话", status_code=409)
    thread = Thread(
        id=str(uuid.uuid4()),
        project_id=project.id,
        title=payload.title or ("分叉会话" if payload.forked_from else "新会话"),
        status="active",
        forked_from=payload.forked_from,
        turn_count=0,
    )
    db.add(thread)
    await db.commit()
    # created_at/updated_at 由服务端生成：MySQL 的 INSERT 没有 RETURNING，不读回来
    # 就会在序列化时触发一次同步懒加载（MissingGreenlet → 500）。见 routes/tasks.py 同一条说明。
    await db.refresh(thread)
    return ThreadOut.model_validate(thread)


@router.get("/projects/{project_id}/threads", response_model=list[ThreadOut])
async def list_threads(
    project_id: str,
    db: DbSession,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ThreadOut]:
    await load_project(db, project_id)
    rows = (
        await db.execute(
            select(Thread)
            .where(Thread.project_id == project_id, Thread.kind == "chat")
            .order_by(Thread.updated_at.desc(), Thread.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    latest = await _latest_turn_status(db, [row.id for row in rows])
    return [
        ThreadOut.model_validate(row).model_copy(update={"last_turn_status": latest.get(row.id, "")})
        for row in rows
    ]


@router.get("/threads/{thread_id}", response_model=ThreadDetailOut)
async def read_thread(thread_id: str, db: DbSession) -> ThreadDetailOut:
    return await _detail(db, await thread_or_404(db, thread_id))


async def _run_control(
    action: str, task_id: str, status: str, request: Request, db: DbSession
) -> TaskOut | None:
    """把控制词路由到**已经存在**的动作，不另写一套状态机（v165 §2.2 / §3.3）。

    返回 ``None`` 表示这件事需要新开一轮（上一轮已成功，用户又提了补充要求）；
    其余情况返回被作用的任务。任务重新查一次：调用方刚 commit 过，拿旧实例
    读字段在 expire_on_commit 的会话上会触发同步懒加载。
    """
    from blank_api.api.routes import tasks as task_routes

    task = await task_routes.load_task(db, task_id)
    if action == "pause":
        return await task_routes.pause_task(task_id, db)
    if action == "cancel":
        return await task_routes.cancel_task(task_id, db)
    if action == "resume":
        if status != "paused":
            return TaskOut.model_validate(task)
        return await task_routes.resume_task(task_id, request, db)
    if action in ("retry", "continue"):
        # 还在跑的轮次只当作催促，不重开第二份执行。
        if status in ("queued", "running", "waiting_approval"):
            return TaskOut.model_validate(task)
        if status == "paused":
            return await task_routes.resume_task(task_id, request, db)
        if status == "succeeded":
            return None
        return await task_routes.retry_task(task_id, request, db)
    if action in ("approve", "reject"):
        if status != "waiting_approval":
            return TaskOut.model_validate(task)
        from blank_api.schemas.task import TaskApprovalIn

        try:
            result = await task_routes.decide_approval(
                task_id,
                TaskApprovalIn(approve=(action == "approve"), scope="once"),
                request,
                db,
            )
        except ApiError:
            # 审批过期、证据不齐：「同意」两个字不该把整轮对话打成 500。
            return TaskOut.model_validate(task)
        return TaskOut.model_validate(result["task"])
    return TaskOut.model_validate(task)


@router.get("/threads/{thread_id}/goal", response_model=GoalOut | None)
async def read_goal(thread_id: str, db: DbSession):
    """当前会话的目标，供顶部状态条显示（v165 §9.1）。没有就是 null。

    先取活跃目标；没有活跃的再退回**最近一个**（含已完成/失败）。状态条是给
    用户「现在到哪了」用的，任务一成功就把目标格清空，等于在唯一该被看见的
    时刻收走结论。新建/复用目标的判定不在这里，仍走 ``GoalTracker.active()``。
    """
    thread = await thread_or_404(db, thread_id)
    tracker = GoalTracker(db)
    goal = await tracker.active(thread.id) or await tracker.latest(thread.id)
    return GoalOut.model_validate(goal) if goal is not None else None


@router.post("/threads/{thread_id}/turns", response_model=TaskOut, status_code=201)
async def create_turn(
    thread_id: str, payload: ThreadTurnIn, request: Request, db: DbSession
) -> TaskOut:
    """Chat Supervisor 入口：先判断这句话该做什么，再决定要不要开新一轮。

    v0.9.1 是「一句话 = 一个任务」，用户说「继续」「再改一下」也新建一轮，
    旧目标和失败原因全留在上一轮，只能靠人不停重说。现在每条消息先过
    ``observe()``：控制词作用于已有任务，补充要求挂回同一个目标，
    只有明确的新目标才归档旧的另起一条。

    仍然走同一个 ``persist_task``：多轮会话不该有第二套任务创建逻辑。
    """
    thread = await thread_or_404(db, thread_id)
    if thread.status != "active":
        raise ApiError(code="thread_archived", message="该会话已归档，不能再开新的一轮", status_code=409)
    from blank_api.schemas.task import TaskCreate

    text = (payload.goal or payload.title or "").strip()
    tracker = GoalTracker(db)
    goal = await tracker.active(thread_id)
    last_task = await tracker.latest_task(goal)
    last_task_id = last_task.id if last_task is not None else ""
    last_status = str(last_task.status or "") if last_task is not None else ""
    judgement = observe(
        text,
        has_active_goal=goal is not None,
        active_task_status=last_status,
        scenario=payload.scenario or (goal.scenario if goal is not None else "dev"),
        automation_mode=(goal.automation_mode if goal is not None else "assist"),
        goal_id=goal.id if goal is not None else "",
        related_task_id=last_task_id,
    )

    # 控制词不新建一轮；作用在已有任务上，事件也挂在那一轮。
    if judgement.control_action and last_task_id:
        if goal is not None:
            if judgement.control_action == "cancel":
                GoalTracker.transition(goal, "cancelled")
            elif judgement.control_action == "pause":
                GoalTracker.transition(goal, "paused")
            elif judgement.control_action in ("continue", "retry", "resume"):
                GoalTracker.transition(goal, "executing", blocked_reason="", next_action=text[:200])
            await db.commit()
        handled = await _run_control(
            judgement.control_action, last_task_id, last_status, request, db
        )
        if handled is not None:
            recorder = EventRecorder(get_session_factory(), last_task_id)
            await recorder.add(
                "message.observed",
                "supervisor",
                {"message": text[:400], "judgement": judgement.as_dict()},
            )
            await recorder.add(
                "supervisor.action",
                "supervisor",
                {
                    "action": judgement.control_action,
                    "goal_id": judgement.goal_id,
                    "task_id": last_task_id,
                    "reason": judgement.reason,
                },
            )
            return handled
        # handled is None：上一轮已完成，接着开新一轮，但目标仍是同一个。

    created_goal = goal is None or wants_new_goal(text)
    if created_goal:
        if goal is not None:
            GoalTracker.transition(goal, "archived")
        goal = await tracker.create(
            project_id=thread.project_id,
            conversation_id=thread_id,
            title=text[:200] or "未命名目标",
            scenario=payload.scenario or "dev",
        )
    goal_id = goal.id
    await db.commit()

    # v165 §5.1：下发前逐条核对七个条件。auto_start 不是「收到消息就开跑」，
    # 同时留下可回放的下发快照——任务结束后仍能回答「当时凭什么自动跑起来」。
    settings = SettingsStore().read()
    # v0.10.1 §4.3：发送栏的选择（payload.permission_mode）优先于设置页默认档。
    # 它随下发快照固定，任务创建后再改全局默认不影响这一轮。
    permission_mode = (
        payload.permission_mode
        or str((settings.get("permissions") or {}).get("default_mode") or "default")
    )
    # 设置页是唯一真源；目标上的存档值只作回落。resolve_mode 与审批矩阵、
    # PermissionPolicy、runtime 共用同一份枚举，脏值一律回到 assist。
    automation_mode = resolve_automation_mode(settings, str(goal.automation_mode or ""))
    policy = effective_policy(automation_mode, permission_mode)
    project = await load_project(db, thread.project_id)
    budget_left = not (
        int(goal.budget_tokens or 0) > 0
        and int(goal.tokens_used or 0) >= int(goal.budget_tokens)
    )
    decision = decide_dispatch(
        judgement,
        project_id=thread.project_id,
        scenario=payload.scenario or str(goal.scenario or "dev"),
        automation_mode=automation_mode,
        permission_mode=permission_mode,
        has_workspace=project.active_workspace is not None,
        has_clarification=judgement.needs_clarification,
        has_active_task=last_status in ("queued", "running", "waiting_approval"),
        budget_left=budget_left,
    )

    next_turn = int(thread.turn_count or 0) + 1
    task = await persist_task(
        db,
        thread.project_id,
        TaskCreate(
            title=payload.title,
            goal=payload.goal,
            acceptance_criteria=payload.acceptance_criteria,
            provider_id=payload.provider_id,
            scenario=payload.scenario,
            max_steps=payload.max_steps,
            attachments=payload.attachments,
        ),
        thread_id=thread_id,
        turn_index=next_turn,
    )
    # persist_task 自己 commit 过：重新取一次目标再挂，别拿可能已过期的实例写字段。
    stored_goal = (
        await db.execute(select(Goal).where(Goal.id == goal_id))
    ).scalar_one_or_none()
    goal_status_by_decision = {
        "immediate": "executing",
        "queue": "planned",
        "wait_approval": "waiting_approval",
        "wait_user": "waiting_user",
        "blocked": "paused",
    }
    if stored_goal is not None:
        tracker.attach(
            stored_goal,
            task,
            status=goal_status_by_decision.get(
                decision["decision"], "planned" if judgement.needs_execution else "captured"
            ),
        )
    dispatch = dispatch_snapshot(
        judgement,
        decision,
        task_id=task.id,
        conversation_id=thread_id,
        goal_id=goal_id,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    # v0.10.1 §2：auto_start 的最终值。监督者判定是底座；用户的显式选择可以
    # 越过 manual 模式的「排队」，但越过不了 blocked / wait_user——那两类
    # 是环境本身不成立（没目标/没工作区/没场景），硬跑只会原地失败。
    auto_start = bool(dispatch.get("auto_start"))
    if payload.auto_start is True and dispatch["decision"] not in ("blocked", "wait_user"):
        auto_start = True
    elif payload.auto_start is False:
        auto_start = False
    dispatch["auto_start"] = auto_start
    # 监督者判定放在结构化目标里（不是用户 prompt 的前缀）。执行侧
    # create_plan_node 读它来纠正「补充要求被判成闲聊」，见 supervisor.execution_override。
    # normalize_goal 只保留四个已知键，所以必须在 persist_task 之后补写。
    task.goal_spec = {
        **dict(task.goal_spec or {}),
        "supervisor": judgement.as_dict(),
        "goal_id": goal_id,
        "dispatch": dispatch,
        # v165 §8：下发那一刻真正的执行边界，GUI 状态条与事后取证都读它。
        "policy": policy,
    }
    thread.turn_count = next_turn
    await db.commit()
    # 这次提交会让服务端生成的 updated_at 过期；不 refresh 就序列化会在
    # 异步会话里触发同步懒加载（MissingGreenlet → 500）。persist_task 自己
    # 也 refresh 过一次，这里多一次提交就多一次。
    await db.refresh(task)

    recorder = EventRecorder(get_session_factory(), task.id)
    await recorder.add(
        "message.observed", "supervisor",
        {"message": text[:400], "judgement": judgement.as_dict()},
    )
    if created_goal:
        await recorder.add("goal.created", "supervisor", {"goal_id": goal_id, "title": text[:200]})
    await recorder.add(
        "goal.linked", "supervisor",
        {"goal_id": goal_id, "task_id": task.id, "intent": judgement.intent},
    )
    await recorder.add("task.dispatched", "supervisor", dispatch)
    await recorder.add(
        "thread.turn", "receive_task", {"thread": thread_id, "turn": next_turn}
    )

    # v0.10.1 §2：后端真正接管自动执行。auto_start 在这里就地入队/启动，
    # 不再依赖前端读快照后二次调用 /start——窗口关了、前端崩了、网络断了，
    # 任务照常执行。启动失败记 task.dispatch_failed，任务留在 draft/queued
    # 可重试的状态，绝不让它停在 planned 无人接管。
    # 监控/定时类的判定是 needs_execution=False（「现在别动手」），但它不能就
    # 这么停着——那句话的意义是「等事件」，所以也要进 defer 分支登记一条待触发。
    # 只判 needs_execution 会让「盯着 X，有变化就告诉我」变成一句什么都不发生的话。
    if auto_start and (judgement.needs_execution or _defer_intent(task)):
        # rollback/commit 都会过期 ORM 实例；id 先取成普通值，except 里才不会
        # 触发同步懒加载（MissingGreenlet）。
        auto_task_id = task.id
        run_payload = TaskRunIn(
            provider_id=payload.provider_id,
            scenario=payload.scenario,
            memory_opt_in=payload.memory_opt_in,
            permission_mode=payload.permission_mode,
        )
        try:
            deferred = await _defer_if_needed(db, task, run_payload)
            if deferred is not None:
                launched = deferred
            else:
                launched = await launch_task(db, task, run_payload, request)
            await recorder.add(
                "task.auto_started",
                "supervisor",
                {
                    "task_id": auto_task_id,
                    "run_id": str(launched.get("run_id") or ""),
                    "status": str(launched.get("status") or ""),
                    "decision": dispatch["decision"],
                    "source": "backend",
                },
            )
        except Exception as error:  # 派发失败不能把这一轮的 201 带走，也不能静默
            log.warning("自动下发失败 task=%s", auto_task_id, exc_info=True)
            await db.rollback()
            failure_payload = {
                "task_id": auto_task_id,
                "reason": str(error)[:400],
                "code": str(getattr(error, "code", "") or ""),
                "decision": dispatch["decision"],
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
            await recorder.add("task.dispatch_failed", "supervisor", failure_payload)
    # launch/rollback 会让 task 实例过期（expire_on_commit），直接序列化会
    # MissingGreenlet；refresh 一次拿到终态（draft/queued/deferred）再出去。
    await db.refresh(task)
    return TaskOut.model_validate(task)


@router.post("/threads/{thread_id}/fork", response_model=ThreadOut, status_code=201)
async def fork_thread(thread_id: str, db: DbSession, payload: ThreadCreate | None = None) -> ThreadOut:
    thread = await thread_or_404(db, thread_id)
    created = await create_thread(
        thread.project_id,
        ThreadCreate(title=(payload.title if payload else "") or "", forked_from=thread_id),
        db,
    )
    # 事件挂在来源会话的最后一轮上：事件表是按 task 组织的，而"分叉"这件事
    # 发生在会话层。挂在一个真实任务上，用户翻来源会话的历史时能看见；
    # 没有轮次就没有可挂的地方，也就不记——空会话分叉本身没有信息量。
    source_turns = await thread_turns(db, thread_id)
    if source_turns:
        await EventRecorder(get_session_factory(), source_turns[-1].id).add(
            "thread.forked", "receive_task", {"from": thread_id, "to": created.id}
        )
    return created


@router.delete("/threads/{thread_id}", status_code=204)
async def archive_thread(thread_id: str, db: DbSession):
    """归档，不是删除。

    删会话会连带把任务的 ``thread_id`` 置空（SET NULL），那些轮次就再也串不回去了。
    归档保留这条线，只是不许再开新的一轮。
    """
    thread = await thread_or_404(db, thread_id)
    thread.status = "archived"
    await db.commit()
    return None


@router.delete("/threads/{thread_id}/permanent", status_code=204)
async def delete_thread(thread_id: str, db: DbSession):
    """物理删除会话及其所有关联任务。

    轮次任务（thread_id 指向这条会话）一并删除；引用链上的事件/运行记录/检查点
    由外键 CASCADE 联动删除。不可恢复，前端须弹出二次确认。
    """
    from sqlalchemy import delete as sa_delete
    from blank_api.models.run import AgentRun, AgentEvent, AgentCheckpoint
    from blank_api.models.search import ChatMessage

    thread = await thread_or_404(db, thread_id)

    tasks = (await db.execute(select(Task).where(Task.thread_id == thread_id))).scalars().all()
    for task in tasks:
        await db.execute(sa_delete(AgentEvent).where(AgentEvent.task_id == task.id))
        await db.execute(sa_delete(AgentRun).where(AgentRun.task_id == task.id))
        await db.execute(sa_delete(AgentCheckpoint).where(AgentCheckpoint.task_id == task.id))
        await db.execute(sa_delete(ChatMessage).where(ChatMessage.thread_id == thread_id))
        await db.delete(task)

    await db.delete(thread)
    await db.commit()
    return None


@router.patch("/threads/{thread_id}")
async def update_thread(thread_id: str, payload: ThreadUpdate, db: DbSession):
    """更新会话的字段（title 或 status）。

    刻意不做全字段更新：这里只有 title 和 status 两个可变字段，
    其他属性（project_id、forked_from 等）是一次创建就不再改的。
    """
    thread = await thread_or_404(db, thread_id)
    if payload.status is not None:
        if payload.status not in ("active", "archived"):
            raise ApiError(code="invalid_status", message="状态只能是 active 或 archived", status_code=422)
        thread.status = payload.status
    if payload.title is not None:
        thread.title = payload.title.strip() or "未命名会话"
    await db.commit()
    await db.refresh(thread)
    return thread


def _snapshots_by_task(store: SnapshotStore) -> dict[str, list[dict]]:
    """一次扫盘按任务分组：列表接口要为每一轮查一次，逐轮扫盘会变成 N 次全目录遍历。"""
    grouped: dict[str, list[dict]] = {}
    for item in store.list():
        grouped.setdefault(str(item.get("task_id") or ""), []).append(item)
    return grouped


def _restore_point(
    store: SnapshotStore, grouped: dict[str, list[dict]], turns: list[Task]
) -> tuple[str, dict]:
    """取一份能把代码放回「这些轮次之前」的快照。

    按时间正序找第一个有可恢复快照的轮次：它的快照拍在它自己写入之前，
    也就是前一轮刚写完的样子。返回 ``(snapshot_id, meta)``，找不到时 id 为空。
    """
    for turn in turns:
        for item in reversed(grouped.get(turn.id, [])):
            snapshot_id = str(item.get("id") or "")
            if not snapshot_id:
                continue
            ok, _ = store.restorable(snapshot_id)
            if ok:
                return snapshot_id, item
    return "", {}


@router.get("/threads/{thread_id}/rewind-points", response_model=list[ThreadRewindPointOut])
async def list_rewind_points(thread_id: str, db: DbSession) -> list[ThreadRewindPointOut]:
    thread = await thread_or_404(db, thread_id)
    turns = await thread_turns(db, thread_id)
    store = SnapshotStore()
    grouped = _snapshots_by_task(store)
    points: list[ThreadRewindPointOut] = []
    for index, turn in enumerate(turns):
        later = turns[index + 1 :]
        snapshot_id, _ = _restore_point(store, grouped, later)
        if snapshot_id:
            reason = ""
        elif not later:
            reason = "已经是最后一轮，没有更晚的快照可用于恢复代码"
        else:
            reason = "这一轮之后没有可恢复的快照，代码回不到这一轮结束时的样子"
        points.append(
            ThreadRewindPointOut(
                turn=int(turn.turn_index or 0),
                task_id=turn.id,
                title=turn.title,
                status=turn.status,
                created_at=turn.created_at,
                restorable=bool(snapshot_id),
                snapshot_id=snapshot_id,
                skipped_reason=reason,
            )
        )
    return points


@router.post("/threads/{thread_id}/rewind", response_model=ThreadRewindOut)
async def rewind_thread(thread_id: str, payload: ThreadRewindIn, db: DbSession) -> ThreadRewindOut:
    """回退到第 ``turn`` 轮：对话截到这一轮，代码尽力回到这一轮结束时的样子。

    两件事分开做、分开汇报：对话截断落库即生效；代码恢复取决于后面某一轮有没有
    留下可恢复的快照，没有就明说没做（``atomic=false``）。把两件事绑成一个事务
    只会让"快照缺失"连累用户本来就想做的对话截断。
    """
    thread = await thread_or_404(db, thread_id)
    if thread.status != "active":
        raise ApiError(code="thread_archived", message="该会话已归档，不能回退", status_code=409)

    before = await thread_turns(db, thread_id)
    target, doomed = await rewind_to_turn(db, thread, payload.turn)

    store = SnapshotStore()
    snapshot_id = ""
    restored: list[str] = []
    skipped: list[str] = []
    skipped_reason = ""
    project = await load_project(db, thread.project_id)
    workspace = project.active_workspace
    later = [item for item in before if int(item.turn_index or 0) > payload.turn]
    if workspace is None:
        skipped_reason = "项目尚未绑定工作区目录，代码未恢复"
    else:
        snapshot_id, _ = _restore_point(store, _snapshots_by_task(store), later)
        if not snapshot_id:
            skipped_reason = (
                "已经是最后一轮，没有更晚的快照可用于恢复代码"
                if not later
                else "被回退的轮次里没有可恢复的快照，代码未恢复"
            )
        else:
            try:
                report = store.restore_report(snapshot_id, workspace.path, None)
            except SnapshotNotRestorable as error:
                # 选中的快照在两次检查之间被删掉/挪走：对话已经截断，这里只报代码没回来。
                skipped_reason = f"快照 {snapshot_id} 在恢复时已不可用：{error.reason}"
                snapshot_id = ""
            else:
                restored = list(report.get("restored") or [])
                skipped = list(report.get("skipped") or [])
                if skipped and not report.get("skipped_reason"):
                    skipped_reason = f"{len(skipped)} 个文件不在快照副本里，保持原样"
                else:
                    skipped_reason = str(report.get("skipped_reason") or "")

    await EventRecorder(get_session_factory(), target.id).add(
        "thread.rewound",
        "receive_task",
        {
            "thread": thread_id,
            "turn": payload.turn,
            "rewound": [item.id for item in doomed],
            "snapshot_id": snapshot_id,
            "restored": len(restored),
            "skipped": len(skipped),
            "skipped_reason": skipped_reason,
        },
    )
    return ThreadRewindOut(
        thread_id=thread_id,
        turn=payload.turn,
        rewound=[
            ThreadRewoundTurnOut(id=item.id, turn=int(item.turn_index or 0)) for item in doomed
        ],
        snapshot_id=snapshot_id,
        restored=restored,
        skipped=skipped,
        skipped_reason=skipped_reason,
        atomic=False,
    )
