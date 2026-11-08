"""消息通道接口：目录、配置、验证、测试发送、厂商回调。

一条边界值得写在这：**callback 两条是不鉴权的**。厂商不可能带我们的 Bearer 令牌，
所以那两条靠另外两把锁——URL 里的 ``callback_token``（不可猜）与厂商自己的签名
（企业微信 msg_signature / 飞书 Verification Token）。少任何一把，公网上的任何人都能
让 Agent 开任务，而任务会真的去改文件。

其余接口一律走 ``require_token``，与全站口径一致。
"""

from __future__ import annotations

import hmac
import logging
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from blank_api.api.auth import require_token
from blank_api.api.errors import ApiError
from blank_api.config import get_settings
from blank_api.deps import DbSession
from blank_api.models.channel import CHANNEL_MODES, Channel, MODE_APP, RECEIVE_OFF
from blank_api.services.agent import triggers as triggers_module
from blank_api.services.channels import adapter_for, catalog
from blank_api.services.channels import store
from blank_api.services.features import is_enabled

log = logging.getLogger(__name__)

router = APIRouter(tags=["channels"], dependencies=[Depends(require_token)])
# 厂商回调：不鉴权（见文件头）。挂载时与 router 同前缀，但依赖表里没有 require_token。
public_router = APIRouter(tags=["channels"])

TEST_TEXT = "【空白格】这是一条连通性测试消息。收到即说明这条通道配置可用。"


class ChannelCreate(BaseModel):
    kind: str
    name: str = Field(default="", max_length=80)
    mode: str = MODE_APP
    config: dict = Field(default_factory=dict)
    push: bool = True
    receive: bool = True
    receive_mode: str = ""
    project_id: str = ""


