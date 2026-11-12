"""S5：设置层级（用户 < 项目 < 本机 < 管理策略）。

对齐 Claude Code 的 settings 级联：

1. **路径**：用户层 ``BLANK_SETTINGS_FILE``；项目层
   ``<workspace>/.blank/settings.json``；本机层
   ``<workspace>/.blank/settings.local.json``；管理层
   ``BLANK_MANAGED_SETTINGS_FILE``（没配就没有这一层）。
2. **叠加**：从低到高，高一层只覆盖它写了的键；嵌套对象逐键合并，数组与
   标量整段替换。
3. **安全段保护**：``safety`` / ``permissions`` / ``sandbox`` / ``guardian`` /
   ``features`` 只认用户层与管理层。项目/本机文件写了也进 ``ignored``，
   有效值不变——否则克隆一个仓库就等于一次提权。
4. **坏层不拖垮其余层**：坏 JSON、顶层不是对象、超过 1 MiB 都只跳过该层，
   错误进 ``errors``。
5. **可观察**：运行时落 ``settings.layers`` 事件（层、来源、ignored，不含
   设置值）；``GET /projects/{id}/settings`` 返回生效值与来源。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blank_api.db import get_session_factory
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.models.registry import build_adapter
from blank_api.services.settings import DEFAULTS, SettingsStore, normalize
from blank_api.services.settings_layers import (
    LAYER_LOCAL,
    LAYER_MANAGED,
    LAYER_PROJECT,
    LAYER_USER,
    MAX_FILE_BYTES,
    load_layered_settings,
)

from tests.test_agent_v04 import (  # noqa: F401  (env 等是夹具/工具，故意导入)
    API,
    create_project,
    create_provider,
    create_task,
    env,
)


@pytest.fixture(autouse=True)
def _no_managed_layer(monkeypatch: pytest.MonkeyPatch):
    """本机环境变量不能给判据加一层：每条用例自己决定要不要管理层。"""
    monkeypatch.delenv("BLANK_MANAGED_SETTINGS_FILE", raising=False)


# --------------------------------------------------------------------------- #
# 帮助
# --------------------------------------------------------------------------- #
def _write(path: Path, data: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    return path


def _write_layer(root: Path, name: str, data: object) -> Path:
    return _write(root / ".blank" / name, data)


def _layered(
    root: Path | str | None,
    tmp_path: Path,
    *,
    user: dict | None = None,
    managed: dict | None = None,
):
    user_path = tmp_path / "user-settings.json"
    if user is not None:
        _write(user_path, user)
    env_map: dict[str, str] = {}
    if managed is not None:
        managed_path = _write(tmp_path / "managed-settings.json", managed)
        env_map["BLANK_MANAGED_SETTINGS_FILE"] = str(managed_path)
    return load_layered_settings(root, user_path=user_path, env=env_map)


def _ignored(result) -> list[dict]:
    return [item for layer in result.layers for item in layer.ignored]


def _messages_text(messages) -> str:
    parts = []
    for item in messages:
        if isinstance(item, dict):
            parts.append(str(item.get("content") or ""))
        else:
            parts.append(str(getattr(item, "content", "") or ""))
    return "\n".join(parts)


def _main_request(env):
    for request in env.mock.requests:
        if "你是空白格 AI 操作台的执行 Agent" in _messages_text(request.get("messages") or []):
            return request
    raise AssertionError("没有找到主代理的请求")


def _tool_names(env) -> list[str]:
    return [
        str((item.get("function") or {}).get("name") or "")
        for item in _main_request(env).get("tools") or []
    ]


async def _provider_row(provider_id: str):
    from blank_api.models.model_provider import ModelProvider

    async with get_session_factory()() as session:
        return await session.get(ModelProvider, provider_id)


async def _run(env, workspace: Path, goal: str = "按项目设置做事"):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["完成"])
    env.mock.reset([{"content": "看完了"}, {"content": "任务完成"}])
    evidence = await AgentRuntime(get_session_factory()).run(
        RunRequest(
            task_id=task_id,
            provider_id=provider_id,
            provider_name="mock-vendor",
            adapter=build_adapter(await _provider_row(provider_id), env.mock.factory),
            root=workspace,
            goal=goal,
            acceptance_criteria=["完成"],
            max_steps=3,
        )
    )
    return evidence, _messages_text(_main_request(env).get("messages") or [])


def _layers_event(evidence) -> dict:
    rows = [item for item in evidence["timeline"] if item["type"] == "settings.layers"]
    assert rows, [item["type"] for item in evidence["timeline"]]
    return rows[0]["payload"]


# --------------------------------------------------------------------------- #
# 1. 路径与顺序
# --------------------------------------------------------------------------- #
def test_four_layers_stack_from_user_to_managed(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    _write_layer(root, "settings.json", {"output_style": "project"})
    _write_layer(root, "settings.local.json", {"output_style": "local"})

    result = _layered(
        root,
        tmp_path,
        user={"output_style": "user", "tools": {"search": False}},
        managed={"output_style": "managed"},
    )

    assert result.settings["output_style"] == "managed"
    assert result.settings["tools"]["search"] is False
    assert result.sources["output_style"] == LAYER_MANAGED
    assert result.sources["tools"] == LAYER_USER
    assert [layer.name for layer in result.layers] == [
        LAYER_USER,
        LAYER_PROJECT,
        LAYER_LOCAL,
        LAYER_MANAGED,
    ]
    assert result.changed() is True


def test_project_overrides_user_and_local_overrides_project(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    project_file = _write_layer(root, "settings.json", {"output_style": "project"})
    local_file = _write_layer(root, "settings.local.json", {"output_style": "local"})

    assert _layered(root, tmp_path, user={"output_style": "user"}).settings["output_style"] == "local"

    local_file.unlink()
    assert (
        _layered(root, tmp_path, user={"output_style": "user"}).settings["output_style"]
        == "project"
    )

    project_file.unlink()
    assert _layered(root, tmp_path, user={"output_style": "user"}).settings["output_style"] == "user"


def test_nested_objects_merge_key_by_key(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    _write_layer(root, "settings.json", {"compaction": {"trigger_ratio": 0.6}})

    result = _layered(
        root,
        tmp_path,
        user={"compaction": {"trigger_ratio": 0.9, "reserve_tokens": 4096}},
    )

    assert result.settings["compaction"] == {"trigger_ratio": 0.6, "reserve_tokens": 4096}
    assert result.sources["compaction"] == LAYER_PROJECT


def test_arrays_are_replaced_not_concatenated(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()

    result = _layered(
        root,
        tmp_path,
        user={"safety": {"allowed_roots": ["a", "b"]}},
        managed={"safety": {"allowed_roots": ["c"]}},
    )

    assert result.settings["safety"]["allowed_roots"] == ["c"]


def test_missing_workspace_means_only_the_user_layer(tmp_path: Path) -> None:
    for workspace in (None, ""):
        result = _layered(workspace, tmp_path, user={"output_style": "user"})
        assert result.settings["output_style"] == "user"
        project = next(layer for layer in result.layers if layer.name == LAYER_PROJECT)
        local = next(layer for layer in result.layers if layer.name == LAYER_LOCAL)
        assert project.path is None and local.path is None
        assert result.changed() is False


def test_without_any_layer_the_effective_values_are_defaults(tmp_path: Path) -> None:
    result = _layered(None, tmp_path)

    assert result.settings == normalize({})
    assert result.sources == {}
    assert result.warnings == [] and result.errors == []
    assert result.changed() is False


# --------------------------------------------------------------------------- #
# 2. 安全段保护
# --------------------------------------------------------------------------- #
def test_project_layer_cannot_touch_security_sections(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    _write_layer(
        root,
        "settings.json",
        {
            "guardian": {"mode": "off"},
            "permissions": {"default_mode": "bypass"},
            "sandbox": {"default_tier": "off"},
            "safety": {"shell_enabled": True, "restrict_to_workspace": False},
            "features": {"tool_search": False},
            "output_style": "project",
        },
    )

    result = _layered(root, tmp_path, user={"guardian": {"mode": "veto"}})

    assert result.settings["guardian"]["mode"] == "veto"
    assert result.settings["permissions"]["default_mode"] == "default"
    assert result.settings["sandbox"]["default_tier"] == "workspace"
    assert result.settings["safety"]["shell_enabled"] is False
    assert result.settings["safety"]["restrict_to_workspace"] is True
    assert result.settings["features"] == {}
    # 非安全段照常生效，证明整份文件没有被一票否决
    assert result.settings["output_style"] == "project"
    ignored = {item["section"] for item in _ignored(result)}
    assert ignored == {"guardian", "permissions", "sandbox", "safety", "features"}
    assert all(item["reason"] for item in _ignored(result))
    assert result.sources["guardian"] == LAYER_USER


def test_local_layer_cannot_touch_security_sections(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    _write_layer(root, "settings.local.json", {"permissions": {"default_mode": "bypass"}})

    result = _layered(root, tmp_path)

    assert result.settings["permissions"]["default_mode"] == "default"
    assert {item["section"] for item in _ignored(result)} == {"permissions"}


def test_managed_layer_may_pin_security_sections(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()

    result = _layered(
        root,
        tmp_path,
        user={"safety": {"shell_enabled": False}},
        managed={
            "permissions": {"default_mode": "plan"},
            "safety": {"shell_enabled": True},
        },
    )

    assert result.settings["permissions"]["default_mode"] == "plan"
    assert result.settings["safety"]["shell_enabled"] is True
    assert result.sources["safety"] == LAYER_MANAGED
    assert result.sources["permissions"] == LAYER_MANAGED


def test_unknown_section_is_ignored_but_does_not_drop_the_file(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    _write_layer(root, "settings.json", {"nonsense": {"x": 1}, "output_style": "project"})

    result = _layered(root, tmp_path)

    assert result.settings["output_style"] == "project"
    reasons = {item["section"]: item["reason"] for item in _ignored(result)}
    assert reasons == {"nonsense": "未知设置段"}


# --------------------------------------------------------------------------- #
# 3. 坏层与边界
# --------------------------------------------------------------------------- #
def test_corrupt_project_layer_is_skipped_but_local_still_applies(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    _write_layer(root, "settings.json", "{not json")
    _write_layer(root, "settings.local.json", {"output_style": "local"})

    result = _layered(root, tmp_path, user={"output_style": "user"})

    assert result.settings["output_style"] == "local"
    project = next(layer for layer in result.layers if layer.name == LAYER_PROJECT)
    assert project.exists is True and project.ok is False
    assert any("不是合法 JSON" in item for item in project.errors)
    assert result.changed() is True


def test_layer_top_level_must_be_an_object(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    _write_layer(root, "settings.json", ["output_style"])

    result = _layered(root, tmp_path)

    project = next(layer for layer in result.layers if layer.name == LAYER_PROJECT)
    assert project.ok is False
    assert any("顶层不是对象" in item for item in project.errors)


def test_oversized_layer_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    path = root / ".blank" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b" " * (MAX_FILE_BYTES + 1))

    result = _layered(root, tmp_path)

    project = next(layer for layer in result.layers if layer.name == LAYER_PROJECT)
    assert project.ok is False
    assert any("超过 1 MiB" in item for item in project.errors)


def test_missing_layer_is_not_an_error(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()

    result = _layered(root, tmp_path)

    project = next(layer for layer in result.layers if layer.name == LAYER_PROJECT)
    assert project.path is not None and project.exists is False and project.ok is False
    assert result.errors == []
    assert result.changed() is False


def test_values_are_normalized_after_the_merge(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    _write_layer(
        root,
        "settings.json",
        {
            "compaction": {"trigger_ratio": 3.0, "reserve_tokens": 1},
            "background": {"max_steps": 10_000, "automation_mode": "autonomous"},
        },
    )

    result = _layered(root, tmp_path)

    assert result.settings["compaction"]["trigger_ratio"] == 0.95
    assert result.settings["compaction"]["reserve_tokens"] == 1024
    assert result.settings["background"]["max_steps"] == DEFAULTS["background"]["max_steps"]


def test_bad_section_type_falls_back_to_defaults(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    _write_layer(root, "settings.json", {"compaction": "oops", "output_style": "project"})

    result = _layered(root, tmp_path)

    assert result.settings["compaction"] == normalize({})["compaction"]
    assert result.settings["output_style"] == "project"


def test_managed_layer_needs_the_environment_variable(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()

    without = load_layered_settings(root, user_path=tmp_path / "missing.json", env={})
    managed = next(layer for layer in without.layers if layer.name == LAYER_MANAGED)
    assert managed.path is None and managed.exists is False

    with_env = _layered(root, tmp_path, managed={"output_style": "managed"})
    managed = next(layer for layer in with_env.layers if layer.name == LAYER_MANAGED)
    assert managed.exists is True and managed.ok is True
    assert with_env.settings["output_style"] == "managed"


# --------------------------------------------------------------------------- #
# 4. 审计面
# --------------------------------------------------------------------------- #
def test_audit_masks_paths_and_carries_no_values(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    _write_layer(root, "settings.json", {"output_style": "secret-style"})

    result = _layered(root, tmp_path, user={"compaction": {"trigger_ratio": 0.7}})
    payload = result.as_dict()
    text = json.dumps(payload, ensure_ascii=False)

    assert "secret-style" not in text
    assert str(tmp_path.as_posix()) not in text.replace("\\\\", "/")
    project = next(item for item in payload["layers"] if item["layer"] == LAYER_PROJECT)
    assert project["path"] == "<workspace:settings.json>"
    assert project["exists"] is True and project["ok"] is True
    assert payload["sources"]["output_style"] == LAYER_PROJECT
    assert payload["active"] == [LAYER_USER, LAYER_PROJECT]


def test_audit_keeps_error_text_but_no_absolute_path(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    _write_layer(root, "settings.json", "{oops")

    result = _layered(root, tmp_path)
    text = json.dumps(result.as_dict(), ensure_ascii=False)

    assert "不是合法 JSON" in text
    assert tmp_path.as_posix() not in text.replace("\\\\", "/")


# --------------------------------------------------------------------------- #
# 5. SettingsStore 接线
# --------------------------------------------------------------------------- #
def test_settings_store_read_layered_uses_its_own_user_file(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    _write_layer(root, "settings.json", {"output_style": "project"})
    user_path = _write(tmp_path / "custom-user.json", {"tools": {"search": False}})

    layered = SettingsStore(user_path).read_layered(root)

    assert layered.settings["output_style"] == "project"
    assert layered.settings["tools"]["search"] is False
    assert layered.sources == {"output_style": LAYER_PROJECT, "tools": LAYER_USER}


def test_read_layered_without_workspace_matches_plain_read(tmp_path: Path) -> None:
    user_path = _write(
        tmp_path / "user.json",
        {"compaction": {"trigger_ratio": 0.7}, "output_style": "user-style"},
    )
    store = SettingsStore(user_path)

    assert store.read_layered(None).settings == store.read()
    assert store.read_layered(tmp_path / "missing").settings == store.read()


# --------------------------------------------------------------------------- #
# 6. 运行时
# --------------------------------------------------------------------------- #
async def test_runtime_applies_project_preferences_and_emits_layers(env, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_layer(workspace, "settings.json", {"output_style": "review"})
    _write(
        workspace / ".blank" / "output-styles" / "review.md",
        "---\ndescription: 评审\n---\n项目样式正文",
    )

    evidence, prompt = await _run(env, workspace)

    assert evidence["task"]["status"] == "succeeded", evidence["task"]
    assert "项目样式正文" in prompt
    payload = _layers_event(evidence)
    assert payload["sources"]["output_style"] == LAYER_PROJECT
    assert LAYER_PROJECT in payload["active"]
    assert "项目样式正文" not in json.dumps(payload, ensure_ascii=False)


async def test_runtime_omits_the_layers_event_without_extra_layers(env, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    evidence, _ = await _run(env, workspace)

    assert evidence["task"]["status"] == "succeeded", evidence["task"]
    assert not [item for item in evidence["timeline"] if item["type"] == "settings.layers"]


async def test_runtime_ignores_security_sections_from_the_project(env, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_layer(
        workspace,
        "settings.json",
        {
            "features": {"tool_search": False},
            "safety": {"restrict_to_workspace": False},
        },
    )

    evidence, _ = await _run(env, workspace)

    assert evidence["task"]["status"] == "succeeded", evidence["task"]
    payload = _layers_event(evidence)
    sections = {item["section"] for item in payload["layers"][1]["ignored"]}
    assert sections == {"features", "safety"}
    # 项目层关不掉按需检索：tool_search 仍在工具表里
    assert "tool_search" in _tool_names(env)


# --------------------------------------------------------------------------- #
# 7. HTTP 端点
# --------------------------------------------------------------------------- #
async def test_project_settings_api_returns_effective_values_and_sources(
    env, tmp_path: Path
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_layer(
        workspace,
        "settings.json",
        {"compaction": {"trigger_ratio": 0.6}, "output_style": "project"},
    )
    project_id = await create_project(env, workspace)

    response = await env.client.get(f"{API}/projects/{project_id}/settings")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["settings"]["compaction"]["trigger_ratio"] == 0.6
    assert body["settings"]["output_style"] == "project"
    assert body["sources"]["compaction"] == LAYER_PROJECT
    assert body["active"] == [LAYER_USER, LAYER_PROJECT]
    assert str(tmp_path.as_posix()) not in response.text.replace("\\\\", "/")


async def test_project_settings_api_ignores_project_security_sections(
    env, tmp_path: Path
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_layer(
        workspace,
        "settings.json",
        {"safety": {"restrict_to_workspace": False, "shell_enabled": True}},
    )
    project_id = await create_project(env, workspace)

    response = await env.client.get(f"{API}/projects/{project_id}/settings")

    assert response.status_code == 200, response.text
    body = response.json()
    user = SettingsStore().read()
    assert body["settings"]["safety"] == user["safety"]
    project_layer = next(item for item in body["layers"] if item["layer"] == LAYER_PROJECT)
    assert [item["section"] for item in project_layer["ignored"]] == ["safety"]


async def test_project_settings_api_shows_the_managed_layer(env, tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    managed = _write(tmp_path / "managed.json", {"output_style": "managed"})
    monkeypatch.setenv("BLANK_MANAGED_SETTINGS_FILE", str(managed))
    project_id = await create_project(env, workspace)

    response = await env.client.get(f"{API}/projects/{project_id}/settings")

    assert response.status_code == 200, response.text
    body = response.json()
    layer = next(item for item in body["layers"] if item["layer"] == LAYER_MANAGED)
    assert layer["exists"] is True and layer["ok"] is True
    assert body["settings"]["output_style"] == "managed"
    assert body["sources"]["output_style"] == LAYER_MANAGED


async def test_output_styles_endpoint_uses_the_workspace_cascade(env, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_layer(workspace, "settings.json", {"output_style": "review"})
    _write(
        workspace / ".blank" / "output-styles" / "review.md",
        "---\ndescription: 评审\n---\n正文",
    )
    project_id = await create_project(env, workspace)

    response = await env.client.get(f"{API}/projects/{project_id}/output-styles")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["selected"] == "review"
    assert body["active"] == "review"
