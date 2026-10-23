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

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from blank_api.api.auth import require_token
from blank_api.api.errors import ApiError
from blank_api.api.routes.projects import load_project
from blank_api.api.routes.tasks import persist_task
from blank_api.db import get_session_factory
from blank_api.deps import DbSession
from blank_api.models.task import Task
from blank_api.models.thread import Thread
from blank_api.schemas.task import TaskOut
from blank_api.schemas.thread import (
    ThreadCreate,
    ThreadDetailOut,
    ThreadOut,
    ThreadTurnIn,
    ThreadTurnOut,
)
from blank_api.services.agent.events import EventRecorder
from blank_api.services.threads import thread_or_404, thread_turns

router = APIRouter(tags=["threads"], dependencies=[Depends(require_token)])


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


async def _detail(db: DbSession, thread: Thread) -> ThreadDetailOut:
    turns = await thread_turns(db, thread.id)
    # 逐字段传，不先 model_dump 再解回来：那条路会把 datetime 序列化成字符串
    # 再解析一遍，绕一圈只为了丢掉时区信息。
    return ThreadDetailOut(
        id=thread.id,
        project_id=thread.project_id,
        title=thread.title,
        status=thread.status,
        forked_from=thread.forked_from,
        turn_count=int(thread.turn_count or 0),
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
    return [ThreadOut.model_validate(row) for row in rows]


@router.get("/threads/{thread_id}", response_model=ThreadDetailOut)
async def read_thread(thread_id: str, db: DbSession) -> ThreadDetailOut:
    return await _detail(db, await thread_or_404(db, thread_id))


@router.post("/threads/{thread_id}/turns", response_model=TaskOut, status_code=201)
async def create_turn(thread_id: str, payload: ThreadTurnIn, db: DbSession) -> TaskOut:
    """开新的一轮。

    走的是同一个 ``persist_task``：多轮会话不该有第二套任务创建逻辑，
    否则"独立任务"和"会话里的一轮"迟早会在字段默认值上分叉。
    """
    thread = await thread_or_404(db, thread_id)
    if thread.status != "active":
        raise ApiError(code="thread_archived", message="该会话已归档，不能再开新的一轮", status_code=409)
    from blank_api.schemas.task import TaskCreate

    next_turn = int(thread.turn_count or 0) + 1
    task = await persist_task(
        db,
        thread.project_id,
        TaskCreate(
            title=payload.title,
            goal=payload.goal,
            acceptance_criteria=payload.acceptance_criteria,
            provider_id=payload.provider_id,
            max_steps=payload.max_steps,
            attachments=payload.attachments,
        ),
        thread_id=thread_id,
        turn_index=next_turn,
    )
    # 轮次计数与序号一起加，而不是每次列表都去 count：列表接口是每次打开页面都调的，
    # 而计数变化只在创建这一处发生。
    thread.turn_count = next_turn
    await db.commit()
    await EventRecorder(get_session_factory(), task.id).add(
        "thread.turn", "receive_task", {"thread": thread_id, "turn": next_turn}
    )
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
