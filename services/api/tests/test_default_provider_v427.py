"""默认模型可以被显式锁定。

原来的默认是「有 key 的第一个」——按名字排序决定。装了多家之后，想换一家就得改排序
或把别家停用，两种做法都会连带影响别的判断；而默认模型恰恰是这条链路上影响结果
最大的变量，不该由字符串序决定。

判据覆盖三件事：显式指定的生效、指向坏值时不把任务挡死、显式请求仍然优先。
"""

from __future__ import annotations

import json

import pytest

from blank_api.api.routes.tasks import _resolve_provider
from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.model_provider import ModelProvider
from blank_api.models.project import Project
from blank_api.models.task import Task
from blank_api.services.settings import SettingsStore


def _write_settings(**model) -> None:
    store = SettingsStore()
    current = store.read()
    current.setdefault("model", {})
    current["model"].update(model)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
async def seeded():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="p1", name="项目"))
        # 名字排序：AAA 在前。没有显式默认时，它就是被选中的那个。
        session.add(
            ModelProvider(
                id="prov-aaa", name="AAA", kind="openai_compatible",
                base_url="https://a.example/v1", api_key_encrypted="k", default_model="m-a",
            )
        )
        session.add(
            ModelProvider(
                id="prov-zzz", name="ZZZ", kind="openai_compatible",
                base_url="https://z.example/v1", api_key_encrypted="k", default_model="m-z",
            )
        )
        session.add(Task(id="t1", project_id="p1", title="t", goal="g"))
        await session.commit()


async def _pick(task_id: str = "t1", requested: str | None = None) -> str:
    async with get_session_factory()() as session:
        task = await session.get(Task, task_id)
        provider = await _resolve_provider(session, task, requested)
        return provider.id


async def test_without_a_default_it_is_still_the_first_by_name(seeded):
    _write_settings(default_provider="")
    assert await _pick() == "prov-aaa"


async def test_the_configured_default_wins_over_name_order(seeded):
    _write_settings(default_provider="prov-zzz")
    assert await _pick() == "prov-zzz"


async def test_an_explicit_request_beats_the_default(seeded):
    """单次任务显式指定时，绝不能被"默认"顶掉。"""
    _write_settings(default_provider="prov-zzz")
    assert await _pick(requested="prov-aaa") == "prov-aaa"


async def test_a_bad_default_does_not_block_everything(seeded):
    """设置里写了个不存在的 id：回落而不是 404。

    否则"填错一个字"就变成"任何任务都跑不了"，这比没有默认更坏。
    """
    _write_settings(default_provider="prov-does-not-exist")
    assert await _pick() == "prov-aaa"


async def test_a_disabled_default_falls_back_too(seeded):
    _write_settings(default_provider="prov-zzz")
    async with get_session_factory()() as session:
        provider = await session.get(ModelProvider, "prov-zzz")
        provider.enabled = False
        await session.commit()
    assert await _pick() == "prov-aaa"


def test_the_setting_is_trimmed_and_length_capped():
    from blank_api.services.settings import normalize

    assert normalize({"model": {"default_provider": "  abc  "}})["model"]["default_provider"] == "abc"
    assert normalize({"model": {"default_provider": "x" * 80}})["model"]["default_provider"] == ""
    assert normalize({})["model"]["default_provider"] == ""
