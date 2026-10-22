"""聊天索引的读写：群消息、会话结论、跨群检索。"""

from __future__ import annotations

from datetime import timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from blank_api.models.search import SearchDoc
from blank_api.models.task import Task
from blank_api.models.team import TeamGroup, TeamMessage
from blank_api.models.thread import Thread
from blank_api.services.text_index import score as text_score
from blank_api.services.text_index import snippet, tokenize

# 候选用 LikE 粗筛后最多取这么多行精确打分。聊天索引是"最近说了什么"的检索，
# 不是全库分析；把候选集限住，查询时间就不会随历史线性上涨。
CANDIDATE_LIMIT = 800
MAX_RESULTS = 50
INDEXABLE_MESSAGE_STATUSES = ("sent", "succeeded", "paused", "failed", "waiting_approval")


def _blob(tokens: list[str]) -> str:
    return " " + " ".join(tokens) + " " if tokens else ""


def _iso(value) -> str:
    if value is None:
        return ""
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return aware.isoformat()


async def _upsert(
    session: AsyncSession,
    *,
    source: str,
    source_id: str,
    **fields,
) -> SearchDoc:
    row = (
        await session.execute(
            select(SearchDoc).where(
                SearchDoc.source == source, SearchDoc.source_id == source_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = SearchDoc(source=source, source_id=source_id, **fields)
        row.tokens = _blob(tokenize(row.content))
        session.add(row)
    else:
        for key, value in fields.items():
            setattr(row, key, value)
        row.tokens = _blob(tokenize(row.content))
    await session.flush()
    return row


async def index_team_message(
    session: AsyncSession, message: TeamMessage, group: TeamGroup | None
) -> SearchDoc | None:
    content = " ".join(str(message.content or "").split())
    if not content or message.status == "running" or message.status == "cancelled":
        return None
    if message.role == "bot" and message.status == "pass":
        return None
    if message.status not in INDEXABLE_MESSAGE_STATUSES and message.role != "user":
        return None
    group_name = group.name if group is not None else ""
    speaker = "用户" if message.role == "user" else (message.bot_name or "系统")
    return await _upsert(
        session,
        source="team_message",
        source_id=str(message.id),
        project_id=group.project_id if group is not None else "",
        group_id=message.group_id,
        thread_id=None,
        task_id=message.task_id,
        message_id=message.id,
        title=group_name,
        speaker=speaker,
        role=message.role,
        content=content[:8_000],
    )


async def index_task_summary(
    session: AsyncSession, task: Task, thread: Thread | None, *, title: str = ""
) -> SearchDoc | None:
    content = " ".join(str(task.result_summary or "").split())
    if not content:
        return None
    return await _upsert(
        session,
        source="task",
        source_id=task.id,
        project_id=task.project_id,
        group_id=None,
        thread_id=task.thread_id,
        task_id=task.id,
        message_id=None,
        title=(title or task.title or "")[:200],
        speaker="助手",
        role="assistant",
        content=content[:8_000],
    )


async def delete_doc(session: AsyncSession, source: str, source_id: str) -> None:
    row = (
        await session.execute(
            select(SearchDoc).where(
                SearchDoc.source == source, SearchDoc.source_id == source_id
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        await session.delete(row)


async def delete_group_docs(session: AsyncSession, group_id: str) -> None:
    rows = (
        await session.execute(select(SearchDoc).where(SearchDoc.group_id == group_id))
    ).scalars().all()
    for row in rows:
        await session.delete(row)


def _like_escape(token: str) -> str:
    return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_chat(
    session: AsyncSession,
    query: str,
    *,
    project_id: str = "",
    group_id: str = "",
    limit: int = 20,
) -> dict:
    tokens = tokenize(query)
    if not tokens:
        return {"query": query, "tokens": [], "results": [], "total": 0, "truncated": False}
    # 单字 token 命中面太宽，只拿它们兜底；有双字/英文词时优先用这些做候选。
    strong = [token for token in tokens if len(token) >= 2] or tokens
    conditions = [
        SearchDoc.tokens.like(f"% {_like_escape(token)} %", escape="\\")
        for token in strong
    ]
    statement = select(SearchDoc).where(or_(*conditions))
    if group_id:
        statement = statement.where(SearchDoc.group_id == group_id)
    if project_id:
        statement = statement.where(SearchDoc.project_id == project_id)
    rows = (
        (await session.execute(statement.order_by(SearchDoc.id.desc()).limit(CANDIDATE_LIMIT)))
        .scalars()
        .all()
    )

    scored: list[tuple[int, SearchDoc]] = []
    for row in rows:
        value = text_score(row.content, tokens) + text_score(row.title, tokens) // 2
        if value:
            scored.append((value, row))
    scored.sort(key=lambda item: (item[0], item[1].id), reverse=True)
    capped = max(1, min(MAX_RESULTS, int(limit)))
    results = [
        {
            "id": row.id,
            "source": row.source,
            "group_id": row.group_id or "",
            "group_name": row.title,
            "thread_id": row.thread_id or "",
            "task_id": row.task_id or "",
            "message_id": row.message_id or 0,
            "speaker": row.speaker,
            "role": row.role,
            "snippet": snippet(row.content, tokens, width=70),
            "content": row.content[:1_200],
            "score": value,
            "created_at": _iso(row.created_at),
        }
        for value, row in scored[:capped]
    ]
    return {
        "query": query,
        "tokens": tokens,
        "results": results,
        "total": len(scored),
        "truncated": len(rows) >= CANDIDATE_LIMIT,
    }


async def backfill(limit: int = 2_000) -> int:
    """给升级前已有的消息补索引。进程启动时跑一次，失败不影响服务。"""
    from blank_api.db import get_session_factory

    factory = get_session_factory()
    added = 0
    async with factory() as session:
        known = set(
            (
                await session.execute(select(SearchDoc.source, SearchDoc.source_id))
            ).all()
        )
        keys = {(source, source_id) for source, source_id in known}
        messages = (
            await session.execute(
                select(TeamMessage, TeamGroup)
                .join(TeamGroup, TeamGroup.id == TeamMessage.group_id)
                .order_by(TeamMessage.id.desc())
                .limit(limit)
            )
        ).all()
        for message, group in messages:
            if ("team_message", str(message.id)) in keys:
                continue
            row = await index_team_message(session, message, group)
            if row is not None:
                added += 1
        tasks = (
            await session.execute(
                select(Task, Thread)
                .join(Thread, Thread.id == Task.thread_id)
                .where(Task.status == "succeeded", Thread.kind != "team")
                .order_by(Task.created_at.desc())
                .limit(limit)
            )
        ).all()
        for task, thread in tasks:
            if ("task", task.id) in keys:
                continue
            row = await index_task_summary(session, task, thread)
            if row is not None:
                added += 1
        if added:
            await session.commit()
    return added
