"""钉钉：企业内部应用（收发）+ 自定义群机器人（只发）。

入站走 **Stream 长连接**而不是回调，这是这台机器上唯一能"不折腾公网"的选择：
桌面版跑在客户自己的电脑上，没有固定域名，回调地址填不进去。Stream 是客户端
主动连出去，连上之后消息从钉钉推过来——内网也能用，也不需要备案域名。

长连接的实现放在 ``stream.py``；本文件只负责凭据、发送与报文解析。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx

from blank_api.services.channels.base import (
    Adapter,
    Check,
    Field,
    Inbound,
    Send,
    as_client,
    post_json,
    short,
)

API_BASE = "https://api.dingtalk.com"
_TOKEN_PATH = "/v1.0/oauth2/accessToken"
_OTO_PATH = "/v1.0/robot/oToMessages/batchSend"
_GROUP_PATH = "/v1.0/robot/groupMessages/send"
STREAM_OPEN_PATH = "/v1.0/gateway/connections/open"

MODE_APP = "app"
MODE_ROBOT = "robot"

# 机器人消息模板。sampleText 是钉钉规定的一条内置模板：不在这里注册 msgKey
# 就发不出去（自己加的模板要先在开放平台审核）。
MSG_KEY_TEXT = "sampleText"
# 订阅的 topic 决定能收到什么。只订机器人单聊/群聊消息——通讯录、审批流
# 那些要企业管理员额外授权，订了也收不到，还会让客户以为"全都在监控"。
BOT_TOPIC = "/v1.0/im/bot/messages/get"


def sign_webhook(secret: str, timestamp: int) -> str:
    """自定义机器人的加签：``HmacSHA256(timestamp + "\\n" + secret)`` 再 urlencode。"""
    raw = f"{timestamp}\n{secret}".encode()
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    return quote_plus(base64.b64encode(digest).decode())


def signed_url(url: str, secret: str, now: float | None = None) -> str:
    """把 timestamp/sign 拼到 Webhook 上。没配密钥就原样返回。"""
    if not secret:
        return url
    stamp = int(now if now is not None else time.time() * 1000)
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}timestamp={stamp}&sign={sign_webhook(secret, stamp)}"


class DingTalkAdapter(Adapter):
    kind = "dingtalk"
    label = "钉钉"
    vendor = "阿里"
    summary = "企业内部应用能收发（Stream 长连接，不需要公网）；群机器人只能发。"
    docs = "https://open.dingtalk.com/document/orgapp/stream"
    modes = {
        MODE_APP: {
            "label": "企业内部应用",
            "hint": "能主动发、也能通过 Stream 长连接收消息开任务；不需要公网地址",
            "push": True,
            "receive": ("stream",),
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
            key="client_id",
            label="Client ID（AppKey）",
            required=True,
            placeholder="dingxxxxxxxxxxxx",
            help="开放平台 → 应用开发 → 企业内部应用 → 凭证与基础信息",
            only_modes=(MODE_APP,),
        ),
        Field(
            key="client_secret",
            label="Client Secret（AppSecret）",
            secret=True,
            required=True,
            help="同上那一页。泄漏等于应用被接管",
            only_modes=(MODE_APP,),
        ),
        Field(
            key="robot_code",
            label="机器人编码（RobotCode）",
            required=True,
            help="应用里添加「机器人」能力后，机器人详情页的 RobotCode；发送消息必填",
            only_modes=(MODE_APP,),
        ),
        Field(
            key="to_user",
            label="发给谁（userId）",
            placeholder="manager0001,manager0002",
            help="多个用逗号分隔。留空则只收不发，靠会话里的「消息」回到聊天",
            only_modes=(MODE_APP,),
        ),
        Field(
            key="webhook",
            label="群机器人 Webhook",
            secret=True,
            required=True,
            placeholder="https://oapi.dingtalk.com/robot/send?access_token=…",
            help="群设置 → 智能群助手 → 添加机器人 → 自定义 → 复制 Webhook",
            only_modes=(MODE_ROBOT,),
        ),
        Field(
            key="sign_secret",
            label="加签密钥（SEC…）",
            secret=True,
            help="安全设置勾了「加签」就必须填，否则报 310000（签名不匹配）",
            only_modes=(MODE_ROBOT,),
        ),
    )
    setup = (
        "开放平台 → 应用开发 → 企业内部应用 → 创建应用，记下 Client ID 与 Client Secret",
        "应用里添加「机器人」能力，发布后拿到 RobotCode",
        "要收消息：机器人 →「消息接收模式」选 **Stream 模式**（不要选 HTTP 回调，那需要公网地址）",
        "把应用发布到可见范围，只有范围内的人发消息才会推过来",
        "群机器人更快：任意群 → 智能群助手 → 添加机器人 → 自定义 → 复制 Webhook（勾了加签就把密钥一起填）",
    )

    async def token(self, config: dict, client: httpx.AsyncClient | None = None) -> tuple[str, str]:
        client_id = str(config.get("client_id") or "").strip()
        secret = str(config.get("client_secret") or "").strip()
        if not client_id or not secret:
            return "", "缺少 Client ID 或 Client Secret"
        http, owned = as_client(client)
        try:
            status, body = await post_json(
                http, API_BASE + _TOKEN_PATH, {"appKey": client_id, "appSecret": secret}
            )
        finally:
            if owned:
                await http.aclose()
        if not isinstance(body, dict):
            return "", f"token 接口返回异常（HTTP {status}）"
        token = str(body.get("accessToken") or "")
        if not token:
            return "", f"取 token 失败：{body.get('message') or short(body)}"
        return token, ""

    async def check(self, config: dict, mode: str, client: httpx.AsyncClient | None = None) -> Check:
        if mode == MODE_ROBOT:
            url = str(config.get("webhook") or "").strip()
            if not url.startswith("https://"):
                return Check(ok=False, detail="Webhook 地址必须是 https://")
            token = (parse_qs(urlparse(url).query).get("access_token") or [""])[0]
            if not token:
                return Check(ok=False, detail="Webhook 地址里没有 access_token 参数")
            # 33012/310000 这类错误只有真发才会暴露；这里不代发，避免每次保存都往群里丢消息。
            return Check(
                ok=False,
                claimed=False,
                detail="群机器人没有只读接口，凭据无法预检；点「发条测试消息」用真实送达来确认",
            )
        token, error = await self.token(config, client)
        if not token:
            return Check(ok=False, detail=error)
        return Check(ok=True, detail="Client Secret 有效，已取到 accessToken")

    async def send(
        self, config: dict, mode: str, text: str, client: httpx.AsyncClient | None = None
    ) -> Send:
        if mode == MODE_ROBOT:
            url = str(config.get("webhook") or "").strip()
            if not url:
                return Send(ok=False, detail="还没填群机器人 Webhook")
            http, owned = as_client(client)
            try:
                status, body = await post_json(
                    http,
                    signed_url(url, str(config.get("sign_secret") or "")),
                    {"msgtype": "text", "text": {"content": text}},
                )
            finally:
                if owned:
                    await http.aclose()
            return _robot_result(status, body)

        users = [item.strip() for item in str(config.get("to_user") or "").split(",") if item.strip()]
        if not users:
            return Send(ok=False, detail="「发给谁」是空的：应用机器人只能发给指定 userId")
        token, error = await self.token(config, client)
        if not token:
            return Send(ok=False, detail=error)
        payload = {
            "robotCode": str(config.get("robot_code") or "").strip(),
            "userIds": users[:20],
            "msgKey": MSG_KEY_TEXT,
            "msgParam": json.dumps({"content": text}, ensure_ascii=False),
        }
        http, owned = as_client(client)
        try:
            status, body = await post_json(
                http,
                API_BASE + _OTO_PATH,
                payload,
                headers={"x-acs-dingtalk-access-token": token},
            )
        finally:
            if owned:
                await http.aclose()
        if status >= 400 or not isinstance(body, dict):
            return Send(ok=False, detail=f"应用消息发送失败（HTTP {status}）：{short(body)}")
        if body.get("flowControlledStaffIdList"):
            return Send(ok=True, detail="已发送，但部分账号被限流：" + ",".join(body["flowControlledStaffIdList"]))
        return Send(ok=True, detail=f"应用消息已发送给 {len(users)} 人")

    # ---- Stream 入站 ------------------------------------------------------

    def parse_stream(self, data: dict) -> Inbound | None:
        """把 ``/v1.0/im/bot/messages/get`` 的 data 解析成一条消息。"""
        if not isinstance(data, dict):
            return None
        if str(data.get("msgtype") or "text") != "text":
            return None
        text = str(((data.get("text") or {}) if isinstance(data.get("text"), dict) else {}).get("content") or "").strip()
        if not text:
            return None
        staff = str(data.get("senderStaffId") or "")
        nick = str(data.get("senderNick") or "")
        return Inbound(
            external_id=str(data.get("msgId") or f"{staff}:{data.get('createAt')}"),
            text=text,
            sender=staff,
            sender_name=nick,
            conversation=str(data.get("conversationId") or ""),
            conversation_name=str(data.get("conversationTitle") or ""),
            raw={
                # 会话 Webhook 是钉钉给的"就地回话"通道，1.5 小时内有效。
                # 存下来任务跑完才能把结论送回这个会话，而不是另开一条通知。
                "session_webhook": str(data.get("sessionWebhook") or ""),
                "conversation_type": str(data.get("conversationType") or ""),
            },
        )


def _robot_result(status: int, body: object) -> Send:
    if not isinstance(body, dict):
        return Send(ok=False, detail=f"群机器人返回异常（HTTP {status}）")
    code = int(body.get("errcode") or 0)
    if code == 0 and status < 400:
        return Send(ok=True, detail="群机器人已发送")
    return Send(ok=False, detail=f"群机器人发送失败 errcode={code}：{body.get('errmsg') or short(body)}")


async def reply_via_session_webhook(url: str, text: str, client: httpx.AsyncClient | None = None) -> Send:
    """用会话 Webhook 回一句话。过期后钉钉返回 300001，这种失败只记不抛。"""
    if not url:
        return Send(ok=False, detail="没有会话 Webhook，无法回到原对话")
    http, owned = as_client(client)
    try:
        status, body = await post_json(http, url, {"msgtype": "text", "text": {"content": text}})
    finally:
        if owned:
            await http.aclose()
    return _robot_result(status, body)


__all__ = [
    "BOT_TOPIC",
    "DingTalkAdapter",
    "MSG_KEY_TEXT",
    "STREAM_OPEN_PATH",
    "reply_via_session_webhook",
    "sign_webhook",
    "signed_url",
]
