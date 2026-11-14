"""Skill frontmatter hooks（对齐上游 lJs）——可失败判据。

上游 ``lJs``（Skill frontmatter hooks 解析器）的语义：

1. **顶层 PreToolUse/PermissionRequest**（不在 ``hooks`` 内）→ 报错，
   ``unloadableGuard=true``。这两个事件必须写在 ``hooks`` 块里。
2. **无 ``hooks`` 字段** → ``hooks=void 0, unloadableGuard=false``（正常加载）。
3. **hooks 结构无效**（坏事件名、坏类型、缺命令）→ ``unloadableGuard=true``。
4. **unloadableGuard=true 时**：Skill 仍加载（正文可用），但**无 hooks 且无
   allowed-tools**——上游 ``Se=pe?[]:OI(e["allowed-tools"])``，``pe`` 为真时
   allowed-tools 也被清空。
5. **hooks 有效时**：hooks 存入 SkillDefinition，``unloadableGuard=false``，
   allowed-tools 保留。
6. **MCP-sourced Skill**：``hooks`` 被忽略并告警 "MCP-sourced skills cannot
   register hooks"（本应用无 MCP-sourced Skill，不覆盖此路径）。

当前 kbgai ``skills_defs.py`` 把 ``hooks`` 放进 ``UNSUPPORTED_FIELDS``（只进
``ignored`` 审计、不执行）。本测试在实现改动前应当红。
"""

from __future__ import annotations

from pathlib import Path

from blank_api.services.skills_defs import discover_skills, load_skill
from blank_api.services.hooks import HookRunner, HookSpec
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.skills import SkillCatalog, skill_tools


def _env(tmp_path: Path) -> dict[str, str]:
    """四个用户层目录指到空目录，插件根指到空。"""
    empty = tmp_path / "_no-user"
    empty.mkdir(exist_ok=True)
    return {
        "BLANK_USER_PLUGINS_DIR": str(empty / "plugins"),
        "BLANK_USER_COMMANDS_DIR": str(empty / "commands"),
        "BLANK_USER_SKILLS_DIR": str(empty / "skills"),
        "BLANK_USER_AGENTS_DIR": str(empty / "agents"),
        "BLANK_USER_OUTPUT_STYLES_DIR": str(empty / "styles"),
    }


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 判据 1：Skill frontmatter hooks 被解析并存入 SkillDefinition
# ---------------------------------------------------------------------------


def test_skill_hooks_parsed_into_definition(tmp_path: Path) -> None:
    """Skill frontmatter 声明 hooks → 解析进 SkillDefinition，unloadable_guard=False。"""
    skill_dir = tmp_path / ".blank" / "skills" / "my-skill"
    _write(
        skill_dir / "SKILL.md",
        "---\n"
        "description: 有钩子的技能\n"
        "hooks:\n"
        "  pre_tool_use:\n"
        "    - matcher: Write\n"
        "      hooks:\n"
        "        - type: command\n"
        "          command: echo guard\n"
        "---\n"
        "正文",
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))

    assert "my-skill" in skills.entries
    entry = skills.entries["my-skill"]
    assert entry.unloadable_guard is False
    assert "pre_tool_use" in entry.hooks
    assert len(entry.hooks["pre_tool_use"]) == 1
    spec = entry.hooks["pre_tool_use"][0]
    assert spec.command == "echo guard"
    assert spec.match == "Write"


# ---------------------------------------------------------------------------
# 判据 2：无 hooks 字段时 hooks 为空、unloadable_guard=False
# ---------------------------------------------------------------------------


def test_skill_no_hooks_field_defaults_empty(tmp_path: Path) -> None:
    """Skill 没声明 hooks → hooks 为空、unloadable_guard=False。"""
    skill_dir = tmp_path / ".blank" / "skills" / "no-hooks"
    _write(
        skill_dir / "SKILL.md",
        "---\ndescription: 无钩子\n---\n正文",
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))

    assert "no-hooks" in skills.entries
    entry = skills.entries["no-hooks"]
    assert entry.hooks == {}
    assert entry.unloadable_guard is False


# ---------------------------------------------------------------------------
# 判据 3：无效 hooks（坏事件名）→ unloadable_guard=True、hooks 为空、allowed_tools 清空
# ---------------------------------------------------------------------------


