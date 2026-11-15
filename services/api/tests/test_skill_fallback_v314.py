"""#314：Skill 的 ``fallback`` 字段——同名后缀的插件/MCP 技能加载后让位。

上游语义（``grep -bo`` 复核）：
* schema（6475366）：``@internal — interim defense-in-depth for thin-pointer skill
  stubs. If true, this skill yields to a same-suffix plugin or MCP skill
  (``<plugin>:<name>`` / ``<server>:<name>``) when one is loaded.``
* ``v2e``（12715657）：``e.type==="prompt" && e.fallback===!0 &&
  e.loadedFrom!=="syncedSkills"``。
* ``q9``（12713520）：先收集 ``loadedFrom`` 属于 plugin/bundled/mcp、且不是
  ``disableModelInvocation`` / ``Rj``（仅用户可调用或关闭）的 prompt 命令名字的
  **最后一段**（``name.lastIndexOf(":")`` 之后）；再把 ``v2e`` 为真且名字命中
  该集合的条目丢掉，并 warn
  ``Dropping fallback skill '<name>' — a plugin/MCP skill with the same suffix is
  loaded``。

本机对应：
* 来源只有 ``user`` / ``project`` / ``plugin:<name>``；插件技能的调用名是
  ``<plugin>:<name>``（命令同理），没有 bundled / MCP prompt 技能链路；
* 全局唯一能看到「技能 + 命令 + 插件技能」的地方是 ``merge_skill_entries``，
  所以丢弃在那里做（等价于上游对合并后命令表调 ``q9``）；
* ``disableModelInvocation`` 对应本机 ``model_invocable=False``——不可调用的
  插件技能不触发丢弃。

判据钉的是会走形的行为：字段解析丢/放宽、``as_entry`` 不携带、支持清单没减去、
合并层不丢、丢错（非 fallback / 未命中 / 不可调用插件项）、告警不产生。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from blank_api.db import get_session_factory
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.commands_defs import COMMAND_UNSUPPORTED_FIELDS
from blank_api.services.models.registry import build_adapter
from blank_api.services.skills_defs import (
    SKILL_UNSUPPORTED_FIELDS,
    UNSUPPORTED_FIELDS,
    SkillDefinition,
    _definition_from_dir,
    merge_skill_entries,
)
from blank_api.services.tools.skills import SkillCatalog
from tests.test_agent_v04 import (  # noqa: F401  (env 是夹具，故意导入)
    API,
    create_provider,
    create_project,
    create_task,
    env,
    provider_row,
)
from tests.test_hook_events_v52 import _hooks_env


# --------------------------------------------------------------------------- #
# 测试装置
# --------------------------------------------------------------------------- #


def _write_skill(root: Path, name: str, frontmatter: str, body: str = "正文") -> Path:
    skill_dir = root / ".blank" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n{frontmatter}---\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def _entry(
    name: str,
    *,
    source: str = "project",
    fallback: bool = False,
    model_invocable: bool = True,
    kind: str = "skill",
) -> dict:
    return {
        "name": name,
        "description": f"{name} 的说明",
        "instructions": f"{name} 的正文",
        "source": source,
        "fallback": fallback,
        "model_invocable": model_invocable,
        "kind": kind,
    }


# --------------------------------------------------------------------------- #
# 1. 解析与携带
# --------------------------------------------------------------------------- #


def test_skill_fallback_defaults_to_false(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "plain", "")
    definition, errors, warnings = _definition_from_dir(skill_dir, "project")
    assert definition is not None and not errors
    assert definition.fallback is False
    assert warnings == []


def test_skill_fallback_true_is_parsed(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "stub", "fallback: true\n")
    definition, errors, warnings = _definition_from_dir(skill_dir, "project")
    assert definition is not None and not errors
    assert definition.fallback is True
    assert warnings == []


def test_skill_fallback_false_is_parsed(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "explicit", "fallback: false\n")
    definition, _, _ = _definition_from_dir(skill_dir, "project")
    assert definition is not None
    assert definition.fallback is False


def test_skill_fallback_string_truthy_is_accepted(tmp_path: Path) -> None:
    """本机沿用 ``_flag`` 口径：``"true"`` / ``"yes"`` / ``"1"`` 都算真。

    偏离：上游 zod boolean 只收布尔；本机对 frontmatter 字符串的宽容与
    ``background`` / ``disable-model-invocation`` 一致（见 README）。
    """
    for index, value in enumerate(("true", "yes", "1", "on")):
        skill_dir = _write_skill(tmp_path, f"truthy-{index}", f'fallback: "{value}"\n')
        definition, _, warnings = _definition_from_dir(skill_dir, "project")
        assert definition is not None
        assert definition.fallback is True, value
        assert warnings == [], value


def test_skill_fallback_non_boolean_warns_and_is_false(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "bogus", 'fallback: "sometimes"\n')
    definition, _, warnings = _definition_from_dir(skill_dir, "project")
    assert definition is not None
    assert definition.fallback is False
    assert any("fallback" in item for item in warnings)


def test_skill_fallback_null_is_silent(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "nullish", "fallback: null\n")
    definition, _, warnings = _definition_from_dir(skill_dir, "project")
    assert definition is not None
    assert definition.fallback is False
    assert warnings == []


def test_skill_fallback_as_entry_and_audit(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "carried", "fallback: true\n")
    definition, _, _ = _definition_from_dir(skill_dir, "project")
    assert definition is not None
    assert definition.as_entry()["fallback"] is True
    assert definition.as_dict().get("fallback") is True


def test_skill_audit_omits_false_fallback(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "quiet", "")
    definition, _, _ = _definition_from_dir(skill_dir, "project")
    assert definition is not None
    assert "fallback" not in definition.as_dict()


def test_unsupported_lists_move_fallback_out_of_skills_only() -> None:
    """Skill 侧生效；命令 schema 上游没有 fallback，命令侧仍按不支持处理。"""
    assert "fallback" in UNSUPPORTED_FIELDS
    assert "fallback" not in SKILL_UNSUPPORTED_FIELDS
    assert "fallback" in COMMAND_UNSUPPORTED_FIELDS


def test_skill_ignored_no_longer_lists_fallback(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "not-ignored", "fallback: true\n")
    definition, _, _ = _definition_from_dir(skill_dir, "project")
    assert definition is not None
    assert "fallback" not in definition.ignored


# --------------------------------------------------------------------------- #
# 2. 合并层丢弃语义（等价上游 q9）
# --------------------------------------------------------------------------- #


def test_fallback_skill_yields_to_plugin_skill() -> None:
    merged = merge_skill_entries(
        [_entry("stub", fallback=True)],
        [_entry("p:stub", source="plugin:p")],
    )
    names = {item["name"] for item in merged}
    assert names == {"p:stub"}


def test_fallback_skill_survives_when_suffix_differs() -> None:
    merged = merge_skill_entries(
        [_entry("stub", fallback=True)],
        [_entry("p:other", source="plugin:p")],
    )
    names = {item["name"] for item in merged}
    assert names == {"stub", "p:other"}


def test_non_fallback_skill_never_yields() -> None:
    merged = merge_skill_entries(
        [_entry("stub", fallback=False)],
        [_entry("p:stub", source="plugin:p")],
    )
    names = {item["name"] for item in merged}
    assert names == {"stub", "p:stub"}


def test_non_invocable_plugin_skill_does_not_shadow() -> None:
    """上游跳过 ``disableModelInvocation`` 的插件命令：不可调用不算「已加载」。"""
    merged = merge_skill_entries(
        [_entry("stub", fallback=True)],
        [_entry("p:stub", source="plugin:p", model_invocable=False)],
    )
    names = {item["name"] for item in merged}
    assert names == {"stub", "p:stub"}


def test_plugin_command_suffix_also_shadows() -> None:
    """上游 q9 看的是合并后的命令表：插件命令同样能顶掉 fallback 技能。"""
    merged = merge_skill_entries(
        [_entry("stub", fallback=True)],
        [_entry("p:stub", source="plugin:p", kind="command")],
    )
    names = {item["name"] for item in merged}
    assert names == {"p:stub"}


def test_dropped_list_records_warning() -> None:
    dropped: list[str] = []
    merge_skill_entries(
        [_entry("stub", fallback=True)],
        [_entry("p:stub", source="plugin:p")],
        dropped=dropped,
    )
    assert len(dropped) == 1
    text = dropped[0]
    assert "stub" in text
    assert "插件" in text
    assert "已加载" in text


def test_dropped_list_is_silent_without_hits() -> None:
    dropped: list[str] = []
    merge_skill_entries(
        [_entry("stub", fallback=True)],
        [_entry("p:other", source="plugin:p")],
        dropped=dropped,
    )
    assert dropped == []


def test_multiple_fallbacks_only_hit_one_drops() -> None:
    merged = merge_skill_entries(
        [_entry("alpha", fallback=True), _entry("beta", fallback=True)],
        [_entry("p:beta", source="plugin:p")],
    )
    names = {item["name"] for item in merged}
    assert names == {"alpha", "p:beta"}


def test_no_plugin_layers_leave_entries_untouched() -> None:
    group = [_entry("stub", fallback=True), _entry("plain")]
    merged = merge_skill_entries(group)
    assert [item["name"] for item in merged] == ["stub", "plain"]


def test_catalog_listing_hides_dropped_fallback() -> None:
    catalog = SkillCatalog(
        merge_skill_entries(
            [_entry("stub", fallback=True)],
            [_entry("p:stub", source="plugin:p")],
        )
    )
    names = [item["name"] for item in catalog.listing()]
    assert names == ["p:stub"]


def test_non_plugin_source_with_colon_does_not_shadow() -> None:
    """只有 plugin/bundled/mcp 来源才进后缀集合；本地名字带冒号不算。"""
    merged = merge_skill_entries(
        [_entry("stub", fallback=True)],
        [_entry("p:stub", source="project")],
    )
    names = {item["name"] for item in merged}
    assert names == {"stub", "p:stub"}


def test_plugin_suffix_without_colon_is_not_a_shadow() -> None:
    """上游 ``lastIndexOf(":") > 0``：没有冒号的名字不产生后缀。"""
    merged = merge_skill_entries(
        [_entry("stub", fallback=True)],
        [_entry("pluginname", source="plugin:p")],
    )
    names = {item["name"] for item in merged}
    assert names == {"stub", "pluginname"}


def test_skill_definition_carries_fallback_into_entry(tmp_path: Path) -> None:
    skill_dir = _write_skill(tmp_path, "stub", "fallback: true\n")
    definition, _, _ = _definition_from_dir(skill_dir, "project")
    assert definition is not None
    assert definition.as_entry()["fallback"] is True
    # 端到端：磁盘定义经 as_entry 进合并层，插件技能已加载时应被丢弃。
    merged = merge_skill_entries(
        [definition.as_entry()],
        [_entry("p:stub", source="plugin:p")],
    )
    assert {item["name"] for item in merged} == {"p:stub"}


def test_skill_definition_default_entry_does_not_yield() -> None:
    """没声明 fallback 的磁盘定义照样保留（``as_entry`` 的默认值是假）。"""
    definition = SkillDefinition(name="stub", description="d", instructions="b")
    assert definition.as_entry()["fallback"] is False
    merged = merge_skill_entries(
        [definition.as_entry()],
        [_entry("p:stub", source="plugin:p")],
    )
    assert {item["name"] for item in merged} == {"stub", "p:stub"}


# --------------------------------------------------------------------------- #
# 3. 运行时接通：丢弃结果进事件时间线
# --------------------------------------------------------------------------- #


async def test_runtime_emits_fallback_shadowed_event(
    env, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BLANK_USER_SKILLS_DIR", str(tmp_path / "no-user-skills"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_skill(workspace, "stub", "fallback: true\n")
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["完成"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 直接完成"},
            {"content": "完成"},
            {"content": "完成"},
        ]
    )
    evidence = await AgentRuntime(get_session_factory()).run(
        RunRequest(
            task_id=task_id,
            provider_id=provider_id,
            provider_name="mock-vendor",
            adapter=build_adapter(await provider_row(provider_id), env.mock.factory),
            root=workspace,
            goal="随便看看",
            acceptance_criteria=["完成"],
            max_steps=4,
            skill_catalog=[
                {
                    "name": "p:stub",
                    "description": "插件占位",
                    "instructions": "插件正文",
                    "source": "plugin:p",
                }
            ],
        )
    )
    assert evidence["task"]["status"] == "succeeded", evidence["task"]
    shadowed = [
        item for item in evidence["timeline"] if item["type"] == "skills.fallback_shadowed"
    ]
    assert shadowed, [item["type"] for item in evidence["timeline"]]
    dropped = shadowed[0]["payload"]["dropped"]
    assert len(dropped) == 1 and "stub" in dropped[0]


def _hook_script(tmp_path: Path, name: str, body: str) -> str:
    script = tmp_path / f"{name}.py"
    script.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


async def test_reload_path_emits_fallback_shadowed_event(
    env, workspace: Path, tmp_path: Path, monkeypatch
) -> None:
    """SessionStart 的 reloadSkills 重扫后再合并，丢弃同样进事件时间线。"""
    monkeypatch.setenv("BLANK_USER_SKILLS_DIR", str(tmp_path / "no-user-skills"))
    monkeypatch.setenv("BLANK_USER_PLUGINS_DIR", str(tmp_path / "no-user-plugins"))
    _write_skill(workspace, "stub", "fallback: true\n")
    plugin_dir = workspace / ".blank" / "plugins" / "p"
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "p"}), encoding="utf-8"
    )
    plugin_skill = plugin_dir / "skills" / "stub"
    plugin_skill.mkdir(parents=True)
    (plugin_skill / "SKILL.md").write_text(
        "---\nname: stub\ndescription: 插件技能\n---\n插件正文\n", encoding="utf-8"
    )
    command = _hook_script(
        tmp_path,
        "reload",
        "import json, sys\n"
        "sys.stdout.write(json.dumps({'hookSpecificOutput': {'reloadSkills': True}}))\n",
    )
    _hooks_env(monkeypatch, session_start=command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])
    env.mock.reset([{"content": "完成"}])

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text
    timeline = resp.json()["evidence"]["timeline"]
    reload_events = [
        item
        for item in timeline
        if item["type"] == "skills.fallback_shadowed"
        and item["payload"].get("event") == "session_start"
    ]
    assert reload_events, [item["type"] for item in timeline]
    assert any("stub" in text for text in reload_events[0]["payload"]["dropped"])
