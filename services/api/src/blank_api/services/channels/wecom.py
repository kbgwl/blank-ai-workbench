"""企业微信：应用消息（收发）+ 群机器人（只发）。

两种接法差别很大，界面上必须说清，否则客户会以为"配了机器人也能收消息"：

* **应用**（corpid + secret + agentid）：能发应用消息，也能收回调。回调要求公网
  可达的 https 地址，并且要在后台配 URL/Token/EncodingAESKey 三项——少一项就是
  「回调地址保存失败」，而企业微信只给一句模糊提示。
* **群机器人**（webhook key）：只能发。没有只读接口，所以凭据验证只能靠发一条
  测试消息——这里如实返回「没验过」，不假装通过。

回调的加解密是本文件最容易写错的地方：签名用 ``sha1(sorted([token, timestamp,
nonce, encrypt]).join())``，AES 用 32 字节块的 PKCS#7，明文是
``16 字节随机 + 4 字节长度(大端) + 正文 + corpid``。
"""

from __future__ import annotations

import base64
import hashlib
import os
import xml.etree.ElementTree as ET
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

API_BASE = "https://qyapi.weixin.qq.com"
_TOKEN_PATH = "/cgi-bin/gettoken"
_SEND_PATH = "/cgi-bin/message/send"

MODE_APP = "app"
MODE_ROBOT = "robot"


def _aes_key(encoding_aes_key: str) -> bytes:
    raw = base64.b64decode((encoding_aes_key or "").strip() + "=")
    if len(raw) != 32:
        raise ValueError("EncodingAESKey 长度不对，应为后台那串 43 位字符")
    return raw


def _unpad(data: bytes, block: int = 32) -> bytes:
    if not data:
        raise ValueError("密文为空")
    pad = data[-1]
    if pad < 1 or pad > block or pad > len(data):
        raise ValueError("填充非法，EncodingAESKey 可能不匹配")
    return data[:-pad]


def _pad(data: bytes, block: int = 32) -> bytes:
    amount = block - len(data) % block
    return data + bytes([amount]) * amount


def decrypt(encrypted: str, encoding_aes_key: str, corp_id: str = "") -> str:
    """解一条回调密文。``corp_id`` 非空时校验末尾的 receiveid，防串号。"""
    key = _aes_key(encoding_aes_key)
    raw = base64.b64decode(encrypted)
    decrypted = _aes_cbc(raw, key, encrypt=False)
    plain = _unpad(decrypted)
    if len(plain) < 20:
        raise ValueError("解密后长度不足，EncodingAESKey 可能不匹配")
    length = int.from_bytes(plain[16:20], "big")
    body = plain[20 : 20 + length]
    if corp_id:
        receive_id = plain[20 + length :].decode("utf-8", "ignore")
        if receive_id and receive_id != corp_id:
            raise ValueError(f"回调不属于这个企业：{receive_id}")
    return body.decode("utf-8", "ignore")


def encrypt(text: str, encoding_aes_key: str, corp_id: str) -> str:
    key = _aes_key(encoding_aes_key)
    body = text.encode("utf-8")
    payload = os.urandom(16) + len(body).to_bytes(4, "big") + body + corp_id.encode("utf-8")
    return base64.b64encode(_aes_cbc(_pad(payload), key, encrypt=True)).decode()


def _aes_cbc(data: bytes, key: bytes, *, encrypt: bool) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
    worker = cipher.encryptor() if encrypt else cipher.decryptor()
    return worker.update(data) + worker.finalize()


def signature(token: str, timestamp: str, nonce: str, encrypted: str) -> str:
    joined = "".join(sorted([str(token), str(timestamp), str(nonce), str(encrypted)]))
    return hashlib.sha1(joined.encode()).hexdigest()


def _xml_text(root: ET.Element, tag: str) -> str:
    node = root.find(tag)
    return (node.text or "").strip() if node is not None else ""