def test_skill_invalid_hooks_event_name_sets_unloadable_guard(tmp_path: Path) -> None:
    """hooks 里写了未支持的事件名 → unloadable_guard=True，hooks 为空，allowed_tools 清空。"""
    skill_dir = tmp_path / ".blank" / "skills" / "bad-hooks"
    _write(
        skill_dir / "SKILL.md",
        "---\n"
        "description: 坏钩子\n"
        "allowed-tools: Read\n"
        "hooks:\n"
        "  not_a_real_event:\n"
        "    - matcher: Write\n"
        "      hooks:\n"
        "        - type: command\n"
        "          command: echo bad\n"
        "---\n"
        "正文",
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))

    assert "bad-hooks" in skills.entries
    entry = skills.entries["bad-hooks"]
    assert entry.unloadable_guard is True
    assert entry.hooks == {}
    # 上游 lJs：unloadableGuard=true 时 allowed-tools 也被清空
    assert entry.allowed_tools == ()


# ---------------------------------------------------------------------------
# 判据 4：顶层 PreToolUse/PermissionRequest（不在 hooks 内）→ unloadable_guard=True
# ---------------------------------------------------------------------------


def test_skill_top_level_pre_tool_use_sets_unloadable_guard(tmp_path: Path) -> None:
    """PreToolUse 写在顶层（不在 hooks 内）→ unloadable_guard=True。"""
    skill_dir = tmp_path / ".blank" / "skills" / "top-level-hook"
    _write(
        skill_dir / "SKILL.md",
        "---\n"
        "description: 顶层钩子\n"
        "pre_tool_use:\n"
        "  - matcher: Write\n"
        "    hooks:\n"
        "      - type: command\n"
        "        command: echo bad\n"
        "---\n"
        "正文",
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))

    assert "top-level-hook" in skills.entries
    entry = skills.entries["top-level-hook"]
    assert entry.unloadable_guard is True
    assert entry.hooks == {}


def test_skill_top_level_permission_request_sets_unloadable_guard(tmp_path: Path) -> None:
    """PermissionRequest 写在顶层（不在 hooks 内）→ unloadable_guard=True。"""
    skill_dir = tmp_path / ".blank" / "skills" / "top-level-pr"
    _write(
        skill_dir / "SKILL.md",
        "---\n"
        "description: 顶层权限钩子\n"
        "permission_request:\n"
        "  - matcher: Write\n"
        "    hooks:\n"
        "      - type: command\n"
        "        command: echo bad\n"
        "---\n"
        "正文",
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))

    assert "top-level-pr" in skills.entries
    entry = skills.entries["top-level-pr"]
    assert entry.unloadable_guard is True
    assert entry.hooks == {}


# ---------------------------------------------------------------------------
# 判据 5：有效 hooks + allowed-tools → 两者都保留
# ---------------------------------------------------------------------------


def test_skill_valid_hooks_preserve_allowed_tools(tmp_path: Path) -> None:
    """hooks 有效时 allowed-tools 不被清空。"""
    skill_dir = tmp_path / ".blank" / "skills" / "good-hooks"
    _write(
        skill_dir / "SKILL.md",
        "---\n"
        "description: 好钩子\n"
        "allowed-tools: Read, Write\n"
        "hooks:\n"
        "  pre_tool_use:\n"
        "    - matcher: Write\n"
        "      hooks:\n"
        "        - type: command\n"
        "          command: echo guard\n"
        "---\n"
        "正文",
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))

    assert "good-hooks" in skills.entries
    entry = skills.entries["good-hooks"]
    assert entry.unloadable_guard is False
    assert entry.hooks != {}
    assert entry.allowed_tools == ("Read", "Write")


# ---------------------------------------------------------------------------
# 判据 6：hooks 无效（缺命令）→ unloadable_guard=True
# ---------------------------------------------------------------------------


def test_skill_hooks_missing_command_sets_unloadable_guard(tmp_path: Path) -> None:
    """hooks 里 command 型钩子缺 command 字段 → unloadable_guard=True。"""
    skill_dir = tmp_path / ".blank" / "skills" / "no-command"
    _write(
        skill_dir / "SKILL.md",
        "---\n"
        "description: 缺命令\n"
        "hooks:\n"
        "  pre_tool_use:\n"
        "    - matcher: Write\n"
        "      hooks:\n"
        "        - type: command\n"
        "---\n"
        "正文",
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))

    assert "no-command" in skills.entries
    entry = skills.entries["no-command"]
    assert entry.unloadable_guard is True
    assert entry.hooks == {}


