"""S7：插件包（``.claude-plugin/plugin.json``）。

对齐 Claude Code 的插件契约：

1. **发现**：用户层 ``~/.blank/plugins/<name>/.claude-plugin/plugin.json``
   （``BLANK_USER_PLUGINS_DIR`` 可改）与项目层
   ``<root>/.blank/plugins/...``；项目同名覆盖；坏清单只跳过该插件。
2. **组件**：``commands`` / ``agents`` / ``skills`` / ``outputStyles`` 四类
   提示词组件接进既有加载器，名字按 ``<plugin>:<name>`` 命名空间；清单里的
   路径覆盖必须以 ``./`` 开头且不得逃出插件目录。``hooks`` / ``mcpServers`` /
   ``lspServers`` / 自带 ``.mcp.json`` 只报告、不加载。
3. **启用**：``enabledPlugins``（顶层）或 ``plugins.enabled``，键可为
   ``name`` 或 ``name@marketplace``；没写时取清单 ``defaultEnabled``（默认
   true）。
4. **可观察**：``GET /projects/{id}/plugins`` 返回清单、组件数量、启用状态。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blank_api.services.agents_defs import discover_agents
from blank_api.services.commands_defs import discover_commands, expand_user_input
from blank_api.services.output_styles_defs import discover_output_styles
from blank_api.services.plugins_defs import (
    MAX_MANIFEST_BYTES,
    PROJECT_PLUGINS_DIR,
    discover_plugins,
)
from blank_api.services.settings import SettingsStore, normalize
from blank_api.services.settings_layers import load_layered_settings
from blank_api.services.skills_defs import discover_skills

from tests.test_agent_v04 import API, create_project, env  # noqa: F401


# --------------------------------------------------------------------------- #
# 帮助
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate_user_layers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """本机的用户层设置与插件目录不能影响判据。"""
    empty = tmp_path / "_no-user"
    empty.mkdir()
    monkeypatch.setenv("BLANK_USER_PLUGINS_DIR", str(empty / "plugins"))
    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(tmp_path / "_settings.json"))
    monkeypatch.delenv("BLANK_MANAGED_SETTINGS_FILE", raising=False)
    monkeypatch.delenv("BLANK_USER_INSTRUCTIONS_DIR", raising=False)
    yield


def _env(tmp_path: Path, *, plugins: Path | None = None) -> dict[str, str]:
    """四个定义加载器的用户层都指到空目录；插件根按需指定。"""
    empty = tmp_path / "_no-user"
    empty.mkdir(exist_ok=True)
    return {
        "BLANK_USER_PLUGINS_DIR": str(plugins or empty / "plugins"),
        "BLANK_USER_COMMANDS_DIR": str(empty / "commands"),
        "BLANK_USER_SKILLS_DIR": str(empty / "skills"),
        "BLANK_USER_AGENTS_DIR": str(empty / "agents"),
        "BLANK_USER_OUTPUT_STYLES_DIR": str(empty / "styles"),
    }


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _plugin(
    root: Path,
    name: str,
    *,
    manifest: dict | None = None,
    source: str = "project",
    user_dir: Path | None = None,
) -> Path:
    """建一个插件包；``source=user`` 时放到用户根。"""
    base = user_dir if source == "user" else root / ".blank" / "plugins"
    assert base is not None
    plugin_dir = base / name
    data = {"name": name}
    if manifest:
        data.update(manifest)
    _write(plugin_dir / ".claude-plugin" / "plugin.json", json.dumps(data, ensure_ascii=False))
    return plugin_dir


def _command(directory: Path, relative: str, body: str = "命令正文") -> Path:
    return _write(directory / relative, f"---\ndescription: 命令说明\n---\n{body}")


def _agent(directory: Path, relative: str, name: str = "reviewer", body: str = "严格评审") -> Path:
    return _write(
        directory / relative,
        f"---\nname: {name}\ndescription: 子代理说明\n---\n{body}",
    )


def _settings(tmp_path: Path, data: dict) -> Path:
    target = tmp_path / "_settings.json"
    _write(target, json.dumps(data, ensure_ascii=False))
    return target


# --------------------------------------------------------------------------- #
# 1. 发现 / 清单字段
# --------------------------------------------------------------------------- #
def test_discovers_plugin_and_counts_components(tmp_path: Path) -> None:
    plugin = _plugin(
        tmp_path,
        "deploy-tools",
        manifest={
            "description": "部署工具",
            "version": "1.2.0",
            "author": {"name": "Anthropic", "email": "support@anthropic.com"},
        },
    )
    _command(plugin / "commands", "review.md")
    _command(plugin / "agents", "reviewer.md")
    _write(plugin / "skills" / "deploy" / "SKILL.md", "---\ndescription: 部署\n---\n正文")
    _write(plugin / "output-styles" / "terse.md", "---\ndescription: 简洁\n---\n正文")

    catalog = discover_plugins(tmp_path, env=_env(tmp_path))

    assert catalog.errors == []
    entry = catalog.entries["deploy-tools"]
    assert entry.source == "project"
    assert entry.description == "部署工具"
    assert entry.version == "1.2.0"
    assert entry.author == "Anthropic <support@anthropic.com>"
    assert entry.enabled is True
    assert entry.default_enabled is True
    assert dict(entry.components) == {
        "commands": (str(plugin / "commands"),),
        "agents": (str(plugin / "agents"),),
        "skills": (str(plugin / "skills"),),
        "output-styles": (str(plugin / "output-styles"),),
    }
    body = catalog.as_dict()
    assert body["count"] == 1
    assert body["enabled"] == ["deploy-tools"]
    assert body["plugins"][0]["components"] == {
        "commands": 1,
        "agents": 1,
        "skills": 1,
        "output-styles": 1,
    }


def test_default_enabled_false_needs_an_explicit_switch(tmp_path: Path) -> None:
    _plugin(tmp_path, "quiet", manifest={"defaultEnabled": False})

    catalog = discover_plugins(tmp_path, env=_env(tmp_path))

    assert catalog.entries["quiet"].enabled is False
    assert catalog.as_dict()["enabled"] == []


def test_disabled_plugin_contributes_no_component(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, "deploy-tools", manifest={"defaultEnabled": False})
    _command(plugin / "commands", "review.md")
    _command(plugin / "agents", "reviewer.md")
    _write(plugin / "skills" / "deploy" / "SKILL.md", "---\ndescription: 部署\n---\n正文")
    _write(plugin / "output-styles" / "terse.md", "---\ndescription: 简洁\n---\n正文")
    env_map = _env(tmp_path)

    assert discover_commands(tmp_path, env=env_map).entries == {}
    assert discover_agents(tmp_path, env=env_map).custom() == []
    assert discover_skills(tmp_path, env=env_map).entries == {}
    assert discover_output_styles(tmp_path, env=env_map).entries == {}


def test_missing_manifest_directory_is_reported(tmp_path: Path) -> None:
    (tmp_path / ".blank" / "plugins" / "not-a-plugin").mkdir(parents=True)

    catalog = discover_plugins(tmp_path, env=_env(tmp_path))

    assert catalog.entries == {}
    assert any("缺少 .claude-plugin/plugin.json" in item for item in catalog.warnings)


def test_broken_manifest_skips_only_that_plugin(tmp_path: Path) -> None:
    bad = tmp_path / ".blank" / "plugins" / "bad"
    _write(bad / ".claude-plugin" / "plugin.json", "{ not json")
    _plugin(tmp_path, "good")

    catalog = discover_plugins(tmp_path, env=_env(tmp_path))

    assert list(catalog.entries) == ["good"]
    assert any("bad" in item and "不是合法 JSON" in item for item in catalog.errors)


def test_manifest_name_rules_match_agent_names(tmp_path: Path) -> None:
    _plugin(tmp_path, "colon", manifest={"name": "a:b"})
    _plugin(tmp_path, "dash", manifest={"name": "-a"})

    catalog = discover_plugins(tmp_path, env=_env(tmp_path))

    assert catalog.entries == {}
    assert any("不能包含 \":\"" in item for item in catalog.errors)
    assert any("不能以 \"-\" 开头" in item for item in catalog.errors)


def test_manifest_over_one_mib_is_skipped(tmp_path: Path) -> None:
    plugin_dir = tmp_path / ".blank" / "plugins" / "huge"
    payload = '{"name": "huge"}'
    raw = payload + " " * (MAX_MANIFEST_BYTES + 1 - len(payload))
    _write(plugin_dir / ".claude-plugin" / "plugin.json", raw)

    catalog = discover_plugins(tmp_path, env=_env(tmp_path))

    assert catalog.entries == {}
    assert any("超过 1 MiB" in item for item in catalog.errors)


def test_user_and_project_plugins_collide_project_wins(tmp_path: Path) -> None:
    user_root = tmp_path / "user-plugins"
    _plugin(tmp_path, "shared", manifest={"description": "用户层"}, source="user", user_dir=user_root)
    _plugin(tmp_path, "shared", manifest={"description": "项目层"})

    catalog = discover_plugins(tmp_path, env=_env(tmp_path, plugins=user_root))

    assert catalog.entries["shared"].description == "项目层"
    assert any("同时存在于 user 层与 project 层" in item for item in catalog.warnings)


def test_no_workspace_scans_only_the_user_layer(tmp_path: Path) -> None:
    user_root = tmp_path / "user-plugins"
    _plugin(tmp_path, "mine", source="user", user_dir=user_root)

    catalog = discover_plugins(None, env=_env(tmp_path, plugins=user_root))

    assert list(catalog.entries) == ["mine"]
    assert catalog.entries["mine"].source == "user"


# --------------------------------------------------------------------------- #
# 2. 组件路径与不加载的组件
# --------------------------------------------------------------------------- #
def test_component_path_override_replaces_the_default_folder(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, "deploy-tools", manifest={"commands": ["./custom"]})
    # 默认 commands/ 里的文件不应被加载：清单已经指定了另一个目录。
    _command(plugin / "commands", "ignored.md")
    _command(plugin / "custom", "review.md")

    commands = discover_commands(tmp_path, env=_env(tmp_path))

    assert list(commands.entries) == ["deploy-tools:review"]


def test_component_path_must_start_with_dot_slash(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, "deploy-tools", manifest={"commands": "custom"})
    _command(plugin / "custom", "review.md")

    catalog = discover_plugins(tmp_path, env=_env(tmp_path))
    commands = discover_commands(tmp_path, env=_env(tmp_path))

    assert commands.entries == {}
    assert any('必须以 "./" 开头' in item for item in catalog.entries["deploy-tools"].warnings)


def test_component_path_cannot_escape_the_plugin_folder(tmp_path: Path) -> None:
    _plugin(tmp_path, "deploy-tools", manifest={"agents": "./../outside"})
    _agent(tmp_path / ".blank" / "plugins" / "outside", "evil.md", name="evil")

    catalog = discover_plugins(tmp_path, env=_env(tmp_path))

    assert catalog.entries["deploy-tools"].component_dirs("agents") == ()
    assert any("跳出了插件目录" in item for item in catalog.entries["deploy-tools"].warnings)


def test_hooks_and_mcp_are_reported_but_not_loaded(tmp_path: Path) -> None:
    plugin = _plugin(
        tmp_path,
        "deploy-tools",
        manifest={"hooks": {"PostToolUse": []}, "mcpServers": {"x": {"command": "x"}}},
    )
    _write(plugin / ".mcp.json", "{}")

    entry = discover_plugins(tmp_path, env=_env(tmp_path)).entries["deploy-tools"]

    assert {key for key, _ in entry.ignored} == {"hooks", "mcpServers", ".mcp.json"}
    # 只报告不执行：加载器里没有任何插件级 hook/MCP 入口。
    assert discover_commands(tmp_path, env=_env(tmp_path)).entries == {}


# --------------------------------------------------------------------------- #
# 3. 命名空间注入
# --------------------------------------------------------------------------- #
def test_plugin_commands_are_namespaced_and_expandable(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, "deploy-tools")
    _command(plugin / "commands", "review.md", body="Review $ARGUMENTS")

    commands = discover_commands(tmp_path, env=_env(tmp_path))

    assert list(commands.entries) == ["deploy-tools:review"]
    entry = commands.entries["deploy-tools:review"]
    assert entry.source == "plugin:deploy-tools"
    expanded, info = expand_user_input(
        "/deploy-tools:review 这个改动",
        root=str(tmp_path),
        env=_env(tmp_path),
    )
    assert expanded == "Review 这个改动"
    assert info is not None and info["source"] == "plugin:deploy-tools"


def test_plugin_skills_are_namespaced(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, "deploy-tools")
    _write(plugin / "skills" / "deploy" / "SKILL.md", "---\ndescription: 部署\n---\n正文")

    skills = discover_skills(tmp_path, env=_env(tmp_path))

    assert list(skills.entries) == ["deploy-tools:deploy"]
    assert skills.entries["deploy-tools:deploy"].source == "plugin:deploy-tools"
    assert skills.entries["deploy-tools:deploy"].instructions == "正文"


def test_plugin_agents_are_namespaced(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, "deploy-tools")
    _agent(plugin / "agents", "reviewer.md", body="严格评审")

    catalog = discover_agents(tmp_path, env=_env(tmp_path))

    assert "deploy-tools:reviewer" in catalog.entries
    assert catalog.entries["deploy-tools:reviewer"].source == "plugin:deploy-tools"
    assert [item.name for item in catalog.custom()] == ["deploy-tools:reviewer"]


def test_plugin_output_styles_are_namespaced_and_selectable(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, "deploy-tools")
    _write(plugin / "output-styles" / "terse.md", "---\ndescription: 简洁\n---\n只写结论。")

    styles = discover_output_styles(tmp_path, env=_env(tmp_path))

    assert list(styles.entries) == ["deploy-tools:terse"]
    selected = styles.resolve("deploy-tools:terse")
    assert selected is not None
    assert selected.prompt == "只写结论。"
    # 裸名字不能命中插件样式：命名空间就是区分手段。
    assert styles.resolve("terse") is None


# --------------------------------------------------------------------------- #
# 4. 启用开关
# --------------------------------------------------------------------------- #
def test_plugins_enabled_map_disables_a_plugin(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, "deploy-tools")
    _command(plugin / "commands", "review.md")
    _settings(tmp_path, {"plugins": {"enabled": {"deploy-tools": False}}})

    catalog = discover_plugins(tmp_path, env=_env(tmp_path))
    commands = discover_commands(tmp_path, env=_env(tmp_path))

    assert catalog.entries["deploy-tools"].enabled is False
    assert commands.entries == {}


def test_top_level_enabled_plugins_enables_a_default_off_plugin(tmp_path: Path) -> None:
    _plugin(tmp_path, "quiet", manifest={"defaultEnabled": False})
    _settings(tmp_path, {"enabledPlugins": {"quiet": True}})

    catalog = discover_plugins(tmp_path, env=_env(tmp_path))

    assert catalog.entries["quiet"].enabled is True


def test_marketplace_key_matches_the_plugin_name(tmp_path: Path) -> None:
    _plugin(tmp_path, "quiet", manifest={"defaultEnabled": False})
    _settings(tmp_path, {"enabledPlugins": {"quiet@claude-plugins-official": True}})

    assert discover_plugins(tmp_path, env=_env(tmp_path)).entries["quiet"].enabled is True


def test_stale_enabled_entry_is_reported(tmp_path: Path) -> None:
    _settings(tmp_path, {"enabledPlugins": {"ghost": True}})

    catalog = discover_plugins(tmp_path, env=_env(tmp_path))

    assert any("ghost" in item and "enabledPlugins" in item for item in catalog.warnings)


def test_normalize_accepts_both_spellings_and_drops_uninterpretable_values(tmp_path: Path) -> None:
    settings = normalize(
        {
            "enabledPlugins": {"a": True, "b": "off"},
            "plugins": {"enabled": {"a": False, "c": "maybe", "d": 1}},
        }
    )

    assert settings["plugins"]["enabled"] == {"a": False, "b": False, "d": True}


def test_layered_settings_let_the_project_layer_toggle_plugins(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(
        root / ".blank" / "settings.json",
        json.dumps({"enabledPlugins": {"deploy-tools": True}}),
    )
    user_path = _write(
        tmp_path / "user.json",
        json.dumps({"plugins": {"enabled": {"deploy-tools": False}}}),
    )

    layered = load_layered_settings(root, user_path=user_path, env={})

    assert layered.settings["plugins"]["enabled"] == {"deploy-tools": True}
    assert layered.sources["plugins"] == "project"
    ignored = [item for layer in layered.layers for item in layer.ignored]
    assert not [item for item in ignored if item["section"] in ("plugins", "enabledPlugins")]


def test_same_layer_plugins_enabled_wins_over_top_level_map(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _write(
        root / ".blank" / "settings.json",
        json.dumps(
            {
                "enabledPlugins": {"deploy-tools": True},
                "plugins": {"enabled": {"deploy-tools": False}},
            }
        ),
    )

    layered = load_layered_settings(root, user_path=tmp_path / "missing.json", env={})

    assert layered.settings["plugins"]["enabled"] == {"deploy-tools": False}


def test_settings_store_round_trips_the_plugins_section(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")

    written = store.update({"plugins": {"enabled": {"deploy-tools": True}}})

    assert written["plugins"]["enabled"] == {"deploy-tools": True}
    assert store.read()["plugins"]["enabled"] == {"deploy-tools": True}


# --------------------------------------------------------------------------- #
# 5. HTTP 列表
# --------------------------------------------------------------------------- #
async def test_project_plugins_api_lists_components_and_switches(
    env, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = _plugin(workspace, "deploy-tools", manifest={"description": "部署工具"})
    _command(plugin / "commands", "review.md")
    project_id = await create_project(env, workspace)
    monkeypatch.setenv("BLANK_SETTINGS_FILE", str(tmp_path / "_settings.json"))

    response = await env.client.get(f"{API}/projects/{project_id}/plugins")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 1
    assert body["enabled"] == ["deploy-tools"]
    entry = body["plugins"][0]
    assert entry["description"] == "部署工具"
    assert entry["components"] == {"commands": 1}
    assert entry["enabled"] is True
    assert entry["path"].startswith("<workspace:")
    # 项目层目录只给工作区相对路径：接口不回显宿主绝对路径。
    assert body["projectDir"] == str(PROJECT_PLUGINS_DIR)
    assert str(workspace) not in response.text
    # 列表只给清单，不返回命令正文。
    assert "命令正文" not in response.text


# --------------------------------------------------------------------------- #
# 6. Y 批：插件 agent 的字段回退与忽略口径（上游 ``Skn``）
# --------------------------------------------------------------------------- #
def test_plugin_agent_name_falls_back_to_the_file_stem(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, "deploy-tools")
    _write(
        plugin / "agents" / "auditor.md",
        "---\ndescription: 审计\n---\n逐条核对。",
    )

    catalog = discover_agents(tmp_path, env=_env(tmp_path))

    assert "deploy-tools:auditor" in catalog.entries
    definition = catalog.entries["deploy-tools:auditor"]
    assert definition.description == "审计"
    assert catalog.errors == []


def test_plugin_agent_description_falls_back_to_when_to_use(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, "deploy-tools")
    _write(
        plugin / "agents" / "a.md",
        "---\nname: a\nwhen_to_use: 需要审计时\n---\n正文",
    )
    _write(
        plugin / "agents" / "b.md",
        "---\nname: b\nwhen-to-use: 需要复核时\n---\n正文",
    )
    _write(plugin / "agents" / "c.md", "---\nname: c\n---\n正文")

    catalog = discover_agents(tmp_path, env=_env(tmp_path))

    assert catalog.entries["deploy-tools:a"].description == "需要审计时"
    assert catalog.entries["deploy-tools:b"].description == "需要复核时"
    # 上游最后的兜底文案：Agent from <plugin> plugin。
    assert catalog.entries["deploy-tools:c"].description == "Agent from deploy-tools plugin"
    assert catalog.errors == []


def test_plugin_agent_permission_controls_are_reported_not_applied(
    tmp_path: Path,
) -> None:
    plugin = _plugin(tmp_path, "deploy-tools")
    _write(
        plugin / "agents" / "locked.md",
        "---\nname: locked\ndescription: 受限\n"
        "permissionMode: plan\nhooks: {}\nmcpServers: {}\n---\n正文",
    )

    catalog = discover_agents(tmp_path, env=_env(tmp_path))

    definition = catalog.entries["deploy-tools:locked"]
    # 插件 agent 不接受这三类控制：解析出来也不生效，且要明确告警。
    # （加载器把单文件"问题"统一收在 errors 清单里，严重程度看文案。）
    assert definition.permission_mode == ""
    assert {"permissionMode", "hooks", "mcpServers"} <= set(definition.ignored)
    for key in ("permissionMode", "hooks", "mcpServers"):
        assert any(
            key in item and "忽略" in item for item in catalog.errors
        ), catalog.errors


def test_plugin_agent_isolation_worktree_is_honoured_and_invalid_is_silent(
    tmp_path: Path,
) -> None:
    plugin = _plugin(tmp_path, "deploy-tools")
    _write(
        plugin / "agents" / "w.md",
        "---\nname: w\ndescription: 隔离\nisolation: worktree\n---\n正文",
    )
    _write(
        plugin / "agents" / "r.md",
        "---\nname: r\ndescription: 远程\nisolation: remote\n---\n正文",
    )

    catalog = discover_agents(tmp_path, env=_env(tmp_path))

    assert catalog.entries["deploy-tools:w"].isolation == "worktree"
    # 插件 agent 只接受 worktree：其余值上游静默丢弃，不写告警。
    remote = catalog.entries["deploy-tools:r"]
    assert remote.isolation == ""
    assert "isolation" in remote.ignored
    assert not any("isolation" in item for item in catalog.errors), catalog.errors
    assert catalog.errors == []
