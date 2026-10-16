import asyncio
import json
import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from blank_api.api.auth import require_token
from blank_api.api.errors import ApiError
from blank_api.api.routes.projects import load_project
from blank_api.db import get_session_factory
from blank_api.deps import DbSession
from blank_api.models.model_provider import ModelProvider
from blank_api.models.task import AgentEvent, Task
from blank_api.models.run import AgentRun
from blank_api.models.skill import Skill
from blank_api.schemas.task import EventOut, TaskApprovalIn, TaskCreate, TaskOut, TaskRunIn
from blank_api.services.agent.evidence import build_evidence, load_task
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.agent.manager import RunAlreadyActive, run_manager
from blank_api.services.agent.queue import claim_run, enqueue_run, finish_run, renew_run
from blank_api.services.models.registry import build_adapter
from blank_api.services.models.routing import FallbackAdapter
from blank_api.services.sandbox import SnapshotStore

router = APIRouter(tags=["tasks"], dependencies=[Depends(require_token)])


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
    provider = (
        await db.execute(select(ModelProvider).where(ModelProvider.enabled == True).order_by(ModelProvider.name))  # noqa: E712
    ).scalars().first()
    if provider is None:
        raise ApiError(
            code="provider_not_configured",
            message="尚未配置可用的模型供应商，请先在设置中添加模型后运行任务",
            status_code=409,
        )
    return provider


@router.post("/projects/{project_id}/tasks", response_model=TaskOut, status_code=201)
async def create_task(project_id: str, payload: TaskCreate, db: DbSession) -> TaskOut:
    project = await load_project(db, project_id)
    task = Task(
        id=str(uuid.uuid4()),
        project_id=project.id,
        provider_id=payload.provider_id,
        title=payload.title,
        goal=payload.goal or payload.title,
        status="draft",
        acceptance_criteria=payload.acceptance_criteria,
        acceptance_results=[],
        max_steps=payload.max_steps,
    )
    db.add(task)
    await db.commit()
    await EventRecorder(get_session_factory(), task.id).add(
        "task.created",
        "receive_task",
        {
            "title": task.title,
            "acceptance_criteria": list(task.acceptance_criteria or []),
        },
    )
    return TaskOut.model_validate(task)


