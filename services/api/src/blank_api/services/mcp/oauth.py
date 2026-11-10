"""Remote MCP over OAuth 2.0：发现 → client_credentials → 刷新。

这类服务端的进程在远端，本机 Job 管不到它的内存与进程；能强制的是**出站身份**：
每个请求必须带一枚本机主动取得的令牌，令牌的受众与过期要能在本机核对。

四条刻意的边界：

* **只走无人工的 client_credentials + refresh_token。** 桌面/后端进程里没有人能
  点浏览器授权页，交互式 authorization_code 不在这里做；需要用户授权的服务端
  应当把 mcp.json 里的 auth 配成对应形态（目前明确拒绝并说明原因）。
* **JWT 只解包、不验签。** 令牌是经 TLS 从令牌端点直接取回的，签名链在传输里已经
  闭环；本机要验的是"这枚令牌是不是发给我、是不是还活着"——即 ``aud`` 与 ``exp``。
  不走 TLS 的令牌（非 loopback 的 http）在连接前就被拒绝，不给自己留"假装验签"的位置。
* **受众缺失就拒绝，不靠猜。** 服务端返回 JWT 却没有 ``aud``、或 ``aud`` 对不上，
  一律拒用并说清期望值与实际值。opaque 令牌无法在本机核对受众，这一事实要写进
  状态（``audience_checked=False``），不许说成"已校验"。
* **令牌不进错误、状态、证据。** 所有对外文案都经过 ``_redact``；事实里只有类型、
  剩余秒数与核对结论。令牌和 client_secret 只活在内存里，连接关闭即抹掉。

发现顺序遵循 MCP 授权规范用到的两份元数据：RFC 9728 受保护资源元数据
（``/.well-known/oauth-protected-resource``）给出授权服务器，RFC 8414 / OIDC
元数据给出 ``token_endpoint`` 与客户端认证方式。
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

DISCOVERY_TIMEOUT = 8.0
MAX_METADATA_BYTES = 256 * 1024
EXPIRY_SKEW = 30.0
AUTH_METHODS = ("client_secret_basic", "client_secret_post")
SUPPORTED_GRANT = "client_credentials"


class AuthError(Exception):
    """带机器可读 code 的授权失败。client.py 负责把它换成 MCPError。

    消息里永远不含令牌或 client_secret：这些都是给人看的，不该带凭证。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class OAuthConfig:
    """mcp.json 里 ``auth`` 段。client_secret 不允许出现在 repr 里。"""

    client_id: str
    client_secret: str = field(repr=False, default="")
    token_url: str = ""
    scope: str = ""
    audience: str = ""
    resource_metadata_url: str = ""
    # "" = 按授权服务器元数据自动挑；也接受 "basic" / "post"
    auth_method: str = ""


def _is_loopback(host: str) -> bool:
    name = str(host or "").strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _safe_url(url: str) -> str:
    """错误文案里出现的 URL 去掉 userinfo：``https://u:p@host`` 不该把密码打印出来。"""
    parts = urlsplit(str(url or ""))
    if parts.username is None and parts.password is None:
        return str(url or "")
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


def require_secure_url(url: str, what: str) -> None:
    """非 loopback 一律 https。本地开发允许 http://127.0.0.1，别的一律不行。"""
    parts = urlsplit(str(url or ""))
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and _is_loopback(parts.hostname or ""):
        return
    raise AuthError(
        "mcp_auth_insecure",
        f"{what}必须是 https（只有本机 loopback 允许 http）：{_safe_url(url)}",
    )


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _well_known_urls(url: str, names: tuple[str, ...]) -> list[str]:
    """RFC 8414 的路径感知候选：``/.well-known/x/<path>`` 先试，``/.well-known/x`` 兜底。"""
    parts = urlsplit(url)
    origin = _origin(url)
    path = parts.path.rstrip("/")
    urls: list[str] = []
    for name in names:
        if path:
            urls.append(f"{origin}/.well-known/{name}{path}")
        urls.append(f"{origin}/.well-known/{name}")
    return urls


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def decode_jwt_claims(token: str) -> dict | None:
    """解 JWT payload；不是三段式或解不开就返回 None（＝opaque 令牌）。

    刻意不验签：验签需要密钥集与算法白名单，而令牌是刚经 TLS 从令牌端点取回的。
    本函数的唯一用途是读 ``aud`` / ``exp`` 这两个事实。
    """
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _audience_matches(aud: Any, expected: str) -> bool:
    if not expected:
        return False
    values = [aud] if isinstance(aud, str) else list(aud) if isinstance(aud, list) else []
    target = expected.rstrip("/")
    return any(isinstance(item, str) and item.rstrip("/") == target for item in values)


