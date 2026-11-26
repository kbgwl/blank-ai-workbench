"""金额闸的设置入口：价目表与上限必须能被界面读到、写回。

金额计算本身在 `test_cost_budget_v427.py` 里钉过；这里只钉一件事——
`GET/PUT /settings/agent` 的返回体里真的带着 `budget`，而不是只存在于
`settings.json` 的原始文件里、界面永远看不到。
"""

from __future__ import annotations

from pathlib import Path

from blank_api.api.routes.settings import _render
from blank_api.schemas.settings import AgentSettingsPatch
from blank_api.services.settings import SettingsStore


def _store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(tmp_path / "settings.json")


def test_agent_settings_render_includes_budget(tmp_path: Path):
    store = _store(tmp_path)
    store.update(
        {
            "budget": {
                "cost_limit": 2.5,
                "pricing": {
                    "DeepSeek": {"input": 0.28, "cached_input": 0.028, "output": 0.42}
                },
            }
        }
    )

    rendered = _render(store.read(), store)

    assert rendered.budget.cost_limit == 2.5
    assert rendered.budget.pricing["deepseek"].input == 0.28
    assert rendered.budget.pricing["deepseek"].cached_input == 0.028
    assert rendered.budget.pricing["deepseek"].output == 0.42


def test_updating_the_limit_keeps_the_price_table(tmp_path: Path):
    store = _store(tmp_path)
    store.update(
        {"budget": {"pricing": {"default": {"input": 2, "cached_input": 0.5, "output": 8}}}}
    )

    store.update({"budget": {"cost_limit": 1.25}})

    payload = store.read()["budget"]
    assert payload["cost_limit"] == 1.25
    assert payload["pricing"]["default"]["output"] == 8, "只改上限不应把价目表清空"


def test_replacing_the_price_table_is_a_whole_section_write(tmp_path: Path):
    store = _store(tmp_path)
    store.update({"budget": {"pricing": {"default": {"input": 2}}}})

    store.update({"budget": {"pricing": {"deepseek": {"input": 0.28, "output": 0.42}}}})

    payload = store.read()["budget"]
    assert payload["pricing"] == {
        "deepseek": {"input": 0.28, "cached_input": 0.0, "output": 0.42}
    }


def test_patch_schema_accepts_budget():
    patch = AgentSettingsPatch.model_validate({"budget": {"cost_limit": 0.5}})

    assert patch.budget is not None
    assert patch.budget.cost_limit == 0.5
    assert patch.model_dump(exclude_none=True) == {"budget": {"cost_limit": 0.5}}


def test_default_is_unlimited_and_unpriced(tmp_path: Path):
    store = _store(tmp_path)

    rendered = _render(store.read(), store)

    assert rendered.budget.cost_limit == 0.0
    assert rendered.budget.pricing == {}
    assert rendered.limits["cost_limit"]["max"] == 1_000_000.0
