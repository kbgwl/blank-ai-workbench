"""飞书：自建应用（收发）+ 自定义群机器人（只发）。

入站只有**事件订阅回调**这一条路（飞书的长连接协议是私有 protobuf 帧，不做猜测
实现）。所以选择飞书时界面上要直说：要么给这台机器一个公网地址，要么用钉钉。

回调有两种形态，都要处理：明文（``schema``/``header``/``event``）与加密
（``{"encrypt": "..."}``，AES-256-CBC，key = sha256(EncryptKey)、IV 取密文前 16 字节）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx

from blank_api.services.channels.base import (
    Adapter,
    Check,
    Field,
    Inbound,
    Send,
    as_client,
    get_json,
    post_json,
    short,
)

API_BASE = "https://open.feishu.cn"
_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
_BOT_PATH = "/open-apis/bot/v3/info"
_SEND_PATH = "/open-apis/im/v1/messages"
# 消息事件。只订这一个：其余（进群、撤回、已读）不是"一句话"，订了也不会开任务。
MESSAGE_EVENT = "im.message.receive_v1"

MODE_APP = "app"
MODE_ROBOT = "robot"

RECEIVE_ID_TYPES = (
    ("chat_id", "chat_id（群/会话 ID，推荐）"),
    ("open_id", "open_id（用户）"),
    ("user_id", "user_id（企业内工号）"),
    ("email", "email（邮箱）"),
)


def sign_webhook(secret: str, timestamp: int) -> str:
    """自定义机器人的加签：以 ``timestamp\\nsecret`` 为密钥对空串做 HmacSHA256。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode(), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def decrypt_event(encrypted: str, encrypt_key: str) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    raw = base64.b64decode(encrypted)
    if len(raw) <= 16:
        raise ValueError("密文过短，Encrypt Key 可能不匹配")
    key = hashlib.sha256(encrypt_key.encode()).digest()
    worker = Cipher(algorithms.AES(key), modes.CBC(raw[:16])).decryptor()
    plain = worker.update(raw[16:]) + worker.finalize()
    pad = plain[-1]
    if pad < 1 or pad > 32 or pad > len(plain):
        raise ValueError("填充非法，Encrypt Key 可能不匹配")
    return plain[:-pad].decode("utf-8", "ignore")


