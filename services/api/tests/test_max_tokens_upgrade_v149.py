"""旧库里的 max_tokens=2048 要抬起来，否则整文件写入会被静默截断。

表现不是报错，是「工具参数偶尔是空的」：输出被切在 2048 token，流式 JSON 不完整，
参数解析成 ``{}``，于是回一句「缺少必填参数：patch」。用户只会看到它反复重来。

0.8.17 真机对照（同一句建站任务，deepseek-flash）：

| 上限 | 步数 | apply_patch 失败 | 产物 |
|---|---|---|---|
| 2048 | 28 | 10 次（6 次是空参数） | 14.7 KB |
| 8192 | 5 | 0 次 | 20.3 KB |

存量用户不会自己去改这一格，所以启动时补一次。这里守两件事：旧值要改，
**用户明确设过的值不能动**。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.model_provider import ModelProvider
from blank_api.services.models.registry import upgrade_truncating_providers


@pytest.fixture
async def db():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield get_session_factory()


async def _add_provider(provider_id: str, max_tokens: int) -> None:
    async with get_session_factory()() as session:
        session.add(
            ModelProvider(
                id=provider_id,
                name=f"vendor-{provider_id}",
                kind="openai_compatible",
                base_url="http://127.0.0.1:9/v1",
                api_key_encrypted="",
                default_model="m",
                max_tokens=max_tokens,
            )
        )
        await session.commit()


async def _read(provider_id: str) -> int:
    async with get_session_factory()() as session:
        return (
            await session.execute(
                select(ModelProvider.max_tokens).where(ModelProvider.id == provider_id)
            )
        ).scalar_one()


async def test_legacy_default_is_raised(db):
    await _add_provider("legacy", 2048)

    assert await upgrade_truncating_providers() == 1
    assert await _read("legacy") == 8192


async def test_custom_small_value_is_also_raised(db):
    """比旧默认更小的档位同样是截断，没有理由留着。"""
    await _add_provider("tiny", 512)

    assert await upgrade_truncating_providers() == 1
    assert await _read("tiny") == 8192


@pytest.mark.parametrize("value", [4096, 8192, 16384])
async def test_values_above_the_legacy_default_are_left_alone(db, value):
    """用户显式设过的不碰——他可能就是按自己模型的上限填的。"""
    await _add_provider(f"custom-{value}", value)

    assert await upgrade_truncating_providers() == 0
    assert await _read(f"custom-{value}") == value


async def test_running_twice_changes_nothing_the_second_time(db):
    await _add_provider("legacy", 2048)

    assert await upgrade_truncating_providers() == 1
    assert await upgrade_truncating_providers() == 0
    assert await _read("legacy") == 8192


async def test_empty_database_is_not_an_error(db):
    assert await upgrade_truncating_providers() == 0
