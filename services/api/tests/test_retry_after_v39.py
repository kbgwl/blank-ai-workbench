"""#57：429 说「多久后再来」时要真的等那么久，压缩那条路也一样。

#56 的退避只按 0.5s/1s 递增——供应商明说 `Retry-After: 3`，客户端照样 0.5 秒后
就去敲门。压缩走 `adapter.invoke`，一次 429 直接把整轮判死。这一格钉两件事：

1. 等待取「指数退避」与 `Retry-After` 的较大者，30s 封顶；表头支持整秒与 HTTP 日期。
2. 压缩路径复用同一套重试、等待与事件，且事件上能读到供应商给的原值。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from blank_api.db import get_session_factory
from blank_api.models.task import AgentEvent
from blank_api.services.agent import graph
from blank_api.services.models.base import (
    ChatMessage,
    ModelError,
    ModelResponse,
    Usage,
    retry_after_seconds,
)

from tests.test_stream_output import (  # noqa: F401
    TOOLS,
    StreamingAdapter,
    _session,
    seeded,
)

SUMMARY_JSON = (
    '{"goal":"改 main.py","done":"读了文件","blockers":"无",'
    '"next":"跑测试","evidence":"pytest 3 passed"}'
)


class RetryAfterAdapter(StreamingAdapter):
    """按次数抛带 `Retry-After` 的 429；流式与整包两条路各记各的调用数。"""

    def __init__(
        self,
        name: str = "m",
        *,
        retry_after: float = 0.0,
        failures: int = 1,
    ) -> None:
        super().__init__(name, pieces=())
        self.retry_after = retry_after
        self.failures = failures
        self.stream_calls = 0
        self.invoke_calls = 0

    def _limit(self) -> ModelError:
        return ModelError(
            "model_http_error", "模型返回 429（限流）", status=429, retry_after=self.retry_after
        )

    async def bind_tools_stream(self, tools, messages, *, on_delta=None, **kwargs):
        self.stream_calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise self._limit()
        if on_delta is not None:
            await on_delta("好")
        return ModelResponse(text="好", usage=Usage(prompt_tokens=5), model=self.config.name)

    async def invoke(self, messages, **kwargs):
        self.invoke_calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise self._limit()
        return ModelResponse(
            text=SUMMARY_JSON,
            usage=Usage(prompt_tokens=8, completion_tokens=4),
            model=self.config.name,
            response_id=f"resp_compact_{self.invoke_calls}",
        )


@pytest.fixture
def sleep_spy(monkeypatch):
    """换掉图模块里的 asyncio，让退避只记账不真等。"""
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(
        graph,
        "asyncio",
        SimpleNamespace(sleep=fake_sleep, to_thread=asyncio.to_thread, gather=asyncio.gather),
    )
    return waits


async def _retry_rows() -> list[tuple[str, dict]]:
    """事件里的 node 是列不是 payload：判「哪条路重试了」必须读列。"""
    async with get_session_factory()() as session:
        rows = (await session.execute(AgentEvent.__table__.select())).fetchall()
    return [(row.node, row.payload) for row in rows if row.type == "model.rate_limit_retry"]


# ------------------------------------------------------------- 表头怎么读
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("3", 3.0),
        ("0", 0.0),
        (" 2.5 ", 2.5),
        ("-5", 0.0),
        ("soon", 0.0),
        ("", 0.0),
        (None, 0.0),
    ],
)
def test_retry_after_seconds_reads_plain_numbers(header: str | None, expected: float) -> None:
    assert retry_after_seconds(header) == expected


def test_retry_after_seconds_reads_http_dates_against_a_fixed_clock() -> None:
    now = datetime(2026, 10, 23, 8, 0, 0, tzinfo=timezone.utc).timestamp()
    assert retry_after_seconds("Fri, 23 Oct 2026 08:00:04 GMT", now=now) == 4.0
    assert retry_after_seconds("Fri, 23 Oct 2026 07:59:00 GMT", now=now) == 0.0, "过去的时间读成负数等于倒扣"


# ------------------------------------------------------------- 等待怎么算
def test_delay_takes_the_larger_of_backoff_and_retry_after() -> None:
    assert graph._rate_limit_delay(1, 0.0) == 0.5
    assert graph._rate_limit_delay(1, 2.0) == 2.0, "供应商说 2 秒，不能 0.5 秒就回去"
    assert graph._rate_limit_delay(2, 0.6) == 1.0, "Retry-After 比退避短时不该缩短自己的退避"


def test_delay_is_capped_so_the_ui_cannot_hang_forever() -> None:
    assert graph._rate_limit_delay(1, 120.0) == 30.0


# ------------------------------------------------------------- 接进流式路径
async def test_streaming_retry_waits_the_retry_after_and_records_it(seeded, tmp_path, sleep_spy):
    adapter = RetryAfterAdapter(retry_after=3.0)
    session = _session(tmp_path, adapter, stream=True)

    response = await session._call_adapter(
        adapter, TOOLS, [ChatMessage("user", "hi")], "execute_action", "coder"
    )

    assert response.text == "好"
    assert adapter.stream_calls == 2
    assert sleep_spy == [3.0], f"等成了 {sleep_spy}，没有听 Retry-After"
    rows = await _retry_rows()
    assert len(rows) == 1
    node, payload = rows[0]
    assert node == "execute_action"
    assert (
        payload["attempt"],
        payload["status"],
        payload["wait_ms"],
        payload["retry_after_ms"],
    ) == (1, 429, 3000, 3000)


async def test_absurd_retry_after_is_capped_but_the_original_value_stays_visible(
    seeded, tmp_path, sleep_spy
):
    adapter = RetryAfterAdapter(retry_after=120.0)
    session = _session(tmp_path, adapter, stream=True)

    response = await session._call_adapter(
        adapter, TOOLS, [ChatMessage("user", "hi")], "execute_action", "coder"
    )

    assert response.text == "好"
    assert sleep_spy == [30.0], "120 秒也照等，界面会像卡死"
    _, payload = (await _retry_rows())[0]
    assert (payload["wait_ms"], payload["retry_after_ms"]) == (30000, 120000), (
        "封顶只能封等待；供应商原话要留在事件里，否则事后分不清「它只要 3 秒」和「它要 120 秒」"
    )


# ------------------------------------------------------------- 压缩那条路
async def test_compaction_429_goes_through_the_same_backoff(seeded, tmp_path, sleep_spy):
    adapter = RetryAfterAdapter(retry_after=2.0)
    session = _session(tmp_path, adapter, stream=True)

    try:
        text = await session.summarize_history(
            [{"role": "user", "content": "把 main.py 的输入校验补上"}], ""
        )
    except ModelError as error:
        pytest.fail(f"压缩那一笔 429 直接判死了，没走退避：{error}")

    assert "目标：改 main.py" in text
    assert adapter.invoke_calls == 2, "压缩那一笔 429 直接判死了，没走退避"
    assert sleep_spy == [2.0]
    rows = await _retry_rows()
    assert len(rows) == 1
    node, payload = rows[0]
    assert (node, payload["role"], payload["attempt"]) == ("context_compact", "summarizer", 1)
    assert payload["retry_after_ms"] == 2000
    assert session.compaction_response_id == "resp_compact_2", "压缩记录该指认重试后成功的那一笔"