class FeishuAdapter(Adapter):
    kind = "feishu"
    label = "飞书"
    vendor = "字节"
    summary = "自建应用可收发，但收消息要公网可达的回调地址；群机器人只能发。"
    docs = "https://open.feishu.cn/document/server-docs/im-v1/message/events/receive"
    modes = {
        MODE_APP: {
            "label": "自建应用",
            "hint": "能主动发消息、也能收事件开任务；事件订阅需要公网可达地址",
            "push": True,
            "receive": ("webhook",),
        },
        MODE_ROBOT: {
            "label": "群机器人",
            "hint": "一个 Webhook 即可；只能发，收不到消息",
            "push": True,
            "receive": (),
        },
    }
    fields = (
        Field(
            key="app_id",
            label="App ID",
            required=True,
            placeholder="cli_axxxxxxxxxxxxxx",
            help="开放平台 → 开发者后台 → 企业自建应用 → 凭证与基础信息",
            only_modes=(MODE_APP,),
        ),
        Field(
            key="app_secret",
            label="App Secret",
            secret=True,
            required=True,
            help="同上那一页。泄漏等于应用被接管",
            only_modes=(MODE_APP,),
        ),
        Field(
            key="receive_id_type",
            label="发送目标类型",
            default="chat_id",
            options=RECEIVE_ID_TYPES,
            help="决定下面那个 ID 怎么读。群消息用 chat_id，个人用 open_id 或 email",
            only_modes=(MODE_APP,),
        ),
        Field(
            key="receive_id",
            label="发送目标 ID",
            placeholder="oc_xxxxxxxxxxxxxxxx",
            help="留空则只收不发；收消息时结论会回到原会话",
            only_modes=(MODE_APP,),
        ),
        Field(
            key="verification_token",
            label="Verification Token",
            secret=True,
            help="只在收消息时需要：事件订阅页的 Verification Token",
            only_modes=(MODE_APP,),
        ),
        Field(
            key="encrypt_key",
            label="Encrypt Key",
            secret=True,
            help="只在收消息时需要：事件订阅页的 Encrypt Key（选了加密才要）",
            only_modes=(MODE_APP,),
        ),
        Field(
            key="webhook",
            label="群机器人 Webhook",
            secret=True,
            required=True,
            placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/…",
            help="群设置 → 群机器人 → 添加 → 自定义机器人 → 复制 Webhook",
            only_modes=(MODE_ROBOT,),
        ),
        Field(
            key="sign_secret",
            label="签名校验密钥",
            secret=True,
            help="安全设置勾了「签名校验」就必须填，否则报 19021",
            only_modes=(MODE_ROBOT,),
        ),
    )
    setup = (
        "开放平台 → 开发者后台 → 创建企业自建应用，记下 App ID 与 App Secret",
        "应用能力里添加「机器人」，并申请 im:message:send_as_bot 与 im:message 权限",
        "要收消息：事件订阅 → 请求地址填本页给的回调地址 → 添加事件 im.message.receive_v1",
        "发布应用并等待管理员审核通过，只有可见范围内的人发消息才会推过来",
        "群机器人更快：任意群 → 群机器人 → 添加 → 自定义机器人 → 复制 Webhook",
    )

    async def token(self, config: dict, client: httpx.AsyncClient | None = None) -> tuple[str, str]:
        app_id = str(config.get("app_id") or "").strip()
        secret = str(config.get("app_secret") or "").strip()
        if not app_id or not secret:
            return "", "缺少 App ID 或 App Secret"
        http, owned = as_client(client)
        try:
            status, body = await post_json(
                http, API_BASE + _TOKEN_PATH, {"app_id": app_id, "app_secret": secret}
            )
        finally:
            if owned:
                await http.aclose()
        if not isinstance(body, dict):
            return "", f"token 接口返回异常（HTTP {status}）"
        token = str(body.get("tenant_access_token") or "")
        if not token:
            return "", f"取 token 失败 code={body.get('code')}：{body.get('msg') or short(body)}"
        return token, ""

    async def check(self, config: dict, mode: str, client: httpx.AsyncClient | None = None) -> Check:
        if mode == MODE_ROBOT:
            url = str(config.get("webhook") or "").strip()
            if not url.startswith("https://"):
                return Check(ok=False, detail="Webhook 地址必须是 https://")
            if not urlparse(url).path.startswith("/open-apis/bot/v2/hook/"):
                return Check(ok=False, detail="这不像飞书自定义机器人的地址（应含 /open-apis/bot/v2/hook/）")
            return Check(
                ok=False,
                claimed=False,
                detail="群机器人没有只读接口，凭据无法预检；点「发条测试消息」用真实送达来确认",
            )
        token, error = await self.token(config, client)
        if not token:
            return Check(ok=False, detail=error)
        # 再打一次 bot/v3/info：token 到手只说明密钥对，应用是否开了机器人能力要问这个接口。
        http, owned = as_client(client)
        try:
            status, body = await get_json(
                http, API_BASE + _BOT_PATH, headers={"Authorization": f"Bearer {token}"}
            )
        finally:
            if owned:
                await http.aclose()
        if not isinstance(body, dict) or int(body.get("code") or 0) != 0:
            detail = body.get("msg") if isinstance(body, dict) else short(body)
            return Check(ok=False, detail=f"应用凭据有效，但机器人能力不可用（HTTP {status}）：{detail}")
        name = str((body.get("bot") or {}).get("app_name") or "")
        return Check(ok=True, detail=f"凭据有效，机器人「{name or '未命名'}」可用")

    async def send(
        self, config: dict, mode: str, text: str, client: httpx.AsyncClient | None = None
    ) -> Send:
        if mode == MODE_ROBOT:
            url = str(config.get("webhook") or "").strip()
            if not url:
                return Send(ok=False, detail="还没填群机器人 Webhook")
            payload: dict = {"msg_type": "text", "content": {"text": text}}
            secret = str(config.get("sign_secret") or "")
            if secret:
                stamp = int(time.time())
                payload["timestamp"] = str(stamp)
                payload["sign"] = sign_webhook(secret, stamp)
            http, owned = as_client(client)
            try:
                status, body = await post_json(http, url, payload)
            finally:
                if owned:
                    await http.aclose()
            return _robot_result(status, body)

        receive_id = str(config.get("receive_id") or "").strip()
        if not receive_id:
            return Send(ok=False, detail="「发送目标 ID」是空的：应用机器人要有收件人才发得出去")
        token, error = await self.token(config, client)
        if not token:
            return Send(ok=False, detail=error)
        kind = str(config.get("receive_id_type") or "chat_id").strip() or "chat_id"
        http, owned = as_client(client)
        try:
            status, body = await post_json(
                http,
                API_BASE + _SEND_PATH,
                {
                    "receive_id": receive_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )
        finally:
            if owned:
                await http.aclose()
        if status < 400 and isinstance(body, dict) and int(body.get("code") or 0) == 0:
            return Send(ok=True, detail="应用消息已发送")
        detail = body.get("msg") if isinstance(body, dict) else short(body)
        return Send(ok=False, detail=f"应用消息发送失败（HTTP {status}）：{detail}")

    # ---- 回调 -------------------------------------------------------------

    def open_envelope(self, config: dict, body: bytes) -> dict:
        """拆回调信封：加密的解开，同时校验 Verification Token。"""
        try:
            envelope = json.loads(body.decode("utf-8", "ignore") or "{}")
        except ValueError:
            raise ValueError("回调不是合法 JSON") from None
        if not isinstance(envelope, dict):
            raise ValueError("回调不是 JSON 对象")
        encrypted = str(envelope.get("encrypt") or "")
        if encrypted:
            key = str(config.get("encrypt_key") or "")
            if not key:
                raise ValueError("收到加密回调，但没填 Encrypt Key")
            envelope = json.loads(decrypt_event(encrypted, key))
        self._verify_token(config, envelope)
        return envelope

    def _verify_token(self, config: dict, envelope: dict) -> None:
        expected = str(config.get("verification_token") or "")
        if not expected:
            return
        header = envelope.get("header") if isinstance(envelope.get("header"), dict) else {}
        actual = str(header.get("token") or envelope.get("token") or "")
        if actual and actual != expected:
            raise ValueError("Verification Token 不匹配：回调不是这个应用发来的")

    async def handshake(self, config: dict, mode: str, query: dict) -> str | None:
        return None

    async def handshake_body(self, config: dict, body: bytes) -> dict | None:
        """URL 校验：原样回 challenge。飞书只认这个形状，回错就一直提示"请求地址校验失败"。"""
        envelope = self.open_envelope(config, body)
        challenge = envelope.get("challenge")
        if challenge is None:
            return None
        return {"challenge": challenge}

    async def parse_inbound(
        self, config: dict, mode: str, *, query: dict, headers: dict, body: bytes
    ) -> Inbound | None:
        envelope = self.open_envelope(config, body)
        header = envelope.get("header") if isinstance(envelope.get("header"), dict) else {}
        if str(header.get("event_type") or "") != MESSAGE_EVENT:
            return None
        event = envelope.get("event") if isinstance(envelope.get("event"), dict) else {}
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        if str(message.get("message_type") or "text") != "text":
            return None
        try:
            content = json.loads(str(message.get("content") or "{}"))
        except ValueError:
            return None
        text = str(content.get("text") or "").strip()
        if not text:
            return None
        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
        return Inbound(
            external_id=str(message.get("message_id") or ""),
            text=text,
            sender=str(sender_id.get("open_id") or ""),
            conversation=str(message.get("chat_id") or ""),
            raw={"chat_type": str(message.get("chat_type") or "")},
        )


def _robot_result(status: int, body: object) -> Send:
    if not isinstance(body, dict):
        return Send(ok=False, detail=f"群机器人返回异常（HTTP {status}）")
    code = int(body.get("code") or body.get("StatusCode") or 0)
    if status < 400 and code == 0:
        return Send(ok=True, detail="群机器人已发送")
    return Send(ok=False, detail=f"群机器人发送失败 code={code}：{body.get('msg') or short(body)}")


__all__ = ["FeishuAdapter", "MESSAGE_EVENT", "decrypt_event", "sign_webhook"]
