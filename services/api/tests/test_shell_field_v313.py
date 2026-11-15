"""#313：Skill/Command frontmatter 的 ``shell`` 字段与 ``!`` 块解释器。

上游取证（E:/env/rea/out/claude.uniq.txt，Claude Code 2.1.271）：

* schema @6475713：``shell`` —— "Shell for `!`-command blocks: `bash` or
  `powershell`. Defaults to bash regardless of platform."
* ``Osn(e,n)`` @17187055：null → 不声明；trim + 小写；空串 → 不声明；
  ``bash`` / ``powershell`` 之外的告警
  "Frontmatter 'shell: ...' is not recognized. Valid values: bash, powershell.
  Falling back to bash."，字段按未声明处理。
* 插件侧另有可用性检查 @4020389：声明 powershell 但 PATH 上找不到
  ``pwsh`` / ``powershell`` 时告警并回落 bash。

本机口径（有意偏离，记录在 README）：没写 ``shell`` 时仍走原来的 host shell，
不把默认改成 bash——已有命令是按 host shell 写的，换解释器会让它们集体变脸。
显式写 ``shell: bash`` / ``shell: powershell`` 才切换；二进制缺失时静默回落
host shell（与上游"回落 bash"同一意图）。

每条判据都指向一种会走形的具体行为：解析丢字段、把非法值猜成合法值、
不告警、仍记进 ignored、执行层不看该字段、render 链路漏传。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blank_api.services.commands_defs import (
    COMMAND_UNSUPPORTED_FIELDS,
    discover_commands,
    expand_user_input,
)
from blank_api.services.prompt_args import execute_shell_blocks, render_prompt
from blank_api.services.skills_defs import (
    SKILL_UNSUPPORTED_FIELDS,
    UNSUPPORTED_FIELDS,
    SkillDefinition,
    _definition_from_dir,
    discover_skills,
)
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.skills import (
    SkillCatalog,
    preload_skills,
    render_skill,
    skill_tools,
)

from tests.test_skills_disk_v45 import _write_skill


PS_MARK = "ps-only-313"
BASH_MARK_EXPR = "$((6*7))"


def _skill_dir(tmp_path: Path, name: str, extra: str = "", body: str = "正文") -> Path:
    directory = tmp_path / name
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n{extra}---\n{body}\n",
        encoding="utf-8",
    )
    return directory


def _command_env(root: Path, user_dir: Path | None = None) -> dict[str, str]:
    return {"BLANK_USER_COMMANDS_DIR": str(user_dir or (root.parent / "no-user-commands"))}


def _write_command(root: Path, name: str, extra: str = "", body: str = "正文") -> None:
    path = root / ".blank" / "commands" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ndescription: d\n{extra}---\n{body}\n",
        encoding="utf-8",
    )


def _skill_tool():
    return {tool.name: tool for tool in skill_tools()}["skill"]


def _tool_context(catalog: SkillCatalog) -> ToolContext:
    return ToolContext(
        root=Path("."),
        task_id="task-v313",
        session_factory=None,
        skills=catalog,
        turn_state={},
    )


# --------------------------------------------------------------------------- #
# 1. Skill frontmatter 解析
# --------------------------------------------------------------------------- #
def test_skill_shell_defaults_to_empty(tmp_path: Path) -> None:
    definition, _, _ = _definition_from_dir(_skill_dir(tmp_path, "plain"), "project")
    assert definition is not None
    assert definition.shell == ""


def test_skill_shell_parsed(tmp_path: Path) -> None:
    definition, _, _ = _definition_from_dir(
        _skill_dir(tmp_path, "ps", "shell: powershell\n"), "project"
    )
    assert definition is not None
    assert definition.shell == "powershell"


def test_skill_shell_normalizes_case_and_whitespace(tmp_path: Path) -> None:
    definition, _, warnings = _definition_from_dir(
        _skill_dir(tmp_path, "mixed", 'shell: "  PowerShell  "\n'), "project"
    )
    assert definition is not None
    assert definition.shell == "powershell"
    assert not any("shell" in item for item in warnings)


def test_skill_shell_invalid_warns_and_drops(tmp_path: Path) -> None:
    definition, _, warnings = _definition_from_dir(
        _skill_dir(tmp_path, "zsh", "shell: zsh\n"), "project"
    )
    assert definition is not None
    assert definition.shell == ""
    assert any("shell" in item for item in warnings)
    assert any("powershell" in item for item in warnings)
    assert "shell" not in definition.ignored


def test_skill_shell_null_leaves_empty_without_warning(tmp_path: Path) -> None:
    definition, _, warnings = _definition_from_dir(
        _skill_dir(tmp_path, "nullshell", "shell: null\n"), "project"
    )
    assert definition is not None
    assert definition.shell == ""
    assert not any("shell" in item for item in warnings)


def test_skill_shell_non_string_warns(tmp_path: Path) -> None:
    definition, _, warnings = _definition_from_dir(
        _skill_dir(tmp_path, "numshell", "shell: 123\n"), "project"
    )
    assert definition is not None
    assert definition.shell == ""
    assert any("shell" in item for item in warnings)


def test_skill_entry_and_audit_carry_shell(tmp_path: Path) -> None:
    definition, _, _ = _definition_from_dir(
        _skill_dir(tmp_path, "carry", "shell: bash\n"), "project"
    )
    assert definition is not None
    assert definition.as_entry()["shell"] == "bash"
    assert definition.as_dict()["shell"] == "bash"


def test_skill_audit_omits_undeclared_shell() -> None:
    definition = SkillDefinition(name="x", description="d", instructions="b")
    assert "shell" not in definition.as_dict()


def test_unsupported_lists_exclude_shell() -> None:
    assert "shell" not in UNSUPPORTED_FIELDS
    assert "shell" not in SKILL_UNSUPPORTED_FIELDS
    assert "shell" not in COMMAND_UNSUPPORTED_FIELDS


# --------------------------------------------------------------------------- #
# 2. Command frontmatter 解析
# --------------------------------------------------------------------------- #
def test_command_shell_parsed_and_carried(tmp_path: Path) -> None:
    _write_command(tmp_path, "ps", "shell: PowerShell\n")
    entry = discover_commands(tmp_path, env=_command_env(tmp_path)).entries["ps"]
    assert entry.shell == "powershell"
    assert entry.as_entry()["shell"] == "powershell"
    assert entry.as_dict()["shell"] == "powershell"


def test_command_shell_invalid_warns_and_not_ignored(tmp_path: Path) -> None:
    _write_command(tmp_path, "bad", "shell: fish\n")
    discovery = discover_commands(tmp_path, env=_command_env(tmp_path))
    entry = discovery.entries["bad"]
    assert entry.shell == ""
    assert any("shell" in item for item in discovery.warnings)
    assert "shell" not in entry.ignored


# --------------------------------------------------------------------------- #
# 3. 执行层：解释器选择
# --------------------------------------------------------------------------- #
def test_powershell_shell_runs_powershell_syntax(tmp_path: Path) -> None:
    result = execute_shell_blocks(
        f"!Write-Output '{PS_MARK}'", root=tmp_path, shell="powershell"
    )
    assert PS_MARK in result
    assert "Write-Output" not in result


def test_host_shell_does_not_understand_powershell_syntax(tmp_path: Path) -> None:
    """对照格：同一条命令交给 host shell 必须跑不出来——证明上面的判据有区分度。"""
    result = execute_shell_blocks(f"!Write-Output '{PS_MARK}'", root=tmp_path)
    assert PS_MARK not in result


def test_bash_shell_runs_bash_arithmetic(tmp_path: Path) -> None:
    result = execute_shell_blocks(
        f"!echo {BASH_MARK_EXPR}", root=tmp_path, shell="bash"
    )
    assert "42" in result


def test_host_shell_does_not_evaluate_bash_arithmetic(tmp_path: Path) -> None:
    result = execute_shell_blocks(f"!echo {BASH_MARK_EXPR}", root=tmp_path)
    assert "42" not in result


def test_missing_shell_binary_falls_back_to_host(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "blank_api.services.prompt_args._resolve_shell_command", lambda shell: None
    )
    result = execute_shell_blocks(
        "!echo fallback-ok", root=tmp_path, shell="powershell"
    )
    assert "fallback-ok" in result


# --------------------------------------------------------------------------- #
# 4. render 链路
# --------------------------------------------------------------------------- #
def test_render_prompt_passes_shell(tmp_path: Path) -> None:
    rendered = render_prompt(
        f"!Write-Output '{PS_MARK}'", "", project_dir=str(tmp_path), shell="powershell"
    )
    assert PS_MARK in rendered


def test_render_skill_passes_shell(tmp_path: Path) -> None:
    rendered = render_skill(
        f"!Write-Output '{PS_MARK}'",
        "",
        project_dir=str(tmp_path),
        shell="powershell",
    )
    assert PS_MARK in rendered


def test_command_expansion_uses_declared_shell(tmp_path: Path) -> None:
    _write_command(tmp_path, "psrun", "shell: powershell\n", body=f"!Write-Output '{PS_MARK}'\n正文")
    expanded, info = expand_user_input("/psrun", root=tmp_path, env=_command_env(tmp_path))
    assert info is not None
    assert info["name"] == "psrun"
    assert PS_MARK in expanded
    assert "正文" in expanded


def test_preload_skills_uses_declared_shell(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "ps-preload",
        "---\nname: ps-preload\ndescription: d\nshell: powershell\n---\n"
        f"!Write-Output '{PS_MARK}'\n",
    )
    found = discover_skills(tmp_path, env={"BLANK_USER_SKILLS_DIR": str(tmp_path / "none")})
    catalog = SkillCatalog([item.as_entry() for item in found.entries.values()])
    loaded, problems = preload_skills(catalog, ["ps-preload"], project_dir=str(tmp_path))
    assert not problems
    assert len(loaded) == 1
    assert PS_MARK in loaded[0][1]


async def test_skill_tool_handler_uses_declared_shell(tmp_path: Path) -> None:
    catalog = SkillCatalog(
        [
            {
                "name": "psrun",
                "description": "d",
                "instructions": f"!Write-Output '{PS_MARK}'",
                "base_dir": str(tmp_path),
                "shell": "powershell",
            }
        ]
    )
    outcome = await _skill_tool().handler(_tool_context(catalog), {"skill": "psrun"})
    assert outcome.ok
    assert PS_MARK in outcome.output


def test_catalog_entry_carries_shell() -> None:
    catalog = SkillCatalog(
        [{"name": "ps", "description": "d", "instructions": "b", "shell": "powershell"}]
    )
    entry = catalog.get("ps")
    assert entry is not None
    assert entry["shell"] == "powershell"
