import asyncio
import json
import logging
import os
import time
import traceback
import uuid
from datetime import datetime, timezone

import anyio
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, select

from blank_api.api.auth import require_token
from blank_api.api.errors import ApiError
from blank_api.api.routes.projects import load_project
from blank_api.db import db_retry, get_session_factory
from blank_api.deps import DbSession
from blank_api.models.model_provider import ModelProvider
from blank_api.models.checkpoint import AgentCheckpoint, AgentCheckpointWrite
from blank_api.models.model_provider import ModelUsage
from blank_api.models.task import AgentEvent, FileChange, Task, ToolCall
from blank_api.models.run import AgentRun
from blank_api.models.skill import Skill
from blank_api.schemas.task import (
    EventOut,
    TaskAnswerIn,
    TaskApprovalIn,
    TaskCreate,
    TaskForkIn,
    TaskOut,
    TaskRunIn,
    TaskSteerIn,
)
from blank_api.services.agent.evidence import build_evidence, load_task
from blank_api.services.agent.artifacts import ArtifactStore
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.fork import FORK_EVENT, fork_thread
from blank_api.services.agent.manager import RunAlreadyActive, run_manager
from blank_api.services.agent.queue import claim_run, enqueue_run, finish_run, renew_run
from blank_api.services.agent.router import candidate_from_provider
from blank_api.services.models.registry import build_adapter
from blank_api.services.models.routing import FallbackAdapter
from blank_api.services.sandbox import SnapshotStore

router = APIRouter(tags=["tasks"], dependencies=[Depends(require_token)])
log = logging.getLogger(__name__)

# 自动重排队的上限：超过就不再无限重试，避免一条坏 run 在后台反复空转。
MAX_RECOVERY_ATTEMPTS = 3


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
    await db.delete(task)
    await db.commit()
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
    snapshot = {
        "task_id": task.id,
        "provider_id": provider.id,
        "provider_name": provider.name,
        "root": workspace.path,
        "title": task.title,
        "goal": task.goal,
        "acceptance_criteria": list(task.acceptance_criteria or []),
        "max_steps": int(task.max_steps),
        "skill_instructions": [str(item) for item in active_skills],
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
        permission_mode=payload.permission_mode,
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


async def _execute_claimed_run(run_id: str, worker_id: str, run_request: RunRequest) -> None:
    """Run one claimed job and keep its database lease alive while it works."""
    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(30)
            try:
                async with get_session_factory()() as session:
                    if not await renew_run(session, run_id, worker_id):
                        return
            except Exception:  # 续租失败不该把执行任务带走，下一轮再试
                log.warning("续租失败 run=%s", run_id, exc_info=True)

    lease_task = asyncio.create_task(heartbeat(), name=f"blank-lease-{run_id}")
    try:
        evidence = await AgentRuntime(get_session_factory()).run(run_request)
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
        await asyncio.gather(lease_task, return_exceptions=True)

    async def finish() -> None:
        async with get_session_factory()() as session:
            await finish_run(session, run_id, status)

    try:
        await db_retry(finish)
    except Exception as error:
        # 收尾失败会让任务永远停在 queued（界面一直转圈），必须留下痕迹并强行置终态
        log.exception("收尾写库失败 run=%s", run_id)
        await _record_run_failure(run_request.task_id, run_id, error)


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
        try:
            async with get_session_factory()() as session:
                if not await claim_run(session, run.id, worker_id):
                    return
            await _execute_claimed_run(run.id, worker_id, run_request)
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
                if run.attempts >= MAX_RECOVERY_ATTEMPTS:
                    # 反复重排同一个 run 只会无限循环（上次收尾没写成就是这种表现）。
                    # 到这里就停手，把原因交给用户，让他决定是否重试。
                    async with get_session_factory()() as failed_session:
                        await finish_run(failed_session, run.id, "failed")
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
    """Replay persisted events and wait for new ones from a sequence cursor."""
    await load_task(db, task_id)

    async def events():
        cursor = after_seq
        idle_rounds = 0
        terminal = {"succeeded", "failed", "cancelled", "paused", "waiting_approval"}
        while idle_rounds < 600:
            # 断流会取消这个生成器：如果取消正好落在连接 close 上，池子里会留下一条
            # 半关闭连接，后面所有请求都可能撞上 no active connection。每次读取都
            # 屏蔽取消，等这一小段数据库操作跑完再说。
            with anyio.CancelScope(shield=True):
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
    await recorder.add(
        "approval.decided",
        "execute_action",
        {"approve": payload.approve, "scope": payload.scope, "tools": pending},
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
        raise ApiError(code="answer_empty", message="答案为空，至少回答一个问题", status_code=422)

    expires_at = evidence.get("question_expires_at") or ""
    if expires_at:
        try:
            expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= datetime.now(timezone.utc):
                raise ApiError(
                    code="question_expired",
                    message="问题已过期，请重新运行任务",
                    status_code=409,
                )
        except ValueError:
            raise ApiError(code="question_invalid", message="问题时间无效", status_code=409) from None

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
    await EventRecorder(get_session_factory(), task_id).add(
        "task.cancel_requested", "receive_task", {"status": "cancelled"}
    )
    return TaskOut.model_validate(task)
