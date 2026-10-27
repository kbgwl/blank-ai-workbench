"""会话：把「轮」串起来，让上一轮说过的话在这一轮还算数。

三条边界：

1. **只带结论，不带全过程。** 一轮里可能有几十条消息、几百个 token 的工具输出，
   全塞进下一轮就是花两倍的钱买一段模型早就不需要的中间过程。带的是
   "这一轮要了什么、最后交付了什么"——对话需要的正是这个。
2. **越近的轮次越优先。** 上下文装不下时先丢最远的：模型真正需要的是刚才那句话，
   而不是十轮之前的开场白。
3. **分叉只继承上下文，不复制任务。** 复制任务等于复制它的全部事件与审计记录，
   而用户要的只是"带上刚才聊过的内容另开一条线"。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from blank_api.models.task import Task
from blank_api.models.thread import Thread

# 默认带最近 6 轮：再多也塞不进"开场几句话"的信息量，却要按轮数线性吃 token。
HISTORY_TURNS = 6
# 整段历史的上限。单条过长时按字数截断，而不是整轮丢弃——截断的损失远小于
# "上一轮做了什么"整个消失。
HISTORY_MAX_CHARS = 6_000
# 分叉链最多回溯 3 层：分叉的分叉的分叉再往上，上下文已经和最初那条没什么关系了。
FORK_DEPTH = 3


async def thread_or_404(session: AsyncSession, thread_id: str) -> Thread:
    thread = await session.get(Thread, thread_id)
    if thread is None:
        from blank_api.api.errors import ApiError

        raise ApiError(code="thread_not_found", message="会话不存在", status_code=404)
    return thread


async def thread_turns(session: AsyncSession, thread_id: str) -> list[Task]:
    """一条会话还在对话里的轮次，按轮次序号正序。

    排序不看 ``created_at``：SQLite 的 ``CURRENT_TIMESTAMP`` 只到秒，同一秒里连开
    两轮时它分不出先后，而顺序决定了历史里哪一句在前——排错了，模型读到的对话就是乱的。
    ``turn_index`` 为 0 的是没有走会话接口的老数据，退回按时间与 id 排。

    被回退掉的轮次（``rewound_at`` 非空）不在此列：它们还在库里供审计，但
    用户已经明确表示"从这里往后都不算数"，再带进下一轮的上下文就是没听懂。
    """
    return list(
        (
            await session.execute(
                select(Task)
                .where(Task.thread_id == thread_id, Task.rewound_at.is_(None))
                .order_by(Task.turn_index, Task.created_at, Task.id)
            )
        )
        .scalars()
        .all()
    )


# 这三种状态的任务可能还在写文件。回退期间放任它们继续跑，等它们结束时会把
# 改动写进一个"对话里已经不存在"的轮次，用户看到的是一份无主的改动。
ACTIVE_TASK_STATUSES = ("queued", "running", "waiting_approval")


async def rewind_to_turn(session: AsyncSession, thread: Thread, turn: int) -> tuple[Task, list[Task]]:
    """把第 ``turn`` 轮之后的轮次标记为已回退，会话计数退回该轮。

    只动「还在对话里」的行：之前回退过的行已经带着原始回退时间，再标一次会把它改掉。
    代码回滚不在这里做——那一步需要工作区路径与快照存储，由路由层拼装并单独汇报，
    因为它可能做不到（没有快照），而对话截断必须照常完成。
    """
    from blank_api.api.errors import ApiError

    turns = await thread_turns(session, thread.id)
    target = next((item for item in turns if int(item.turn_index or 0) == turn), None)
    if target is None:
        raise ApiError(code="turn_not_found", message=f"该会话没有第 {turn} 轮", status_code=404)
    running = [item for item in turns if item.status in ACTIVE_TASK_STATUSES]
    if running:
        raise ApiError(
            code="task_active",
            message="还有轮次在执行，等它结束后才能回退",
            status_code=409,
        )
    doomed = [item for item in turns if int(item.turn_index or 0) > turn]
    stamp = datetime.utcnow()
    for item in doomed:
        item.rewound_at = stamp
    thread.turn_count = turn
    await session.commit()
    return target, doomed


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _turn_messages(turn: Task, number: int) -> list[dict[str, str]]:
    """``number`` 是这一轮在自己那条会话里的序号。

    分叉链上会拼好几条会话的轮次，所以不能直接用列表下标——那会让分叉后的第一轮
    显示成"第 3 轮"，而它其实是新会话的第 1 轮。
    """
    goal = _clip(turn.goal or turn.title, HISTORY_MAX_CHARS // 3)
    reply = str(turn.result_summary or "").strip()
    if not reply and turn.failure_reason:
        # 失败也要带：下一轮如果不知道上一轮为什么失败，多半会照着同样的方式失败一次。
        reply = f"（这一轮没有完成：{_clip(turn.failure_reason, 400)}）"
    if not reply:
        reply = "（这一轮没有产出总结）"
    return [
        {"role": "user", "content": f"第 {number} 轮：{goal}{_attachment_note(turn)}"},
        {"role": "assistant", "content": _clip(reply, HISTORY_MAX_CHARS // 2)},
    ]


def _attachment_note(turn: Task) -> str:
    """这一轮带过什么附件，压成一句话附在目标后面。

    附件本身不重发（图要占几万 token，且用户问的往往只是"接着说"），但"它当时
    给过图"这个事实必须留下。否则下一轮的"刚才那张图是什么颜色"在模型看来是一个
    凭空出现的问题，它只能瞎猜或者否认——两种都像 bug。
    """
    items = [item for item in (turn.attachments or []) if isinstance(item, dict)]
    if not items:
        return ""

    def _kind(item: dict) -> str:
        mime = str(item.get("mime") or "")
        if mime.startswith("image/"):
            return "image"
        if mime == "application/pdf":
            return "pdf"
        return "text"

    images = sum(1 for item in items if _kind(item) == "image")
    others = [str(item.get("name") or "文件") for item in items if _kind(item) != "image"]
    parts: list[str] = []
    if images:
        parts.append(f"附图 {images} 张")
    if others:
        head = "、".join(others[:3])
        more = f" 等 {len(others)} 份" if len(others) > 3 else ""
        parts.append(f"附文件 {head}{more}")
    return f"（这一轮用户{'、'.join(parts)}，内容未随历史重发）"


async def _own_turns(
    session: AsyncSession, thread_id: str, *, exclude_task_id: str | None
) -> list[Task]:
    turns = await thread_turns(session, thread_id)
    if exclude_task_id:
        turns = [turn for turn in turns if turn.id != exclude_task_id]
    return turns


async def history_for(
    session_factory: async_sessionmaker[AsyncSession],
    thread_id: str | None,
    *,
    exclude_task_id: str | None = None,
    turns: int = HISTORY_TURNS,
    max_chars: int = HISTORY_MAX_CHARS,
) -> list[dict[str, str]]:
    """把一条会话（及其分叉来源）的最近几轮压成消息对。

    返回的形状与 ``initial_messages`` 直接拼接：``[user, assistant, user, assistant, ...]``。
    空会话、或者只有当前这一轮时返回 ``[]``——此时调用方拼出来的提示词
    与"没有会话"时完全一致。
    """
    if not thread_id:
        return []

    async with session_factory() as session:
        collected: list[Task] = []
        cursor: str | None = thread_id
        seen: set[str] = set()
        for _ in range(FORK_DEPTH):
            if not cursor or cursor in seen:
                break
            seen.add(cursor)
            # 当前这条会话自己的轮次算完了才往上找：分叉之后新说的话优先，
            # 老会话的内容只是背景。
            own = await _own_turns(session, cursor, exclude_task_id=exclude_task_id)
            collected = own + collected
            thread = await session.get(Thread, cursor)
            cursor = thread.forked_from if thread is not None else None

    if not collected:
        return []

    # 从最近往前挑，装不下就停在能装下的那一轮；至少保留最后一轮（截断而不是丢弃）。
    picked: list[list[dict[str, str]]] = []
    spent = 0
    for turn in reversed(collected):
        messages = _turn_messages(turn, int(turn.turn_index or 0) or 1)
        size = sum(len(item["content"]) for item in messages)
        if picked and (len(picked) >= turns or spent + size > max_chars):
            break
        picked.append(messages)
        spent += size
    picked.reverse()
    return [message for pair in picked for message in pair]


def summarize_history(messages: list[dict[str, str]]) -> dict[str, Any]:
    """给事件流用的一句话摘要。带了多少轮、多少字，一眼能看出来。"""
    return {
        "turns": len(messages) // 2,
        "chars": sum(len(item.get("content") or "") for item in messages),
    }
