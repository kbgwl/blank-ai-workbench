"""web_search / web_fetch: the agent's only outbound window.

Network tools are the easiest way to leak data or to be tricked into hitting an
internal service, so the guards are explicit rather than incidental:

* only http/https, and only after DNS resolution confirms **every** answer is a
  public address (blocks ``localhost``, ``127.0.0.1``, ``10/8``, ``169.254.169.254``
  and friends);
* redirects are followed by hand so每一跳都重新校验 — httpx's auto-follow would
  let a public URL bounce to an internal one;
* responses are capped in bytes and in time, and the extracted text is capped
  again before it enters the context;
* an optional allow/deny domain list, configured through the environment.

Everything here is best-effort and fails closed with a readable message.
"""

from __future__ import annotations

import html
import ipaddress
import os
import re
import socket
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import httpx

from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok

MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
DEFAULT_TIMEOUT = 20.0
MAX_TEXT_CHARS = 12_000
MAX_SEARCH_RESULTS = 8
USER_AGENT = "BlankAI-Workbench/0.2 (+https://blank.local)"
ALLOWED_SCHEMES = ("http", "https")
BLOCKED_HOST_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa")

# 测试可替换；生产直接走 httpx
CLIENT_FACTORY: Callable[..., httpx.AsyncClient] = httpx.AsyncClient

_TAG = re.compile(r"<(script|style|noscript|template)[^>]*>.*?</\1>", re.S | re.I)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_BLOCK_TAG = re.compile(r"</?(p|div|br|li|tr|h[1-6]|section|article|pre|blockquote)[^>]*>", re.I)
_ANY_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\u00a0]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_DDG_RESULT = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I
)
_DDG_SNIPPET = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S | re.I)


@dataclass
class FetchResult:
    url: str
    status: int
    content_type: str
    title: str = ""
    text: str = ""
    truncated: bool = False
    redirects: list[str] = field(default_factory=list)


