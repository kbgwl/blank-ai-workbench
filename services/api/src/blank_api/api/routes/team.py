"""团队 Bot 与群聊的 HTTP 面。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from blank_api.api.auth import require_token
from blank_api.api.errors import ApiError
from blank_api.db import get_session_factory
from blank_api.deps import DbSession
from blank_api.models.model_provider import ModelProvider
from blank_api.models.project import Project, Workspace
from blank_api.models.task import Task
from blank_api.models.team import TeamBot, TeamGroup, TeamMember, TeamMessage, TeamRound
from blank_api.schemas.team import (
    TeamAnswerIn,
    TeamApprovalIn,
    TeamBotIn,
    TeamBotUpdate,
    TeamGroupIn,
    TeamGroupUpdate,
    TeamMessageIn,
)
from blank_api.services.agent.evidence import approval_is_expired, build_evidence
from blank_api.services.chat_index import delete_group_docs, search_chat
from blank_api.services.sandbox.tiers import normalize_tier, resolve_tier
from blank_api.services.team import (
    ADMIN_NAME,
    MAX_BOTS,
    MAX_MEMBERS,
    MAX_QUEUE_DEPTH,
    MIN_TOKEN_BUDGET,
    TeamOrchestrator,
    active_round,
    bot_payload,
    create_user_round,
    ensure_admin,
    group_state,
    list_bots,
    list_groups,
    load_group_or_404,
    prepare_group_members,
    request_stop,
)
from blank_api.services.workspace import mask_path

router = APIRouter(prefix="/team", tags=["team"], dependencies=[Depends(require_token)])
log = logging.getLogger(__name__)

_BACKGROUND: set[asyncio.Task] = set()


def _orchestrator(request: Request) -> TeamOrchestrator:
    """整个进程共用一个编排器。

    ``_active`` 用来挡住同一轮被重复调度；每次请求新建实例会让这道锁形同虚设。
    测试会把 mock 的 client factory 挂在 app.state 上，所以每次取用都要重新读一遍。
    """
    state = request.app.state
    orchestrator = getattr(state, "team_orchestrator", None)
    if orchestrator is None:
        orchestrator = TeamOrchestrator()
        state.team_orchestrator = orchestrator
    orchestrator.client_factory = getattr(state, "model_client_factory", None)
    return orchestrator


def _spawn_resume(
    orchestrator: TeamOrchestrator, message_id: int, kind: str, decision: dict
) -> None:
    """审批/回答后的续跑放到后台：一次完整模型调用不该占住 HTTP 请求。"""

    async def runner() -> None:
        try:
            await orchestrator.resume_message(message_id, kind, decision)
        except Exception:
            # 失败时 resume_message 会把轮次退回等待态（真正的恢复错误）
            # 或抛出并发冲突；两种都留给用户重试，这里只负责留下日志。
            log.exception("团队消息恢复失败 message=%s kind=%s", message_id, kind)

    task = asyncio.create_task(runner(), name=f"blank-team-resume-{kind}-{message_id}")
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


def _reject_expired(expires_at: str | None, *, what: str) -> None:
    if not expires_at:
        return
    try:
        datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        raise ApiError(code=f"{what}_invalid", message=f"{what} 时间无效，请重新发起", status_code=409) from None
    # TTL 比较交给读侧同一个函数：这两处一旦各算各的，`/state` 说还能批、
    # 批下去却 409（真模型点验实测到的就是这个错位）。
    if approval_is_expired(expires_at):
        raise ApiError(
            code=f"{what}_expired",
            message="审批请求已过期，请重新运行这一轮" if what == "approval" else "问题已过期，请重新运行这一轮",
            status_code=409,
        )


async def _validate_binding(
    db: DbSession, provider_id: str | None, workspace_id: str | None
) -> None:
    if provider_id:
        provider = await db.get(ModelProvider, provider_id)
        if provider is None:
            raise ApiError(code="provider_not_found", message="模型供应商不存在", status_code=404)
        if not provider.enabled:
            raise ApiError(code="provider_disabled", message="该模型供应商已停用", status_code=409)
    if workspace_id:
        workspace = await db.get(Workspace, workspace_id)
        if workspace is None:
            raise ApiError(code="workspace_not_found", message="工作区不存在", status_code=404)


async def _workspace_fits_bot(db: DbSession, bot_id: str, workspace_id: str) -> None:
    """Bot 可能同时在多个群：新工作区必须属于它所在的所有群的项目。"""
    workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        raise ApiError(code="workspace_not_found", message="工作区不存在", status_code=404)
    project_ids = set(
        (
            await db.execute(
                select(TeamGroup.project_id)
                .join(TeamMember, TeamMember.group_id == TeamGroup.id)
                .where(TeamMember.bot_id == bot_id)
            )
        )
        .scalars()
        .all()
    )
    if project_ids and project_ids != {workspace.project_id}:
        raise ApiError(
            code="bot_workspace_mismatch",
            message="这个工作区不属于该 Bot 所在的团队项目",
            status_code=409,
        )


@router.get("/bootstrap")
async def bootstrap(db: DbSession) -> dict:
    """前端一次拿齐：Bot 名册、群列表、可绑定的模型与项目。"""
    bots = await list_bots(db)
    groups = await list_groups(db)
    providers = (
        await db.execute(select(ModelProvider).order_by(ModelProvider.name))
    ).scalars().all()
    projects = (
        await db.execute(select(Project).options(selectinload(Project.workspaces)).order_by(Project.created_at))
    ).scalars().all()
    project_names = {project.id: project.name for project in projects}
    for group in groups:
        group["project_name"] = project_names.get(group["project_id"], "")
    return {
        "bots": bots,
        "groups": groups,
        "providers": [
            {
                "id": provider.id,
                "name": provider.name,
                "kind": provider.kind,
                "model": provider.default_model,
                "supports_tools": bool(provider.supports_tools),
                "supports_vision": bool(provider.supports_vision),
                "enabled": bool(provider.enabled),
            }
            for provider in providers
        ],
        "projects": [
            {
                "id": project.id,
                "name": project.name,
                "workspaces": [
                    {
                        "id": workspace.id,
                        "label": workspace.label,
                        "path": mask_path(workspace.path),
                        "is_active": bool(workspace.is_active),
                    }
                    for workspace in project.workspaces
                ],
            }
            for project in projects
        ],
        "limits": {
            "max_bots": MAX_BOTS,
            "max_members": MAX_MEMBERS,
            "max_queue_depth": MAX_QUEUE_DEPTH,
            "max_speakers": 8,
            "min_token_budget": MIN_TOKEN_BUDGET,
            "max_token_budget": 2_000_000,
            "default_token_budget": 60_000,
            "pass_marker": "[PASS]",
        },
        "admin_name": ADMIN_NAME,
    }


@router.get("/search")
async def search_messages(
    db: DbSession,
    q: str = Query(min_length=1, max_length=200),
    project_id: str = Query(default=""),
    group_id: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=50),
) -> dict:
    """跨群、跨任务的全文检索。不传 group_id 时在整个项目（或全部项目）里找。"""
    return await search_chat(
        db, q, project_id=project_id, group_id=group_id, limit=limit
    )


@router.get("/bots")
async def get_bots(db: DbSession) -> list[dict]:
    return await list_bots(db)


@router.post("/bots", status_code=201)
async def create_bot(payload: TeamBotIn, db: DbSession) -> dict:
    count = int((await db.execute(select(func.count(TeamBot.id)))).scalar_one() or 0)
    if count >= MAX_BOTS:
        raise ApiError(code="team_too_many_bots", message=f"最多创建 {MAX_BOTS} 个 Bot（含主理人）", status_code=409)
    await _validate_binding(db, payload.provider_id, payload.workspace_id)
    bot = TeamBot(
        id=uuid.uuid4().hex,
        name=payload.name,
        role=payload.role,
        instructions=payload.instructions,
        provider_id=payload.provider_id or None,
        workspace_id=payload.workspace_id or None,
        permission_mode=payload.permission_mode,
        max_steps=payload.max_steps,
        is_admin=False,
        enabled=payload.enabled,
    )
    db.add(bot)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ApiError(code="bot_name_taken", message=f"名字「{payload.name}」已经被占用", status_code=409) from None
    # bot_payload 要读 created_at/updated_at，那两个是服务端生成的：MySQL 的 INSERT 没有
    # RETURNING，不先读回来就是一次同步懒加载（MissingGreenlet → 500）。
    await db.refresh(bot)
    return await bot_payload(db, bot)


@router.patch("/bots/{bot_id}")
async def update_bot(bot_id: str, payload: TeamBotUpdate, db: DbSession) -> dict:
    bot = await db.get(TeamBot, bot_id)
    if bot is None:
        raise ApiError(code="bot_not_found", message="Bot 不存在", status_code=404)
    fields = payload.model_fields_set
    if bot.is_admin:
        if "name" in fields and payload.name and payload.name != bot.name:
            raise ApiError(code="admin_fixed", message=f"主理人固定为「{ADMIN_NAME}」，不能改名", status_code=409)
        if "enabled" in fields and payload.enabled is False:
            raise ApiError(code="admin_fixed", message="主理人不能停用", status_code=409)
    if "provider_id" in fields:
        await _validate_binding(db, payload.provider_id, None)
        bot.provider_id = payload.provider_id or None
    if "workspace_id" in fields:
        await _validate_binding(db, None, payload.workspace_id)
        if payload.workspace_id:
            await _workspace_fits_bot(db, bot.id, payload.workspace_id)
        bot.workspace_id = payload.workspace_id or None
    if "name" in fields and payload.name:
        bot.name = payload.name
    if "role" in fields and payload.role is not None and not bot.is_admin:
        bot.role = payload.role
    if "instructions" in fields and payload.instructions is not None:
        bot.instructions = payload.instructions
    if "permission_mode" in fields and payload.permission_mode:
        bot.permission_mode = payload.permission_mode
    if "max_steps" in fields and payload.max_steps is not None:
        bot.max_steps = payload.max_steps
    if "enabled" in fields and payload.enabled is not None and not bot.is_admin:
        bot.enabled = payload.enabled
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ApiError(code="bot_name_taken", message="这个名字已经被占用", status_code=409) from None
    # onupdate 会让 updated_at 在 UPDATE 后过期；异步会话里直接读会触发同步懒加载。
    await db.refresh(bot)
    return await bot_payload(db, bot)


@router.delete("/bots/{bot_id}", status_code=204)
async def delete_bot(bot_id: str, db: DbSession) -> Response:
    bot = await db.get(TeamBot, bot_id)
    if bot is None:
        raise ApiError(code="bot_not_found", message="Bot 不存在", status_code=404)
    if bot.is_admin:
        raise ApiError(code="admin_fixed", message="主理人不能删除", status_code=409)
    await db.execute(delete(TeamMember).where(TeamMember.bot_id == bot_id))
    await db.delete(bot)
    await db.commit()
    return Response(status_code=204)


@router.get("/groups")
async def get_groups(db: DbSession, project_id: str | None = Query(default=None)) -> list[dict]:
    groups = await list_groups(db)
    if project_id:
        groups = [group for group in groups if group["project_id"] == project_id]
    return groups


@router.post("/groups", status_code=201)
async def create_group(payload: TeamGroupIn, db: DbSession) -> dict:
    project = await db.get(Project, payload.project_id)
    if project is None:
        raise ApiError(code="project_not_found", message="项目不存在", status_code=404)
    admin = await ensure_admin(db)
    # 群档位在这里解析一次：不可用就拒绝建群，而不是让成员任务跑到一半才发现。
    tier = normalize_tier(payload.sandbox_tier, strict=True) if payload.sandbox_tier else ""
    if tier:
        resolution = resolve_tier(tier, strict=True)
        if not resolution.available:
            raise ApiError(
                code="sandbox_tier_unavailable",
                message=resolution.notice,
                status_code=409,
            )
    group = TeamGroup(
        id=uuid.uuid4().hex,
        project_id=payload.project_id,
        name=payload.name.strip(),
        topic=payload.topic,
        admin_bot_id=admin.id,
        max_speakers=payload.max_speakers,
        token_budget=payload.token_budget,
        sandbox_tier=tier,
    )
    db.add(group)
    await db.flush()
    await prepare_group_members(db, group, admin, payload.member_ids)
    await db.commit()
    return await group_state(group.id)


@router.get("/groups/{group_id}")
async def get_group(
    group_id: str,
    db: DbSession,
    after_id: int = Query(default=0, ge=0),
    around_id: int = Query(default=0, ge=0),
) -> dict:
    await load_group_or_404(db, group_id)
    return await group_state(group_id, after_id, around_id)


@router.get("/groups/{group_id}/state")
async def get_group_state(
    group_id: str,
    db: DbSession,
    after_id: int = Query(default=0, ge=0),
    around_id: int = Query(default=0, ge=0),
) -> dict:
    await load_group_or_404(db, group_id)
    return await group_state(group_id, after_id, around_id)


@router.patch("/groups/{group_id}")
async def update_group(group_id: str, payload: TeamGroupUpdate, db: DbSession) -> dict:
    group = await load_group_or_404(db, group_id)
    fields = payload.model_fields_set
    if "name" in fields and payload.name:
        group.name = payload.name.strip()
    if "topic" in fields and payload.topic is not None:
        group.topic = payload.topic
    if "max_speakers" in fields and payload.max_speakers is not None:
        group.max_speakers = payload.max_speakers
    if "token_budget" in fields and payload.token_budget is not None:
        group.token_budget = payload.token_budget
    if "member_ids" in fields and payload.member_ids is not None:
        if await active_round(db, group_id) is not None:
            raise ApiError(code="team_group_busy", message="这一轮还没结束，暂不能调整成员", status_code=409)
        admin = await ensure_admin(db)
        await prepare_group_members(db, group, admin, payload.member_ids)
    group.updated_at = datetime.utcnow()
    await db.commit()
    return await group_state(group_id)


@router.delete("/groups/{group_id}", status_code=204)
async def delete_group(group_id: str, db: DbSession) -> Response:
    group = await load_group_or_404(db, group_id)
    if await active_round(db, group_id) is not None:
        raise ApiError(code="team_group_busy", message="这一轮还没结束，先停止再删除", status_code=409)
    # 显式删子表：SQLite 默认不强制外键，只靠 ondelete 会留下孤儿行。
    await db.execute(delete(TeamMessage).where(TeamMessage.group_id == group.id))
    await db.execute(delete(TeamRound).where(TeamRound.group_id == group.id))
    await db.execute(delete(TeamMember).where(TeamMember.group_id == group.id))
    await delete_group_docs(db, group.id)
    await db.delete(group)
    await db.commit()
    return Response(status_code=204)


@router.post("/groups/{group_id}/messages", status_code=201)
async def post_message(group_id: str, payload: TeamMessageIn, request: Request, db: DbSession) -> dict:
    group = await load_group_or_404(db, group_id)
    admin = await ensure_admin(db)
    message, round_row = await create_user_round(db, group, payload.content, admin)
    await _orchestrator(request).schedule(round_row.id)
    state = await group_state(group_id)
    state["accepted"] = {"message_id": message.id, "round_id": round_row.id}
    return state


@router.post("/groups/{group_id}/stop")
async def stop_group(group_id: str, db: DbSession) -> dict:
    group = await load_group_or_404(db, group_id)
    round_row = await request_stop(db, group)
    if round_row is None:
        return {"stopped": False, "status": "idle"}
    return {"stopped": True, "round_id": round_row.id, "status": round_row.status}


async def _waiting_message(db: DbSession, message_id: int) -> tuple[TeamMessage, Task]:
    message = await db.get(TeamMessage, message_id)
    if message is None or not message.round_id:
        raise ApiError(code="team_message_not_found", message="群消息不存在", status_code=404)
    if message.status != "waiting_approval" or not message.task_id:
        raise ApiError(
            code="team_message_not_waiting",
            message="这条消息当前不在等待审批/回答状态",
            status_code=409,
        )
    task = await db.get(Task, message.task_id)
    if task is None or task.status != "waiting_approval":
        raise ApiError(
            code="team_task_not_waiting",
            message="对应的任务已经不在等待输入状态",
            status_code=409,
        )
    return message, task


@router.post("/messages/{message_id}/approval", status_code=202)
async def approve_message(
    message_id: int, payload: TeamApprovalIn, request: Request, db: DbSession
) -> dict:
    message, _ = await _waiting_message(db, message_id)
    evidence = await build_evidence(get_session_factory(), message.task_id or "")
    approvals = evidence.get("pending_approvals") or []
    if not approvals:
        raise ApiError(
            code="no_pending_approval",
            message="当前没有待审批的操作，这是一条待回答的问题",
            status_code=409,
        )
    for approval in approvals:
        _reject_expired(approval.get("expires_at"), what="approval")
    _spawn_resume(
        _orchestrator(request),
        message_id,
        "approval",
        {"approve": payload.approve, "scope": payload.scope},
    )
    return {"accepted": True, "message_id": message_id, "action": "approval"}


@router.post("/messages/{message_id}/answer", status_code=202)
async def answer_message(
    message_id: int, payload: TeamAnswerIn, request: Request, db: DbSession
) -> dict:
    message, _ = await _waiting_message(db, message_id)
    evidence = await build_evidence(get_session_factory(), message.task_id or "")
    questions = evidence.get("pending_questions") or []
    if not questions:
        raise ApiError(
            code="no_pending_question",
            message="当前没有待回答的问题，这是一条待审批的操作",
            status_code=409,
        )
    known = {str(item.get("id") or "") for item in questions}
    answers = {key: str(value)[:4000] for key, value in payload.answers.items() if key in known}
    if payload.answer.strip():
        answers.setdefault(str(questions[0].get("id") or ""), payload.answer.strip()[:4000])
    if not answers:
        raise ApiError(code="answer_empty", message="答案为空，至少回答一个问题", status_code=422)
    _reject_expired(evidence.get("question_expires_at"), what="question")
    _spawn_resume(_orchestrator(request), message_id, "answer", {"answers": answers})
    return {"accepted": True, "message_id": message_id, "action": "answer"}
