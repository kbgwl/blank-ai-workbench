"""MCP wire-level pieces: naming, JSON-RPC envelopes, schema and result mapping.

MCP servers speak JSON-RPC 2.0, and they are *someone else's process*. So
everything here is defensive rather than optimistic:

* Names are rewritten into the character set model APIs actually accept, and
  namespaced so a server's ``run_shell`` can never shadow ours.
* Schemas are repaired when a server sends something a model API would reject
  — an empty schema is a legal MCP tool, an empty schema is not a legal OpenAI
  function.
* Results are flattened to text instead of dropping blocks we cannot render.
  A silently empty result makes the model call the same tool again.
"""

from __future__ import annotations

import hashlib
import json
import re

# 模型 API 的 function name 上限是 64 字符、字符集是 [a-zA-Z0-9_-]。
# 服务器与工具名可以包含点、斜杠、中文——这些都必须在这里被磨平。
NAME_LIMIT = 64
MCP_PREFIX = "mcp__"
SEGMENT_SEPARATOR = "__"

_UNSAFE = re.compile(r"[^0-9A-Za-z_-]+")

# 握手版本，从新到旧依次尝试。客户端声明自己支持的最高版本，服务端回它要用的；
# 老服务器认不出新版本时会直接报错，所以留一个更老的号可以少一轮排查。
PROTOCOL_VERSIONS = ("2025-06-18", "2024-11-05")
CLIENT_NAME = "blank-agent"
CLIENT_VERSION = "0.7.0"

MAX_LIST_PAGES = 20


def _clean(segment: str) -> str:
    text = _UNSAFE.sub("_", (segment or "").strip()).strip("_")
    return text or "x"


def qualify(server: str, tool: str) -> str:
    """``mcp__<server>__<tool>``，超长时截断并补稳定哈希。

    稳定是硬要求：同一个 server/tool 每次都必须算出同一个名字，否则
    ``tool_search`` 拉回来的名字和下一步要调的名字对不上，表现为"工具丢失"。
    """
    name = f"{MCP_PREFIX}{_clean(server)}{SEGMENT_SEPARATOR}{_clean(tool)}"
    if len(name) <= NAME_LIMIT:
        return name
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"{name[: NAME_LIMIT - len(digest) - 1]}_{digest}"


def is_mcp_name(name: str) -> bool:
    return str(name or "").startswith(MCP_PREFIX)


def request(request_id: int, method: str, params: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def notification(method: str, params: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "method": method, "params": params or {}}


def parse_message(raw: str | bytes) -> dict | None:
    """一行原始输出 → 一条消息。不是 JSON 的行（日志、横幅）直接忽略。

    不少服务器会在握手前往 stdout 打一行业务版本号。为此中断整次连接不值得，
    但也绝不能把它当成消息塞给上层。
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    text = (raw or "").strip()
    if not text or not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def parse_sse(body: str) -> list[dict]:
    """Streamable HTTP 的响应体 → 消息列表。

    SSE 里 ``event:`` 行只是分类，真正的载荷全在 ``data:`` 里；同一事件的多行
    ``data:`` 按规范要用换行拼起来。
    """
    messages: list[dict] = []
    buffer: list[str] = []
    for line in (body or "").splitlines():
        if not line.strip():
            if buffer:
                message = parse_message("\n".join(buffer))
                if message is not None:
                    messages.append(message)
                buffer = []
            continue
        if line.startswith("data:"):
            buffer.append(line[5:].lstrip())
    if buffer:
        message = parse_message("\n".join(buffer))
        if message is not None:
            messages.append(message)
    return messages


def normalize_schema(raw) -> dict:
    """把服务端给的 inputSchema 修成模型 API 能接受的形状。"""
    schema = dict(raw) if isinstance(raw, dict) else {}
    if not schema:
        return {"type": "object", "properties": {}}
    schema.setdefault("type", "object")
    if schema.get("type") == "object":
        # 老服务器会给 {"type":"object"} 而不给 properties。
        # validate_arguments 按 properties 查类型，缺了不报错但也无从校验。
        schema.setdefault("properties", {})
    return schema


def summarize_description(name: str, description: str, server: str, limit: int = 400) -> str:
    """工具描述：模型靠它决定要不要调，所以必须留下"哪个服务、干什么"。

    服务端的描述常常很短（"Search repositories"），脱离上下文无法判断；
    把服务名带到前面，模型才不会把两家的同名工具搞混。
    """
    text = (description or "").strip()
    if not text:
        text = name
    if len(text) > limit:
        text = text[:limit] + "…"
    return f"[{server}] {text}"


def flatten_result(result) -> tuple[bool, str, dict]:
    """``tools/call`` 的结果 → (是否错误, 文本, 结构化数据)。

    非文本块不能丢。模型看不到"这里有一张图"，只会认为服务端什么都没返回，
    然后重复调用同一条——比报错更难查。
    """
    if not isinstance(result, dict):
        return False, str(result or ""), {}

    is_error = bool(result.get("isError"))
    parts: list[str] = []
    media: list[dict] = []

    for block in result.get("content") or []:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text") or ""))
        elif kind == "image":
            encoded = str(block.get("data") or "")
            size = len(encoded) * 3 // 4
            mime = str(block.get("mimeType") or "image/png")
            media.append({"type": "image", "mime": mime, "bytes": size})
            parts.append(f"[图片 {mime}，约 {size} 字节，文本中不展开]")
        elif kind in ("resource", "resource_link"):
            resource = block.get("resource") if isinstance(block.get("resource"), dict) else block
            uri = str(resource.get("uri") or resource.get("name") or "")
            body = resource.get("text")
            if isinstance(body, str):
                parts.append(f"[资源 {uri}]\n{body}")
            else:
                media.append({"type": "resource", "uri": uri})
                parts.append(f"[资源 {uri}，二进制内容未展开]")
        elif kind == "audio":
            mime = str(block.get("mimeType") or "audio/wav")
            media.append({"type": "audio", "mime": mime, "bytes": len(str(block.get("data") or "")) * 3 // 4})
            parts.append(f"[音频 {mime}，文本中不展开]")
        else:
            parts.append(json.dumps(block, ensure_ascii=False))

    structured = result.get("structuredContent")
    if not parts and structured is not None:
        parts.append(json.dumps(structured, ensure_ascii=False))

    text = "\n".join(part for part in parts if part).strip()
    if not text:
        # 明确的"服务端没给内容"胜过空白：空白会被模型当成分页/截断
        text = "（服务端没有返回内容）"

    data: dict = {"is_error": is_error}
    if media:
        data["media"] = media
    if structured is not None:
        data["structured"] = structured
    return is_error, text, data