def _env_list(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


def _is_public_host(host: str) -> bool:
    lowered = host.strip().strip(".").lower()
    if not lowered or lowered in ("localhost", "localhost.localdomain"):
        return False
    if any(lowered.endswith(suffix) for suffix in BLOCKED_HOST_SUFFIXES):
        return False
    try:
        infos = socket.getaddrinfo(lowered, None)
    except socket.gaierror:
        return False
    seen = False
    for info in infos:
        address = info[4][0]
        try:
            parsed = ipaddress.ip_address(address.split("%")[0])
        except ValueError:
            return False
        seen = True
        if (
            parsed.is_private
            or parsed.is_loopback
            or parsed.is_link_local
            or parsed.is_multicast
            or parsed.is_reserved
            or parsed.is_unspecified
        ):
            return False
    return seen


def check_url(url: str) -> str:
    """Return "" when the URL may be fetched, otherwise a Chinese refusal reason."""
    parsed = urlparse(str(url).strip())
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return f"只允许 http/https，收到：{parsed.scheme or '空'}"
    host = parsed.hostname or ""
    if not host:
        return "URL 缺少主机名"
    allow = _env_list("BLANK_WEB_ALLOW_DOMAINS")
    deny = _env_list("BLANK_WEB_DENY_DOMAINS")
    lowered = host.lower()
    if any(lowered == item or lowered.endswith("." + item) for item in deny):
        return f"域名 {host} 在禁止访问列表中"
    if allow and not any(lowered == item or lowered.endswith("." + item) for item in allow):
        return f"域名 {host} 不在允许访问列表中"
    if not _is_public_host(host):
        return f"域名 {host} 解析到内网/回环地址，已阻止访问"
    return ""


def extract_text(raw: str) -> tuple[str, str]:
    """Strip HTML down to readable text; returns (title, text)."""
    title_match = _TITLE.search(raw)
    title = html.unescape(_ANY_TAG.sub("", title_match.group(1))).strip() if title_match else ""
    cleaned = _COMMENT.sub(" ", raw)
    cleaned = _TAG.sub(" ", cleaned)
    cleaned = _BLOCK_TAG.sub("\n", cleaned)
    cleaned = _ANY_TAG.sub(" ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = _WS.sub(" ", cleaned)
    lines = [line.strip() for line in cleaned.splitlines()]
    text = "\n".join(line for line in lines if line)
    return title[:200], _BLANK_LINES.sub("\n\n", text).strip()


async def fetch(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> FetchResult:
    """Fetch a public URL, validating every redirect hop."""
    target = str(url).strip()
    redirects: list[str] = []
    async with CLIENT_FACTORY(timeout=timeout, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            reason = check_url(target)
            if reason:
                raise ValueError(reason)
            response = await client.get(
                target,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json,text/plain,*/*"},
            )
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location") or ""
                if not location:
                    raise ValueError("重定向缺少 Location 头")
                target = urljoin(target, location)
                redirects.append(target)
                continue
            body = response.content[:MAX_BYTES]
            truncated = len(response.content) > MAX_BYTES
            content_type = (response.headers.get("content-type") or "").lower()
            charset = response.encoding or "utf-8"
            try:
                raw = body.decode(charset, errors="replace")
            except LookupError:
                raw = body.decode("utf-8", errors="replace")
            if "html" in content_type or raw.lstrip().startswith("<"):
                title, text = extract_text(raw)
            else:
                title, text = "", raw.strip()
            return FetchResult(
                url=target,
                status=response.status_code,
                content_type=content_type,
                title=title,
                text=text,
                truncated=truncated,
                redirects=redirects,
            )
    raise ValueError(f"重定向次数超过上限（{MAX_REDIRECTS}）")


async def search(query: str, *, limit: int = 5, timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
    """Search the web. Tavily when a key is configured, DuckDuckGo HTML otherwise."""
    key = os.getenv("BLANK_SEARCH_API_KEY", "").strip()
    provider = os.getenv("BLANK_SEARCH_PROVIDER", "").strip().lower()
    if key and provider in ("", "tavily"):
        return await _tavily(query, key, limit, timeout)
    return await _duckduckgo(query, limit, timeout)


async def _tavily(query: str, key: str, limit: int, timeout: float) -> list[dict]:
    async with CLIENT_FACTORY(timeout=timeout) as client:
        response = await client.post(
            "https://api.tavily.com/search",
            json={"api_key": key, "query": query, "max_results": limit, "search_depth": "basic"},
        )
        response.raise_for_status()
        payload = response.json()
    results = []
    for item in payload.get("results") or []:
        url = str(item.get("url") or "")
        if not url:
            continue
        results.append(
            {
                "title": str(item.get("title") or "")[:200],
                "url": url,
                "snippet": str(item.get("content") or "")[:600],
            }
        )
    return results[:limit]


async def _duckduckgo(query: str, limit: int, timeout: float) -> list[dict]:
    endpoint = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    async with CLIENT_FACTORY(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(endpoint, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        raw = response.text
    snippets = [html.unescape(_ANY_TAG.sub("", item)).strip() for item in _DDG_SNIPPET.findall(raw)]
    results: list[dict] = []
    for index, match in enumerate(_DDG_RESULT.finditer(raw)):
        href = html.unescape(match.group(1))
        parsed = urlparse(href)
        if parsed.netloc.endswith("duckduckgo.com") and "uddg" in parsed.query:
            href = parse_qs(parsed.query).get("uddg", [href])[0]
        title = html.unescape(_ANY_TAG.sub("", match.group(2))).strip()
        if not href.startswith("http"):
            continue
        results.append(
            {
                "title": title[:200],
                "url": href,
                "snippet": (snippets[index] if index < len(snippets) else "")[:600],
            }
        )
        if len(results) >= limit:
            break
    return results


async def _web_fetch(context: ToolContext, arguments: dict) -> ToolOutcome:
    url = str(arguments.get("url") or "").strip()
    if not url:
        return fail("error", "缺少 url 参数")
    limit = int(arguments.get("max_chars") or 8_000)
    limit = max(500, min(MAX_TEXT_CHARS, limit))
    try:
        result = await fetch(url, timeout=float(arguments.get("timeout_seconds") or DEFAULT_TIMEOUT))
    except ValueError as exc:
        return fail("denied", str(exc))
    except httpx.HTTPError as exc:
        return fail("error", f"抓取失败：{exc.__class__.__name__}")
    text = result.text[:limit]
    truncated = result.truncated or len(result.text) > limit
    header = f"# {result.title}\n{result.url}（HTTP {result.status}）\n\n" if result.title else f"{result.url}（HTTP {result.status}）\n\n"
    return ok(
        header + (text or "（页面没有可提取的正文）") + ("\n…（已截断）" if truncated else ""),
        url=result.url,
        status=result.status,
        content_type=result.content_type,
        title=result.title,
        chars=len(text),
        truncated=truncated,
        redirects=result.redirects,
    )


async def _web_search(context: ToolContext, arguments: dict) -> ToolOutcome:
    query = str(arguments.get("query") or "").strip()
    if not query:
        return fail("error", "缺少 query 参数")
    limit = int(arguments.get("max_results") or 5)
    limit = max(1, min(MAX_SEARCH_RESULTS, limit))
    try:
        results = await search(query, limit=limit)
    except httpx.HTTPError as exc:
        return fail("error", f"搜索失败：{exc.__class__.__name__}")
    except ValueError as exc:
        return fail("error", str(exc))
    if not results:
        return ok(f"没有找到与「{query}」相关的结果。", query=query, results=[])
    lines = [f"搜索：{query}", ""]
    for index, item in enumerate(results, start=1):
        lines.append(f"{index}. {item['title']}")
        lines.append(f"   {item['url']}")
        if item.get("snippet"):
            lines.append(f"   {item['snippet']}")
    return ok("\n".join(lines), query=query, results=results, count=len(results))


def web_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="web_search",
            description="联网搜索，返回标题、链接与摘要。需要最新信息或工作区之外的资料时使用。",
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "搜索词"},
                    "max_results": {"type": "integer", "description": "返回条数，默认 5，上限 8"},
                },
            },
            risk_level="medium",
            handler=_web_search,
            timeout_seconds=45.0,
        ),
        ToolDefinition(
            name="web_fetch",
            description=(
                "抓取一个公开网页并提取正文（自动去掉 HTML 标签）。"
                "只允许 http/https 与公网地址，内网、回环地址会被拒绝。"
            ),
            input_schema={
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {"type": "string", "description": "要抓取的 URL"},
                    "max_chars": {"type": "integer", "description": "正文最多返回多少字符，默认 8000"},
                    "timeout_seconds": {"type": "number", "description": "超时秒数，默认 20"},
                },
            },
            risk_level="medium",
            handler=_web_fetch,
            timeout_seconds=60.0,
        ),
    ]
