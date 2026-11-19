"""主模型看不见图时，请一家能看的代看一次。

判据的重点不是"能调通"，而是三条纪律：只在看不了时调用、失败绝不编、每轮有上限。
"""

from __future__ import annotations

import types

import pytest

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.model_provider import ModelProvider
from blank_api.services.agent import vision_proxy
from blank_api.services.settings import SettingsStore

TEXT_PROVIDER = "prov-text"
EYE_PROVIDER = "prov-eye"


def _write_settings(**model) -> None:
    store = SettingsStore()
    current = store.read()
    current.setdefault("model", {})
    current["model"].update(model)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    import json

    store.path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
async def seeded():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(
            ModelProvider(
                id=TEXT_PROVIDER, name="纯文本", kind="openai_compatible",
                base_url="https://t.example/v1", api_key_encrypted="k",
                default_model="spark-x2.6", supports_vision=False,
            )
        )
        session.add(
            ModelProvider(
                id=EYE_PROVIDER, name="能看图", kind="openai_compatible",
                base_url="https://e.example/v1", api_key_encrypted="k",
                default_model="deepseek-flash", supports_vision=True,
            )
        )
        await session.commit()


async def test_it_picks_a_provider_that_can_actually_see(seeded):
    _write_settings(vision_provider="")
    provider = await vision_proxy._pick_provider(TEXT_PROVIDER)

    assert provider is not None and provider.id == EYE_PROVIDER


async def test_it_never_asks_the_blind_model_to_describe_a_picture(seeded):
    """主模型刚被判定看不见，再问它一次只是浪费一轮。"""
    _write_settings(vision_provider="")
    provider = await vision_proxy._pick_provider(EYE_PROVIDER)

    assert provider is None


async def test_an_explicit_choice_wins(seeded):
    _write_settings(vision_provider=TEXT_PROVIDER)  # 配错了：这家看不了图
    provider = await vision_proxy._pick_provider("")

    # 显式指定但看不了图 → 不能硬用，回落到能看的那家
    assert provider is not None and provider.id == EYE_PROVIDER


async def test_no_vision_provider_means_empty_not_invented(seeded):
    """一家能看的都没有时返回空串——绝不能编一段"看起来像图"的描述。"""
    _write_settings(vision_provider="")
    async with get_session_factory()() as session:
        provider = await session.get(ModelProvider, EYE_PROVIDER)
        provider.enabled = False
        await session.commit()

    text, by = await vision_proxy.describe({"media_type": "image/jpeg", "data": "AAAA"}, exclude_provider=TEXT_PROVIDER)
    assert (text, by) == ("", "")


async def test_an_empty_image_is_not_sent_anywhere(seeded):
    text, by = await vision_proxy.describe({"media_type": "image/jpeg", "data": ""})
    assert (text, by) == ("", "")


async def test_the_cap_stops_a_model_from_turning_screenshots_into_a_hobby(seeded, monkeypatch):
    _write_settings(vision_provider="")

    async def fake_describe(image, *, question="", exclude_provider=""):
        return "一段描述", "能看图"

    monkeypatch.setattr(vision_proxy, "describe", fake_describe)
    session = types.SimpleNamespace(supports_vision=False, provider_id=TEXT_PROVIDER, vision_proxy_calls=0)

    for _ in range(vision_proxy.MAX_PROXY_CALLS):
        text, _ = await vision_proxy.describe_for_session({"data": "AAAA"}, session=session, step=1)
        assert text
    assert session.vision_proxy_calls == vision_proxy.MAX_PROXY_CALLS

    text, by = await vision_proxy.describe_for_session({"data": "AAAA"}, session=session, step=1)
    assert (text, by) == ("", ""), "超过上限还在看图，等于给它一个无限花钱的按钮"


async def test_a_failed_call_degrades_instead_of_raising(seeded, monkeypatch):
    """代看失败只能降级：把工具调用带崩，等于让"看一眼"这个动作反噬主流程。"""
    _write_settings(vision_provider="")

    class Boom:
        async def invoke(self, messages):
            raise RuntimeError("网关 502")

    monkeypatch.setattr(vision_proxy, "build_adapter", lambda provider: Boom())

    text, by = await vision_proxy.describe({"media_type": "image/jpeg", "data": "AAAA"}, exclude_provider="")
    assert (text, by) == ("", "")


async def test_the_description_is_capped(seeded, monkeypatch):
    _write_settings(vision_provider="")

    class Verbose:
        async def invoke(self, messages):
            return types.SimpleNamespace(text="描述" * 3000)

    monkeypatch.setattr(vision_proxy, "build_adapter", lambda provider: Verbose())

    text, _ = await vision_proxy.describe({"media_type": "image/jpeg", "data": "AAAA"}, exclude_provider="")
    assert text and len(text) <= 2000, "代看的产出会原样进主模型上下文，不能没有上限"

