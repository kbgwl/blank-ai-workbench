"""通用 Webhook：把提醒 POST 到客户自己的系统。

这一条存在的理由是"总有第四个厂商"。企业微信/钉钉/飞书之外还有自建机器人网关、
内部告警平台、n8n/Zapier 之类；给它们一个可配模板的出站通道，比每次加一个适配器
快得多，也不会让"支持 N 家"变成产品承诺。

只做发，不做收：入站要认定谁是谁、哪句话算任务，各家形状差太远，硬统一会做错。
"""

from __future__ import annotations

import json

import httpx

from blank_api.services.channels.base import (
    Adapter,
    Check,
    Field,
    Send,
    as_client,
    post_json,
)

MODE_APP = "app"
# 正文替换点。用 ``{text}`` 而不是 ``%s``：模板是客户手写的 JSON，
# 占位符必须一眼认得出，而且要能被 json.dumps 安全转义。
PLACEHOLDER = "{text}"

DEFAULT_TEMPLATE = '{"text": "{text}"}'


class WebhookAdapter(Adapter):
    kind = "webhook"
    label = "通用 Webhook"
    vendor = "自建"
    summary = "把任务提醒 POST 到任意 HTTP 接口。只发不收。"
    docs = ""
    modes = {
        MODE_APP: {"label": "HTTP 出站", "hint": "自定义请求体，适配自建机器人或告警平台", "push": True, "receive": ()},
    }
    fields = (
        Field(key="url", label="请求地址", required=True, placeholder="https://example.com/hooks/blank"),
        Field(
            key="header_name",
            label="鉴权头名称",
            placeholder="Authorization",
            help="可选。例如 Authorization，配合下面的值发 Bearer xxx",
        ),
        Field(key="header_value", label="鉴权头的值", secret=True, placeholder="Bearer xxxxxxxx", help="可选"),
        Field(
            key="template",
            label="请求体模板",
            default=DEFAULT_TEMPLATE,
            help='JSON，用 {text} 表示正文。例如 {"msgtype":"markdown","markdown":{"content":"{text}"}}',
        ),
    )
    setup = (
        "准备好接收 HTTP POST 的地址（自建网关、告警平台、n8n/Zapier 都行）",
        "请求体模板里的 {text} 会被替换成提醒正文（已按 JSON 转义，可以直接放进字符串里）",
        "如果对方要求鉴权，把请求头名称和值填上；值加密保存，不会回显",
    )

    async def check(self, config: dict, mode: str, client: httpx.AsyncClient | None = None) -> Check:
        url = str(config.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return Check(ok=False, detail="请求地址必须是 http(s):// 开头")
        if PLACEHOLDER not in str(config.get("template") or ""):
            return Check(ok=False, detail=f"请求体模板里没有 {PLACEHOLDER}，正文会被丢掉")
        if not _valid_template(str(config.get("template") or "")):
            return Check(ok=False, detail="请求体模板不是合法 JSON（替换 {text} 之后才能判断）")
        return Check(
            ok=False,
            claimed=False,
            detail="通用 Webhook 没有只读接口，凭据无法预检；点「发条测试消息」用真实送达来确认",
        )

    async def send(
        self, config: dict, mode: str, text: str, client: httpx.AsyncClient | None = None
    ) -> Send:
        url = str(config.get("url") or "").strip()
        if not url:
            return Send(ok=False, detail="还没填请求地址")
        template = str(config.get("template") or "") or DEFAULT_TEMPLATE
        body = render(template, text)
        try:
            payload = json.loads(body)
        except ValueError:
            return Send(ok=False, detail="请求体模板替换正文后不是合法 JSON")
        headers = {"Content-Type": "application/json"}
        name = str(config.get("header_name") or "").strip()
        if name:
            headers[name] = str(config.get("header_value") or "")
        http, owned = as_client(client)
        try:
            status, reply = await post_json(http, url, payload, headers=headers)
        finally:
            if owned:
                await http.aclose()
        if 200 <= status < 300:
            return Send(ok=True, detail=f"已投递（HTTP {status}）")
        return Send(ok=False, detail=f"投递失败（HTTP {status}）：{reply}")


def render(template: str, text: str) -> str:
    """把正文塞进模板。JSON 转义后再拼，避免引号/换行把模板拆坏。"""
    encoded = json.dumps(text, ensure_ascii=False)[1:-1]
    return template.replace(PLACEHOLDER, encoded)


def _valid_template(template: str) -> bool:
    try:
        json.loads(render(template, "x"))
    except ValueError:
        return False
    return True


__all__ = ["DEFAULT_TEMPLATE", "PLACEHOLDER", "WebhookAdapter", "render"]