class ChannelPatch(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    mode: str | None = None
    config: dict | None = None
    push: bool | None = None
    receive: bool | None = None
    receive_mode: str | None = None
    project_id: str | None = None
    clear_secrets: bool = False


def _public_base() -> str:
    return str(get_settings().public_base_url or "").strip()


def _view(channel: Channel) -> dict:
    adapter = adapter_for(channel.kind)
    return {
        "id": channel.id,
        "kind": channel.kind,
        "kind_label": adapter.label if adapter else channel.kind,
        "name": channel.name,
        "mode": channel.mode,
        "status": channel.status,
        "status_note": channel.status_note,
        "checked_at": channel.checked_at.isoformat() if channel.checked_at else "",
        "push": channel.push,
        "receive": channel.receive,
        "receive_mode": channel.receive_mode,
        "project_id": channel.project_id,
        "thread_id": channel.thread_id,
        # 回调地址的最后一段。它不是"又一个密钥"：界面上本来就把它完整显示给
        # 客户去填厂商后台，藏起来只会让"复制地址"必须走后端拼。
        "callback_token": channel.callback_token,
        "config": store.config_view(channel),
        "receipt": store.receipt(channel, _public_base()),
        "last_inbound_at": channel.last_inbound_at.isoformat() if channel.last_inbound_at else "",
        "last_inbound_error": channel.last_inbound_error,
        "capabilities": {
            "push": bool(adapter.supports(channel.mode, "push")) if adapter else False,
            "receive": adapter.receive_modes(channel.mode) if adapter else (),
        },
    }


async def _get(db, channel_id: str) -> Channel:
    channel = await db.get(Channel, channel_id)
    if channel is None:
        raise ApiError(code="channel_not_found", message="这条通道不存在", status_code=404)
    return channel


def _check_mode(channel: Channel, mode: str) -> str:
    adapter = adapter_for(channel.kind)
    if adapter is None:
        raise ApiError(code="channel_kind_unknown", message="不支持的通道类型", status_code=400)
    if mode not in adapter.modes:
        raise ApiError(code="channel_mode_unknown", message=f"{adapter.label} 不支持这个接法", status_code=400)
    return mode


def _apply_config(channel: Channel, patch: dict, *, clear: bool = False) -> None:
    """合并配置并校验必填。缺哪个字段直接点名，不返回"参数错误"。"""
    config = {} if clear else store.merge_config(channel, patch, channel.mode)
    missing = store.missing_required(channel.kind, channel.mode, config)
    if missing:
        raise ApiError(
            code="channel_config_incomplete",
            message=f"还差这些没填：{'、'.join(missing)}",
            status_code=400,
        )
    store.write_config(channel, config)


@router.get("/channels/catalog")
async def channels_catalog() -> dict:
    """客户能配哪些通道、每种要填什么。表单完全由这份声明生成。"""
    return {
        "adapters": catalog(),
        "public_base": _public_base(),
        # 收消息的两种接法差别很大，界面上要直说，否则客户会以为"配好了就能收"。
        "receive_notes": [
            {
                "mode": "stream",
                "label": "长连接（推荐）",
                "note": "本机主动连出去，不需要公网地址。目前钉钉支持。",
            },
            {
                "mode": "webhook",
                "label": "回调地址",
                "note": "需要公网可达的 https 地址。企业微信、飞书走这条；本机形态要先挂反向代理或内网穿透。",
            },
        ],
    }


@router.get("/channels")
async def list_channels(db: DbSession) -> dict:
    rows = (await db.execute(select(Channel).order_by(Channel.created_at))).scalars().all()
    return {
        "channels": [_view(row) for row in rows],
        "public_base": _public_base(),
        "triggers_enabled": is_enabled("background_execution"),
        "im_events": triggers_module.EVENT_IM_MESSAGE in triggers_module.EVENT_TYPES,
    }


@router.post("/channels")
async def create_channel(payload: ChannelCreate, db: DbSession) -> dict:
    adapter = adapter_for(payload.kind)
    if adapter is None:
        raise ApiError(code="channel_kind_unknown", message="不支持的通道类型", status_code=400)
    mode = payload.mode if payload.mode in CHANNEL_MODES else MODE_APP
    if mode not in adapter.modes:
        raise ApiError(code="channel_mode_unknown", message=f"{adapter.label} 不支持这个接法", status_code=400)
    name = (payload.name or "").strip() or adapter.label
    exists = (
        await db.execute(select(Channel).where(Channel.kind == payload.kind, Channel.name == name))
    ).scalar_one_or_none()
    if exists is not None:
        raise ApiError(code="channel_name_taken", message=f"已经有同名通道「{name}」", status_code=409)

    channel = Channel(
        id=str(uuid.uuid4()),
        kind=payload.kind,
        name=name,
        mode=mode,
        push=payload.push,
        receive=payload.receive,
        receive_mode=RECEIVE_OFF,
        callback_token=store.new_callback_token(),
        project_id=payload.project_id or "",
    )
    store.apply_mode(channel, mode)
    _apply_config(channel, payload.config)
    channel.receive_mode = store.normalize_receive_mode(
        channel, payload.receive_mode or channel.receive_mode
    )
    # 入站开着但这条通道收不到消息时，不要留一个"看起来在收"的开关。
    if channel.receive and channel.receive_mode == RECEIVE_OFF:
        channel.receive = False
    db.add(channel)
    await db.commit()
    await db.refresh(channel)
    return {"channel": _view(channel)}


@router.patch("/channels/{channel_id}")
async def patch_channel(channel_id: str, payload: ChannelPatch, db: DbSession) -> dict:
    channel = await _get(db, channel_id)
    if payload.name is not None:
        name = payload.name.strip()
        if name and name != channel.name:
            clash = (
                await db.execute(
                    select(Channel).where(
                        Channel.kind == channel.kind, Channel.name == name, Channel.id != channel.id
                    )
                )
            ).scalar_one_or_none()
            if clash is not None:
                raise ApiError(code="channel_name_taken", message=f"已经有同名通道「{name}」", status_code=409)
            channel.name = name
    if payload.mode is not None and payload.mode != channel.mode:
        _check_mode(channel, payload.mode)
        store.apply_mode(channel, payload.mode)
        # 换模式等于换了整套字段：旧模式的必填项对新模式没意义，
        # 所以这里不重算必填，等用户把新字段填完再存。
    if payload.config is not None:
        _apply_config(channel, payload.config, clear=payload.clear_secrets)
    if payload.push is not None:
        channel.push = payload.push
    if payload.receive is not None:
        channel.receive = payload.receive
    if payload.receive_mode is not None:
        channel.receive_mode = store.normalize_receive_mode(channel, payload.receive_mode)
    if payload.project_id is not None:
        channel.project_id = payload.project_id
        # 换项目等于换了工作区：旧会话挂在旧项目下，继续用会让 IM 的消息落错地方。
        channel.thread_id = ""
    if channel.receive and channel.receive_mode == RECEIVE_OFF:
        channel.receive = False
    # 配置变了，上次的验证结论就不成立了。留着 ok 比"未验证"更误导。
    if payload.config is not None or payload.mode is not None:
        channel.status = "unverified"
        channel.status_note = "配置已修改，请重新验证"
    await db.commit()
    await db.refresh(channel)
    return {"channel": _view(channel)}


@router.delete("/channels/{channel_id}", status_code=204)
async def delete_channel(channel_id: str, db: DbSession) -> Response:
    channel = await _get(db, channel_id)
    await db.delete(channel)
    await db.commit()
    return Response(status_code=204)


@router.post("/channels/{channel_id}/verify")
async def verify_channel(channel_id: str, db: DbSession) -> dict:
    """真的打一次厂商接口。凭据对不对，只有这一步能回答。"""
    channel = await _get(db, channel_id)
    adapter = adapter_for(channel.kind)
    config = store.read_config(channel)
    if adapter is None or not config:
        raise ApiError(code="channel_config_empty", message="这条通道还没有可用配置", status_code=400)
    result = await adapter.check(config, channel.mode)
    channel.checked_at = triggers_module.utc_now()
    if result.ok:
        channel.status = "ok"
        channel.status_note = result.detail
    elif result.claimed:
        channel.status = "failed"
        channel.status_note = result.detail
    else:
        channel.status = "unverified"
        channel.status_note = result.detail
    await db.commit()
    return {"check": result.as_dict(), "channel": _view(channel)}


@router.post("/channels/{channel_id}/test")
async def test_channel(channel_id: str, db: DbSession) -> dict:
    """发一条真消息。这是唯一能证明"这台机器的配置真的能用"的动作。"""
    channel = await _get(db, channel_id)
    from blank_api.services.channels.delivery import send_to_channel

    result = await send_to_channel(channel, TEST_TEXT)
    channel.checked_at = triggers_module.utc_now()
    channel.status = "ok" if result.get("ok") else "failed"
    channel.status_note = str(result.get("detail") or "")
    await db.commit()
    return {"send": result, "channel": _view(channel)}


# ── 厂商回调（匿名，靠 callback_token + 厂商签名）───────────────────────────────


async def _callback_channel(db, channel_id: str, token: str) -> Channel:
    channel = await db.get(Channel, channel_id)
    # 令牌不对时一律 404：告诉对方"这个 id 存在但令牌错"等于帮他枚举。
    if channel is None or not channel.callback_token or not hmac.compare_digest(channel.callback_token, token):
        raise ApiError(code="channel_callback_not_found", message="回调地址无效", status_code=404)
    return channel


def _vendor_ok(channel: Channel) -> Response:
    """回执。企业微信要空串（表示不作被动回复），飞书要 code=0。"""
    if channel.kind == "feishu":
        return JSONResponse({"code": 0, "msg": "ok"})
    return PlainTextResponse("")


def _vendor_error(channel: Channel, message: str) -> Response:
    # 回 4xx 厂商会在自己的后台标红并显示原因，客户才能在"配错了"的地方看见。
    if channel.kind == "feishu":
        return JSONResponse({"code": 400, "msg": message}, status_code=400)
    return PlainTextResponse(message, status_code=400)


@public_router.get("/channels/{channel_id}/callback/{token}")
async def channel_callback_verify(
    channel_id: str, token: str, request: Request, db: DbSession
) -> Response:
    channel = await _callback_channel(db, channel_id, token)
    adapter = adapter_for(channel.kind)
    if adapter is None:
        return _vendor_error(channel, "通道类型不支持")
    config = store.read_config(channel)
    query = {key: request.query_params.get(key, "") for key in request.query_params}
    try:
        challenge = await adapter.handshake(config, channel.mode, query)
    except Exception as error:  # noqa: BLE001 - 校验失败要让厂商看见原因
        return _vendor_error(channel, f"校验失败：{error}")
    if challenge is None:
        return _vendor_ok(channel)
    return PlainTextResponse(challenge)


@public_router.post("/channels/{channel_id}/callback/{token}")
async def channel_callback(
    channel_id: str, token: str, request: Request, db: DbSession
) -> Response:
    channel = await _callback_channel(db, channel_id, token)
    adapter = adapter_for(channel.kind)
    if adapter is None:
        return _vendor_error(channel, "通道类型不支持")
    if not channel.receive:
        return _vendor_ok(channel)
    body = await request.body()
    if len(body) > 512 * 1024:
        # 回调只该是几百字节到几 KB。允许大包等于把解析器暴露给公网。
        return _vendor_error(channel, "回调体过大")
    config = store.read_config(channel)
    query = {key: request.query_params.get(key, "") for key in request.query_params}
    headers = {key.lower(): value for key, value in request.headers.items()}

    # 飞书的 URL 校验是 POST 一个 challenge，与普通事件同一条路。
    handshake_body = getattr(adapter, "handshake_body", None)
    if callable(handshake_body) and channel.kind == "feishu":
        try:
            envelope = await handshake_body(config, body)
        except Exception as error:  # noqa: BLE001
            return _vendor_error(channel, f"回调校验失败：{error}")
        if envelope is not None:
            return JSONResponse(envelope)

    try:
        message = await adapter.parse_inbound(
            config, channel.mode, query=query, headers=headers, body=body
        )
    except Exception as error:  # noqa: BLE001 - 解析失败是配置问题，退回给厂商看得见
        channel.last_inbound_error = str(error)[:500]
        await db.commit()
        return _vendor_error(channel, f"回调解析失败：{error}")

    if message is None:
        # 事件类回调（进群、已读、非文本）不是一句话，不回任务但要 ACK。
        return _vendor_ok(channel)

    from blank_api.services.channels.inbound import accept

    try:
        await accept(channel.id, message)
    except Exception:  # noqa: BLE001 - 已经有消息了，再报错只会让厂商重投三遍
        log.warning("通道 %s 入站建任务失败", channel.id, exc_info=True)
    return _vendor_ok(channel)


__all__ = ["public_router", "router"]