class OAuthProvider:
    """一枚令牌的生命周期：取得、保持、401 后刷新一次。"""

    def __init__(self, config: OAuthConfig, *, server_url: str, name: str = "") -> None:
        self.config = config
        self._server_url = str(server_url or "")
        self._name = name or "mcp"
        require_secure_url(self._server_url, "MCP 服务端地址")
        if config.token_url:
            require_secure_url(config.token_url, "令牌端点")
        if config.auth_method and config.auth_method not in ("basic", "post"):
            raise AuthError(
                "mcp_auth_config",
                f"auth_method 只支持 basic 或 post（实际 {config.auth_method}）",
            )
        self._token = ""
        self._refresh_token = ""
        self._token_type = "Bearer"
        self._expires_at = 0.0
        self._token_endpoint = str(config.token_url or "")
        self._auth_method = ""
        self._expected_audience = str(config.audience or "")
        self._resource = ""
        self._source = ""
        self._expiry_checked = False
        self._audience_checked = False
        self._audience_note = ""

    # -- 生命周期 -----------------------------------------------------------
    async def start(self, client: httpx.AsyncClient) -> None:
        await self._resolve(client)
        await self._acquire(client)

    def close(self) -> None:
        self._token = ""
        self._refresh_token = ""
        self._expires_at = 0.0

    # -- 对外事实 -----------------------------------------------------------
    def authorization(self) -> str:
        if not self._token:
            raise AuthError("mcp_auth_missing_token", f"{self._name} 还没有可用的 OAuth 令牌")
        return f"{self._token_type} {self._token}"

    def facts(self) -> dict[str, Any]:
        """状态与证据里能说的全部。刻意不含令牌、刷新令牌与 client_secret。"""
        remaining = max(0, int(self._expires_at - time.time())) if self._expires_at else 0
        if self._audience_checked:
            audience_note = f"JWT 的 aud 已核对（期望 {self._expected_audience}）"
        elif self._audience_note:
            audience_note = self._audience_note
        else:
            audience_note = "受众未在本机核对"
        return {
            "scheme": "oauth",
            "grant": SUPPORTED_GRANT,
            "token_type": self._token_type,
            "expires_in": remaining,
            "expiry_checked": self._expiry_checked,
            "audience_checked": self._audience_checked,
            "audience": self._expected_audience,
            "metadata_source": self._source,
            "note": audience_note,
        }

    def needs_refresh(self) -> bool:
        return bool(self._expires_at) and time.time() >= self._expires_at - EXPIRY_SKEW

    async def ensure_fresh(self, client: httpx.AsyncClient) -> None:
        if self.needs_refresh():
            await self.refresh(client)

    async def refresh(self, client: httpx.AsyncClient) -> None:
        """先试 refresh_token；被拒就退回 client_credentials。401 后由调用方触发一次。"""
        if self._refresh_token:
            try:
                await self._token_request(
                    client,
                    {"grant_type": "refresh_token", "refresh_token": self._refresh_token},
                    via="refresh_token",
                )
                return
            except AuthError as error:
                # 刷新令牌过期/被吊销是常态；退回客户端凭据能继续跑，且原因进日志。
                self._refresh_token = ""
                self._audience_note = f"刷新令牌被拒（{error.message}），已改用客户端凭据重新取令牌"
        await self._acquire(client)

    # -- 发现 ---------------------------------------------------------------
    async def _resolve(self, client: httpx.AsyncClient) -> None:
        if self._token_endpoint:
            self._auth_method = (
                "client_secret_post"
                if self.config.auth_method == "post"
                else "client_secret_basic"
            )
            self._source = "configured"
            self._expected_audience = self._expected_audience or self._server_url
            self._resource = self._expected_audience
            return

        issuer, issuer_strict, reason = await self._discover_authorization_server(client)
        metadata = await self._discover_metadata(client, issuer)
        endpoint = metadata.get("token_endpoint")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise AuthError(
                "mcp_auth_discovery",
                f"授权服务器元数据没有 token_endpoint：{_safe_url(issuer)}（{reason}）",
            )
        require_secure_url(endpoint, "令牌端点")
        declared_issuer = metadata.get("issuer")
        if issuer_strict and isinstance(declared_issuer, str) and declared_issuer.rstrip("/") != issuer.rstrip("/"):
            raise AuthError(
                "mcp_auth_discovery",
                f"授权服务器元数据自称 {_safe_url(declared_issuer)}，与发现到的 {_safe_url(issuer)} 不一致",
            )
        methods = metadata.get("token_endpoint_auth_methods_supported")
        supported = [item for item in methods if isinstance(item, str)] if isinstance(methods, list) else []
        preferred = next((item for item in AUTH_METHODS if item in supported), "")
        self._auth_method = (
            ("client_secret_basic" if self.config.auth_method == "basic" else "client_secret_post")
            if self.config.auth_method
            else preferred or "client_secret_basic"
        )
        self._token_endpoint = endpoint.strip()
        self._source = "discovery"
        self._expected_audience = self._expected_audience or self._resource or self._server_url
        self._resource = self._resource or self._expected_audience

    async def _discover_authorization_server(
        self, client: httpx.AsyncClient
    ) -> tuple[str, bool, str]:
        """返回 (issuer, 是否来自受保护资源元数据, 来源说明)。"""
        if self.config.resource_metadata_url:
            candidates = [self.config.resource_metadata_url]
        else:
            candidates = _well_known_urls(self._server_url, ("oauth-protected-resource",))
        payload, reason = await self._first_json(client, candidates, "受保护资源元数据")
        if payload is not None:
            resource = payload.get("resource")
            if isinstance(resource, str) and resource.strip():
                self._resource = resource.strip()
            servers = payload.get("authorization_servers")
            if isinstance(servers, list):
                for item in servers:
                    if isinstance(item, str) and item.strip():
                        issuer = item.strip()
                        require_secure_url(issuer, "授权服务器地址")
                        return issuer, True, "受保护资源元数据"
            reason = "受保护资源元数据没有 authorization_servers"
        # 有些服务端只发布 AS 元数据：按同源找一次，仍然要求 https/loopback 规则。
        # 这一路没有"发现到的 issuer"可以比对，所以 issuer_strict=False。
        return _origin(self._server_url), False, reason

    async def _discover_metadata(self, client: httpx.AsyncClient, issuer: str) -> dict:
        candidates = _well_known_urls(issuer, ("oauth-authorization-server",))
        parts = urlsplit(issuer)
        path = parts.path.rstrip("/")
        candidates.append(f"{_origin(issuer)}{path}/.well-known/openid-configuration")
        candidates.append(f"{_origin(issuer)}/.well-known/openid-configuration")
        payload, reason = await self._first_json(client, candidates, "授权服务器元数据")
        if payload is None:
            raise AuthError("mcp_auth_discovery", f"找不到授权服务器元数据：{reason}")
        return payload

    async def _first_json(
        self, client: httpx.AsyncClient, urls: list[str], what: str
    ) -> tuple[dict | None, str]:
        reasons: list[str] = []
        for url in urls:
            require_secure_url(url, what)
            try:
                response = await client.get(
                    url,
                    headers={"Accept": "application/json"},
                    timeout=DISCOVERY_TIMEOUT,
                )
            except httpx.HTTPError as error:
                reasons.append(f"{_safe_url(url)} 请求失败（{error.__class__.__name__}）")
                continue
            if response.status_code != 200:
                reasons.append(f"{_safe_url(url)} 返回 {response.status_code}")
                continue
            if len(response.content) > MAX_METADATA_BYTES:
                reasons.append(f"{_safe_url(url)} 元数据超过 {MAX_METADATA_BYTES} 字节")
                continue
            try:
                payload = response.json()
            except ValueError:
                reasons.append(f"{_safe_url(url)} 不是 JSON")
                continue
            if not isinstance(payload, dict):
                reasons.append(f"{_safe_url(url)} 不是 JSON 对象")
                continue
            return payload, ""
        detail = "；".join(reasons) if reasons else "没有候选地址"
        return None, f"{what}读取失败：{detail}"

    # -- 令牌 ---------------------------------------------------------------
    async def _acquire(self, client: httpx.AsyncClient) -> None:
        form: dict[str, str] = {"grant_type": SUPPORTED_GRANT}
        if self.config.scope:
            form["scope"] = self.config.scope
        if self._resource:
            # RFC 8707：让令牌绑定到这台 MCP 服务端，而不是"随便哪个资源都能用"
            form["resource"] = self._resource
        await self._token_request(client, form, via="client_credentials")

    async def _token_request(
        self, client: httpx.AsyncClient, form: dict[str, str], *, via: str
    ) -> None:
        if not self._token_endpoint:
            raise AuthError("mcp_auth_discovery", "没有令牌端点，无法取得 OAuth 令牌")
        data = dict(form)
        headers = {"Accept": "application/json"}
        auth: httpx.BasicAuth | None = None
        if self._auth_method == "client_secret_post":
            data["client_id"] = self.config.client_id
            data["client_secret"] = self.config.client_secret
        else:
            auth = httpx.BasicAuth(self.config.client_id, self.config.client_secret)
        try:
            response = await client.post(self._token_endpoint, data=data, headers=headers, auth=auth)
        except httpx.HTTPError as error:
            raise AuthError(
                "mcp_auth_transport",
                f"{self._name} 的令牌请求失败：{error.__class__.__name__}",
            ) from None
        if response.status_code >= 400:
            raise AuthError(
                "mcp_auth_token_endpoint",
                f"令牌端点返回 {response.status_code}：{self._error_detail(response)}",
            )
        try:
            payload = response.json()
        except ValueError:
            raise AuthError("mcp_auth_bad_token", "令牌端点返回的不是 JSON") from None
        if not isinstance(payload, dict):
            raise AuthError("mcp_auth_bad_token", "令牌端点返回的不是 JSON 对象")
        self._apply(payload, via)

    def _error_detail(self, response: httpx.Response) -> str:
        detail = ""
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            description = payload.get("error_description")
            if isinstance(error, str):
                detail = error
            if isinstance(description, str) and description:
                detail = f"{detail}（{description}）" if detail else description
        if not detail:
            detail = response.text[:200]
        return _redact(detail, self.config)

    def _apply(self, payload: dict, via: str) -> None:
        token = payload.get("access_token")
        if not isinstance(token, str) or not token.strip():
            raise AuthError("mcp_auth_bad_token", "令牌端点没有返回 access_token")
        token_type = str(payload.get("token_type") or "Bearer")
        if token_type.lower() != "bearer":
            raise AuthError(
                "mcp_auth_bad_token",
                f"只支持 Bearer 令牌（令牌端点返回 {token_type}）",
            )
        expires_in = payload.get("expires_in")
        try:
            seconds = float(expires_in)
        except (TypeError, ValueError):
            seconds = 0.0
        if seconds <= 0:
            seconds = 0.0

        claims = decode_jwt_claims(token)
        self._expiry_checked = False
        self._audience_checked = False
        self._audience_note = ""
        if claims is None:
            # opaque 令牌：受众与过期都只能由服务端判定，这一事实要在状态里说清。
            self._audience_note = "令牌不是 JWT：受众与过期无法在本机核对，只能靠服务端 401 驱动刷新"
        else:
            seconds = self._verify_claims(claims, seconds)
        self._token = token
        self._token_type = "Bearer"
        self._expires_at = time.time() + seconds if seconds else 0.0
        refresh = payload.get("refresh_token")
        self._refresh_token = refresh.strip() if isinstance(refresh, str) else ""
        self._source = self._source or via

    def _verify_claims(self, claims: dict, seconds: float) -> float:
        """核对 exp / aud，返回可用于调度刷新的有效秒数（令牌没写 expires_in 时取 exp-now）。"""
        now = time.time()
        exp = claims.get("exp")
        if isinstance(exp, (int, float)):
            self._expiry_checked = True
            if now >= float(exp):
                raise AuthError("mcp_auth_expired", "服务端返回的 JWT 已经过期（exp 早于本机当前时间）")
            if not seconds:
                seconds = max(1.0, float(exp) - now)
        if not self._expected_audience:
            self._audience_note = "配置与元数据都没有给出受众，JWT 的 aud 未核对"
            return seconds
        aud = claims.get("aud")
        if aud is None:
            raise AuthError(
                "mcp_auth_audience",
                f"JWT 没有 aud，无法确认令牌是发给 {_safe_url(self._expected_audience)} 的",
            )
        if not _audience_matches(aud, self._expected_audience):
            actual = aud if isinstance(aud, str) else json.dumps(aud, ensure_ascii=False)
            raise AuthError(
                "mcp_auth_audience",
                f"JWT 的 aud（{actual}）与期望的 {_safe_url(self._expected_audience)} 不一致",
            )
        self._audience_checked = True
        return seconds


def _redact(text: str, config: OAuthConfig) -> str:
    redacted = str(text or "")
    for secret in (config.client_secret, config.client_id):
        if secret:
            redacted = redacted.replace(secret, "***")
    return redacted


__all__ = [
    "AuthError",
    "OAuthConfig",
    "OAuthProvider",
    "decode_jwt_claims",
    "require_secure_url",
]