class WeComAdapter(Adapter):
    kind = "wecom"
    label = "企业微信"
    vendor = "腾讯"
    summary = "应用消息可收发；群机器人只能发。回调需要公网可达的 https 地址。"
    docs = "https://developer.work.weixin.qq.com/document/path/90930"
    modes = {
        MODE_APP: {
            "label": "企业应用",
            "hint": "能主动发消息、也能收消息开任务；需要应用 Secret 与 AgentId",
            "push": True,
            "receive": ("webhook",),
        },
        MODE_ROBOT: {
            "label": "群机器人",
            "hint": "只需一个 Webhook，最快能用；只能发，收不到消息，也无法验证凭据",
            "push": True,
            "receive": (),
        },
    }
    fields = (
        Field(
            key="corp_id",
            label="企业 ID（CorpId）",
            required=True,
            placeholder="ww1234567890abcdef",
            help="企业微信后台 →「我的企业」→ 企业信息 → 企业 ID",
            only_modes=(MODE_APP,),
        ),
        Field(
            key="agent_id",
            label="应用 AgentId",
            required=True,
            placeholder="1000002",
            help="自建应用详情页的 AgentId。填错会报 81013（应用不存在）",
            only_modes=(MODE_APP,),
        ),
        Field(
            key="secret",
            label="应用 Secret",
            secret=True,
            required=True,
            help="自建应用详情页的 Secret。它决定能发消息给谁，泄漏等于账号被接管",
            only_modes=(MODE_APP,),
        ),
        Field(
            key="to_user",
            label="发给谁",
            placeholder="@all",
            default="@all",
            help="成员账号，多个用 | 分隔；部门用 @all 或 partyid。留空按 @all",
            only_modes=(MODE_APP,),
        ),
        Field(
            key="token",
            label="回调 Token",
            secret=True,
            help="只在收消息时需要：接收消息服务器配置里的 Token（3–32 位）",
            only_modes=(MODE_APP,),
        ),
        Field(
            key="encoding_aes_key",
            label="EncodingAESKey",
            secret=True,
            help="只在收消息时需要：接收消息服务器配置里的 43 位 EncodingAESKey",
            only_modes=(MODE_APP,),
        ),
        Field(
            key="webhook",
            label="群机器人 Webhook",
            secret=True,
            required=True,
            placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…",
            help="群设置 → 群机器人 → 添加 → 复制 Webhook 地址",
            only_modes=(MODE_ROBOT,),
        ),
    )
    setup = (
        "企业微信后台创建「自建应用」，记下 AgentId 与 Secret",
        "应用详情页 →「企业可信 IP」里加上这台机器的出口 IP，否则调用会被拒",
        "要收消息：应用详情页 →「接收消息」→ 设置 API 接收，URL 填本页给的回调地址，随机生成 Token 与 EncodingAESKey 后填回来",
        "群机器人更快：任意群 → 群机器人 → 添加 → 复制 Webhook 地址，选「群机器人」模式粘贴即可",
    )

    async def _token(self, config: dict, client: httpx.AsyncClient | None) -> tuple[str, str]:
        """返回 (access_token, 错误说明)。"""
        corp_id = str(config.get("corp_id") or "").strip()
        secret = str(config.get("secret") or "").strip()
        if not corp_id or not secret:
            return "", "缺少企业 ID 或应用 Secret"
        http, owned = as_client(client)
        try:
            status, body = await get_json(
                http, API_BASE + _TOKEN_PATH, params={"corpid": corp_id, "corpsecret": secret}
            )
        finally:
            if owned:
                await http.aclose()
        if not isinstance(body, dict):
            return "", f"token 接口返回异常（HTTP {status}）"
        code = int(body.get("errcode") or 0)
        if code != 0 or not body.get("access_token"):
            return "", f"取 token 失败 errcode={code}：{body.get('errmsg') or short(body)}"
        return str(body["access_token"]), ""

    async def check(self, config: dict, mode: str, client: httpx.AsyncClient | None = None) -> Check:
        if mode == MODE_ROBOT:
            url = str(config.get("webhook") or "").strip()
            if not url.startswith("https://"):
                return Check(ok=False, detail="Webhook 地址必须是 https://")
            parsed = urlparse(url)
            key = (parse_qs(parsed.query).get("key") or [""])[0]
            if not key:
                return Check(ok=False, detail="Webhook 地址里没有 key 参数，可能复制漏了")
            return Check(
                ok=False,
                claimed=False,
                detail="群机器人没有只读接口，凭据无法预检；点「发条测试消息」用真实送达来确认",
            )
        token, error = await self._token(config, client)
        if not token:
            return Check(ok=False, detail=error)
        return Check(ok=True, detail="Secret 有效，已取到 access_token")

    async def send(
        self, config: dict, mode: str, text: str, client: httpx.AsyncClient | None = None
    ) -> Send:
        if mode == MODE_ROBOT:
            url = str(config.get("webhook") or "").strip()
            if not url:
                return Send(ok=False, detail="还没填群机器人 Webhook")
            http, owned = as_client(client)
            try:
                status, body = await post_json(http, url, {"msgtype": "text", "text": {"content": text}})
            finally:
                if owned:
                    await http.aclose()
            return _send_result(status, body, "群机器人")

        token, error = await self._token(config, client)
        if not token:
            return Send(ok=False, detail=error)
        payload = {
            "touser": str(config.get("to_user") or "@all").strip() or "@all",
            "msgtype": "text",
            "agentid": int(str(config.get("agent_id") or "0").strip() or 0),
            "text": {"content": text},
            "safe": 0,
        }
        http, owned = as_client(client)
        try:
            status, body = await post_json(
                http, API_BASE + _SEND_PATH, payload, headers={"Content-Type": "application/json"}
            )
        finally:
            if owned:
                await http.aclose()
        return _send_result(status, body, "应用消息")

    async def handshake(self, config: dict, mode: str, query: dict) -> str | None:
        """GET 回调校验：验签后把 echostr 解出来原样回。"""
        encrypted = str(query.get("echostr") or "")
        if not encrypted:
            return None
        self._verify(config, query, encrypted)
        return decrypt(encrypted, str(config.get("encoding_aes_key") or ""), str(config.get("corp_id") or ""))

    def _verify(self, config: dict, query: dict, encrypted: str) -> None:
        token = str(config.get("token") or "")
        if not token:
            # 空 Token 也会算出个签名，但永远对不上——报"签名不匹配"会把客户
            # 引去查错地方（他以为泄漏了），实际只是没填。
            raise ValueError("这条通道没填回调 Token，企业微信的消息无法验签；去设置里补上再试")
        expected = signature(
            token,
            str(query.get("timestamp") or ""),
            str(query.get("nonce") or ""),
            encrypted,
        )
        if expected != str(query.get("msg_signature") or ""):
            raise ValueError("回调签名不匹配：Token 填错了，或者回调地址被伪造")

    async def parse_inbound(
        self, config: dict, mode: str, *, query: dict, headers: dict, body: bytes
    ) -> Inbound | None:
        if mode != MODE_APP:
            return None
        if not str(config.get("encoding_aes_key") or ""):
            raise ValueError("这条通道没填 EncodingAESKey，密文解不开；去设置里补上再试")
        root = ET.fromstring(body.decode("utf-8", "ignore"))
        encrypted = _xml_text(root, "Encrypt")
        if not encrypted:
            return None
        self._verify(config, query, encrypted)
        plain = ET.fromstring(decrypt(encrypted, str(config.get("encoding_aes_key") or ""), str(config.get("corp_id") or "")))
        if _xml_text(plain, "MsgType") != "text":
            # 图片/事件回调不是"一句话"，不开任务。退回 None 让上层照常 200。
            return None
        text = _xml_text(plain, "Content").strip()
        if not text:
            return None
        sender = _xml_text(plain, "FromUserName")
        return Inbound(
            external_id=_xml_text(plain, "MsgId") or f"{sender}:{_xml_text(plain, 'CreateTime')}",
            text=text,
            sender=sender,
            conversation=_xml_text(plain, "ToUserName"),
            raw={"msg_type": "text", "agent_id": _xml_text(plain, "AgentID")},
        )


def _send_result(status: int, body: object, what: str) -> Send:
    if not isinstance(body, dict):
        return Send(ok=False, detail=f"{what}返回异常（HTTP {status}）")
    code = int(body.get("errcode") or 0)
    if code == 0 and status < 400:
        invalid = str(body.get("invaliduser") or "").strip()
        if invalid:
            # 发送成功但有人没收到的情形：不报错，但要念出来，否则客户以为所有人都收到了。
            return Send(ok=True, detail=f"{what}已发送，但这些账号没收件人：{invalid}")
        return Send(ok=True, detail=f"{what}已发送")
    return Send(ok=False, detail=f"{what}发送失败 errcode={code}：{body.get('errmsg') or short(body)}")


__all__ = ["WeComAdapter", "decrypt", "encrypt", "signature"]
