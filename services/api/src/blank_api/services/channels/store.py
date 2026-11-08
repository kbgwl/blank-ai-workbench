"""通道配置的存取：加密、掩码、合并、必填校验。

三条规则来自"客户配错了要能自己看出来"：

1. **密钥只回显掩码。** 前端拿到 ``****abcd`` 再原样提交回来时，按"没改"处理——
   否则客户点一次保存就会把 Secret 存成那串星号，而症状是"昨天还好好的"。
2. **必填按模式算。** 「群机器人」模式不该被要求填 AgentId；反过来填了也不该顶掉。
3. **保存失败要说清缺哪个。** 一条 ``缺少：Client Secret`` 比 ``参数错误`` 有用得多。

不在这里做网络验证：保存配置和验证凭据是两件事，混在一起会让"我先把字段填完
再一起验"变成不可能，而且每次保存都打一次厂商接口，离线时连存都存不下去。
"""

from __future__ import annotations

import json
import secrets

from blank_api.models.channel import (
    CHANNEL_MODES,
    Channel,
    MODE_APP,
    RECEIVE_MODES,
    RECEIVE_OFF,
)
from blank_api.services.channels import adapter_for
from blank_api.services.crypto import ApiError, decrypt_secret, encrypt_secret

MASK_PREFIX = "****"


def new_callback_token() -> str:
    """回调地址里的不可猜段。URL 安全的 32 字节，够挡住扫目录的。"""
    return secrets.token_urlsafe(24)


def mask(value: str) -> str:
    if not value:
        return ""
    tail = value[-4:] if len(value) > 4 else ""
    return f"{MASK_PREFIX}{tail}"


def looks_masked(value: str) -> bool:
    return str(value or "").startswith(MASK_PREFIX)


def read_config(channel: Channel) -> dict:
    """解密配置。解不开时返回空 dict——上层据此报「密钥已变更，请重新填写」。"""
    if not channel.config_encrypted:
        return {}
    try:
        raw = decrypt_secret(channel.config_encrypted)
    except ApiError:
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def write_config(channel: Channel, config: dict) -> None:
    channel.config_encrypted = encrypt_secret(json.dumps(config, ensure_ascii=False)) if config else ""


def merge_config(channel: Channel, patch: dict, mode: str) -> dict:
    """把这次提交的字段合并进已有配置。

    掩码/空串的密钥字段一律保留原值：前端根本拿不到原文，把它当"要清空"会让
    每次保存都把密钥抹掉。真要清空得显式传 ``clear_secrets``（接口层处理）。
    """
    current = read_config(channel)
    adapter = adapter_for(channel.kind)
    secrets_keys = {f.key for f in (adapter.fields if adapter else ()) if f.secret}
    merged = dict(current)
    for key, value in (patch or {}).items():
        text = "" if value is None else str(value)
        if key in secrets_keys and (not text or looks_masked(text)):
            continue
        merged[key] = text
    return merged


def missing_required(kind: str, mode: str, config: dict) -> list[str]:
    adapter = adapter_for(kind)
    if adapter is None:
        return []
    missing: list[str] = []
    for field in adapter.fields:
        if not field.required:
            continue
        if field.only_modes and mode not in field.only_modes:
            continue
        if not str(config.get(field.key) or "").strip():
            missing.append(field.label)
    return missing


def apply_mode(channel: Channel, mode: str) -> None:
    """换模式时把入站方式一起纠正：群机器人收不到消息，不能留着 webhook。"""
    channel.mode = mode if mode in CHANNEL_MODES else MODE_APP
    adapter = adapter_for(channel.kind)
    allowed = adapter.receive_modes(channel.mode) if adapter else ()
    if channel.receive_mode not in allowed:
        channel.receive_mode = allowed[0] if allowed else RECEIVE_OFF


def normalize_receive_mode(channel: Channel, value: str) -> str:
    adapter = adapter_for(channel.kind)
    allowed = adapter.receive_modes(channel.mode) if adapter else ()
    text = str(value or "").strip()
    if text in RECEIVE_MODES and text in allowed:
        return text
    return RECEIVE_OFF


def config_view(channel: Channel) -> dict:
    """给界面的配置：密钥打码，其余原样。"""
    adapter = adapter_for(channel.kind)
    config = read_config(channel)
    out: dict = {}
    for field in (adapter.fields if adapter else ()):
        value = str(config.get(field.key) or "")
        out[field.key] = mask(value) if field.secret else value
    # 未声明的键不回显：客户旧版本留下的字段不该悄悄流回界面。
    return out


def receipt(channel: Channel, public_base: str) -> dict:
    """回调地址与"还要做什么"。空公网地址时明确说收不到消息。"""
    adapter = adapter_for(channel.kind)
    modes = adapter.receive_modes(channel.mode) if adapter else ()
    if not modes:
        return {"mode": RECEIVE_OFF, "url": "", "note": "这个模式只发不收，不需要回调地址"}
    if "webhook" in modes:
        if not public_base:
            return {
                "mode": "webhook",
                "url": "",
                "note": "收消息需要公网可达的地址。本机没有公网地址，回调填不进去——要么在前面挂反向代理/内网穿透，要么改用钉钉 Stream。",
            }
        base = public_base.rstrip("/")
        return {
            "mode": "webhook",
            "url": f"{base}/api/v1/channels/{channel.id}/callback/{channel.callback_token}",
            "note": "把这个地址填到厂商后台的回调/事件订阅里，先保存再点「验证凭据」",
        }
    return {
        "mode": "stream",
        "url": "",
        "note": "长连接由本机主动连出去，不需要回调地址；保存后在厂商后台把接收模式选成 Stream",
    }


__all__ = [
    "MASK_PREFIX",
    "apply_mode",
    "config_view",
    "looks_masked",
    "mask",
    "merge_config",
    "missing_required",
    "new_callback_token",
    "normalize_receive_mode",
    "read_config",
    "receipt",
    "write_config",
]
