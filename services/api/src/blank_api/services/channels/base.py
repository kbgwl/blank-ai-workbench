"""通道适配器的公共形状：字段声明、验证、发送、入站解析。

一条规矩贯穿这个包：**能不能用，只能由一次真实调用回答**。字段填齐了不算通，
"URL 看起来是对的"也不算——``check()`` 必须打到厂商接口上，拿回对方的错误码。
群机器人没有只读接口，那就老实返回 ``claimed=False``（"没验过"），而不是假装通过。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_TIMEOUT = 8.0
# 厂商接口偶尔挂很久：8 秒是"用户点了按钮愿意等的上限"，不是网络本身的上限。
CHECK_TIMEOUT = 10.0


@dataclass(frozen=True)
class Field:
    """一个配置项。``secret`` 的项永不回显原文，只回 ``****后四位``。"""

    key: str
    label: str
    secret: bool = False
    required: bool = False
    placeholder: str = ""
    help: str = ""
    default: str = ""
    options: tuple[tuple[str, str], ...] = ()
    # 只在某些模式下显示（例如「应用」模式不需要群机器人 Webhook）。
    only_modes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "secret": self.secret,
            "required": self.required,
            "placeholder": self.placeholder,
            "help": self.help,
            "default": self.default,
            "options": [{"value": value, "label": label} for value, label in self.options],
            "only_modes": list(self.only_modes),
        }


@dataclass
class Check:
    """一次凭据验证的结果。"""

    ok: bool
    detail: str
    # 是否真的打到了厂商接口。False = 这条通道只能靠"发一条测试消息"验。
    claimed: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "detail": self.detail, "claimed": self.claimed}


@dataclass
class Send:
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "detail": self.detail}


@dataclass
class Inbound:
    """一条收到的消息，已经归一成与厂商无关的形状。"""

    external_id: str
    text: str
    sender: str = ""
    sender_name: str = ""
    conversation: str = ""
    conversation_name: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "external_id": self.external_id,
            "text": self.text,
            "sender": self.sender,
            "sender_name": self.sender_name,
            "conversation": self.conversation,
            "conversation_name": self.conversation_name,
        }


class AdapterError(RuntimeError):
    """适配器内部错误。消息直接展示给用户，所以必须是人话。"""


def as_client(client: httpx.AsyncClient | None) -> tuple[httpx.AsyncClient, bool]:
    """返回 (客户端, 是否由本函数创建)。测试注入 MockTransport 时第二个值是 False。"""
    if client is not None:
        return client, False
    return httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True), True


async def post_json(
    client: httpx.AsyncClient, url: str, payload: Any, *, headers: dict | None = None
) -> tuple[int, Any]:
    """POST JSON 并解析返回。非 JSON 的响应按原文返回，便于把厂商的 HTML 报错念出来。"""
    response = await client.post(url, json=payload, headers=headers or {})
    return response.status_code, _body(response)


async def get_json(
    client: httpx.AsyncClient, url: str, *, params: dict | None = None, headers: dict | None = None
) -> tuple[int, Any]:
    response = await client.get(url, params=params or {}, headers=headers or {})
    return response.status_code, _body(response)


def _body(response: httpx.Response) -> Any:
    text = response.text or ""
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {"_text": text[:400]}


def short(value: Any, limit: int = 240) -> str:
    """把厂商的报错压成一行能读完的话。"""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(str(text).split())
    return text[:limit] + ("…" if len(text) > limit else "")


class Adapter:
    """适配器基类。子类覆盖需要的部分，没实现的能力显式留空。"""

    kind: str = ""
    label: str = ""
    vendor: str = ""
    summary: str = ""
    docs: str = ""
    # 每个模式的支持情况：{"app": {"push": True, "receive": ("webhook",)}, ...}
    modes: dict[str, dict[str, Any]] = {}
    fields: tuple[Field, ...] = ()
    # 客户在厂商后台要点哪几步。界面原样展示——少一步就连不上，而"缺一步"很难自查。
    setup: tuple[str, ...] = ()

    def mode_meta(self, mode: str) -> dict[str, Any]:
        return dict(self.modes.get(mode) or {})

    def supports(self, mode: str, ability: str) -> bool:
        return bool(self.mode_meta(mode).get(ability))

    def receive_modes(self, mode: str) -> tuple[str, ...]:
        return tuple(self.mode_meta(mode).get("receive") or ())

    def schema(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "vendor": self.vendor,
            "summary": self.summary,
            "docs": self.docs,
            "modes": [
                {
                    "value": value,
                    "label": str(meta.get("label") or value),
                    "hint": str(meta.get("hint") or ""),
                    "push": bool(meta.get("push")),
                    "receive": list(meta.get("receive") or []),
                }
                for value, meta in self.modes.items()
            ],
            "fields": [item.as_dict() for item in self.fields],
            "setup": list(self.setup),
        }

    # ---- 子类要实现的三件事 ------------------------------------------------

    async def check(self, config: dict, mode: str, client: httpx.AsyncClient | None = None) -> Check:
        raise NotImplementedError

    async def send(self, config: dict, mode: str, text: str, client: httpx.AsyncClient | None = None) -> Send:
        raise NotImplementedError

    async def handshake(self, config: dict, mode: str, query: dict) -> str | None:
        """URL 校验（企业微信的 echostr、飞书的 url_verification）。不需要就返回 None。"""
        return None

    async def parse_inbound(
        self, config: dict, mode: str, *, query: dict, headers: dict, body: bytes
    ) -> Inbound | None:
        """把厂商回调解析成一条消息。不是消息（进群事件、已读回执）返回 None。"""
        return None

    # ---- 入站回复 ---------------------------------------------------------

    def reply_body(self, config: dict, mode: str, text: str) -> tuple[bytes, str]:
        """被动回复。默认不回（空串=厂商服务器不作处理），避免机器人对每句话都答一遍。"""
        return b"", "text/plain"


__all__ = [
    "Adapter",
    "AdapterError",
    "Check",
    "CHECK_TIMEOUT",
    "DEFAULT_TIMEOUT",
    "Field",
    "Inbound",
    "Send",
    "as_client",
    "get_json",
    "post_json",
    "short",
]
