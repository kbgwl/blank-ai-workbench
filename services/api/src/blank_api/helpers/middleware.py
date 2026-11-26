import json
import time
import logging

from starlette.datastructures import MutableHeaders

from blank_api.api.errors import RATE_LIMIT_CODE, error_body

logger = logging.getLogger(__name__)


class SecurityHeadersMiddleware:
    """给每个 HTTP 响应补最小安全头。

    本机服务也值得有：浏览器可能被诱导直接打开 ``/api/...`` 下的附件或工作区文件，
    没有 ``nosniff`` 时一个 ``text/html`` 附件就可能在 API 源上执行脚本。
    ``frame-ancestors`` 同时挡住点击劫持；CSP 只写这一条，避免把 /docs 弄坏。
    """

    _HEADERS = (
        ("x-content-type-options", "nosniff"),
        ("x-frame-options", "DENY"),
        ("referrer-policy", "no-referrer"),
        ("cross-origin-resource-policy", "same-origin"),
        ("content-security-policy", "frame-ancestors 'none'"),
    )

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in self._HEADERS:
                    headers.setdefault(key, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


# 探针与文档不进限流窗口。
#
# 桌面版的全部窗口都从 127.0.0.1 进来，共用同一个 per-IP 配额；健康探针每几秒
# 一次、前端每开一页又是十来个请求，120/min 的窗口会被正常使用打满，表现出来
# 就是「连 health 都 429、页面全是读取失败」——用户只会觉得软件坏了。限流的
# 目的是挡暴力脚本，不是罚正常前端，所以把探针、预检和文档放行。
EXEMPT_PATHS = frozenset({"/api/v1/health", "/health", "/docs", "/redoc", "/openapi.json"})


class RateLimitMiddleware:
    """Simple per-client rate limiter using in-memory sliding window."""

    def __init__(self, app, rpm=600):
        self.app = app
        self.rpm = rpm
        self.counters: dict[str, list[float]] = {}
        self.max_records = rpm * 2

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in EXEMPT_PATHS or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        key = client[0] if client else "unknown"
        now = time.time()
        requests = self.counters.get(key, [])

        cutoff = now - 60.0
        active = [t for t in requests if t > cutoff]
        active.append(now)

        if len(active) > self.max_records:
            active = active[-self.max_records:]

        self.counters[key] = active

        if len(active) > self.rpm:
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [(b"content-type", b"application/json"), (b"retry-after", b"30")],
            })
            await send({
                "type": "http.response.body",
                "body": json.dumps(
                    error_body(RATE_LIMIT_CODE, "请求过于频繁，请稍后再试"),
                    ensure_ascii=False,
                ).encode("utf-8"),
            })
            return

        await self.app(scope, receive, send)


class CircuitBreaker:
    """State-machine breaker for provider calls."""

    def __init__(self, threshold: int = 3, cooldown_s: int = 30):
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self.failures = 0
        self.last_failure = 0.0

    def succeed(self):
        self.failures = 0

    def fail(self):
        self.failures += 1
        self.last_failure = time.time()

    def allow(self) -> bool:
        if self.failures >= self.threshold:
            if time.time() - self.last_failure < self.cooldown_s:
                return False
            self.failures = 0
        return True


_provider_breakers: dict[str, CircuitBreaker] = {}


def breaker_for(name: str) -> CircuitBreaker:
    b = _provider_breakers.get(name)
    if b is None:
        _provider_breakers[name] = b = CircuitBreaker()
    return b