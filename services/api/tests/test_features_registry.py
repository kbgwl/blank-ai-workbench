"""特性开关注册表：一层一层的优先级，以及"拼错名字必须报错"。

这里不测某个能力的行为，只测开关本身：谁说了算、说清楚了没有、以及
写坏了一个开关之后系统会不会变成随机行为。
"""

import json

import pytest

from blank_api.services import features as registry
from blank_api.services.settings import DEFAULTS, SettingsStore, normalize


@pytest.fixture
def auth_settings(tmp_path, monkeypatch):
    """把设置文件指到本用例自己的目录，避免开关写进别的测试的夹具里。"""
    target = tmp_path / "settings.json"
    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(target))
    return target


def test_registry_entries_are_well_formed():
    seen = set()
    for flag in registry.FEATURES.values():
        assert flag.name not in seen, f"重复的开关名 {flag.name}"
        seen.add(flag.name)
        assert flag.stage in registry.STAGES, f"{flag.name} 的 stage 不合法"
        assert flag.summary.strip(), f"{flag.name} 缺 summary，设置页会渲染成空白"
        # beta 默认必须是关的：默认开着的实验能力会在升级时悄悄改变行为
        if flag.stage == registry.BETA:
            assert flag.default is False, f"{flag.name} 是 beta 却默认开启"
        if flag.stage == registry.DEPRECATED:
            assert flag.default is False, f"{flag.name} 已废弃却默认开启"


def test_default_when_nothing_is_configured():
    for flag in registry.FEATURES.values():
        assert registry.resolve(flag.name, {}) == (flag.default, registry.SRC_DEFAULT)


def test_env_beats_settings_file(auth_settings, monkeypatch):
    store = SettingsStore(auth_settings)
    store.update({"features": {"tool_search": True}})
    monkeypatch.setenv("BLANK_FEATURE_TOOL_SEARCH", "0")

    enabled, source = registry.resolve("tool_search", store.read())
    assert enabled is False
    assert source == registry.SRC_ENV


def test_env_accepts_written_out_booleans(auth_settings, monkeypatch):
    for text, expected in (("on", True), ("OFF", False), ("yes", True), ("no", False)):
        monkeypatch.setenv("BLANK_FEATURE_TOOL_SEARCH", text)
        assert registry.is_enabled("tool_search", {}) is expected


def test_unparsable_env_falls_through(monkeypatch):
    """环境变量写了个看不懂的值，应该当没设，而不是当关。"""
    monkeypatch.setenv("BLANK_FEATURE_HANDOFF_NOTES", "maybe")
    assert registry.resolve("handoff_notes", {}) == (True, registry.SRC_DEFAULT)


def test_settings_file_beats_legacy(auth_settings):
    store = SettingsStore(auth_settings)
    # 老字段说关，新字段说开 —— 新的是用户最近一次的选择
    store.write({**DEFAULTS, "tools": {"search": False}, "features": {"tool_search": True}})

    assert registry.resolve("tool_search", store.read()) == (True, registry.SRC_SETTINGS)


def test_legacy_path_still_works(auth_settings):
    """升级前只配过 tools.search 的用户，行为不能变。"""
    store = SettingsStore(auth_settings)
    store.write({**DEFAULTS, "tools": {"search": False}})

    assert registry.resolve("tool_search", store.read()) == (False, registry.SRC_LEGACY)


def test_unknown_name_raises_instead_of_silently_failing():
    with pytest.raises(registry.UnknownFeature):
        registry.is_enabled("tool_searchh", {})
    with pytest.raises(KeyError):
        registry.resolve("nope", {})