@router.get("/tasks", response_model=list[TaskOut])
async def list_tasks(
    db: DbSession,
    project_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[TaskOut]:
    stmt = select(Task).order_by(Task.created_at.desc()).limit(limit)
    if project_id:
        stmt = stmt.where(Task.project_id == project_id)
    if status:
        stmt = stmt.where(Task.status == status)
    return [TaskOut.model_validate(task) for task in (await db.execute(stmt)).scalars()]


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(task_id: str, db: DbSession) -> TaskOut:
    return TaskOut.model_validate(await load_task(db, task_id))


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
    return SnapshotStore().list(task_id)


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
    if project.active_workspace is None:
        raise ApiError(code="workspace_not_bound", message="该项目尚未绑定工作区目录", status_code=409)
    restored = store.restore(snapshot_id, project.active_workspace, None)
    await EventRecorder(get_session_factory(), task_id).add(
        "snapshot.restored", "complete", {"snapshot_id": snapshot_id, "files": restored}
    )
    return {"task_id": task_id, "snapshot_id": snapshot_id, "restored": restored}


async def _run_request(db: DbSession, task: Task, payload: TaskRunIn, request: Request) -> RunRequest:
    project = await load_project(db, task.project_id)
    workspace = project.active_workspace
    if workspace is None:
        raise ApiError(code="workspace_not_bound", message="该项目尚未绑定工作区目录", status_code=409)
    provider = await _resolve_provider(db, task, payload.provider_id)
    task.provider_id = provider.id
    await db.commit()
    client_factory = _factory_from(request)
    adapters = [build_adapter(provider, client_factory)]
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
    active_skills = (
        await db.execute(select(Skill.instructions).where(Skill.status == "active").order_by(Skill.updated_at))
    ).scalars().all()
    # The runtime owns its own sessions; end this request transaction first so a
    # single-connection SQLite backend never interleaves them mid-run.
    await db.rollback()
    return RunRequest(
        task_id=task.id,
        provider_id=provider.id,
        provider_name=provider.name,
        adapter=adapter,
        root=workspace.path,
        title=task.title,
        goal=task.goal,
        acceptance_criteria=list(task.acceptance_criteria or []),
        max_steps=int(task.max_steps),
        approve_high_risk=payload.approve_high_risk,
        approved_tools=set(payload.approved_tools),
        approved_actions=set(payload.approved_actions),
        skill_instructions=[str(item) for item in active_skills],
        role_adapters=role_adapters,
        role_provider_ids=role_provider_ids,
    )


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


async def _execute_claimed_run(run_id: str, worker_id: str, run_request: RunRequest) -> None:
    """Run one claimed job and keep its database lease alive while it works."""
    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(30)
            async with get_session_factory()() as session:
                if not await renew_run(session, run_id, worker_id):
                    return

    lease_task = asyncio.create_task(heartbeat(), name=f"blank-lease-{run_id}")
    try:
        evidence = await AgentRuntime(get_session_factory()).run(run_request)
        status = str((evidence.get("task") or {}).get("status") or "succeeded")
    except asyncio.CancelledError:
        # Keep the row running so startup recovery can safely reclaim it.
        raise
    except Exception:
        status = "failed"
    finally:
        lease_task.cancel()
        await asyncio.gather(lease_task, return_exceptions=True)
    async with get_session_factory()() as session:
        await finish_run(session, run_id, status)


@router.post("/tasks/{task_id}/start", status_code=202)
async def start_task(task_id: str, payload: TaskRunIn, request: Request, db: DbSession) -> dict:
    """Start a run without holding the HTTP request open."""
    task = await load_task(db, task_id)
    if task.status in ("running", "queued"):
        raise ApiError(code="task_running", message="任务正在执行中", status_code=409)
    run_request = await _run_request(db, task, payload, request)
    run = await enqueue_run(db, task_id, payload.model_dump(mode="json"))
    run_request.run_id = run.id
    task.status = "queued"
    await db.commit()

    async def runner() -> None:
        worker_id = f"{os.environ.get('COMPUTERNAME', 'worker')}-{uuid.uuid4().hex[:8]}"
        async with get_session_factory()() as session:
            if not await claim_run(session, run.id, worker_id):
                return
        await _execute_claimed_run(run.id, worker_id, run_request)

    try:
        await run_manager.start(run.id, runner)
    except RunAlreadyActive:
        raise ApiError(code="task_running", message="任务正在执行中", status_code=409) from None
    return {"task_id": task_id, "run_id": run.id, "status": "queued"}


async def recover_pending_runs() -> int:
    """Requeue tasks left queued/running when the API process was restarted."""
    async def load():
        items: list[tuple[str, object]] = []
        async with get_session_factory()() as session:
            runs = (await session.execute(select(AgentRun).where(AgentRun.status.in_(("queued", "running"))))).scalars().all()
        for run in runs:
            async with get_session_factory()() as session:
                task = await session.get(Task, run.task_id)
                if task is None:
                    continue
                try:
                    request_payload = TaskRunIn.model_validate(run.request_payload or {})
                    request = await _run_request(session, task, request_payload, None)
                    request.run_id = run.id
                except Exception:
                    async with get_session_factory()() as failed_session:
                        await finish_run(failed_session, run.id, "failed")
                    continue

                async def runner(run_id=run.id, request=request):
                    worker_id = f"recovery-{uuid.uuid4().hex[:8]}"
                    async with get_session_factory()() as claim_session:
                        if not await claim_run(claim_session, run_id, worker_id):
                            return
                    await _execute_claimed_run(run_id, worker_id, request)

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


@router.get("/tasks/{task_id}/events/stream")
async def task_event_stream(
    task_id: str,
    db: DbSession,
    after_seq: int = Query(default=0, ge=0),
) -> StreamingResponse:
    """Replay persisted events and wait for new ones from a sequence cursor."""
    await load_task(db, task_id)

    async def events():
        cursor = after_seq
        idle_rounds = 0
        terminal = {"succeeded", "failed", "cancelled", "paused", "waiting_approval"}
        while idle_rounds < 600:
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
                idle_rounds = 0
            else:
                idle_rounds += 1
                if status in terminal:
                    break
                yield ": keep-alive\n\n"
                await asyncio.sleep(0.5)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/tasks/{task_id}/evidence")
async def task_evidence(task_id: str, db: DbSession) -> dict:
    await load_task(db, task_id)
    return await build_evidence(get_session_factory(), task_id)


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
    task = await load_task(db, task_id)
    if task.status != "waiting_approval":
        raise ApiError(
            code="task_not_waiting_approval",
            message=f"任务当前不在待审批状态（当前状态：{task.status}），无需重复审批",
            status_code=409,
        )
    evidence = await build_evidence(get_session_factory(), task_id)
    pending = [str(item.get("tool") or "") for item in evidence.get("pending_approvals") or []]
    for approval in evidence.get("pending_approvals") or []:
        expires_at = approval.get("expires_at")
        if expires_at:
            try:
                expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires <= datetime.now(timezone.utc):
                    raise ApiError(code="approval_expired", message="审批请求已过期，请重新运行任务生成新的审批请求", status_code=409)
            except ValueError:
                raise ApiError(code="approval_invalid", message="审批请求时间无效，无法继续执行", status_code=409) from None
    recorder = EventRecorder(get_session_factory(), task_id)
    await recorder.add("approval.decided", "execute_action", {"approve": payload.approve, "tools": pending})
    # 决定只通过 Command(resume=...) 交回图里：批准时中断返回 True，图写入 approved_tools 并补记 approval.granted。
    # 在这里预先塞 approved_tools 会让中断被跳过，审批轨迹反而丢失。
    run_request = await _run_request(
        db, task, TaskRunIn(provider_id=payload.provider_id), request
    )
    evidence = await AgentRuntime(get_session_factory()).resume(run_request, decision=payload.approve)
    async with get_session_factory()() as session:
        finished = await load_task(session, task_id)
        return {"task": TaskOut.model_validate(finished).model_dump(), "evidence": evidence}


@router.post("/tasks/{task_id}/cancel", response_model=TaskOut)
async def cancel_task(task_id: str, db: DbSession) -> TaskOut:
    task = await load_task(db, task_id)
    if task.status in ("succeeded", "failed", "cancelled"):
        raise ApiError(code="task_not_cancellable", message=f"任务已结束，无法取消（当前状态：{task.status}）", status_code=409)
    task.status = "cancelled"
    await db.commit()
    await EventRecorder(get_session_factory(), task_id).add(
        "task.cancel_requested", "receive_task", {"status": "cancelled"}
    )
    return TaskOut.model_validate(task)
