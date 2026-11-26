"""供应商列表带 is_default：界面显示「默认模型」时不再按列表第一项猜。

真实问题：设置里锁的是星火，界面却写 deepseek-flash——用户以为在用 DeepSeek，
任务实际跑的是另一家。默认供应商由后端解析，列表就该如实标出来。
"""
from __future__ import annotations

import json

from blank_api.db import get_session_factory
from blank_api.models.model_provider import ModelProvider
from blank_api.services.settings import SettingsStore

API = "/api/v1"


def _write_default(provider_id: str) -> None:
    store = SettingsStore()
    data = store.read()
    data.setdefault("model", {})["default_provider"] = provider_id
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


async def _seed() -> None:
    async with get_session_factory()() as session:
        session.add(
            ModelProvider(
                id="p-ds", name="deepseek", kind="openai_compatible",
                base_url="https://api.deepseek.com/v1", api_key_encrypted="k",
                default_model="deepseek-flash",
            )
        )
        session.add(
            ModelProvider(
                id="p-spark", name="讯飞星火", kind="openai_compatible",
                base_url="https://spark.example/v2", api_key_encrypted="k",
                default_model="spark-x2.6",
            )
        )
        await session.commit()


async def test_list_marks_the_configured_default(client):
    await _seed()
    _write_default("p-spark")

    rows = {row["id"]: row for row in (await client.get(f"{API}/providers")).json()}

    assert rows["p-spark"]["is_default"] is True
    assert rows["p-ds"]["is_default"] is False


async def test_without_a_configured_default_nobody_is_marked(client):
    await _seed()
    _write_default("")

    rows = (await client.get(f"{API}/providers")).json()

    assert all(row["is_default"] is False for row in rows)


async def test_put_default_provider_sets_and_clears(client):
    await _seed()
    _write_default("")

    saved = await client.put(f"{API}/providers/default", json={"provider_id": "p-spark"})
    assert saved.status_code == 200
    assert saved.json()["default_provider"] == "p-spark"
    rows = {row["id"]: row for row in (await client.get(f"{API}/providers")).json()}
    assert rows["p-spark"]["is_default"] is True

    cleared = await client.put(f"{API}/providers/default", json={"provider_id": ""})
    assert cleared.status_code == 200
    rows = (await client.get(f"{API}/providers")).json()
    assert all(row["is_default"] is False for row in rows)


async def test_put_default_provider_rejects_missing_key_or_disabled(client):
    await _seed()
    async with get_session_factory()() as session:
        session.add(
            ModelProvider(
                id="p-nokey", name="no-key", kind="openai_compatible",
                base_url="https://nokey.example/v1", api_key_encrypted="",
                default_model="m",
            )
        )
        await session.commit()

    missing = await client.put(f"{API}/providers/default", json={"provider_id": "p-missing"})
    assert missing.status_code == 404
    no_key = await client.put(f"{API}/providers/default", json={"provider_id": "p-nokey"})
    assert no_key.status_code == 409

    async with get_session_factory()() as session:
        provider = await session.get(ModelProvider, "p-spark")
        provider.enabled = False
        await session.commit()
    disabled = await client.put(f"{API}/providers/default", json={"provider_id": "p-spark"})
    assert disabled.status_code == 409
