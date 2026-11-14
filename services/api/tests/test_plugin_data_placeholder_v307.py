"""#307：``${CLAUDE_PLUGIN_ROOT}`` / ``${CLAUDE_PLUGIN_DATA}`` 占位符展开。

上游锚点：``C9(e, n)`` 在 ``n.source`` 非空时才展开 ``${CLAUDE_PLUGIN_DATA}``；
``${CLAUDE_PLUGIN_ROOT}`` 只要有 ``n.path`` 就展开。``Bwe`` 计算
``<plugin_root>/data/<plugin_name>`` 并 mkdirSync 作为副作用。

本批把两个占位符接到 substitute_paths / _tool_placeholders / render_skill /
expand_user_input 四层，并让 SkillDefinition / CommandDefinition 携带
plugin_root / plugin_data（非插件来源时为空串，substitute_paths 不展开）。

钉的不是「字段存在」——每条判据指向一个会走形的具体行为：解析丢了字段、
目录不携带、handler 不展开、非插件也展开（违反 n.source 门控）、
plugin_data 目录不创建。改前这些用例要么断言失败、要么 AttributeError。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.services.commands_defs import (
    CommandDefinition,
    _definition,
    discover_commands,
    expand_user_input,
)
from blank_api.services.prompt_args import render_prompt, substitute_paths
from blank_api.services.skills_defs import (
    SkillDefinition,
    _definition_from_dir,
    discover_skills,
)
from blank_api.services.tools.skills import render_skill, skill_tools, SkillCatalog
from blank_api.services.tools.base import ToolContext


# --------------------------------------------------------------------------- #
# 测试装置
# --------------------------------------------------------------------------- #

def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _env(tmp_path: Path) -> dict[str, str]:
    """所有用户层目录指到空目录，本机 ``~/.blank`` 不污染判据。"""
    empty = tmp_path / "_no-user"
    empty.mkdir(exist_ok=True)
    return {
        "BLANK_USER_PLUGINS_DIR": str(empty / "plugins"),
        "BLANK_USER_COMMANDS_DIR": str(empty / "commands"),
        "BLANK_USER_SKILLS_DIR": str(empty / "skills"),
        "BLANK_USER_AGENTS_DIR": str(empty / "agents"),
        "BLANK_USER_OUTPUT_STYLES_DIR": str(empty / "styles"),
    }


def _plugin(root: Path, name: str) -> Path:
    plugin = root / ".blank" / "plugins" / name
    _write(
        plugin / ".claude-plugin" / "plugin.json",
        json.dumps({"name": name}, ensure_ascii=False),
    )
    return plugin


def _fwd(path: Path) -> str:
    return str(path).replace("\\", "/")


def _skill_text(body: str = "正文", extra: str = "") -> str:
    return f"---\ndescription: 技能说明\n{extra}---\n{body}"


def _command_text(body: str = "正文", extra: str = "") -> str:
    return f"---\ndescription: 命令说明\n{extra}---\n{body}"


# --------------------------------------------------------------------------- #
# 1. substitute_paths：占位符展开的原子层
# --------------------------------------------------------------------------- #

def test_plugin_root_placeholder_expands_in_substitute_paths() -> None:
    """``${CLAUDE_PLUGIN_ROOT}`` 有 plugin_root 就展开，路径归一正斜杠。"""
    result = substitute_paths(
        "用 ${CLAUDE_PLUGIN_ROOT}/rules.md",
        plugin_root=r"E:\plugins\deploy",
    )
    assert result == "用 E:/plugins/deploy/rules.md"


def test_plugin_data_placeholder_expands_in_substitute_paths() -> None:
    """``${CLAUDE_PLUGIN_DATA}`` 有 plugin_data 就展开（C9 在 n.source 非空时做）。"""
    result = substitute_paths(
        "数据在 ${CLAUDE_PLUGIN_DATA}/db.sqlite",
        plugin_data=r"E:\plugins\deploy\data\deploy",
    )
    assert result == "数据在 E:/plugins/deploy/data/deploy/db.sqlite"


def test_plugin_data_stays_literal_when_plugin_data_empty() -> None:
    """``plugin_data`` 为空串时 ``${CLAUDE_PLUGIN_DATA}`` 保持字面量——
    上游 ``n.source`` 门控，非插件正文不展开。"""
    result = substitute_paths(
        "数据在 ${CLAUDE_PLUGIN_DATA}/db.sqlite",
        plugin_data="",
    )
    assert "${CLAUDE_PLUGIN_DATA}" in result


def test_plugin_root_stays_literal_when_plugin_root_empty() -> None:
    """``plugin_root`` 为空串时 ``${CLAUDE_PLUGIN_ROOT}`` 保持字面量。"""
    result = substitute_paths(
        "用 ${CLAUDE_PLUGIN_ROOT}/rules.md",
        plugin_root="",
    )
    assert "${CLAUDE_PLUGIN_ROOT}" in result


def test_both_plugin_placeholders_expand_together() -> None:
    result = substitute_paths(
        "root=${CLAUDE_PLUGIN_ROOT} data=${CLAUDE_PLUGIN_DATA}",
        plugin_root=r"E:\plugins\p",
        plugin_data=r"E:\plugins\p\data\p",
    )
    assert result == "root=E:/plugins/p data=E:/plugins/p/data/p"


# --------------------------------------------------------------------------- #
# 2. render_prompt / render_skill：透传层
# --------------------------------------------------------------------------- #

def test_render_prompt_passes_plugin_params() -> None:
    result = render_prompt(
        "根 ${CLAUDE_PLUGIN_ROOT} 数据 ${CLAUDE_PLUGIN_DATA}",
        "",
        plugin_root=r"E:\plugins\p",
        plugin_data=r"E:\plugins\p\data\p",
    )
    assert result == "根 E:/plugins/p 数据 E:/plugins/p/data/p"


def test_render_skill_passes_plugin_params() -> None:
    """``render_skill`` 把 plugin_root / plugin_data 透传给 render_prompt。"""
    result = render_skill(
        "根 ${CLAUDE_PLUGIN_ROOT} 数据 ${CLAUDE_PLUGIN_DATA}",
        "",
        plugin_root=r"E:\plugins\p",
        plugin_data=r"E:\plugins\p\data\p",
    )
    assert result == "根 E:/plugins/p 数据 E:/plugins/p/data/p"


def test_render_skill_without_plugin_params_keeps_literals() -> None:
    """不传 plugin 参数时两个占位符都保持字面量（非插件 Skill）。"""
    result = render_skill(
        "根 ${CLAUDE_PLUGIN_ROOT} 数据 ${CLAUDE_PLUGIN_DATA}",
        "",
    )
    assert "${CLAUDE_PLUGIN_ROOT}" in result
    assert "${CLAUDE_PLUGIN_DATA}" in result


# --------------------------------------------------------------------------- #
# 3. SkillDefinition 携带 plugin_root / plugin_data
# --------------------------------------------------------------------------- #

def test_plugin_skill_definition_carries_plugin_fields(tmp_path: Path) -> None:
    """插件 Skill 的 plugin_root / plugin_data 来自插件包路径。"""
    plugin = _plugin(tmp_path, "deploy-tools")
    _write(
        plugin / "skills" / "review" / "SKILL.md",
        _skill_text(),
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))

    entry = skills.entries["deploy-tools:review"]
    assert entry.plugin_root == str(plugin)
    assert entry.plugin_data == str(plugin / "data" / "deploy-tools")


def test_non_plugin_skill_has_empty_plugin_fields(tmp_path: Path) -> None:
    """项目层 Skill 的 plugin_root / plugin_data 是空串——n.source 门控。"""
    _write(
        tmp_path / ".blank" / "skills" / "local" / "SKILL.md",
        _skill_text(),
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))

    entry = skills.entries["local"]
    assert entry.plugin_root == ""
    assert entry.plugin_data == ""


def test_skill_definition_as_entry_includes_plugin_fields(tmp_path: Path) -> None:
    """``as_entry()`` 把 plugin_root / plugin_data 放进消费方看到的 dict。"""
    plugin = _plugin(tmp_path, "deploy-tools")
    _write(
        plugin / "skills" / "review" / "SKILL.md",
        _skill_text(),
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))
    entry_dict = skills.entries["deploy-tools:review"].as_entry()

    assert entry_dict["plugin_root"] == str(plugin)
    assert entry_dict["plugin_data"] == str(plugin / "data" / "deploy-tools")


# --------------------------------------------------------------------------- #
# 4. _tool_placeholders：allowed-tools 的 PLUGIN_DATA 展开
# --------------------------------------------------------------------------- #

def test_plugin_skill_allowed_tools_expand_plugin_data(tmp_path: Path) -> None:
    """插件 Skill 的 allowed-tools 里 ``${CLAUDE_PLUGIN_DATA}`` 展开。"""
    plugin = _plugin(tmp_path, "deploy-tools")
    _write(
        plugin / "skills" / "review" / "SKILL.md",
        _skill_text(extra="allowed-tools: 'read_file ${CLAUDE_PLUGIN_DATA}/x'\n"),
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))
    entry = skills.entries["deploy-tools:review"]

    expected_data = _fwd(plugin / "data" / "deploy-tools")
    assert entry.allowed_tools == ("read_file", f"{expected_data}/x")


def test_non_plugin_skill_allowed_tools_keeps_plugin_data_literal(tmp_path: Path) -> None:
    """非插件 Skill 的 allowed-tools 里 ``${CLAUDE_PLUGIN_DATA}`` 保持字面量。"""
    _write(
        tmp_path / ".blank" / "skills" / "local" / "SKILL.md",
        _skill_text(extra="allowed-tools: 'read_file ${CLAUDE_PLUGIN_DATA}/x'\n"),
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))
    entry = skills.entries["local"]

    assert entry.allowed_tools == ("read_file", "${CLAUDE_PLUGIN_DATA}/x")


# --------------------------------------------------------------------------- #
# 5. CommandDefinition 携带 plugin_root / plugin_data
# --------------------------------------------------------------------------- #

def test_plugin_command_definition_carries_plugin_fields(tmp_path: Path) -> None:
    """插件命令的 plugin_root / plugin_data 来自插件包路径。"""
    plugin = _plugin(tmp_path, "deploy-tools")
    _write(
        plugin / "commands" / "review.md",
        _command_text(),
    )

    commands = discover_commands(tmp_path, env=_env(tmp_path))

    entry = commands.entries["deploy-tools:review"]
    assert entry.plugin_root == str(plugin)
    assert entry.plugin_data == str(plugin / "data" / "deploy-tools")


def test_non_plugin_command_has_empty_plugin_fields(tmp_path: Path) -> None:
    """项目层命令的 plugin_root / plugin_data 是空串。"""
    _write(
        tmp_path / ".blank" / "commands" / "deploy.md",
        _command_text(),
    )

    commands = discover_commands(tmp_path, env=_env(tmp_path))

    entry = commands.entries["deploy"]
    assert entry.plugin_root == ""
    assert entry.plugin_data == ""


def test_command_definition_as_entry_includes_plugin_fields(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, "deploy-tools")
    _write(
        plugin / "commands" / "review.md",
        _command_text(),
    )

    commands = discover_commands(tmp_path, env=_env(tmp_path))
    entry_dict = commands.entries["deploy-tools:review"].as_entry()

    assert entry_dict["plugin_root"] == str(plugin)
    assert entry_dict["plugin_data"] == str(plugin / "data" / "deploy-tools")


def test_plugin_command_allowed_tools_expand_plugin_data(tmp_path: Path) -> None:
    """插件命令的 allowed-tools 里 ``${CLAUDE_PLUGIN_DATA}`` 展开。"""
    plugin = _plugin(tmp_path, "deploy-tools")
    _write(
        plugin / "commands" / "review.md",
        _command_text(
            extra="allowed-tools: 'read_file ${CLAUDE_PLUGIN_DATA}/x'\n"
        ),
    )

    commands = discover_commands(tmp_path, env=_env(tmp_path))
    entry = commands.entries["deploy-tools:review"]

    expected_data = _fwd(plugin / "data" / "deploy-tools")
    assert entry.allowed_tools == ("read_file", f"{expected_data}/x")


def test_non_plugin_command_allowed_tools_keeps_plugin_data_literal(tmp_path: Path) -> None:
    """非插件命令的 allowed-tools 里 ``${CLAUDE_PLUGIN_DATA}`` 保持字面量。"""
    _write(
        tmp_path / ".blank" / "commands" / "deploy.md",
        _command_text(
            extra="allowed-tools: 'read_file ${CLAUDE_PLUGIN_DATA}/x'\n"
        ),
    )

    commands = discover_commands(tmp_path, env=_env(tmp_path))
    entry = commands.entries["deploy"]

    assert entry.allowed_tools == ("read_file", "${CLAUDE_PLUGIN_DATA}/x")


# --------------------------------------------------------------------------- #
# 6. 插件发现：plugin_data 目录的 mkdir 副作用（对齐上游 Bwe）
# --------------------------------------------------------------------------- #

def test_plugin_discovery_creates_plugin_data_dir(tmp_path: Path) -> None:
    """上游 ``Bwe``：发现插件时 ``<plugin_root>/data/<plugin_name>`` 目录被创建。"""
    plugin = _plugin(tmp_path, "deploy-tools")
    _write(
        plugin / "commands" / "review.md",
        _command_text(),
    )
    data_dir = plugin / "data" / "deploy-tools"

    # 发现前目录不存在
    assert not data_dir.exists()

    discover_commands(tmp_path, env=_env(tmp_path))

    # 发现后目录被创建（Bwe 的 mkdirSync 副作用）
    assert data_dir.is_dir()


def test_plugin_skill_discovery_creates_plugin_data_dir(tmp_path: Path) -> None:
    """Skill 侧的插件发现同样创建 data 目录。"""
    plugin = _plugin(tmp_path, "deploy-tools")
    _write(
        plugin / "skills" / "review" / "SKILL.md",
        _skill_text(),
    )
    data_dir = plugin / "data" / "deploy-tools"

    assert not data_dir.exists()

    discover_skills(tmp_path, env=_env(tmp_path))

    assert data_dir.is_dir()


# --------------------------------------------------------------------------- #
# 7. expand_user_input：插件命令的正文展开
# --------------------------------------------------------------------------- #

def test_expand_user_input_expands_plugin_placeholders_in_command_body(
    tmp_path: Path,
) -> None:
    """插件命令正文里的 ``${CLAUDE_PLUGIN_ROOT}`` / ``${CLAUDE_PLUGIN_DATA}`` 展开。"""
    plugin = _plugin(tmp_path, "deploy-tools")
    _write(
        plugin / "commands" / "review.md",
        _command_text(
            body="用 ${CLAUDE_PLUGIN_ROOT}/rules 和 ${CLAUDE_PLUGIN_DATA}/db"
        ),
    )

    expanded, info = expand_user_input(
        "/deploy-tools:review",
        root=tmp_path,
        env=_env(tmp_path),
    )

    assert info is not None
    assert info["name"] == "deploy-tools:review"
    assert _fwd(plugin) in expanded
    assert _fwd(plugin / "data" / "deploy-tools") in expanded
    assert "${CLAUDE_PLUGIN_ROOT}" not in expanded
    assert "${CLAUDE_PLUGIN_DATA}" not in expanded


def test_expand_user_input_keeps_plugin_literals_for_non_plugin_command(
    tmp_path: Path,
) -> None:
    """非插件命令正文里的 plugin 占位符保持字面量。"""
    _write(
        tmp_path / ".blank" / "commands" / "deploy.md",
        _command_text(body="用 ${CLAUDE_PLUGIN_ROOT}/rules"),
    )

    expanded, info = expand_user_input(
        "/deploy",
        root=tmp_path,
        env=_env(tmp_path),
    )

    assert info is not None
    assert "${CLAUDE_PLUGIN_ROOT}" in expanded


# --------------------------------------------------------------------------- #
# 8. _definition / _definition_from_dir：直接传参的原子层
# --------------------------------------------------------------------------- #

def test_definition_accepts_plugin_data_param(tmp_path: Path) -> None:
    """``_definition`` 接受 plugin_data 参数并设置到 CommandDefinition。"""
    cmd_path = tmp_path / "cmd.md"
    _write(cmd_path, _command_text())

    definition, errors, warnings = _definition(
        cmd_path, tmp_path, "project",
        plugin_root=r"E:\plugins\p",
        plugin_data=r"E:\plugins\p\data\p",
    )

    assert definition is not None
    assert errors == []
    assert definition.plugin_root == r"E:\plugins\p"
    assert definition.plugin_data == r"E:\plugins\p\data\p"


def test_definition_from_dir_accepts_plugin_params(tmp_path: Path) -> None:
    """``_definition_from_dir`` 接受 plugin_root / plugin_data 并设置到 SkillDefinition。"""
    skill_dir = tmp_path / "myskill"
    _write(skill_dir / "SKILL.md", _skill_text())

    definition, errors, warnings = _definition_from_dir(
        skill_dir, "project",
        plugin_root=r"E:\plugins\p",
        plugin_data=r"E:\plugins\p\data\p",
    )

    assert definition is not None
    assert errors == []
    assert definition.plugin_root == r"E:\plugins\p"
    assert definition.plugin_data == r"E:\plugins\p\data\p"


# --------------------------------------------------------------------------- #
# 9. _skill handler：插件 Skill 正文的占位符展开（穿透层）
# --------------------------------------------------------------------------- #

def _skill_tool():
    return {tool.name: tool for tool in skill_tools()}["skill"]


async def test_skill_handler_expands_plugin_placeholders_in_plugin_skill_body(
    tmp_path: Path,
) -> None:
    """插件 Skill 经 ``_skill`` handler 调用时，正文里的 plugin 占位符展开。

    handler 不把 entry 的 plugin_root/plugin_data 透传给 render_skill，
    插件 Skill 正文里的 ``${CLAUDE_PLUGIN_ROOT}`` / ``${CLAUDE_PLUGIN_DATA}``
    就会保持字面量——与上游 SkillTool 的 C9 展开行为不一致。
    """
    plugin = _plugin(tmp_path, "deploy-tools")
    _write(
        plugin / "skills" / "review" / "SKILL.md",
        _skill_text(body="用 ${CLAUDE_PLUGIN_ROOT}/rules 和 ${CLAUDE_PLUGIN_DATA}/db"),
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))
    catalog = SkillCatalog([d.as_entry() for d in skills.entries.values()])
    context = ToolContext(
        root=tmp_path,
        task_id="task-v307",
        session_factory=None,
        skills=catalog,
        turn_state={},
    )
    tool = _skill_tool()
    outcome = await tool.handler(context, {"skill": "deploy-tools:review"})

    assert outcome.ok
    text = outcome.output
    assert _fwd(plugin) in text
    assert _fwd(plugin / "data" / "deploy-tools") in text
    assert "${CLAUDE_PLUGIN_ROOT}" not in text
    assert "${CLAUDE_PLUGIN_DATA}" not in text


async def test_skill_handler_keeps_plugin_literals_for_non_plugin_skill(
    tmp_path: Path,
) -> None:
    """非插件 Skill 经 handler 调用时，plugin 占位符保持字面量。"""
    _write(
        tmp_path / ".blank" / "skills" / "local" / "SKILL.md",
        _skill_text(body="用 ${CLAUDE_PLUGIN_ROOT}/rules"),
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))
    catalog = SkillCatalog([d.as_entry() for d in skills.entries.values()])
    context = ToolContext(
        root=tmp_path,
        task_id="task-v307",
        session_factory=None,
        skills=catalog,
        turn_state={},
    )
    tool = _skill_tool()
    outcome = await tool.handler(context, {"skill": "local"})

    assert outcome.ok
    assert "${CLAUDE_PLUGIN_ROOT}" in outcome.output
