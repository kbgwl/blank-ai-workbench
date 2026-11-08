"""钉钉 Stream 长连接：让「收消息」不需要公网地址。

为什么单独一个模块：它不是一个请求/响应，而是一条长期活的连接 + 重连 + ACK。
塞进适配器会让 ``check()`` 和 ``send()`` 也被迫关心连接状态，而那两件事跟连接无关。

协议要点（钉钉官方 Stream 模式）：

1. ``POST /v1.0/gateway/connections/open`` 用 Client ID/Secret 换 ``endpoint + ticket``。
2. 连 ``ws://endpoint?ticket=…``，服务端推 JSON 帧：``{code, headers:{topic, messageId}, messageId, data}``。
3. 每帧都要回 ACK，``headers.messageId`` 与帧里的 ``messageId`` 一致；不回就一直重推。
4. ``topic == ping`` 是保活，要回 ``topic == pong`` 并把 data 原样带回。

不做的事：不解析通讯录、不订非机器人 topic。多订的每一类都意味着更大的授权面，
而"我只想让它看这个群"是客户配完之后最自然的要求。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import quote

from blank_api.db import get_session_factory
from blank_api.models.channel import RECEIVE_OFF, RECEIVE_STREAM, Channel
from blank_api.services.channels import adapter_for
from blank_api.services.channels.base import as_client
from blank_api.services.channels.store import read_config

log = logging.getLogger(__name__)

TOPIC_PING = "ping"
TOPIC_PONG = "pong"
RECONNECT_MIN = 3.0
RECONNECT_MAX = 60.0
# 一轮连接最长活 10 分钟就主动换：钉钉侧的连接有最大存活，被动断开时
# 那一段时间的消息会重推，但主动换能让"重连逻辑"平时也被走过一遍。
MAX_SESSION_SECONDS = 600.0


def build_ack(message_id: str, *, topic: str = "", data: str = "{}") -> str:
    """ACK 帧。钉钉只认这个形状，字段少一个就当没收到。"""
    headers: dict[str, Any] = {
        "contentType": "application/json",
        "messageId": message_id,
    }
    if topic:
        headers["topic"] = topic
    return json.dumps(
        {"code": 200, "headers": headers, "messageId": message_id, "data": data},
        ensure_ascii=False,
    )


def parse_frame(raw: str) -> tuple[str, str, str]:
    """返回 (topic, message_id, data 原文)。坏帧不抛，交给调用方跳过。"""
    try:
        frame = json.loads(raw)
    except ValueError:
        return "", "", ""
    if not isinstance(frame, dict):
        return "", "", ""
    headers = frame.get("headers") if isinstance(frame.get("headers"), dict) else {}
    return (
        str(headers.get("topic") or ""),
        str(frame.get("messageId") or headers.get("messageId") or ""),
        str(frame.get("data") or ""),
    )


async def open_connection(config: dict) -> tuple[str, str]:
    """换 endpoint + ticket。失败时的报错要能直接照着改。"""
    adapter = adapter_for("dingtalk")
    if adapter is None:  # pragma: no cover - 注册表里一定有
        raise RuntimeError("钉钉适配器未注册")
    client_id = str(config.get("client_id") or "").strip()
    secret = str(config.get("client_secret") or "").strip()
    if not client_id or not secret:
        raise RuntimeError("缺少 Client ID 或 Client Secret")
    payload = {
        "clientId": client_id,
        "clientSecret": secret,
        "subscriptions": [{"type": "CALLBACK", "topic": "/v1.0/im/bot/messages/get"}],
        "ua": "blank-ai-workbench",
        "localIp": "127.0.0.1",
    }
    http, owned = as_client(None)
    try:
        response = await http.post("https://api.dingtalk.com/v1.0/gateway/connections/open", json=payload)
        body = response.json()
    finally:
        if owned:
            await http.aclose()
    endpoint = str((body or {}).get("endpoint") or "")
    ticket = str((body or {}).get("ticket") or "")
    if not endpoint or not ticket:
        raise RuntimeError(f"打开长连接失败：{(body or {}).get('message') or body}")
    return endpoint, ticket


async def handle_frame(channel: Channel, config: dict, raw: str, *, client: Any = None) -> bool:
    """处理一帧。返回 True 表示这一帧有消息被接住（测试与日志用）。"""
    from blank_api.services.channels.dingtalk import DingTalkAdapter
    from blank_api.services.channels.inbound import accept

    topic, message_id, data = parse_frame(raw)
    if topic == TOPIC_PING:
        return False
    if topic != "/v1.0/im/bot/messages/get":
        # 其它 topic 只 ACK 不处理：订都没订，出现就是配置漂了，记一笔即可。
        if topic:
            log.info("钉钉推来未订阅的 topic=%s channel=%s", topic, channel.id)
        return False
    try:
        body = json.loads(data) if data else {}
    except ValueError:
        return False
    adapter = DingTalkAdapter()
    message = adapter.parse_stream(body if isinstance(body, dict) else {})
    if message is None:
        return False
    result = await accept(channel.id, message)
    log.info("钉钉入站 channel=%s msg=%s → %s", channel.id, message.external_id, result.get("reason") or result.get("task_id"))
    return bool(result.get("accepted"))


async def run_channel(channel_id: str) -> None:
    """一条通道的长连接主循环。被取消即退出（通道被停用/进程退出）。"""
    import websockets

    backoff = RECONNECT_MIN
    while True:
        async with get_session_factory()() as db:
            channel = await db.get(Channel, channel_id)
        if channel is None or channel.receive_mode != RECEIVE_STREAM:
            return
        config = read_config(channel)
        if not config:
            log.warning("钉钉通道 %s 配置为空，长连接不启动", channel_id)
            return
        try:
            endpoint, ticket = await open_connection(config)
            url = f"{endpoint}{'&' if '?' in endpoint else '?'}ticket={quote(ticket, safe='')}"
            async with websockets.connect(url, open_timeout=15, ping_interval=20) as socket:
                backoff = RECONNECT_MIN
                log.info("钉钉长连接已建立 channel=%s", channel_id)
                deadline = asyncio.get_running_loop().time() + MAX_SESSION_SECONDS
                while asyncio.get_running_loop().time() < deadline:
                    try:
                        raw = await asyncio.wait_for(socket.recv(), timeout=MAX_SESSION_SECONDS)
                    except asyncio.TimeoutError:
                        break
                    topic, message_id, data = parse_frame(str(raw))
                    if topic == TOPIC_PING:
                        await socket.send(build_ack(message_id, topic=TOPIC_PONG, data=data))
                        continue
                    await socket.send(build_ack(message_id))
                    try:
                        await handle_frame(channel, config, str(raw))
                    except Exception:  # noqa: BLE001 - 单条消息解析失败不该断连
                        log.warning("钉钉入站处理失败 channel=%s", channel_id, exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 断网/被顶号都要重连
            log.warning("钉钉长连接断开 channel=%s：%s（%ss 后重连）", channel_id, error, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)


class StreamSupervisor:
    """按库里的配置起停长连接。每拍只做差量，不重建已有连接。"""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    async def sync(self) -> dict[str, int]:
        async with get_session_factory()() as db:
            from sqlalchemy import select

            rows = (
                await db.execute(
                    select(Channel).where(Channel.receive_mode == RECEIVE_STREAM, Channel.receive.is_(True))
                )
            ).scalars().all()
        wanted = {row.id for row in rows if row.kind == "dingtalk"}
        for channel_id in list(self._tasks):
            if channel_id not in wanted:
                self._tasks.pop(channel_id).cancel()
        for channel_id in wanted:
            task = self._tasks.get(channel_id)
            if task is None or task.done():
                self._tasks[channel_id] = asyncio.create_task(
                    run_channel(channel_id), name=f"blank-stream-{channel_id[:8]}"
                )
        return {"running": len(self._tasks)}

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()


__all__ = [
    "StreamSupervisor",
    "build_ack",
    "handle_frame",
    "open_connection",
    "parse_frame",
    "run_channel",
    "RECEIVE_OFF",
]
