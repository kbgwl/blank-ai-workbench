import time
import logging

logger = logging.getLogger(__name__)


class RateLimitMiddleware:
    """Simple per-client rate limiter using in-memory sliding window."""

    def __init__(self, app, rpm=120):
        self.app = app
        self.rpm = rpm
        self.counters: dict[str, list[float]] = {}
        self.max_records = rpm * 2

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
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
                "body": b'{"error":"rate_limit_exceeded","message":"Too many requests"}',
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