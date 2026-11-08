"""把「要不要打扰」变成「真的发到客户手机上」，以及「把结论回到原会话」。

与 ``agent/notify.py`` 的分工不变：那边只判断该不该出声，这边只负责投递。
所以本文件里没有任何策略判断——策略算错了是那边的问题，发送失败了是这边的问题，
混在一起之后排查只能靠猜。

两条投递路径：

* ``fanout`` —— 任务提醒（等审批/失败/交付）推到所有开了「推送」的通道。
* ``reply_to_source`` —— IM 里问的那句话，结论回到**原会话**。它不看 notify 策略：
  用户在聊天里问了一句，答案就该出现在聊天里，那不是"打扰"。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy import select

from blank_api.config import get_settings
from blank_api.db import get_session_factory
from blank_api.models.channel import Channel
from blank_api.models.trigger import AgentTrigger
from blank_api.services.agent import notify
from blank_api.services.channels import adapter_for
from blank_api.services.channels.store import read_config

log = logging.getLogger(__name__)

# IM 单条消息的长度上限各家不同（飞书 30KB、钉钉 20KB、企业微信 2048 字节）。
# 取 1200 字是最小公约数，也是"手机上不用点开就能看完"的长度。
MAX_REPLY_CHARS = 1200

_TITLES = {
    notify.EVENT_APPROVAL: "等你批准",
    notify.EVENT_FAILURE: "任务失败",
    notify.EVENT_DELIVERABLE: "任务完成",
    notify.EVENT_MILESTONE: "阶段进展",
    notify.EVENT_PROGRESS: "进度",
}


def clip(text: str, limit: int = MAX_REPLY_CHARS) -> str:
    body = " ".join(str(text or "").split())
    if len(body) <= limit:
        return body
    return body[: limit - 1] + "…"


def notification_text(*, title: str, goal: str, kind: str, detail: str = "") -> str:
    """提醒正文。第一行就是结论，客户在通知栏里就能读完。"""
    head = _TITLES.get(kind, "任务有进展")
    lines = [f"【{head}】{clip(title or goal, 60)}"]
    if goal and goal != title:
        lines.append(clip(goal, 160))
    if detail:
        lines.append(clip(detail, 500))
    return "\n".join(lines)


def reply_text(*, status: str, summary: str = "", failure: str = "", task_id: str = "") -> str:
    """把任务结果回成一句话。空结果不装成功——如实说"跑完了但没话讲"。"""
    if status in ("failed", "cancelled"):
        body = clip(failure or "任务没有跑完，原因没记下来", 900)
        return f"没做成：{body}"
    if status == "waiting_approval":
        short = task_id[:8] if task_id else ""
        return f"卡在一个需要你批准的操作上{f'（任务 {short}）' if short else ''}，在工作台里点一下就行。"
    text = clip(summary, 900) or "跑完了，但没有产出文字结论。"
    return text


async def _channels(db) -> list[Channel]:
    rows = (
        await db.execute(select(Channel).where(Channel.push.is_(True)).order_by(Channel.created_at))
    ).scalars().all()
    return list(rows)


async def send_to_channel(
    channel: Channel, text: str, *, client: httpx.AsyncClient | None = None
) -> dict:
    """往一条通道发一条消息。异常一律收敛成 ok=False：发送是旁路。"""
    adapter = adapter_for(channel.kind)
    if adapter is None:
        return {"channel_id": channel.id, "name": channel.name, "ok": False, "detail": "未知通道类型"}
    config = read_config(channel)
    if not config:
        return {
            "channel_id": channel.id,
            "name": channel.name,
            "ok": False,
            "detail": "配置为空或解不开（主密钥可能变过，请重新填写）",
        }
    try:
        result = await adapter.send(config, channel.mode, text, client)
    except Exception as error:  # noqa: BLE001 - 单条通道坏了不影响其余通道
        log.warning("通道发送异常 channel=%s: %s", channel.id, error)
        return {"channel_id": channel.id, "name": channel.name, "ok": False, "detail": str(error)}
    return {"channel_id": channel.id, "name": channel.name, **result.as_dict()}


def enabled() -> bool:
    """总闸。半夜被推醒时先拉这个，比逐条关通道快，也不会漏掉新加的通道。"""
    return bool(get_settings().channels_enabled)


async def fanout(text: str, *, client: httpx.AsyncClient | None = None) -> list[dict]:
    """推到所有开了推送的通道。返回每一条的真实结果。"""
    if not enabled():
        return []
    async with get_session_factory()() as db:
        rows = await _channels(db)
    if not rows:
        return []
    results: list[dict] = []
    for channel in rows:
        results.append(await send_to_channel(channel, text, client=client))
    return results


async def reply_to_source(
    task_id: str, text: str, *, client: httpx.AsyncClient | None = None
) -> dict | None:
    """把结论回到这条任务进来的那个会话。不是 IM 来的任务返回 None。

    找的是**触发器行**而不是任务字段：入站那一刻的"从哪来"就是那条 trigger 的
    payload，它是唯一没有被后续步骤改写过的记录。
    """
    if not task_id or not enabled():
        return None
    async with get_session_factory()() as db:
        row = (
            await db.execute(
                select(AgentTrigger)
                .where(
                    AgentTrigger.task_id == task_id,
                    AgentTrigger.event_type == "im_message",
                )
                .order_by(AgentTrigger.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        payload = dict(row.payload or {})
        reply = dict(payload.get("reply") or {})
        channel = await db.get(Channel, str(reply.get("channel_id") or ""))
        if channel is None:
            return None
        return await _deliver_reply(channel, reply, text, client=client)


async def _deliver_reply(
    channel: Channel, reply: dict, text: str, *, client: httpx.AsyncClient | None
) -> dict:
    adapter = adapter_for(channel.kind)
    config = read_config(channel)
    target = str(reply.get("target") or "")
    session_webhook = str(reply.get("session_webhook") or "")
    try:
        # 钉钉的会话 Webhook 是最省事的一条：钉钉自己给的、1.5 小时内有效，
        # 不需要额外权限。过期了就走应用消息兜底。
        if channel.kind == "dingtalk" and session_webhook:
            from blank_api.services.channels.dingtalk import reply_via_session_webhook

            result = await reply_via_session_webhook(session_webhook, text, client)
            if result.ok:
                return {"channel_id": channel.id, "name": channel.name, **result.as_dict()}
        if adapter is None or not config:
            return {"channel_id": channel.id, "name": channel.name, "ok": False, "detail": "通道配置不可用"}
        narrowed: dict[str, Any] = dict(config)
        if channel.kind == "wecom" and target:
            narrowed["to_user"] = target
        if channel.kind == "feishu" and target:
            narrowed["receive_id"] = target
            narrowed["receive_id_type"] = str(reply.get("receive_id_type") or "chat_id")
        result = await adapter.send(narrowed, channel.mode, text, client)
    except Exception as error:  # noqa: BLE001 - 回话失败不该影响任务终态
        log.warning("回话失败 channel=%s task 已结束: %s", channel.id, error)
        return {"channel_id": channel.id, "name": channel.name, "ok": False, "detail": str(error)}
    return {"channel_id": channel.id, "name": channel.name, **result.as_dict()}


__all__ = [
    "MAX_REPLY_CHARS",
    "clip",
    "enabled",
    "fanout",
    "notification_text",
    "reply_text",
    "reply_to_source",
    "send_to_channel",
]