def test_describe_reports_value_and_source(auth_settings, monkeypatch):
    store = SettingsStore(auth_settings)
    store.update({"features": {"handoff_notes": False}})
    monkeypatch.setenv("BLANK_FEATURE_TOOL_SEARCH", "0")

    payload = registry.describe(store.read())
    by_name = {item["name"]: item for item in payload["features"]}

    assert by_name["handoff_notes"]["enabled"] is False
    assert by_name["handoff_notes"]["source"] == registry.SRC_SETTINGS
    assert by_name["tool_search"]["enabled"] is False
    assert by_name["tool_search"]["source"] == registry.SRC_ENV
    assert by_name["prompt_cache"]["source"] == registry.SRC_DEFAULT
    assert payload["env_prefix"] == "BLANK_FEATURE_"


def test_describe_surfaces_typos_in_the_file(auth_settings):
    store = SettingsStore(auth_settings)
    store.update({"features": {"tool_serach": True}})

    payload = registry.describe(store.read())
    # 拼错的键不会生效，但必须被看见，否则"开了没反应"永远查不出来
    assert payload["unknown"] == ["tool_serach"]
    assert all(item["name"] != "tool_serach" for item in payload["features"])


def test_apply_patch_persists_and_reports(auth_settings):
    result = registry.apply_patch({"parallel_read_tools": "on"})
    by_name = {item["name"]: item for item in result["features"]}
    assert by_name["parallel_read_tools"]["enabled"] is True
    assert by_name["parallel_read_tools"]["source"] == registry.SRC_SETTINGS

    # 落盘了：换一个 store 实例读回来仍然是开
    assert SettingsStore(auth_settings).read()["features"]["parallel_read_tools"] is True


def test_apply_patch_rejects_the_whole_batch(auth_settings):
    with pytest.raises(registry.UnknownFeature):
        registry.apply_patch({"handoff_notes": False, "typo_flag": True})

    # 一条都不许落地：部分生效会让用户以为全开了
    assert SettingsStore(auth_settings).read()["features"] == {}


def test_apply_patch_rejects_non_boolean(auth_settings):
    with pytest.raises(ValueError):
        registry.apply_patch({"handoff_notes": "有时候"})
    assert SettingsStore(auth_settings).read()["features"] == {}


def test_apply_patch_keeps_other_sections(auth_settings):
    store = SettingsStore(auth_settings)
    store.update({"guardian": {"mode": "veto"}})

    registry.apply_patch({"tool_search": False})

    payload = SettingsStore(auth_settings).read()
    assert payload["guardian"]["mode"] == "veto"
    assert payload["features"]["tool_search"] is False


def test_normalize_keeps_only_interpretable_flags():
    merged = normalize({"features": {"a": "on", "b": 0, "c": "偶尔", "": True, "d": None}})
    assert merged["features"] == {"a": True, "b": False}


def test_update_does_not_filter_unknown_sections_keys(auth_settings):
    """features 段的键不在 DEFAULTS 里预置，不能被"已有键"过滤掉。"""
    store = SettingsStore(auth_settings)
    store.update({"features": {"brand_new_flag": True}})
    assert SettingsStore(auth_settings).read()["features"] == {"brand_new_flag": True}


async def test_features_endpoint_roundtrip(client, auth_settings):
    read = await client.get("/api/v1/settings/features")
    assert read.status_code == 200
    body = read.json()
    assert body["unknown"] == []
    names = [item["name"] for item in body["features"]]
    assert "tool_search" in names

    patched = await client.patch("/api/v1/settings/features", json={"handoff_notes": False})
    assert patched.status_code == 200
    by_name = {item["name"]: item for item in patched.json()["features"]}
    assert by_name["handoff_notes"]["enabled"] is False
    # 接口返回的就是规范化后的值，设置页不会显示一个运行时没用上的数
    assert json.loads(auth_settings.read_text(encoding="utf-8"))["features"]["handoff_notes"] is False


async def test_features_endpoint_rejects_unknown_name(client, auth_settings):
    response = await client.patch("/api/v1/settings/features", json={"no_such_flag": True})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_feature"
    assert "no_such_flag" in response.json()["error"]["message"]
