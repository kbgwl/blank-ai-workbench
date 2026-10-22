"""主理人（小白管家）的团队管理工具。

这些工具只在团队任务里、且只对主理人可见（运行时按 ``team_scope`` 裁剪）。
即便如此，工具自己也再校验一次身份：可见性是一层，执行边界是另一层，
不能因为某次装配漏了裁剪就变成任意 Bot 都能改团队。

所有写操作都是 high 风险，走现有审批链路；审批通过后才真正落库。
硬边界（不依赖审批结果）：

* 不能修改/停用/删除主理人；
* 不能把任何 Bot 设成 ``bypass`` 权限；
* 成员上限、名称唯一、供应商/工作区归属都按群所在项目校验；
* 主理人只在一轮之内调用这些工具，所以改动当轮生效；移出成员时同步把该 Bot
  从本轮队列里摘掉，避免下一个发言位指向已经不在群里的人。
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok

TEAM_ADMIN_TOOL_NAMES = frozenset(
    {
        "team_status",
        "team_create_bot",
        "team_update_bot",
        "team_add_member",
        "team_remove_member",
        "team_assign_task",
    }
)

_ALLOWED_MODES = ("default", "accept_edits", "plan")


def _scope(context: ToolContext) -> dict:
    return dict(getattr(context, "team_scope", None) or {})


def _forbidden() -> ToolOutcome:
    return fail("denied", "只有团队主理人（小白管家）能在群聊里管理团队")


def _clean_name(value: str) -> tuple[str, str]:
    name = str(value or "").strip()
    if not name:
        return "", "名字不能为空"
    if len(name) > 24:
        return "", "名字最多 24 个字符"
    if any(ch.isspace() or ch in "@＠" for ch in name):
        return "", "名字不能包含空格或 @"
    return name, ""


async def _resolve_bot(session, ref: str):
    from blank_api.models.team import TeamBot

    key = str(ref or "").strip()
    if not key:
        return None
    bot = await session.get(TeamBot, key)
    if bot is not None:
        return bot
    return (
        await session.execute(select(TeamBot).where(TeamBot.name == key).limit(1))
    ).scalar_one_or_none()


async def _current_group(session, context: ToolContext):
    from blank_api.models.team import TeamGroup

    group_id = str(_scope(context).get("group_id") or "")
    if not group_id:
        return None
    return await session.get(TeamGroup, group_id)


async def _member_of(session, group_id: str, bot_id: str):
    from blank_api.models.team import TeamMember

    return (
        await session.execute(
            select(TeamMember).where(
                TeamMember.group_id == group_id, TeamMember.bot_id == bot_id
            )
        )
    ).scalar_one_or_none()


async def _member_count(session, group_id: str) -> int:
    from blank_api.models.team import TeamMember

    return int(
        (
            await session.execute(
                select(func.count(TeamMember.id)).where(TeamMember.group_id == group_id)
            )
        ).scalar_one()
        or 0
    )


async def _status(context: ToolContext, arguments: dict) -> ToolOutcome:
    if not _scope(context).get("group_id"):
        return fail("denied", "team_status 只能在团队群聊的任务里使用")
    from blank_api.services.team import _member_rows, active_round, group_summary

    async with context.session_factory() as session:
        group = await _current_group(session, context)
        if group is None:
            return fail("error", "当前团队群不存在")
        rows = await _member_rows(session, group.id)
        summary = await group_summary(session, group)
        round_row = await active_round(session, group.id)
        members = [
            {
                "name": bot.name,
                "role": bot.role,
                "is_admin": bool(bot.is_admin),
                "enabled": bool(member.enabled and bot.enabled),
                "permission_mode": bot.permission_mode,
                "max_steps": int(bot.max_steps or 8),
            }
            for member, bot in rows
        ]
        payload = {
            "group": {"id": group.id, "name": group.name, "topic": group.topic},
            "members": members,
            "max_members": 5,
            "round": (
                {
                    "status": round_row.status,
                    "tokens": int(round_row.tokens or 0),
                    "spoken": len(round_row.spoken or []),
                    "queue": list(round_row.queue or []),
                }
                if round_row is not None
                else None
            ),
            "last_message": summary.get("last_message", ""),
        }
    lines = [
        f"团队「{payload['group']['name']}」，成员 {len(members)}/5：",
        *[
            f"- {item['name']}"
            + ("（主理人）" if item["is_admin"] else "")
            + ("" if item["enabled"] else "（已停用）")
            + f"｜{item['role'] or '未写角色'}｜权限 {item['permission_mode']}"
            for item in members
        ],
    ]
    if payload["round"]:
        lines.append(
            f"当前轮次 {payload['round']['status']}，已发言 {payload['round']['spoken']} 次，"
            f"队列 {len(payload['round']['queue'])} 人，累计 {payload['round']['tokens']} tokens。"
        )
    else:
        lines.append("当前没有进行中的轮次。")
    return ok("\n".join(lines), **payload)


async def _create_bot(context: ToolContext, arguments: dict) -> ToolOutcome:
    scope = _scope(context)
    if not scope.get("is_admin"):
        return _forbidden()
    from blank_api.models.model_provider import ModelProvider
    from blank_api.models.project import Project, Workspace
    from blank_api.models.team import TeamBot, TeamMember
    from blank_api.services.team import MAX_BOTS, MAX_MEMBERS, ensure_group_thread

    name, error = _clean_name(arguments.get("name"))
    if error:
        return fail("error", error)
    mode = str(arguments.get("permission_mode") or "default")
    if mode == "bypass":
        return fail("denied", "不能用工具创建 bypass 权限的 Bot；需要全放行请在设置页手动确认")
    if mode not in _ALLOWED_MODES:
        return fail("error", "permission_mode 只能是 default / accept_edits / plan")
    max_steps = int(arguments.get("max_steps") or 8)
    if not 1 <= max_steps <= 30:
        return fail("error", "max_steps 必须在 1 到 30 之间")

    async with context.session_factory() as session:
        group = await _current_group(session, context)
        if group is None:
            return fail("error", "当前团队群不存在")
        project = await session.get(Project, group.project_id)
        if project is None:
            return fail("error", "团队群所属项目不存在")
        count = int((await session.execute(select(func.count(TeamBot.id)))).scalar_one() or 0)
        if count >= MAX_BOTS:
            return fail("error", f"最多创建 {MAX_BOTS} 个 Bot（含主理人）")
        if await _member_count(session, group.id) >= MAX_MEMBERS:
            return fail("error", f"每个群最多 {MAX_MEMBERS} 名成员（含主理人）")

        provider_id = str(arguments.get("provider_id") or "").strip()
        if provider_id:
            provider = await session.get(ModelProvider, provider_id)
            if provider is None or not provider.enabled:
                return fail("error", "指定的模型供应商不存在或已停用")
        workspace_id = str(arguments.get("workspace_id") or "").strip()
        if workspace_id:
            workspace = await session.get(Workspace, workspace_id)
            if workspace is None or workspace.project_id != project.id:
                return fail("error", "指定的工作区不属于这个团队项目")

        # 重名要先在这里拦住。`team_bots.name` 是 UNIQUE，而冲突会在下面第一次
        # flush() 就抛 IntegrityError——那时还轮不到包着 commit 的那个 try。真模型
        # 实测踩过：工具回一句「工具执行失败：IntegrityError」，模型看不懂该改哪，
        # 只能原样重试同一个高风险调用，于是又挂一次审批，整轮永远等在 waiting_approval。
        # 这里给的是可执行的下一步，不是错误码。
        if await _resolve_bot(session, name) is not None:
            return fail(
                "error",
                f"名字「{name}」已经有一个 Bot 了。要把已有 Bot 拉进群请改用 team_add_member，"
                "换一个人设请改个名字再调 team_create_bot。",
            )

        bot = TeamBot(
            id=uuid.uuid4().hex,
            name=name,
            role=str(arguments.get("role") or "")[:100],
            instructions=str(arguments.get("instructions") or "")[:4000],
            provider_id=provider_id or None,
            workspace_id=workspace_id or None,
            permission_mode=mode,
            max_steps=max_steps,
            is_admin=False,
            enabled=True,
        )
        session.add(bot)
        await session.flush()
        member = TeamMember(
            id=uuid.uuid4().hex,
            group_id=group.id,
            bot_id=bot.id,
            position=await _member_count(session, group.id),
            enabled=True,
        )
        session.add(member)
        await session.flush()
        await ensure_group_thread(session, group, bot, member)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return fail("error", f"名字「{name}」已经被占用")
        group_name, bot_id = group.name, bot.id
    return ok(
        f"已创建 Bot「{name}」并加入团队「{group_name}」。",
        bot_id=bot_id,
        name=name,
        group_id=scope.get("group_id", ""),
        permission_mode=mode,
    )


async def _update_bot(context: ToolContext, arguments: dict) -> ToolOutcome:
    scope = _scope(context)
    if not scope.get("is_admin"):
        return _forbidden()
    from blank_api.models.model_provider import ModelProvider
    from blank_api.models.project import Project, Workspace

    async with context.session_factory() as session:
        group = await _current_group(session, context)
        if group is None:
            return fail("error", "当前团队群不存在")
        bot = await _resolve_bot(session, str(arguments.get("bot") or ""))
        if bot is None:
            return fail("error", "要调整的 Bot 不存在")
        if bot.is_admin:
            return fail("denied", "主理人固定为小白管家，不能用工具改名、改权限或停用")
        if await _member_of(session, group.id, bot.id) is None:
            return fail("error", "这个 Bot 不是当前群的成员，先 team_add_member 再加入")
        project = await session.get(Project, group.project_id)
        if project is None:
            return fail("error", "团队群所属项目不存在")

        if "name" in arguments:
            name, error = _clean_name(arguments.get("name"))
            if error:
                return fail("error", error)
            bot.name = name
        if "role" in arguments:
            bot.role = str(arguments.get("role") or "")[:100]
        if "instructions" in arguments:
            bot.instructions = str(arguments.get("instructions") or "")[:4000]
        if "permission_mode" in arguments:
            mode = str(arguments.get("permission_mode") or "")
            if mode == "bypass":
                return fail("denied", "不能用工具把 Bot 调成 bypass 权限")
            if mode not in _ALLOWED_MODES:
                return fail("error", "permission_mode 只能是 default / accept_edits / plan")
            bot.permission_mode = mode
        if "max_steps" in arguments:
            max_steps = int(arguments.get("max_steps") or 0)
            if not 1 <= max_steps <= 30:
                return fail("error", "max_steps 必须在 1 到 30 之间")
            bot.max_steps = max_steps
        if "enabled" in arguments:
            bot.enabled = bool(arguments.get("enabled"))
        if "provider_id" in arguments:
            provider_id = str(arguments.get("provider_id") or "").strip()
            if provider_id:
                provider = await session.get(ModelProvider, provider_id)
                if provider is None or not provider.enabled:
                    return fail("error", "指定的模型供应商不存在或已停用")
            bot.provider_id = provider_id or None
        if "workspace_id" in arguments:
            workspace_id = str(arguments.get("workspace_id") or "").strip()
            if workspace_id:
                workspace = await session.get(Workspace, workspace_id)
                if workspace is None or workspace.project_id != project.id:
                    return fail("error", "指定的工作区不属于这个团队项目")
            bot.workspace_id = workspace_id or None
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return fail("error", "这个名字已经被占用")
        await session.refresh(bot)
        payload = {
            "bot_id": bot.id,
            "name": bot.name,
            "role": bot.role,
            "permission_mode": bot.permission_mode,
            "max_steps": int(bot.max_steps or 8),
            "enabled": bool(bot.enabled),
        }
    return ok(f"已调整 Bot「{payload['name']}」。", **payload)


async def _add_member(context: ToolContext, arguments: dict) -> ToolOutcome:
    if not _scope(context).get("is_admin"):
        return _forbidden()
    from blank_api.models.project import Project
    from blank_api.models.team import TeamMember
    from blank_api.services.team import (
        MAX_MEMBERS,
        ensure_group_thread,
        validate_bot_for_group,
    )

    async with context.session_factory() as session:
        group = await _current_group(session, context)
        if group is None:
            return fail("error", "当前团队群不存在")
        # 主理人只会在一轮之内调用这些工具，"本轮进行中"不能当拦截条件，
        # 否则这条能力在真实运行里永远走不通。新成员不进当前轮的队列，
        # 由 @ 或 team_assign_task 排进来。
        bot = await _resolve_bot(session, str(arguments.get("bot") or ""))
        if bot is None:
            return fail("error", "要加入的 Bot 不存在")
        if bot.is_admin or await _member_of(session, group.id, bot.id) is not None:
            return fail("error", "这个 Bot 已经是群成员")
        if await _member_count(session, group.id) >= MAX_MEMBERS:
            return fail("error", f"每个群最多 {MAX_MEMBERS} 名成员（含主理人）")
        project = await session.get(Project, group.project_id)
        if project is None:
            return fail("error", "团队群所属项目不存在")
        try:
            await validate_bot_for_group(session, group, bot, project)
        except Exception as exc:
            return fail("error", str(getattr(exc, "message", "") or exc))
        member = TeamMember(
            id=uuid.uuid4().hex,
            group_id=group.id,
            bot_id=bot.id,
            position=await _member_count(session, group.id),
            enabled=True,
        )
        session.add(member)
        await session.flush()
        await ensure_group_thread(session, group, bot, member)
        await session.commit()
        bot_id, bot_name, group_id, group_name = bot.id, bot.name, group.id, group.name
    return ok(f"已把「{bot_name}」加入团队「{group_name}」。", bot_id=bot_id, group_id=group_id)


async def _remove_member(context: ToolContext, arguments: dict) -> ToolOutcome:
    if not _scope(context).get("is_admin"):
        return _forbidden()
    from blank_api.services.team import active_round

    async with context.session_factory() as session:
        group = await _current_group(session, context)
        if group is None:
            return fail("error", "当前团队群不存在")
        bot = await _resolve_bot(session, str(arguments.get("bot") or ""))
        if bot is None:
            return fail("error", "要移出的 Bot 不存在")
        if bot.is_admin:
            return fail("denied", "主理人不能移出团队")
        member = await _member_of(session, group.id, bot.id)
        if member is None:
            return fail("error", "这个 Bot 不在当前群里")
        await session.delete(member)
        # 队列是每轮从库里重读的：这里把已排到的名字摘掉，下一个发言位才不会
        # 指向刚被移出群的人。
        round_row = await active_round(session, group.id)
        if round_row is not None:
            queue = list(round_row.queue or [])
            kept = [item for item in queue if item != bot.id]
            if len(kept) != len(queue):
                round_row.queue = kept
        await session.commit()
        bot_id, bot_name, group_id, group_name = bot.id, bot.name, group.id, group.name
    return ok(f"已把「{bot_name}」移出团队「{group_name}」。", bot_id=bot_id, group_id=group_id)


async def _assign_task(context: ToolContext, arguments: dict) -> ToolOutcome:
    if not _scope(context).get("is_admin"):
        return _forbidden()
    from blank_api.models.team import TeamMessage, TeamRound

    task_text = str(arguments.get("task") or "").strip()
    if not task_text:
        return fail("error", "task 不能为空")
    async with context.session_factory() as session:
        group = await _current_group(session, context)
        if group is None:
            return fail("error", "当前团队群不存在")
        bot = await _resolve_bot(session, str(arguments.get("bot") or ""))
        if bot is None:
            return fail("error", "要分派的成员不存在")
        if bot.is_admin:
            return fail("error", "不需要把任务分派给主理人自己")
        member = await _member_of(session, group.id, bot.id)
        if member is None or not member.enabled or not bot.enabled:
            return fail("error", "这个成员不在群里或已停用")
        message = (
            await session.execute(
                select(TeamMessage)
                .where(TeamMessage.task_id == context.task_id)
                .order_by(TeamMessage.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        round_row = (
            await session.get(TeamRound, message.round_id)
            if message is not None and message.round_id
            else None
        )
        if round_row is None or round_row.status != "running":
            return fail("error", "当前没有进行中的轮次，无法分派任务")
        queue = list(round_row.queue or [])
        spoken = list(round_row.spoken or [])
        if bot.id in queue or bot.id in spoken:
            return fail("error", f"「{bot.name}」已经在本轮队列里或已经发过言")
        if len(spoken) + len(queue) >= int(group.max_speakers or 5):
            return fail("error", "本轮发言次数已达上限，无法再分派")
        queue.append(bot.id)
        round_row.queue = queue
        session.add(
            TeamMessage(
                group_id=group.id,
                round_id=round_row.id,
                role="system",
                bot_name="主理人",
                content=f"主理人指派任务给 @{bot.name}：{task_text[:1500]}",
                status="sent",
            )
        )
        await session.commit()
        bot_id, bot_name = bot.id, bot.name
    return ok(f"已把任务分派给「{bot_name}」，它会在本轮内发言。", bot_id=bot_id, task=task_text)


_STATUS_SCHEMA = {"type": "object", "properties": {}}

_CREATE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Bot 名字，不超过 24 字，不能含空格或 @"},
        "role": {"type": "string", "description": "角色，例如「后端」「评审」"},
        "instructions": {"type": "string", "description": "这个 Bot 的固定工作说明"},
        "provider_id": {"type": "string", "description": "绑定的模型供应商 ID；留空跟随默认"},
        "workspace_id": {"type": "string", "description": "绑定的工作区 ID；必须在当前项目内"},
        "permission_mode": {"type": "string", "enum": list(_ALLOWED_MODES)},
        "max_steps": {"type": "integer", "minimum": 1, "maximum": 30},
    },
    "required": ["name"],
}

_UPDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "bot": {"type": "string", "description": "Bot 名字或 ID"},
        "name": {"type": "string"},
        "role": {"type": "string"},
        "instructions": {"type": "string"},
        "provider_id": {"type": "string"},
        "workspace_id": {"type": "string"},
        "permission_mode": {"type": "string", "enum": list(_ALLOWED_MODES)},
        "max_steps": {"type": "integer", "minimum": 1, "maximum": 30},
        "enabled": {"type": "boolean"},
    },
    "required": ["bot"],
}

_MEMBER_SCHEMA = {
    "type": "object",
    "properties": {"bot": {"type": "string", "description": "Bot 名字或 ID"}},
    "required": ["bot"],
}

_ASSIGN_SCHEMA = {
    "type": "object",
    "properties": {
        "bot": {"type": "string", "description": "成员名字或 ID"},
        "task": {"type": "string", "description": "要分派的具体任务"},
    },
    "required": ["bot", "task"],
}


def team_admin_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="team_status",
            description="查看当前团队群的成员、各自角色与权限，以及本轮进行的状态。",
            input_schema=_STATUS_SCHEMA,
            risk_level="low",
            handler=_status,
        ),
        ToolDefinition(
            name="team_create_bot",
            description=(
                "创建一个新 Bot 并加入当前群。名字唯一且不能含空格/@；不能创建 bypass 权限。"
                "需要用户审批后才会执行。"
            ),
            input_schema=_CREATE_SCHEMA,
            risk_level="high",
            handler=_create_bot,
        ),
        ToolDefinition(
            name="team_update_bot",
            description=(
                "调整当前群某个 Bot 的角色、说明、供应商、工作区、权限档或停用状态。"
                "不能修改主理人，不能设成 bypass。需要用户审批。"
            ),
            input_schema=_UPDATE_SCHEMA,
            risk_level="high",
            handler=_update_bot,
        ),
        ToolDefinition(
            name="team_add_member",
            description="把一个已有 Bot 加入当前群。受成员上限约束，需要用户审批。",
            input_schema=_MEMBER_SCHEMA,
            risk_level="high",
            handler=_add_member,
        ),
        ToolDefinition(
            name="team_remove_member",
            description="把一个 Bot 移出当前群（不会删除 Bot 本身）。不能移出主理人，需要用户审批。",
            input_schema=_MEMBER_SCHEMA,
            risk_level="high",
            handler=_remove_member,
        ),
        ToolDefinition(
            name="team_assign_task",
            description="给当前群的一名成员分派具体任务，它会在本轮内发言。需要用户审批。",
            input_schema=_ASSIGN_SCHEMA,
            risk_level="high",
            handler=_assign_task,
        ),
    ]