# ---------------------------------------------------------------------------
# 判据 7：hooks 从 SkillDefinition 传到 SkillCatalog（as_entry 携带）
# ---------------------------------------------------------------------------


def test_skill_hooks_carried_in_as_entry(tmp_path: Path) -> None:
    """SkillDefinition.as_entry() 携带 hooks 与 unloadable_guard。"""
    skill_dir = tmp_path / ".blank" / "skills" / "entry-hooks"
    _write(
        skill_dir / "SKILL.md",
        "---\n"
        "description: 带钩子\n"
        "hooks:\n"
        "  post_tool_use:\n"
        "    - matcher: Bash\n"
        "      hooks:\n"
        "        - type: command\n"
        "          command: echo done\n"
        "---\n"
        "正文",
    )

    skills = discover_skills(tmp_path, env=_env(tmp_path))
    entry = skills.entries["entry-hooks"]
    entry_dict = entry.as_entry()

    assert "hooks" in entry_dict
    assert "post_tool_use" in entry_dict["hooks"]
    assert entry_dict["unloadable_guard"] is False


# ---------------------------------------------------------------------------
# 判据 8：load_skill 单文件入口也解析 hooks
# ---------------------------------------------------------------------------


def test_load_skill_parses_hooks(tmp_path: Path) -> None:
    """load_skill（单文件入口）同样解析 hooks。"""
    skill_dir = tmp_path / "single-skill"
    _write(
        skill_dir / "SKILL.md",
        "---\n"
        "description: 单文件钩子\n"
        "hooks:\n"
        "  pre_tool_use:\n"
        "    - matcher: Edit\n"
        "      hooks:\n"
        "        - type: command\n"
        "          command: echo single\n"
        "---\n"
        "正文",
    )

    definition, errors, warnings = load_skill(
        skill_dir / "SKILL.md",
        source="project",
        project_root=tmp_path,
    )

    assert definition is not None
    assert definition.unloadable_guard is False
    assert "pre_tool_use" in definition.hooks
    assert definition.hooks["pre_tool_use"][0].command == "echo single"


# ---------------------------------------------------------------------------
# 判据 9：Skill 加载时钩子注册进 HookRunner
# ---------------------------------------------------------------------------


async def test_skill_hooks_registered_on_load(tmp_path: Path) -> None:
    """Skill 加载时 register_skill_hooks 把钩子并入 HookRunner。"""
    spec = HookSpec(command="echo guard", match="Write", source="skill")
    catalog = SkillCatalog(
        [{"name": "deploy", "instructions": "正文", "hooks": {"pre_tool_use": [spec]}}]
    )
    runner = HookRunner()
    context = ToolContext(
        root=tmp_path,
        task_id="task-skill",
        session_factory=None,
        skills=catalog,
        hooks=runner,
    )
    tool = {t.name: t for t in skill_tools()}["skill"]
    outcome = await tool.handler(context, {"skill": "deploy"})

    assert outcome.ok, outcome.message
    assert "pre_tool_use" in runner.hooks
    assert len(runner.hooks["pre_tool_use"]) == 1
    assert runner.hooks["pre_tool_use"][0].command == "echo guard"


async def test_skill_unloadable_guard_skips_registration(tmp_path: Path) -> None:
    """unloadable_guard=True 时加载 Skill 但不注册钩子。"""
    spec = HookSpec(command="echo guard", match="Write", source="skill")
    catalog = SkillCatalog(
        [
            {
                "name": "bad",
                "instructions": "正文",
                "hooks": {"pre_tool_use": [spec]},
                "unloadable_guard": True,
            }
        ]
    )
    runner = HookRunner()
    context = ToolContext(
        root=tmp_path,
        task_id="task-skill",
        session_factory=None,
        skills=catalog,
        hooks=runner,
    )
    tool = {t.name: t for t in skill_tools()}["skill"]
    outcome = await tool.handler(context, {"skill": "bad"})

    assert outcome.ok, outcome.message
    assert "pre_tool_use" not in runner.hooks
