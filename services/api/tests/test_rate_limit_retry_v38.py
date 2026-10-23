"""#56：429 是「现在别问」，不是「这一轮废了」。

主流客户端在限流时会退避后再问。这一批只做两件事：给 429 两次短退避；
一个字都已经出去了就不再重试（否则用户会看到两遍开头）。5xx/超时/4xx 的口径不动。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blank_api.services.agent import graph
from blank_api.services.models.base import ChatMessage, ModelError, ModelResponse, Usage

from tests.test_stream_output import TOOLS, StreamingAdapter, _events, _session, seeded  # noqa: F401


class RateLimitAdapter(StreamingAdapter):
    """按次数抛 429，之后正常应答；整包与流式两条路各记各的调用数。"""

    def __init__(
        self,
        name: str = "m",
        *,
        failures: int = 1,
        status: int = 429,
        piece_before_failure: str = "",
    ) -> None:
        super().__init__(name, pieces=())
        self.failures = failures
        self.status = status
        self.piece_before_failure = piece_before_failure
        self.stream_calls = 0
        self.whole_calls = 0

    async def bind_tools(self, tools, messages, **kwargs):
        self.whole_calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise ModelError("model_http_error", f"模型返回 {self.status}（限流）", status=self.status)
        return ModelResponse(text="整包结果", usage=Usage(prompt_tokens=3), model=self.config.name)

    async def bind_tools_stream(self, tools, messages, *, on_delta=None, **kwargs):
        self.stream_calls += 1
        if self.failures > 0:
            if self.piece_before_failure and on_delta is not None:
                await on_delta(self.piece_before_failure)
            self.failures -= 1
            raise ModelError("model_http_error", f"模型返回 {self.status}（限流）", status=self.status)
        if on_delta is not None:
            await on_delta("好")
        return ModelResponse(text="好", usage=Usage(prompt_tokens=5), model=self.config.name)


@pytest.fixture
def no_wait(monkeypatch):
    # 判据关心的是"有没有退避重试"，不是真的等 1.5 秒；等待秒数由纯函数单独钉。
    monkeypatch.setattr(graph, "RATE_LIMIT_BASE_DELAY_SECONDS", 0.0)


async def test_one_429_is_retried_then_the_task_succeeds(seeded, tmp_path, no_wait):
    adapter = RateLimitAdapter(failures=1)
    session = _session(tmp_path, adapter, stream=True)

    response = await session._call_adapter(
        adapter, TOOLS, [ChatMessage("user", "hi")], "execute_action", "coder"
    )

    assert response.text == "好"
    assert adapter.stream_calls == 2, "第一次 429 之后没有重问"
    assert adapter.whole_calls == 0, "429 不该被当成「换形状」退路"
    retries = await _events("model.rate_limit_retry")
    assert [(row.get("attempt"), row.get("status"), row.get("wait_ms")) for row in retries] == [(1, 429, 0)]
    assert await _events("model.stream_fallback_skipped") == []


async def test_persistent_429_stops_after_two_retries_and_still_fails_loudly(seeded, tmp_path, no_wait):
    adapter = RateLimitAdapter(failures=99)
    session = _session(tmp_path, adapter, stream=True)

    with pytest.raises(ModelError) as caught:
        await session._call_adapter(adapter, TOOLS, [ChatMessage("user", "hi")], "execute_action", "coder")

    assert caught.value.status == 429
    assert adapter.stream_calls == graph.RATE_LIMIT_RETRIES + 1
    retries = await _events("model.rate_limit_retry")
    assert [row.get("attempt") for row in retries] == [1, 2]
    skipped = await _events("model.stream_fallback_skipped")
    assert len(skipped) == 1 and skipped[0].get("status") == 429
    assert await _events("model.stream_fallback") == []


async def test_429_after_the_first_delta_is_never_retried(seeded, tmp_path, no_wait):
    adapter = RateLimitAdapter(failures=1, piece_before_failure="一")
    session = _session(tmp_path, adapter, stream=True)

    with pytest.raises(ModelError):
        await session._call_adapter(adapter, TOOLS, [ChatMessage("user", "hi")], "execute_action", "coder")

    assert adapter.stream_calls == 1, "已经出字还重问，用户会看到两遍开头"
    assert await _events("model.rate_limit_retry") == []


async def test_non_stream_429_is_retried_too(seeded, tmp_path, no_wait):
    adapter = RateLimitAdapter(failures=1)
    session = _session(tmp_path, adapter, stream=False)

    response = await session._call_adapter(
        adapter, TOOLS, [ChatMessage("user", "hi")], "execute_action", "coder"
    )

    assert response.text == "整包结果"
    assert adapter.whole_calls == 2
    assert len(await _events("model.rate_limit_retry")) == 1


async def test_503_is_not_retried(seeded, tmp_path, no_wait):
    adapter = RateLimitAdapter(failures=99, status=503)
    session = _session(tmp_path, adapter, stream=True)

    with pytest.raises(ModelError) as caught:
        await session._call_adapter(adapter, TOOLS, [("user", "hi")], "execute_action", "coder")

    assert caught.value.status == 503
    assert adapter.stream_calls == 1, "5xx 的「现在不行」与 429 不是一回事，本批不放宽"
    assert await _events("model.rate_limit_retry") == []


def test_delay_doubles_and_retry_budget_is_two():
    error = ModelError("model_http_error", "429", status=429)
    assert graph._rate_limit_delay(1) == 0.5
    assert graph._rate_limit_delay(2) == 1.0
    assert graph._rate_limit_may_retry(error, 0) is True
    assert graph._rate_limit_may_retry(error, 1) is True
    assert graph._rate_limit_may_retry(error, 2) is False
    assert graph._rate_limit_may_retry(ModelError("model_http_error", "503", status=503), 0) is False
    assert graph._rate_limit_may_retry(ModelError("model_timeout", "慢"), 0) is False
