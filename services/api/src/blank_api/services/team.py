"""团队协作：Bot 名册、群聊轮次，以及把一轮讨论真正跑起来的编排器。

设计边界（刻意收窄，避免第一版变成不可控的蜂群）：

* 每条 Bot 发言仍是一条普通 Task。运行、审批、事件、用量、快照、回滚全部
  走既有链路；团队层不新开执行引擎，也不复制一套权限判断。
* 一个群同一时间只有一轮。主理人先发言；被 @ 的成员按点名顺序发言；
  没人可点名时结束。主理人在有成员发言后会收尾一次。
* 成员只输出 ``[PASS]`` 就等于没说话——记录仍然保留，方便回放"它确实被叫过"。
* 队列落库。等待审批时这一轮停在半路，批准后从停下的地方继续，而不是重跑。

编排器只做三件事：排队、构造 RunRequest、把 Task 的结局翻译成群消息。
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from blank_api.api.errors import ApiError
from blank_api.db import get_session_factory
from blank_api.models.model_provider import ModelProvider, ModelUsage
from blank_api.models.project import Project, Workspace
from blank_api.models.task import Task
from blank_api.models.team import (
    TeamBot,
    TeamGroup,
    TeamMember,
    TeamMessage,
    TeamRound,
)
from blank_api.models.thread import Thread
from blank_api.services.agent.evidence import build_evidence
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.chat_index import delete_doc, index_team_message
from blank_api.services.models.registry import build_adapter
from blank_api.services.permissions import MODE_DEFAULT, MODES
from blank_api.services.workspace import mask_path

log = logging.getLogger(__name__)

ADMIN_NAME = "小白管家"
ADMIN_ROLE = "主理人"
ADMIN_INSTRUCTIONS = (
    "你是小白管家，这个团队的主理人。先自己判断：能独立完成就给出结论；"
    "需要分工时精确 @对应成员。不要空话，不要复述别人刚说过的话。"
)

MAX_BOTS = 8
# 管理员 + 最多 4 名成员。人再多，一轮讨论的 token 和不确定性都会失控。
MAX_MEMBERS = 5
MAX_QUEUE_DEPTH = 4
MIN_TOKEN_BUDGET = 5_000

ACTIVE_ROUND_STATUSES = ("running", "waiting_approval", "cancelling")
TERMINAL_ROUND_STATUSES = ("completed", "failed", "cancelled")
PASS_MARKERS = ("[pass]", "[pass] ", "（pass）", "(pass)", "pass")
_TRANSCRIPT_TURNS = 18
_TRANSCRIPT_MAX_CHARS = 5_000


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return aware.isoformat()


def _normalize(text: str) -> str:
    return " ".join(str(text or "").split()).strip()


def is_pass(content: str) -> bool:
    """``[PASS]`` 是"这次不该我说话"，不是一条正常回复。"""
    cleaned = _normalize(content).lower()
    return cleaned in PASS_MARKERS or (cleaned.startswith("[pass]") and len(cleaned) <= 24)


def mentioned_names(text: str, names: list[str] | tuple[str, ...]) -> list[str]:
    """按出现位置解析 @名字。

    中文名字后面常常直接跟标点或动词（"@后端 看一下"、"＠小白管家，请收尾"），
    用"@ 到下一个空白"切会把名字切坏。这里反过来：用已知成员名去文本里找
    ``@名字``，并按首次出现的位置排序。名字在建 Bot 时已经禁止空格和 @，
    所以不会出现前缀歧义。
    """
    body = str(text or "")
    hits: list[tuple[int, str]] = []
    seen: set[str] = set()
    for name in names:
        if not name or name in seen:
            continue
        positions = [index for token in ("@", "＠") if (index := body.find(token + name)) >= 0]
        if positions:
            hits.append((min(positions), name))
            seen.add(name)
    hits.sort(key=lambda item: item[0])
    return [name for _, name in hits]


async def ensure_admin(session: AsyncSession) -> TeamBot:
    """确保主理人存在。名字是固定的：用户要求"主要管家叫小白管家"。"""
    existing = (
        await session.execute(select(TeamBot).where(TeamBot.is_admin == True).limit(1))  # noqa: E712
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    bot = TeamBot(
        id=str(uuid.uuid4()),
        name=ADMIN_NAME,
        role=ADMIN_ROLE,
        instructions=ADMIN_INSTRUCTIONS,
        permission_mode=MODE_DEFAULT,
        max_steps=8,
        is_admin=True,
        enabled=True,
    )
    session.add(bot)
    try:
        await session.commit()
        return bot
    except IntegrityError:
        # 两个首页请求同时到达时可能都走了"没有主理人"分支；unique(name) 兜底。
        await session.rollback()
        found = (
            await session.execute(select(TeamBot).where(TeamBot.name == ADMIN_NAME).limit(1))
        ).scalar_one_or_none()
        if found is None:
            raise
        return found


async def resolve_provider(session: AsyncSession, provider_id: str | None) -> ModelProvider:
    if provider_id:
        provider = await session.get(ModelProvider, provider_id)
        if provider is None:
            raise ApiError(code="provider_not_found", message="模型供应商不存在", status_code=404)
        if not provider.enabled:
            raise ApiError(code="provider_disabled", message="该模型供应商已停用", status_code=409)
        return provider
    provider = (
        await session.execute(
            select(ModelProvider)
            .where(ModelProvider.enabled == True)  # noqa: E712
            .order_by(ModelProvider.name)
        )
    ).scalars().first()
    if provider is None:
        raise ApiError(
            code="provider_not_configured",
            message="尚未配置可用的模型供应商，请先在设置中添加模型",
            status_code=409,
        )
    return provider


async def load_group_or_404(session: AsyncSession, group_id: str) -> TeamGroup:
    group = await session.get(TeamGroup, group_id)
    if group is None:
        raise ApiError(code="team_group_not_found", message="团队群不存在", status_code=404)
    return group


async def active_round(session: AsyncSession, group_id: str) -> TeamRound | None:
    return (
        await session.execute(
            select(TeamRound)
            .where(
                TeamRound.group_id == group_id,
                TeamRound.status.in_(ACTIVE_ROUND_STATUSES),
            )
            .order_by(TeamRound.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def latest_round(session: AsyncSession, group_id: str) -> TeamRound | None:
    """这个群最近的一轮。

    `team_rounds.created_at` 走服务端 `CURRENT_TIMESTAMP`：SQLite 上只到秒。同一秒里
    先后开的两轮会并列，只按时间排序时 SQLite 会把**上一轮**读成当前轮——前端面板
    于是显示旧轮的状态、队列与用量（真跑里就是这么露出来的）。消息 id 是群内自增序号，
    用它兜底把并列拆开；`coalesce` 是为了让没有消息的轮在各方言下都排最后（PG 的
    DESC 默认把 NULL 排最前）。
    """
    last_message_id = func.coalesce(
        select(func.max(TeamMessage.id))
        .where(TeamMessage.group_id == group_id, TeamMessage.round_id == TeamRound.id)
        .correlate(TeamRound)
        .scalar_subquery(),
        0,
    )
    return (
        await session.execute(
            select(TeamRound)
            .where(TeamRound.group_id == group_id)
            .order_by(TeamRound.created_at.desc(), last_message_id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def round_origin(round_id: str) -> str:
    """一轮群聊的发起轮 id：写进账本和按它查账用的是同一个函数，不留两处各自拼一遍的机会。

    带 `team-round-` 前缀而不是裸 uuid——这一列和 run_id、task_id、thread_id 同住一张表，
    一个谁都可能长得像的值在库里读不出"它是什么"，就得回头翻代码才知道。
    """
    return f"team-round-{round_id}"


async def _member_rows(session: AsyncSession, group_id: str) -> list[tuple[TeamMember, TeamBot]]:
    rows = (
        await session.execute(
            select(TeamMember, TeamBot)
            .join(TeamBot, TeamBot.id == TeamMember.bot_id)
            .where(TeamMember.group_id == group_id)
            .order_by(TeamMember.position, TeamMember.created_at)
        )
    ).all()
    return [(member, bot) for member, bot in rows]


async def ensure_group_thread(session: AsyncSession, group: TeamGroup, bot: TeamBot, member: TeamMember) -> Thread:
    """每个 Bot 在群里一条隐藏会话。没有就建，有就复用。"""
    if member.thread_id:
        thread = await session.get(Thread, member.thread_id)
        if thread is not None:
            return thread
    thread = Thread(
        id=str(uuid.uuid4()),
        project_id=group.project_id,
        title=(f"团队·{group.name}·{bot.name}")[:200],
        status="active",
        kind="team",
        turn_count=0,
    )
    session.add(thread)
    await session.flush()
    member.thread_id = thread.id
    return thread


async def validate_bot_for_group(
    session: AsyncSession, group: TeamGroup, bot: TeamBot, project: Project
) -> None:
    if not bot.enabled:
        raise ApiError(code="bot_disabled", message=f"Bot「{bot.name}」已停用", status_code=409)
    if bot.provider_id:
        provider = await session.get(ModelProvider, bot.provider_id)
        if provider is None or not provider.enabled:
            raise ApiError(
                code="bot_provider_unavailable",
                message=f"Bot「{bot.name}」绑定的模型供应商不存在或已停用",
                status_code=409,
            )
    if bot.workspace_id:
        workspace = await session.get(Workspace, bot.workspace_id)
        if workspace is None or workspace.project_id != project.id:
            raise ApiError(
                code="bot_workspace_mismatch",
                message=f"Bot「{bot.name}」绑定的工作区不属于这个项目",
                status_code=409,
            )


async def prepare_group_members(
    session: AsyncSession,
    group: TeamGroup,
    admin: TeamBot,
    member_ids: list[str],
) -> None:
    """创建/重建群成员。主理人永远在第一位，成员数量有硬上限。"""
    project = (
        await session.execute(
            select(Project).options(selectinload(Project.workspaces)).where(Project.id == group.project_id)
        )
    ).scalar_one_or_none()
    if project is None:
        raise ApiError(code="project_not_found", message="项目不存在", status_code=404)

    cleaned: list[str] = []
    for bot_id in member_ids:
        if bot_id == admin.id or bot_id in cleaned:
            continue
        cleaned.append(bot_id)
    if len(cleaned) + 1 > MAX_MEMBERS:
        raise ApiError(
            code="team_too_many_members",
            message=f"每个群最多 {MAX_MEMBERS} 名成员（含主理人）",
            status_code=422,
        )

    selected: list[TeamBot] = []
    for bot_id in cleaned:
        bot = await session.get(TeamBot, bot_id)
        if bot is None:
            raise ApiError(code="bot_not_found", message="成员 Bot 不存在", status_code=404)
        await validate_bot_for_group(session, group, bot, project)
        selected.append(bot)

    existing = {member.bot_id: member for member, _ in await _member_rows(session, group.id)}
    keep = {admin.id, *(bot.id for bot in selected)}
    for bot_id, member in existing.items():
        if bot_id not in keep:
            await session.delete(member)
    for position, bot in enumerate([admin, *selected]):
        member = existing.get(bot.id)
        if member is None:
            member = TeamMember(
                id=str(uuid.uuid4()),
                group_id=group.id,
                bot_id=bot.id,
                position=position,
                enabled=True,
            )
            session.add(member)
            await session.flush()
        else:
            member.position = position
            member.enabled = True
        await ensure_group_thread(session, group, bot, member)


async def create_user_round(
    session: AsyncSession,
    group: TeamGroup,
    content: str,
    admin: TeamBot,
) -> tuple[TeamMessage, TeamRound]:
    current = await active_round(session, group.id)
    if current is not None:
        raise ApiError(
            code="team_group_busy",
            message="这个群上一轮还没说完，等它结束或先停止",
            status_code=409,
        )
    text = str(content or "").strip()
    if not text:
        raise ApiError(code="team_message_empty", message="消息不能为空", status_code=422)

    if not group.admin_bot_id:
        group.admin_bot_id = admin.id
    rows = [(member, bot) for member, bot in await _member_rows(session, group.id) if member.enabled and bot.enabled]
    if not any(bot.id == admin.id for _, bot in rows):
        # 老群或异常数据：主理人不在成员表里就补进去，否则这一轮没人开场。
        member = TeamMember(
            id=str(uuid.uuid4()), group_id=group.id, bot_id=admin.id, position=0, enabled=True
        )
        session.add(member)
        await session.flush()
        await ensure_group_thread(session, group, admin, member)
        rows.insert(0, (member, admin))

    by_name = {bot.name: bot for _, bot in rows}
    picked_names = mentioned_names(text, list(by_name))
    picked_ids = [by_name[name].id for name in picked_names if by_name[name].id != admin.id]

    cap = max(1, int(group.max_speakers or 5))
    queue = [admin.id]
    for bot_id in picked_ids:
        if bot_id not in queue:
            queue.append(bot_id)
    # 超出上限的人不是"没被点名"，只是排不上：静默砍掉会让群看起来没听用户说话，
    # 所以记下被挤掉的人，在建轮时补一句可核对的系统说明。
    dropped = queue[cap:]
    queue = queue[:cap]

    message = TeamMessage(
        group_id=group.id,
        role="user",
        content=text,
        status="sent",
        meta={"mentions": picked_names},
    )
    session.add(message)
    await session.flush()
    # 用户这句话立刻可检索：发送成功与索引成功是同一笔事务，不会出现"消息在、搜不到"。
    await index_team_message(session, message, group)
    round_row = TeamRound(
        id=str(uuid.uuid4()),
        group_id=group.id,
        user_message_id=message.id,
        status="running",
        queue=queue,
        spoken=[],
        tokens=0,
        wrap_up_done=False,
    )
    session.add(round_row)
    message.round_id = round_row.id
    if dropped:
        names = {bot.id: bot.name for _, bot in rows}
        session.add(
            TeamMessage(
                group_id=group.id,
                round_id=round_row.id,
                role="system",
                bot_name="系统",
                content=(
                    f"本轮发言上限是 {cap} 次（主理人的开场也算在内），"
                    + "、".join(f"@{names.get(bot_id, '某位成员')}" for bot_id in dropped)
                    + " 这次没排上，需要时请下一轮再点名。"
                ),
                status="sent",
            )
        )
    group.updated_at = datetime.utcnow()
    await session.commit()
    return message, round_row


def build_protocol(
    *,
    group: TeamGroup,
    bot: TeamBot,
    admin: TeamBot,
    member_names: list[str],
    mentioned: bool,
    wrap_up: bool,
    spoken_count: int,
    remaining_tokens: int,
) -> str:
    roster = "、".join(member_names)
    lines = [
        f"你正在参与多 Bot 群聊「{group.name}」。" + (f"群主题：{group.topic}。" if group.topic else ""),
        f"你的身份：{bot.name}" + (f"（{bot.role}）" if bot.role else "") + ("；你是主理人。" if bot.is_admin else "。"),
        f"群成员：{roster}。主理人：{admin.name}。",
        "协作规则：",
        "1. 用户的消息是唯一任务来源。只回应与本群任务相关的内容。",
        "2. 需要其他成员补充或执行时，在回复里用 @成员名 点名；被点名的人会紧接着发言。名字必须写准确。",
        "3. 如果你没有新信息、不能执行或不该插话，只输出 [PASS]，不要输出别的内容。",
        "4. 不要假装调用过工具；需要读文件、改文件、执行命令就实际调用工具。",
        "5. 不要重复别人已经说过的内容；给出可核对的事实、改动或结论。",
        f"6. 本轮已发言 {spoken_count} 次，最多 {group.max_speakers} 次；token 预算剩余约 {remaining_tokens}。",
    ]
    if bot.is_admin:
        if wrap_up:
            lines.append("现在轮到你收尾：整合成员发言，给出最终结论、已完成事项与仍需用户决定的问题。不要再重复点名已经发言过的成员。")
        else:
            lines.append("你是主理人，先回答用户。能独立解决就自己解决；需要分工时点名最合适的成员。")
            lines.append(
                "需要调整团队时，你可以用 team_status 查看状态、team_create_bot / team_update_bot "
                "创建或调整 Bot、team_add_member / team_remove_member 管理本群成员、"
                "team_assign_task 给成员分派任务。这些写操作会请求用户审批；不要声称已经完成尚未执行的操作。"
            )
    else:
        lines.append("你是成员。只有在你能补充事实、执行任务或指出风险时发言；否则 [PASS]。")
    if mentioned:
        lines.append("用户这条消息点名了你，请正面回应。")
    return "\n".join(lines)


async def _transcript(
    session: AsyncSession, group_id: str, round_id: str
) -> str:
    rows = (
        await session.execute(
            select(TeamMessage)
            .where(TeamMessage.group_id == group_id, TeamMessage.role.in_(("user", "bot")))
            .order_by(TeamMessage.id.desc())
            .limit(_TRANSCRIPT_TURNS)
        )
    ).scalars().all()
    blocks: list[str] = []
    total = 0
    for row in reversed(rows):
        if row.status in ("pass", "running", "cancelled") or not _normalize(row.content):
            continue
        body = _normalize(row.content)
        if len(body) > 700:
            body = body[:699].rstrip() + "…"
        speaker = "用户" if row.role == "user" else (row.bot_name or "成员")
        block = f"[{speaker}] {body}"
        if total + len(block) > _TRANSCRIPT_MAX_CHARS:
            break
        blocks.append(block)
        total += len(block)
    return "\n".join(blocks)


@dataclass
class _TurnContext:
    group: TeamGroup
    bot: TeamBot
    admin: TeamBot
    member: TeamMember
    provider: ModelProvider
    workspace: Workspace
    thread: Thread


class TeamOrchestrator:
    """一轮群聊的推进器。所有状态都从数据库读，进程重启后仍可继续。"""

    def __init__(self, client_factory: Callable[..., httpx.AsyncClient] | None = None) -> None:
        self.client_factory = client_factory
        self._active: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._resuming: set[str] = set()

    async def shutdown(self) -> None:
        """停掉本进程里还在推进的轮次（进程退出用，不改数据库状态）。"""
        tasks = [task for task in self._active.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()

    async def schedule(self, round_id: str) -> None:
        async with self._lock:
            current = self._active.get(round_id)
            if current is not None and not current.done():
                return
            task = asyncio.create_task(self._advance(round_id), name=f"blank-team-{round_id}")
            self._active[round_id] = task

            def _cleanup(finished: asyncio.Task, rid: str = round_id) -> None:
                if self._active.get(rid) is finished:
                    self._active.pop(rid, None)

            task.add_done_callback(_cleanup)

    async def _advance(self, round_id: str) -> None:
        factory = get_session_factory()
        try:
            while True:
                async with factory() as session:
                    round_row = await session.get(TeamRound, round_id)
                    if round_row is None:
                        return
                    if round_row.status in ("cancelled", "cancelling"):
                        if round_row.status == "cancelling":
                            round_row.status = "cancelled"
                            await session.commit()
                        return
                    if round_row.status != "running":
                        return
                    group = await session.get(TeamGroup, round_row.group_id)
                    if group is None:
                        return
                    # 已经有人在发言时只旁观：没有这层判断，第二个推进器会在第一位
                    # Bot 还在跑的时候看到「队列空了」，把整轮提前判定为完成。
                    in_flight = (
                        await session.execute(
                            select(TeamMessage.id)
                            .where(
                                TeamMessage.round_id == round_row.id,
                                TeamMessage.status == "running",
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if in_flight is not None:
                        return
                    queue = list(round_row.queue or [])
                    spoken = list(round_row.spoken or [])

                    if not queue:
                        if (
                            not round_row.wrap_up_done
                            and group.admin_bot_id
                            and any(bot_id != group.admin_bot_id for bot_id in spoken)
                            and len(spoken) < int(group.max_speakers or 5)
                        ):
                            round_row.queue = [group.admin_bot_id]
                            round_row.wrap_up_done = True
                            await session.commit()
                            continue
                        round_row.status = "completed"
                        await session.commit()
                        return

                    admin_bot_id = group.admin_bot_id
                    if len(spoken) >= int(group.max_speakers or 5):
                        session.add(
                            TeamMessage(
                                group_id=group.id,
                                round_id=round_row.id,
                                role="system",
                                bot_name="系统",
                                content=f"本轮达到 {group.max_speakers} 次发言上限，先停在这里。",
                                status="sent",
                            )
                        )
                        round_row.status = "completed"
                        await session.commit()
                        return
                    if int(round_row.tokens or 0) >= int(group.token_budget or 0):
                        session.add(
                            TeamMessage(
                                group_id=group.id,
                                round_id=round_row.id,
                                role="system",
                                bot_name="系统",
                                content=f"本轮已用 {round_row.tokens} tokens，达到预算上限，先停在这里。",
                                status="sent",
                            )
                        )
                        round_row.status = "completed"
                        await session.commit()
                        return

                    bot_id = queue.pop(0)
                    round_row.queue = queue
                    await session.commit()
                    if bot_id == admin_bot_id:
                        # 主理人可能同时是队列里的普通成员；这里无需特殊处理。
                        pass

                outcome = await self._run_turn(round_id, bot_id)
                if outcome in ("waiting", "stopped"):
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 编排器崩了必须把原因交给用户，不能让群永远转圈
            log.exception("团队轮次执行失败 round=%s", round_id)
            async with factory() as session:
                round_row = await session.get(TeamRound, round_id)
                if round_row is not None and round_row.status in ACTIVE_ROUND_STATUSES:
                    round_row.status = "failed"
                    round_row.error = str(exc)[:2000]
                    session.add(
                        TeamMessage(
                            group_id=round_row.group_id,
                            round_id=round_row.id,
                            role="system",
                            bot_name="系统",
                            content=f"这一轮执行失败：{str(exc)[:500]}",
                            status="failed",
                        )
                    )
                    await session.commit()

    async def _load_context(
        self, session: AsyncSession, group_id: str, bot_id: str
    ) -> _TurnContext | None:
        group = await session.get(TeamGroup, group_id)
        if group is None:
            return None
        member = (
            await session.execute(
                select(TeamMember).where(
                    TeamMember.group_id == group_id, TeamMember.bot_id == bot_id
                )
            )
        ).scalar_one_or_none()
        bot = await session.get(TeamBot, bot_id)
        if member is None or bot is None or not member.enabled or not bot.enabled:
            return None
        admin = await session.get(TeamBot, group.admin_bot_id) if group.admin_bot_id else None
        if admin is None:
            admin = await ensure_admin(session)
        project = (
            await session.execute(
                select(Project)
                .options(selectinload(Project.workspaces))
                .where(Project.id == group.project_id)
            )
        ).scalar_one_or_none()
        if project is None:
            raise ApiError(code="project_not_found", message="项目不存在", status_code=404)
        if bot.workspace_id:
            workspace = next((w for w in project.workspaces if w.id == bot.workspace_id), None)
            if workspace is None:
                raise ApiError(
                    code="bot_workspace_mismatch",
                    message=f"Bot「{bot.name}」绑定的工作区不属于这个项目",
                    status_code=409,
                )
        else:
            workspace = project.active_workspace
            if workspace is None:
                raise ApiError(
                    code="workspace_not_bound",
                    message="该项目尚未绑定工作区目录",
                    status_code=409,
                )
        provider = await resolve_provider(session, bot.provider_id)
        thread = await ensure_group_thread(session, group, bot, member)
        return _TurnContext(
            group=group,
            bot=bot,
            admin=admin,
            member=member,
            provider=provider,
            workspace=workspace,
            thread=thread,
        )

    def _request(
        self,
        ctx: _TurnContext,
        task: Task,
        goal: str,
        system_instructions: list[str],
        run_id: str,
        origin_run_id: str = "",
    ) -> RunRequest:
        return RunRequest(
            task_id=task.id,
            provider_id=ctx.provider.id,
            provider_name=ctx.provider.name,
            adapter=build_adapter(ctx.provider, self.client_factory),
            root=Path(ctx.workspace.path),
            run_id=run_id,
            origin_run_id=origin_run_id,
            title=task.title,
            goal=goal,
            acceptance_criteria=(),
            max_steps=max(1, int(ctx.bot.max_steps or 8)),
            permission_mode=ctx.bot.permission_mode or MODE_DEFAULT,
            # 成员任务逐字继承群档位：群是成员任务的父级边界，子级没有"自己挑一档"的入口。
            sandbox_tier=str(ctx.group.sandbox_tier or ""),
            system_instructions=system_instructions,
            thread_ref=ctx.thread.id,
            supports_vision=bool(ctx.provider.supports_vision),
            team_scope={
                "group_id": ctx.group.id,
                "bot_id": ctx.bot.id,
                "is_admin": bool(ctx.bot.is_admin),
                "project_id": ctx.group.project_id,
            },
            memory_source=f"团队·{ctx.group.name}·{ctx.bot.name}",
            # 群消息在 _finalize_turn 里统一入索引；任务级再索引一次会得到重复结果。
            index_summary=False,
        )

    async def _run_turn(self, round_id: str, bot_id: str) -> str:
        factory = get_session_factory()
        async with factory() as session:
            round_row = await session.get(TeamRound, round_id)
            if round_row is None or round_row.status != "running":
                return "stopped"
            ctx = await self._load_context(session, round_row.group_id, bot_id)
            if ctx is None:
                session.add(
                    TeamMessage(
                        group_id=round_row.group_id,
                        round_id=round_id,
                        role="system",
                        bot_name="系统",
                        content="有一位成员已停用或已被移出群聊，已跳过。",
                        status="sent",
                    )
                )
                await session.commit()
                return "skipped"

            user_message = (
                await session.get(TeamMessage, round_row.user_message_id)
                if round_row.user_message_id
                else None
            )
            user_text = user_message.content if user_message is not None else ""
            mentioned_ids = set()
            if user_message is not None:
                mentioned_ids = {
                    bot.id
                    for member, bot in await _member_rows(session, ctx.group.id)
                    if bot.name in list((user_message.meta or {}).get("mentions") or [])
                }
            spoken = list(round_row.spoken or [])
            wrap_up = ctx.bot.is_admin and bot_id in spoken
            remaining = max(0, int(ctx.group.token_budget or 0) - int(round_row.tokens or 0))
            rows = await _member_rows(session, ctx.group.id)
            protocol = build_protocol(
                group=ctx.group,
                bot=ctx.bot,
                admin=ctx.admin,
                member_names=[bot.name for _, bot in rows],
                mentioned=bot_id in mentioned_ids,
                wrap_up=wrap_up,
                spoken_count=len(spoken),
                remaining_tokens=remaining,
            )
            transcript = await _transcript(session, ctx.group.id, round_id)
            system_instructions = [protocol]
            if transcript:
                system_instructions.append("最近群聊记录（由旧到新）：\n" + transcript)

            goal = user_text
            if wrap_up:
                goal = f"请收尾本轮讨论。用户最初的要求：{user_text}"
            title = (f"@{ctx.bot.name} · {ctx.group.name}")[:200]
            thread = ctx.thread
            turn_index = int(thread.turn_count or 0) + 1
            thread.turn_count = turn_index
            task = Task(
                id=str(uuid.uuid4()),
                project_id=ctx.group.project_id,
                provider_id=ctx.provider.id,
                thread_id=thread.id,
                turn_index=turn_index,
                title=title,
                goal=goal,
                status="draft",
                acceptance_criteria=[],
                acceptance_results=[],
                attachments=[],
                max_steps=max(1, int(ctx.bot.max_steps or 8)),
            )
            session.add(task)
            await session.flush()
            message = TeamMessage(
                group_id=ctx.group.id,
                round_id=round_id,
                role="bot",
                bot_id=ctx.bot.id,
                bot_name=ctx.bot.name,
                content="",
                status="running",
                task_id=task.id,
                meta={
                    "provider": ctx.provider.name,
                    "model": ctx.provider.default_model,
                    "wrap_up": wrap_up,
                    "mentions": [],
                },
            )
            session.add(message)
            await session.flush()
            message_id = message.id
            await session.commit()

            request = self._request(
                ctx,
                task,
                goal,
                system_instructions,
                run_id=f"team-{round_id}-{message_id}",
                origin_run_id=round_origin(round_id),
            )
            task_id = task.id
            bot_name = ctx.bot.name

        recorder = EventRecorder(
            factory, task_id, run_id=request.run_id, origin_run_id=request.origin_run_id
        )
        await recorder.add(
            "team.turn.started",
            "receive_task",
            {
                "group_id": ctx.group.id,
                "round_id": round_id,
                "bot_id": bot_id,
                "bot_name": bot_name,
                "message_id": message_id,
                "wrap_up": wrap_up,
            },
        )
        error = ""
        try:
            evidence = await AgentRuntime(factory).run(request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # ModelError 也会走到这里；Task 状态由 runtime 尽力落库
            error = str(exc) or exc.__class__.__name__
            log.warning("团队 Bot 发言失败 round=%s bot=%s: %s", round_id, bot_id, error)
            await self._mark_task_failed(task_id, error)
            try:
                evidence = await build_evidence(factory, task_id)
            except Exception:
                evidence = {}
        return await self._finalize_turn(round_id, message_id, bot_id, evidence, error)

    async def _mark_task_failed(self, task_id: str, error: str) -> None:
        factory = get_session_factory()
        changed = False
        async with factory() as session:
            task = await session.get(Task, task_id)
            if task is not None and task.status not in (
                "succeeded",
                "failed",
                "cancelled",
                "waiting_approval",
                "paused",
            ):
                task.status = "failed"
                task.failure_reason = error[:2000]
                changed = True
                await session.commit()
        if changed:
            await EventRecorder(factory, task_id).add(
                "task.failed", "error", {"reason": error[:2000], "error": error[:2000]}
            )

    async def _finalize_turn(
        self,
        round_id: str,
        message_id: int,
        bot_id: str,
        evidence: dict,
        error: str = "",
    ) -> str:
        factory = get_session_factory()
        async with factory() as session:
            round_row = await session.get(TeamRound, round_id)
            message = await session.get(TeamMessage, message_id)
            if round_row is None or message is None:
                return "stopped"
            task = await session.get(Task, message.task_id) if message.task_id else None
            group = await session.get(TeamGroup, round_row.group_id)
            if group is None:
                round_row.status = "failed"
                round_row.error = "团队群已被删除"
                await session.commit()
                return "stopped"

            usage = evidence.get("usage") or {}
            tokens = int(usage.get("total_tokens") or 0)
            round_row.tokens = int(round_row.tokens or 0) + tokens
            meta = dict(message.meta or {})
            meta.update(
                {
                    "usage": {
                        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                        "completion_tokens": int(usage.get("completion_tokens") or 0),
                        "total_tokens": tokens,
                    },
                    "steps": int(getattr(task, "steps_used", 0) or 0),
                }
            )
            if error:
                meta["error"] = error[:1200]

            summary = (getattr(task, "result_summary", "") or "").strip() if task is not None else ""
            failure = (getattr(task, "failure_reason", "") or "").strip() if task is not None else ""

            if round_row.status in ("cancelled", "cancelling"):
                message.status = "cancelled"
                # 用户停止后模型仍可能晚到一段输出，它不是这一轮的结论，不能当正文展示；
                # 原文留在 meta 里备查，气泡只说"已停止"。
                message.content = "已停止"
                if summary:
                    meta["discarded_output"] = summary[:1200]
                message.meta = meta
                round_row.status = "cancelled"
                await session.commit()
                return "stopped"

            task_status = getattr(task, "status", "failed") if task is not None else "failed"
            if task_status == "waiting_approval":
                message.status = "waiting_approval"
                message.content = (getattr(task, "result_summary", "") or "").strip()
                meta["pending"] = {
                    "approvals": len(evidence.get("pending_approvals") or []),
                    "questions": len(evidence.get("pending_questions") or []),
                }
                message.meta = meta
                if summary:
                    await index_team_message(session, message, group)
                round_row.status = "waiting_approval"
                await session.commit()
                return "waiting"

            if task_status == "succeeded":
                if is_pass(summary):
                    message.status = "pass"
                    message.content = ""
                else:
                    message.status = "succeeded"
                    message.content = summary or "（这一轮没有输出结论）"
            elif task_status == "cancelled":
                message.status = "cancelled"
                message.content = summary or "已停止"
            elif task_status == "paused":
                # 驳回高风险操作不是失败：这一步没执行，任务停在安全闸门上，
                # 群聊里要如实说明，主理人随后会收尾。
                message.status = "paused"
                message.content = summary or failure or "用户驳回了高风险操作，该步骤未执行"
            else:
                message.status = "failed"
                message.content = failure or summary or error or "这一轮没有完成"

            spoken = [*list(round_row.spoken or []), bot_id]
            round_row.spoken = spoken
            queue = list(round_row.queue or [])

            if message.status not in ("pass", "failed", "cancelled") and summary:
                members = await _member_rows(session, group.id)
                by_name = {bot.name: bot for member, bot in members if member.enabled and bot.enabled}
                picked = mentioned_names(summary, list(by_name))
                meta["mentions"] = picked
                planned = len(spoken) + len(queue)
                max_speakers = int(group.max_speakers or 5)
                dropped: list[str] = []
                dropped_by = ""
                for index, name in enumerate(picked):
                    target = by_name.get(name)
                    if target is None or target.id in spoken or target.id in queue:
                        continue
                    if target.id == group.admin_bot_id and round_row.wrap_up_done:
                        continue
                    # 排不下不是"没被点名"：记下是谁、被哪条闸挡住，末尾补一句可核对的话。
                    if planned >= max_speakers or len(queue) >= MAX_QUEUE_DEPTH:
                        dropped_by = "speakers" if planned >= max_speakers else "queue"
                        for rest in picked[index:]:
                            other = by_name.get(rest)
                            if other is None or other.id in spoken or other.id in queue:
                                continue
                            if other.id == group.admin_bot_id and round_row.wrap_up_done:
                                continue
                            if rest not in dropped:
                                dropped.append(rest)
                        break
                    if target.id == group.admin_bot_id:
                        round_row.wrap_up_done = True
                    queue.append(target.id)
                    planned += 1
                round_row.queue = queue
                if dropped:
                    reason = (
                        f"本轮发言上限是 {max_speakers} 次（主理人的开场也算在内）"
                        if dropped_by == "speakers"
                        else f"这一轮的待发言队列最多 {MAX_QUEUE_DEPTH} 位"
                    )
                    session.add(
                        TeamMessage(
                            group_id=group.id,
                            round_id=round_row.id,
                            role="system",
                            bot_name="系统",
                            content=(
                                f"{reason}，"
                                + "、".join(f"@{name}" for name in dropped)
                                + " 这次没排上，需要时请下一轮再点名。"
                            ),
                            status="sent",
                        )
                    )
            elif message.status == "pass":
                meta["mentions"] = []

            message.meta = meta
            await index_team_message(session, message, group)
            await session.commit()
            return "ok"

    async def resume_message(self, message_id: int, kind: str, decision: dict) -> str:
        """批准/驳回/回答之后接着跑。kind 为 approval 或 answer。"""
        factory = get_session_factory()
        key = f"{message_id}:{kind}"
        async with self._lock:
            if key in self._resuming:
                raise ApiError(code="team_resuming", message="这一条正在恢复，请稍候", status_code=409)
            self._resuming.add(key)
        try:
            async with factory() as session:
                message = await session.get(TeamMessage, message_id)
                if message is None or not message.round_id:
                    raise ApiError(code="team_message_not_found", message="群消息不存在", status_code=404)
                round_row = await session.get(TeamRound, message.round_id)
                if round_row is None:
                    raise ApiError(code="team_round_not_found", message="协作轮次不存在", status_code=404)
                if message.status != "waiting_approval":
                    raise ApiError(
                        code="team_message_not_waiting",
                        message="这条消息当前不在等待审批/回答状态",
                        status_code=409,
                    )
                task = await session.get(Task, message.task_id) if message.task_id else None
                if task is None or task.status != "waiting_approval":
                    raise ApiError(
                        code="team_task_not_waiting",
                        message="对应的任务已经不在等待输入状态",
                        status_code=409,
                    )
                ctx = await self._load_context(session, round_row.group_id, message.bot_id or "")
                if ctx is None:
                    raise ApiError(
                        code="team_member_unavailable",
                        message="发言的成员已停用或已被移出群聊",
                        status_code=409,
                    )
                request = self._request(
                    ctx,
                    task,
                    task.goal,
                    [f"你正在以 {ctx.bot.name} 的身份参与群聊「{ctx.group.name}」。"],
                    run_id=f"team-{round_row.id}-{message.id}-resume",
                    # 放行续跑花的钱属于被放行那一轮，不属于"另一轮"：run_id 变了，发起轮没变。
                    origin_run_id=round_origin(round_row.id),
                )
                round_row.status = "running"
                await session.commit()
                task_id = task.id
                round_id = round_row.id
                bot_id = message.bot_id or ""

            recorder = EventRecorder(
                factory, task_id, run_id=request.run_id, origin_run_id=request.origin_run_id
            )
            if kind == "approval":
                await recorder.add(
                    "approval.decided",
                    "execute_action",
                    {
                        "approve": bool(decision.get("approve")),
                        "scope": str(decision.get("scope") or "once"),
                        "tools": [],
                    },
                )
            else:
                await recorder.add(
                    "question.decided",
                    "execute_action",
                    {"answers": decision.get("answers") or {}},
                )

            try:
                evidence = await AgentRuntime(factory).resume(request, decision=decision)
            except Exception:
                async with factory() as session:
                    round_row = await session.get(TeamRound, round_id)
                    if round_row is not None and round_row.status == "running":
                        round_row.status = "waiting_approval"
                        await session.commit()
                raise

            outcome = await self._finalize_turn(round_id, message_id, bot_id, evidence)
            if outcome == "ok":
                await self.schedule(round_id)
            return outcome
        finally:
            async with self._lock:
                self._resuming.discard(key)


# --------------------------------------------------------------------------- #
# 读模型：群列表、群状态、待审批
# --------------------------------------------------------------------------- #


async def bot_payload(session: AsyncSession, bot: TeamBot) -> dict:
    provider = await session.get(ModelProvider, bot.provider_id) if bot.provider_id else None
    workspace = await session.get(Workspace, bot.workspace_id) if bot.workspace_id else None
    return {
        "id": bot.id,
        "name": bot.name,
        "role": bot.role,
        "instructions": bot.instructions,
        "provider_id": bot.provider_id or "",
        "provider_name": provider.name if provider is not None else "",
        "model": provider.default_model if provider is not None else "",
        "workspace_id": bot.workspace_id or "",
        "workspace_label": workspace.label if workspace is not None else "",
        "workspace_path": mask_path(workspace.path) if workspace is not None else "",
        "permission_mode": bot.permission_mode or MODE_DEFAULT,
        "max_steps": int(bot.max_steps or 8),
        "is_admin": bool(bot.is_admin),
        "enabled": bool(bot.enabled),
        "created_at": _iso(bot.created_at),
        "updated_at": _iso(bot.updated_at),
    }


async def list_bots(session: AsyncSession) -> list[dict]:
    admin = await ensure_admin(session)
    rows = (
        await session.execute(select(TeamBot).order_by(TeamBot.is_admin.desc(), TeamBot.created_at))
    ).scalars().all()
    if admin not in rows:
        rows = [admin, *rows]
    return [await bot_payload(session, bot) for bot in rows]


async def group_summary(session: AsyncSession, group: TeamGroup) -> dict:
    member_count = int(
        (
            await session.execute(
                select(func.count(TeamMember.id)).where(TeamMember.group_id == group.id)
            )
        ).scalar_one()
        or 0
    )
    last = (
        await session.execute(
            select(TeamMessage)
            .where(TeamMessage.group_id == group.id, TeamMessage.role.in_(("user", "bot")))
            .order_by(TeamMessage.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    round_row = await latest_round(session, group.id)
    preview = ""
    if last is not None:
        preview = _normalize(last.content)
        if len(preview) > 80:
            preview = preview[:79].rstrip() + "…"
    return {
        "id": group.id,
        "project_id": group.project_id,
        "name": group.name,
        "topic": group.topic,
        "admin_bot_id": group.admin_bot_id or "",
        "max_speakers": int(group.max_speakers or 5),
        "token_budget": int(group.token_budget or 0),
        "sandbox_tier": str(group.sandbox_tier or ""),
        "member_count": member_count,
        "last_message": preview,
        "round_status": round_row.status if round_row is not None else "",
        "created_at": _iso(group.created_at),
        "updated_at": _iso(group.updated_at),
    }


async def list_groups(session: AsyncSession) -> list[dict]:
    rows = (
        await session.execute(select(TeamGroup).order_by(TeamGroup.updated_at.desc(), TeamGroup.created_at.desc()))
    ).scalars().all()
    return [await group_summary(session, group) for group in rows]


async def _messages_payload(rows: list[TeamMessage]) -> list[dict]:
    return [
        {
            "id": row.id,
            "group_id": row.group_id,
            "round_id": row.round_id or "",
            "role": row.role,
            "bot_id": row.bot_id or "",
            "bot_name": row.bot_name,
            "content": row.content,
            "status": row.status,
            "task_id": row.task_id or "",
            "meta": row.meta or {},
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]


async def _round_ledger_usage(session: AsyncSession, round_id: str) -> dict:
    """按发起轮问一句「这一轮几个人合起来花了多少」，钱全部从账本读。

    原来这一句问不出：一轮里每个发言成员各有自己的 task 与 run_id，`run_id` 是每人一个的键，
    而轮只有一个；能给的只有 `TeamRound.tokens`——那是编排器在发言收尾时自己累加的数，
    中途失败的发言钱花了却不计进去。
    每个成员是谁，靠本轮发言行对上 task_id；对不上的一律归到"未对应发言"那一档，
    这样 `by_bot` 加起来永远等于 `tokens`，不会出现"合计对不上却没地方摆"的差额。
    """
    origin = round_origin(round_id)
    speaking = (
        await session.execute(select(TeamMessage).where(TeamMessage.round_id == round_id))
    ).scalars().all()
    bot_by_task = {row.task_id: (row.bot_id or "", row.bot_name) for row in speaking if row.task_id}
    ledger = (
        await session.execute(
            select(
                ModelUsage.task_id,
                ModelUsage.run_id,
                ModelUsage.node,
                ModelUsage.prompt_tokens,
                ModelUsage.completion_tokens,
            ).where(ModelUsage.origin_run_id == origin)
        )
    ).all()
    per_bot: dict[str, dict] = {}
    for task_id, run_id, node, prompt_tokens, completion_tokens in ledger:
        bot_id, bot_name = bot_by_task.get(task_id or "", ("", "未对应发言"))
        entry = per_bot.setdefault(
            bot_id or f"__{bot_name}",
            {"bot_id": bot_id, "bot_name": bot_name, "tokens": 0, "calls": 0, "subagent_calls": 0},
        )
        tokens = int(prompt_tokens or 0) + int(completion_tokens or 0)
        entry["tokens"] += tokens
        entry["calls"] += 1
        if str(node or "") == "subagent":
            entry["subagent_calls"] += 1
    by_bot = sorted(per_bot.values(), key=lambda item: (-item["tokens"], item["bot_name"]))
    return {
        "origin_run_id": origin,
        "tokens": sum(int(p or 0) + int(c or 0) for _t, _r, _n, p, c in ledger),
        "calls": len(ledger),
        "tasks": len({row[0] for row in ledger if row[0]}),
        "runs": len({row[1] for row in ledger if row[1]}),
        "subagent_calls": sum(1 for row in ledger if str(row[2] or "") == "subagent"),
        "by_bot": by_bot,
    }


def _chunked(items: list[str], size: int = 500):
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def _group_ledger_usage(session: AsyncSession, group_id: str) -> dict:
    """全群累计：账本行只要挂在这个群的发言任务上、或发起轮属于这个群的某一轮，就计入。

    旧写法是 `TeamMessage JOIN ModelUsage ON task_id`：inner join 只认"有发言行"的钱。
    一笔发起轮在本群、task_id 却没挂到任何消息上的账（比如中途失败后没写成消息的那次调用）
    会被静默丢掉，而 `usage_by_bot` 又没有"未对应发言"档可以摆它。
    这里两个来源取并集、按 usage.id 去重；对不上发言的归"未对应发言"，
    于是 `Σ by_bot == tokens` 是构造出来的，不是巧合。
    """
    messages = (
        await session.execute(
            select(TeamMessage.task_id, TeamMessage.bot_id, TeamMessage.bot_name).where(
                TeamMessage.group_id == group_id, TeamMessage.task_id.isnot(None)
            )
        )
    ).all()
    bot_by_task = {
        task_id: (bot_id or "", bot_name) for task_id, bot_id, bot_name in messages
    }
    round_ids = (
        await session.execute(select(TeamRound.id).where(TeamRound.group_id == group_id))
    ).scalars().all()
    origins = [round_origin(round_id) for round_id in round_ids]

    columns = (
        ModelUsage.id,
        ModelUsage.task_id,
        ModelUsage.run_id,
        ModelUsage.node,
        ModelUsage.prompt_tokens,
        ModelUsage.completion_tokens,
    )
    # 同一笔账可能两个来源都命中（正常的发言账就是这样），按 id 去重，不许加两次。
    found: dict[str, tuple] = {}

    async def collect(condition) -> None:
        rows = (await session.execute(select(*columns).where(condition))).all()
        for row in rows:
            found.setdefault(row[0], row[1:])

    for chunk in _chunked(list(bot_by_task)):
        await collect(ModelUsage.task_id.in_(chunk))
    for chunk in _chunked(origins):
        await collect(ModelUsage.origin_run_id.in_(chunk))

    per_bot: dict[str, dict] = {}
    for task_id, _run_id, node, prompt_tokens, completion_tokens in found.values():
        bot_id, bot_name = bot_by_task.get(task_id or "", ("", "未对应发言"))
        entry = per_bot.setdefault(
            bot_id or f"__{bot_name}",
            {"bot_id": bot_id, "bot_name": bot_name, "tokens": 0, "calls": 0, "subagent_calls": 0},
        )
        entry["tokens"] += int(prompt_tokens or 0) + int(completion_tokens or 0)
        entry["calls"] += 1
        if str(node or "") == "subagent":
            entry["subagent_calls"] += 1
    by_bot = sorted(per_bot.values(), key=lambda item: (-item["tokens"], item["bot_name"]))
    ledger = list(found.values())
    return {
        "tokens": sum(int(p or 0) + int(c or 0) for _t, _r, _n, p, c in ledger),
        "calls": len(ledger),
        "tasks": len({row[0] for row in ledger if row[0]}),
        "runs": len({row[1] for row in ledger if row[1]}),
        "subagent_calls": sum(1 for row in ledger if str(row[2] or "") == "subagent"),
        "by_bot": by_bot,
    }


async def group_state(group_id: str, after_id: int = 0, around_id: int = 0) -> dict:
    """一次轮询要的全部东西：群、成员、当前轮、增量消息、待审批与用量。"""
    factory = get_session_factory()
    async with factory() as session:
        group = await load_group_or_404(session, group_id)
        member_rows = await _member_rows(session, group_id)
        members: list[dict] = []
        for member, bot in member_rows:
            provider = await session.get(ModelProvider, bot.provider_id) if bot.provider_id else None
            workspace = await session.get(Workspace, bot.workspace_id) if bot.workspace_id else None
            members.append(
                {
                    "id": member.id,
                    "bot_id": bot.id,
                    "name": bot.name,
                    "role": bot.role,
                    "position": member.position,
                    "enabled": bool(member.enabled and bot.enabled),
                    "thread_id": member.thread_id or "",
                    "provider_id": bot.provider_id or "",
                    "provider_name": provider.name if provider is not None else "",
                    "model": provider.default_model if provider is not None else "",
                    "workspace_id": bot.workspace_id or "",
                    "workspace_label": workspace.label if workspace is not None else "",
                    "permission_mode": bot.permission_mode or MODE_DEFAULT,
                    "max_steps": int(bot.max_steps or 8),
                    "is_admin": bool(bot.is_admin),
                }
            )

        round_row = await active_round(session, group_id) or await latest_round(session, group_id)
        round_payload = None
        if round_row is not None:
            round_payload = {
                "id": round_row.id,
                "group_id": round_row.group_id,
                "user_message_id": round_row.user_message_id or 0,
                "status": round_row.status,
                "queue": list(round_row.queue or []),
                "spoken": list(round_row.spoken or []),
                "tokens": int(round_row.tokens or 0),
                "wrap_up_done": bool(round_row.wrap_up_done),
                "error": round_row.error,
                "created_at": _iso(round_row.created_at),
                "updated_at": _iso(round_row.updated_at),
                # 账本口径的本轮合计：按发起轮（origin_run_id）筛出来的，不经过编排器的累加器。
                # 与上面那个 `tokens` 是两笔账——那个只在发言正常收尾时加，这个连中途失败的
                # 那次发言也算。两个数各说各的口径，不许互相冒充。
                "usage": await _round_ledger_usage(session, round_row.id),
            }

        if after_id > 0:
            rows = (
                await session.execute(
                    select(TeamMessage)
                    .where(TeamMessage.group_id == group_id, TeamMessage.id > after_id)
                    .order_by(TeamMessage.id)
                    .limit(500)
                )
            ).scalars().all()
        elif around_id > 0:
            # 搜索结果可能早于"最近 120 条"。从命中消息向两侧各取一屏，
            # 保证点开旧结果时那条消息真的在返回集里，而不是跳到群尾。
            anchor = await session.get(TeamMessage, around_id)
            if anchor is None or anchor.group_id != group_id:
                rows = list(
                    reversed(
                        (
                            await session.execute(
                                select(TeamMessage)
                                .where(TeamMessage.group_id == group_id)
                                .order_by(TeamMessage.id.desc())
                                .limit(120)
                            )
                        ).scalars().all()
                    )
                )
            else:
                before = (
                    await session.execute(
                        select(TeamMessage)
                        .where(
                            TeamMessage.group_id == group_id,
                            TeamMessage.id <= around_id,
                        )
                        .order_by(TeamMessage.id.desc())
                        .limit(60)
                    )
                ).scalars().all()
                after = (
                    await session.execute(
                        select(TeamMessage)
                        .where(
                            TeamMessage.group_id == group_id,
                            TeamMessage.id > around_id,
                        )
                        .order_by(TeamMessage.id)
                        .limit(60)
                    )
                ).scalars().all()
                rows = sorted([*before, *after], key=lambda row: row.id)
        else:
            rows = list(
                reversed(
                    (
                        await session.execute(
                            select(TeamMessage)
                            .where(TeamMessage.group_id == group_id)
                            .order_by(TeamMessage.id.desc())
                            .limit(120)
                        )
                    ).scalars().all()
                )
            )
        messages = await _messages_payload(rows)
        waiting = [row for row in rows if row.status == "waiting_approval" and row.task_id]

        # 全群累计（跨所有轮）：两个来源取并集（挂在本群发言任务上 / 发起轮属于本群某一轮），
        # 再按 usage.id 去重。问"这一轮"要用 round.usage——单轮群里两者相等，
        # 所以拿单轮群验不出这里的口径；两个数各说各的，不许互相冒充。
        usage = await _group_ledger_usage(session, group_id)
        summary = await group_summary(session, group)

    pending: list[dict] = []
    for row in waiting:
        try:
            evidence = await build_evidence(factory, row.task_id or "")
        except Exception:
            evidence = {}
        approvals = list(evidence.get("pending_approvals") or [])
        questions = list(evidence.get("pending_questions") or [])
        if not approvals and not questions:
            continue
        pending.append(
            {
                "message_id": row.id,
                "task_id": row.task_id or "",
                "bot_id": row.bot_id or "",
                "bot_name": row.bot_name,
                "approvals": approvals,
                "questions": questions,
                "question_expires_at": evidence.get("question_expires_at") or "",
                "question_expired": bool(evidence.get("question_expired")),
            }
        )

    return {
        "group": summary,
        "members": members,
        "round": round_payload,
        "messages": messages,
        "pending": pending,
        "usage": usage,
        "usage_by_bot": usage["by_bot"],
    }


async def request_stop(session: AsyncSession, group: TeamGroup) -> TeamRound | None:
    round_row = await active_round(session, group.id)
    if round_row is None:
        return None
    rows = (
        await session.execute(
            select(TeamMessage).where(
                TeamMessage.round_id == round_row.id,
                TeamMessage.status.in_(("running", "waiting_approval")),
            )
        )
    ).scalars().all()
    task_ids = [row.task_id for row in rows if row.task_id]
    for row in rows:
        if row.status == "waiting_approval":
            row.status = "cancelled"
            row.content = row.content or "已停止"
            await delete_doc(session, "team_message", str(row.id))
    if task_ids:
        tasks = (
            await session.execute(select(Task).where(Task.id.in_(task_ids)))
        ).scalars().all()
        for task in tasks:
            if task.status in ("queued", "running", "waiting_approval", "draft"):
                task.status = "cancelled"
                task.failure_reason = "用户停止了团队讨论"
    # 直接置 cancelled 而不是中间态：当前正在跑的那次模型调用会在收尾时看到这个状态
    # 并丢弃结果；若进程恰好此刻退出，也不会留下一个永远等不到推进的 cancelling。
    round_row.status = "cancelled"
    await session.commit()
    for task_id in task_ids:
        await EventRecorder(get_session_factory(), task_id).add(
            "task.cancel_requested", "receive_task", {"status": "cancelled", "source": "team.stop"}
        )
    return round_row


async def recover_interrupted_rounds() -> int:
    """服务重启后收掉上一进程没走完的轮次。

    编排器活在进程内存里，重启后没人会继续推同一个队列；留在 running 会让这个群
    永远「上一轮还没说完」。等待审批的轮次不动：那条路径靠用户点按钮恢复，
    和进程是否重启无关。
    """
    factory = get_session_factory()
    async with factory() as session:
        rows = (
            await session.execute(
                select(TeamRound).where(TeamRound.status.in_(("running", "cancelling")))
            )
        ).scalars().all()
        for round_row in rows:
            round_row.status = "failed"
            round_row.error = "服务重启，本轮讨论已中断，请重新发送消息"
            messages = (
                await session.execute(
                    select(TeamMessage).where(
                        TeamMessage.round_id == round_row.id,
                        TeamMessage.status == "running",
                    )
                )
            ).scalars().all()
            for message in messages:
                message.status = "failed"
                message.content = "服务重启，这条发言被中断"
                if message.task_id:
                    task = await session.get(Task, message.task_id)
                    if task is not None and task.status in ("draft", "queued", "running"):
                        task.status = "failed"
                        task.failure_reason = "服务重启，团队讨论已中断"
            session.add(
                TeamMessage(
                    group_id=round_row.group_id,
                    round_id=round_row.id,
                    role="system",
                    bot_name="系统",
                    content="服务重启，本轮讨论已中断。请重新发送消息。",
                    status="failed",
                )
            )
        await session.commit()
        return len(rows)


def valid_permission_mode(value: str | None) -> str:
    mode = str(value or MODE_DEFAULT)
    return mode if mode in MODES else MODE_DEFAULT
