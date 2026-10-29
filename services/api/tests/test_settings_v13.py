"""运行时设置：持久化、夹取（clamp）与真被内核读取。

设置页最容易出的两个问题都不是界面问题：

1. **显示的和生效的不是一回事**：所以 PUT 返回的是归一化后的值，这里断言夹取结果；
2. **改了没人读**：所以最后一组测试直接构造运行时作用域，检查权限模式、水位与工具加载是否真的变了。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blank_api.services.agent.context import ContextCompactor
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.guardian import Guardian
from blank_api.services.models.base import AdapterConfig, ChatMessage, ModelAdapter, ModelResponse, Usage
from blank_api.services.settings import DEFAULTS, SettingsStore, normalize


class TinyAdapter(ModelAdapter):
    def __init__(self) -> None:
        super().__init__(
            AdapterConfig(
                name="tiny",
                kind="openai",
                base_url="http://localhost",
                api_key="",
                model="tiny",
                context_window=32_000,
                supports_tools=True,
            )
        )

    async def invoke(self, messages, **kwargs) -> ModelResponse:  # pragma: no cover
        return ModelResponse(text="{}", usage=Usage(1, 1), model="tiny")

    async def stream(self, messages, **kwargs):  # pragma: no cover
        yield ""

    async def bind_tools(self, specs, messages, **kwargs) -> ModelResponse:  # pragma: no cover
        return ModelResponse(text="ok", usage=Usage(1, 1), model="tiny")


@pytest.fixture
def store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(tmp_path / "settings.json")


# --------------------------------------------------------------------------- #
# 1. 归一化：任何输入都不能把水位推到危险位置
# --------------------------------------------------------------------------- #
def test_defaults_are_the_conservative_ones():
    payload = normalize({})
    assert payload["guardian"]["mode"] == "off", "审查默认关：开审查要用户自己选，不能升级后悄悄加延迟"
    assert payload["compaction"]["trigger_ratio"] == 0.82
    assert payload["compaction"]["reserve_tokens"] == 16_384
    assert payload["permissions"]["default_mode"] == "default"
    assert payload["tools"]["search"] is True


def test_out_of_range_numbers_are_clamped_not_rejected():
    payload = normalize(
        {
            "compaction": {"trigger_ratio": 3, "reserve_tokens": 10},
            "guardian": {"timeout_seconds": 999},
        }
    )
    assert payload["compaction"]["trigger_ratio"] == 0.95
    assert payload["compaction"]["reserve_tokens"] == 1_024
    assert payload["guardian"]["timeout_seconds"] == 60.0

    payload = normalize({"compaction": {"trigger_ratio": 0.01}, "guardian": {"timeout_seconds": -5}})
    assert payload["compaction"]["trigger_ratio"] == 0.5
    assert payload["guardian"]["timeout_seconds"] == 1.0


def test_unknown_enum_values_fall_back_instead_of_breaking_the_runtime():
    payload = normalize(
        {"guardian": {"mode": "god"}, "permissions": {"default_mode": "yolo"}}
    )
    assert payload["guardian"]["mode"] == "off"
    assert payload["permissions"]["default_mode"] == "default"


def test_non_numeric_and_nan_are_ignored():
    payload = normalize({"compaction": {"trigger_ratio": "abc", "reserve_tokens": float("nan")}})
    assert payload["compaction"]["trigger_ratio"] == DEFAULTS["compaction"]["trigger_ratio"]
    assert payload["compaction"]["reserve_tokens"] == DEFAULTS["compaction"]["reserve_tokens"]


# --------------------------------------------------------------------------- #
# 2. 持久化：局部更新不能抹掉别的段
# --------------------------------------------------------------------------- #
def test_partial_update_keeps_other_sections(store: SettingsStore):
    store.write({"guardian": {"mode": "veto"}, "permissions": {"default_mode": "accept_edits"}})
    store.update({"compaction": {"trigger_ratio": 0.9}})
    payload = store.read()
    assert payload["guardian"]["mode"] == "veto", "只改水位不该把审查模式重置"
    assert payload["permissions"]["default_mode"] == "accept_edits"
    assert payload["compaction"]["trigger_ratio"] == 0.9


def test_settings_survive_a_new_process(store: SettingsStore):
    store.write({"guardian": {"mode": "auto", "timeout_seconds": 3}})
    reloaded = SettingsStore(store.path).read()
    assert reloaded["guardian"] == {"mode": "auto", "timeout_seconds": 3.0}


def test_corrupt_file_falls_back_to_defaults(store: SettingsStore):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ 这不是 JSON", encoding="utf-8")
    assert store.read() == normalize({})


def test_reset_restores_defaults(store: SettingsStore):
    store.write({"guardian": {"mode": "auto"}})
    assert store.reset()["guardian"]["mode"] == "off"


def test_file_is_written_atomically_as_utf8(store: SettingsStore):
    store.write({"permissions": {"default_mode": "plan"}})
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert raw["permissions"]["default_mode"] == "plan"
    assert not store.path.with_suffix(".tmp").exists(), "临时文件必须已经换名，不能留在旁边"


# --------------------------------------------------------------------------- #
# 3. 路由：返回的是真的生效值
# --------------------------------------------------------------------------- #
async def test_settings_route_round_trip(client, store, monkeypatch):  # noqa: F811
    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(store.path))
    # 先把目标文件写成默认值，让「搬迁老位置」那条路无从发生。
    # `_path()` 在目标不存在时会把 cwd/data/settings.json 搬过来（桌面版升级不丢设置的正路），
    # 于是这台机器上跑过桌面版之后，下面那条 `== "off"` 就会读到别人配的 auto。
    # 这条用例量的是「路由往返」，不是「搬迁」，所以先落一份默认值再发请求。
    SettingsStore(store.path).reset()
    initial = await client.get("/api/v1/settings/agent")
    assert initial.status_code == 200
    assert initial.json()["guardian"]["mode"] == "off"
    assert initial.json()["limits"]["trigger_ratio"]["max"] == 0.95

    updated = await client.put(
        "/api/v1/settings/agent",
        json={"guardian": {"mode": "veto"}, "compaction": {"trigger_ratio": 9}},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["guardian"]["mode"] == "veto"
    # 越界的值被夹到边界，界面显示的就是生效值
    assert body["compaction"]["trigger_ratio"] == 0.95
    assert body["path"] == str(store.path)

    reset = await client.post("/api/v1/settings/agent/reset")
    assert reset.status_code == 200
    assert reset.json()["guardian"]["mode"] == "off"


# --------------------------------------------------------------------------- #
# 4. Guardian 读设置：设置页优先，环境变量只在没有设置文件时兜底
# --------------------------------------------------------------------------- #
def test_guardian_follows_settings_file(store: SettingsStore, monkeypatch):
    monkeypatch.setenv("BLANK_GUARDIAN", "auto")  # 故意留一个冲突的环境变量
    store.write({"guardian": {"mode": "veto", "timeout_seconds": 4}})
    guardian = Guardian.from_settings(TinyAdapter(), settings=store.read(), store=store)
    assert guardian.mode == "veto", "有设置文件时以设置页为准"
    assert guardian.timeout == 4.0
    assert guardian.enabled


def test_guardian_off_means_no_adapter(store: SettingsStore):
    store.write({"guardian": {"mode": "off"}})
    guardian = Guardian.from_settings(TinyAdapter(), settings=store.read(), store=store)
    assert guardian.mode == "off"
    assert guardian.adapter is None and not guardian.enabled


def test_guardian_falls_back_to_env_without_a_settings_file(tmp_path: Path, monkeypatch):
    missing = SettingsStore(tmp_path / "nope" / "settings.json")
    monkeypatch.setenv("BLANK_GUARDIAN", "veto")
    guardian = Guardian.from_settings(TinyAdapter(), store=missing)
    assert guardian.mode == "veto" and guardian.enabled

    monkeypatch.delenv("BLANK_GUARDIAN")
    assert Guardian.from_settings(TinyAdapter(), store=missing).enabled is False


# --------------------------------------------------------------------------- #
# 5. 运行时就绪检查：设置真的改变内核行为
# --------------------------------------------------------------------------- #
async def _scope(workspace: Path, store: SettingsStore, settings: dict, monkeypatch):
    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(store.path))
    store.write(settings)
    runtime = AgentRuntime(session_factory=None)
    request = RunRequest(
        task_id="task-settings",
        provider_id="p",
        provider_name="tiny",
        adapter=TinyAdapter(),
        root=workspace,
        goal="检查设置是否生效",
        max_steps=5,
    )
    return await runtime._prepare(request, "task-settings:1")


async def test_default_permission_mode_comes_from_settings(workspace: Path, store: SettingsStore, monkeypatch):
    scope = await _scope(workspace, store, {"permissions": {"default_mode": "accept_edits"}}, monkeypatch)
    assert scope.session.permission_mode == "accept_edits"


async def test_water_line_is_configured_from_settings(workspace: Path, store: SettingsStore, monkeypatch):
    scope = await _scope(
        workspace,
        store,
        {"compaction": {"trigger_ratio": 0.6, "reserve_tokens": 4_096}},
        monkeypatch,
    )
    compactor = scope.session.compactor
    assert isinstance(compactor, ContextCompactor)
    # 适配器声明了 32k 窗口：水位按 60% 与"窗口减预留"取小
    assert compactor.context_window == 32_000
    assert compactor.trigger_tokens() == 19_200


async def test_turning_off_tool_search_shows_every_tool(workspace: Path, store: SettingsStore, monkeypatch):
    scope = await _scope(workspace, store, {"tools": {"search": False}}, monkeypatch)
    registry = scope.session.gateway.registry
    assert "tool_search" not in registry.visible_names()
    assert set(registry.visible_names()) == set(scope.session.gateway.tools)


async def test_tool_search_on_by_default_keeps_git_hidden(workspace: Path, store: SettingsStore, monkeypatch):
    scope = await _scope(workspace, store, {}, monkeypatch)
    registry = scope.session.gateway.registry
    assert "tool_search" in registry.visible_names()
    assert "git" not in registry.visible_names()


async def test_guardian_mode_reaches_the_session(workspace: Path, store: SettingsStore, monkeypatch):
    scope = await _scope(workspace, store, {"guardian": {"mode": "auto"}}, monkeypatch)
    assert scope.session.guardian.mode == "auto"
    assert scope.session.guardian.enabled
