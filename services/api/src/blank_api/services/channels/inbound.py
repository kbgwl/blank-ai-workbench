"""入站：把「客户在 IM 里说的一句话」变成一轮任务。

走的是既有链路，不新开执行路径：

    IM 消息 → 触发器（im_message，fire_at=现在）→ release_due_triggers → launch_task

为什么绕一次触发器而不是直接 ``launch_task``：客户会看到「这条消息到底有没有被
接住」。触发器行是那条证据——它记着来源、外部消息 id、绑定到哪个任务。直接起跑
之后，消息丢了、重复了、被拦了都无从查起。

幂等也钉在这一层：同一条外部消息 id 在有效期内只建一个任务。厂商的重试很常见
（钉钉 Stream 没 ACK 就重推、飞书 3 秒没响应就重投），不去重就是"一句话跑三遍"。
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.channel import Channel
from blank_api.models.project import Project
from blank_api.models.thread import Thread
from blank_api.services.agent import triggers
from blank_api.services.channels.base import Inbound

log = logging.getLogger(__name__)

# 去重窗口。厂商的重投都在秒级到分钟级，一天足够；再长会让"客户真的又发了一遍
# 同一条消息"被当成重复吞掉。
DEDUPE_WINDOW = timedelta(hours=24)
# 站内任务标题长度。全文进 goal，标题只做列表展示用。
TITLE_CHARS = 60


async def find_project(db, channel: Channel) -> Project | None:
    """消息落到哪个项目：通道上指定的那个，否则第一个项目。"""
    if channel.project_id:
        project = await db.get(Project, channel.project_id)
        if project is not None:
            return project
    return (await db.execute(select(Project).order_by(Project.created_at).limit(1))).scalar_one_or_none()


async def ensure_thread(db, channel: Channel, project: Project) -> Thread:
    """每一条通道一条固定会话：聊天要接着上文。

    每句话新开一条会话会让模型每次都失忆——客户在群里说"再改一下"，
    它却不知道改什么，而这会被归成"模型不行"。
    """
    if channel.thread_id:
        thread = await db.get(Thread, channel.thread_id)
        if thread is not None:
            return thread
    thread = Thread(
        id=str(uuid.uuid4()),
        project_id=project.id,
        title=f"{channel.label if hasattr(channel, 'label') else channel.name} 对话",
        status="active",
        kind="chat",
        turn_count=0,
    )
    db.add(thread)
    await db.flush()
    channel.thread_id = thread.id
    return thread


def reply_hint(channel: Channel, message: Inbound) -> dict:
    """回话要用的东西。原样存进触发器，任务跑完照着它把结论送回去。"""
    return {
        "channel_id": channel.id,
        "kind": channel.kind,
        # 企业微信回给发送者；飞书回到 chat_id；钉钉优先用会话 Webhook。
        "target": message.conversation if channel.kind == "feishu" else message.sender,
        "session_webhook": str(message.raw.get("session_webhook") or ""),
        "receive_id_type": "chat_id" if channel.kind == "feishu" else "",
        "conversation_name": message.conversation_name,
    }


async def accept(channel_id: str, message: Inbound, *, release: bool = True) -> dict:
    """收下一条消息，开一轮任务。返回给调用方做日志/回执用的摘要。"""
    text = str(message.text or "").strip()
    if not text:
        return {"accepted": False, "reason": "空消息"}

    from blank_api.api.routes.tasks import persist_task
    from blank_api.schemas.task import TaskCreate

    async with get_session_factory()() as db:
        channel = await db.get(Channel, channel_id)
        if channel is None:
            return {"accepted": False, "reason": "通道不存在"}
        if not channel.receive:
            return {"accepted": False, "reason": "这条通道没有开启收消息"}
        project = await find_project(db, channel)
        if project is None:
            return {"accepted": False, "reason": "还没有项目，消息无处可落"}

        dedupe = f"im:{channel.id}:{message.external_id}"
        existing = await triggers.find_active(db, project.id, dedupe)
        if existing is not None:
            # 重投/重试：既不再建任务，也不当失败报错——它本来就已经被接住了。
            return {
                "accepted": True,
                "duplicate": True,
                "task_id": existing.task_id,
                "trigger_id": existing.id,
                "reason": "同一条消息已经开过任务",
            }

        thread = await ensure_thread(db, channel, project)
        title = text[:TITLE_CHARS] or "IM 消息"
        task = await persist_task(
            db,
            project.id,
            TaskCreate(title=title, goal=text),
            thread_id=thread.id,
            turn_index=int(thread.turn_count or 0) + 1,
        )
        thread.turn_count = int(thread.turn_count or 0) + 1
        # 监督者的判定段由 persist_task 之外的链路写；IM 这条没有那一段，
        # 于是这里补一个最小快照，让监控页的「谁判成了什么」不至于整行空白。
        snapshot = dict(task.dispatch_snapshot or {})
        snapshot.setdefault("source", "im")
        snapshot.setdefault("channel_id", channel.id)
        snapshot.setdefault("channel_kind", channel.kind)
        task.dispatch_snapshot = snapshot

        event = triggers.normalize(
            event_type=triggers.EVENT_IM_MESSAGE,
            source=f"{channel.kind}:{channel.name}"[:120],
            payload={
                "goal": text[:400],
                "channel_id": channel.id,
                "channel_kind": channel.kind,
                "sender": message.sender,
                "sender_name": message.sender_name,
                "conversation": message.conversation,
                "external_id": message.external_id,
                "reply": reply_hint(channel, message),
            },
            dedupe_key=dedupe,
            expires_at=triggers.utc_now() + DEDUPE_WINDOW,
        )
        row, created = await triggers.ingest(
            db,
            project.id,
            event,
            task_id=task.id,
            run_payload={},
            # 到点=现在：IM 里的问句不该排队等下一拍巡检。
            fire_at=triggers.utc_now(),
        )
        channel.last_inbound_at = triggers.utc_now()
        channel.last_inbound_error = ""
        await db.commit()
        trigger_id = row.id
        channel_id = channel.id
        task_id = task.id

    if release:
        # 立刻入队一次，不等 30 秒巡检。这里**刻意不看 background_execution**：
        # 那个开关管的是"后台自己发起的事"，而这是一个人刚发来的一句话——
        # 关着就不动，等于客户配好通道发消息永远没反应。
        # 入队动作仍然复用 worker.release_one，执行路径只有一条。
        try:
            from blank_api.worker import release_one

            await release_one(trigger_id, start_runner=True)
        except Exception as error:  # noqa: BLE001 - 入队失败下一拍巡检会补上
            log.warning("IM 入站立即放行失败，交给后台巡检", exc_info=True)
            await _report_launch_failure(channel_id, task_id, str(error))

    return {
        "accepted": True,
        "duplicate": not created,
        "task_id": task.id,
        "trigger_id": trigger_id,
        "project_id": project.id,
    }


def note_error(channel_id: str, message: str) -> None:
    """入站解析失败也要留痕：客户问"我发了怎么没反应"时，这是唯一答案。"""
    log.warning("通道 %s 入站失败：%s", channel_id, message)


async def _report_launch_failure(channel_id: str, task_id: str, reason: str) -> None:
    """入队失败时把原因写到通道上，并尽量回一句话给发消息的人。

    最常见的成因是"项目还没绑定工作区"——这件事在聊天软件那边完全看不出来，
    不说明白就只会得到"发了没反应"。
    """
    from blank_api.models.channel import Channel
    from blank_api.services.channels.delivery import reply_to_source

    try:
        async with get_session_factory()() as db:
            channel = await db.get(Channel, channel_id)
            if channel is not None:
                channel.last_inbound_error = reason[:500]
                await db.commit()
    except Exception:  # noqa: BLE001
        log.warning("记录入站失败原因失败 channel=%s", channel_id, exc_info=True)
    try:
        await reply_to_source(task_id, f"这条消息没能开始处理：{reason[:300]}")
    except Exception:  # noqa: BLE001 - 回话失败不能再抛，否则连号都没回
        log.warning("入站失败回话失败 task=%s", task_id, exc_info=True)


__all__ = ["DEDUPE_WINDOW", "accept", "ensure_thread", "find_project", "note_error", "reply_hint"]
