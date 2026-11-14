"""#302：命令 ``allowed-tools`` 的占位符展开（对齐上游 ``lme`` 的 ``Se``/``C9``）。

上游 ``lme`` 构建命令定义时，对 ``allowed-tools`` 逐条走 ``Se`` 展开器：
``C9`` 先把 ``${CLAUDE_PLUGIN_ROOT}`` / ``${CLAUDE_PROJECT_DIR}`` /
``${CLAUDE_PLUGIN_DATA}`` 替换掉（所有模式都做），``Se`` 再在
``isSkillMode`` 为真时补 ``${CLAUDE_SKILL_DIR}``。命令构建时
``isSkillMode=false``，所以：

* ``${CLAUDE_PROJECT_DIR}`` → 项目根（所有层都展开）；
* ``${CLAUDE_PLUGIN_ROOT}`` → 插件根（仅插件命令）；
* ``${CLAUDE_SKILL_DIR}`` → **不展开**，保持字面量（skill 模式专属）；
* ``disallowed-tools`` → 不展开（上游只改写 ``allowedTools``）；
* 路径统一正斜杠（Windows 反斜杠不能被当成转义）。

kbgai 没有上游 ``${CLAUDE_PLUGIN_DATA}`` 的全局数据目录概念，该项不展开
（记为偏离，见 README）。

注：``_tool_list`` 先按空格/逗号分词（括号内保持完整），``_tool_placeholders``
再逐 token 展开——占位符本身不含空格，所以与上游「先展开再分词」等价。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blank_api.services.commands_defs import discover_commands


# --------------------------------------------------------------------------- #
# 帮助
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate_user_prompt_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """用户层目录指到空目录：本机 ``~/.blank`` 里的东西不能影响判据。"""
    empty = tmp_path / "_no-user-prompts"
    empty.mkdir()
    monkeypatch.setenv("BLANK_USER_COMMANDS_DIR", str(empty / "commands"))
    monkeypatch.setenv("BLANK_USER_SKILLS_DIR", str(empty / "skills"))
    monkeypatch.setenv("BLANK_USER_AGENTS_DIR", str(empty / "agents"))
    monkeypatch.setenv("BLANK_USER_OUTPUT_STYLES_DIR", str(empty / "styles"))
    monkeypatch.setenv("BLANK_USER_PLUGINS_DIR", str(empty / "plugins"))
    yield


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _command(extra: str = "", body: str = "按命令正文执行。") -> str:
    return f"---\ndescription: 命令说明\n{extra}---\n{body}"


def _write_command(root: Path, relative: str, text: str) -> Path:
    return _write(root / ".blank" / "commands" / relative, text)


def _env(tmp_path: Path, *, commands: Path | None = None) -> dict[str, str]:
    empty = tmp_path / "_no-user"
    empty.mkdir(exist_ok=True)
    return {
        "BLANK_USER_PLUGINS_DIR": str(empty / "plugins"),
        "BLANK_USER_COMMANDS_DIR": str(commands or empty / "commands"),
        "BLANK_USER_SKILLS_DIR": str(empty / "skills"),
        "BLANK_USER_AGENTS_DIR": str(empty / "agents"),
        "BLANK_USER_OUTPUT_STYLES_DIR": str(empty / "styles"),
    }


def _plugin(root: Path, name: str) -> Path:
    plugin_dir = root / ".blank" / "plugins" / name
    _write(
        plugin_dir / ".claude-plugin" / "plugin.json",
        json.dumps({"name": name}, ensure_ascii=False),
    )
    return plugin_dir


def _discover(root: Path, env: dict[str, str] | None = None):
    return discover_commands(root, env=env or _env(root))


def _fwd(path: Path) -> str:
    return str(path).replace("\\", "/")


# --------------------------------------------------------------------------- #
# 1. ${CLAUDE_PROJECT_DIR}：所有层都展开
# --------------------------------------------------------------------------- #
def test_project_dir_placeholder_expands_in_project_layer(tmp_path: Path) -> None:
    _write_command(
        tmp_path,
        "deploy.md",
        _command(extra="allowed-tools: 'read_file ${CLAUDE_PROJECT_DIR}/config.toml'\n"),
    )

    entry = _discover(tmp_path).entries["deploy"]

    assert entry.allowed_tools == ("read_file", f"{_fwd(tmp_path)}/config.toml")


def test_project_dir_placeholder_expands_in_user_layer(tmp_path: Path) -> None:
    """用户层命令同样拿到 project_root——上游 ``C9`` 用 ``gn()`` 不分来源。"""
    user_dir = tmp_path / "user-commands"
    _write(
        user_dir / "deploy.md",
        _command(extra="allowed-tools: 'read_file ${CLAUDE_PROJECT_DIR}/x'\n"),
    )

    entry = _discover(tmp_path, _env(tmp_path, commands=user_dir)).entries["deploy"]

    assert entry.allowed_tools == ("read_file", f"{_fwd(tmp_path)}/x")
    assert entry.source == "user"


def test_project_dir_placeholder_not_expanded_without_root(tmp_path: Path) -> None:
    """``root=None`` 时没有项目根，``${CLAUDE_PROJECT_DIR}`` 保持字面量。"""
    user_dir = tmp_path / "user-commands"
    _write(
        user_dir / "deploy.md",
        _command(extra="allowed-tools: 'read_file ${CLAUDE_PROJECT_DIR}/x'\n"),
    )

    found = discover_commands(None, env={"BLANK_USER_COMMANDS_DIR": str(user_dir)})
    entry = found.entries["deploy"]

    assert entry.allowed_tools == ("read_file", "${CLAUDE_PROJECT_DIR}/x")


def test_backslashes_in_project_dir_normalized_to_forward_slashes(tmp_path: Path) -> None:
    """Windows 路径的反斜杠必须归一成正斜杠，否则会被工具规格当成转义。"""
    _write_command(
        tmp_path,
        "deploy.md",
        _command(extra="allowed-tools: 'read_file ${CLAUDE_PROJECT_DIR}'\n"),
    )

    entry = _discover(tmp_path).entries["deploy"]

    # tmp_path 在 Windows 上带反斜杠，展开后必须全正斜杠
    assert "\\" not in entry.allowed_tools[1], entry.allowed_tools
    assert entry.allowed_tools[1] == _fwd(tmp_path)


# --------------------------------------------------------------------------- #
# 2. ${CLAUDE_PLUGIN_ROOT}：仅插件命令展开
# --------------------------------------------------------------------------- #
def test_plugin_root_placeholder_expands_in_plugin_command(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, "deploy-tools")
    _write(
        plugin / "commands" / "review.md",
        _command(extra="allowed-tools: 'read_file ${CLAUDE_PLUGIN_ROOT}/rules.md'\n"),
    )

    entry = _discover(tmp_path).entries["deploy-tools:review"]

    assert entry.allowed_tools == ("read_file", f"{_fwd(plugin)}/rules.md")
    assert entry.source == "plugin:deploy-tools"


def test_plugin_command_expands_both_project_dir_and_plugin_root(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, "deploy-tools")
    _write(
        plugin / "commands" / "review.md",
        _command(
            extra=(
                "allowed-tools: "
                "'read_file ${CLAUDE_PROJECT_DIR}/a ${CLAUDE_PLUGIN_ROOT}/b'\n"
            )
        ),
    )

    entry = _discover(tmp_path).entries["deploy-tools:review"]

    assert entry.allowed_tools == (
        "read_file",
        f"{_fwd(tmp_path)}/a",
        f"{_fwd(plugin)}/b",
    )


def test_plugin_root_not_expanded_for_non_plugin_commands(tmp_path: Path) -> None:
    """项目层命令没有插件根，``${CLAUDE_PLUGIN_ROOT}`` 保持字面量。"""
    _write_command(
        tmp_path,
        "deploy.md",
        _command(extra="allowed-tools: 'read_file ${CLAUDE_PLUGIN_ROOT}/x'\n"),
    )

    entry = _discover(tmp_path).entries["deploy"]

    assert entry.allowed_tools == ("read_file", "${CLAUDE_PLUGIN_ROOT}/x")


# --------------------------------------------------------------------------- #
# 3. ${CLAUDE_SKILL_DIR}：命令不展开（skill 模式专属）
# --------------------------------------------------------------------------- #
def test_skill_dir_placeholder_stays_literal_in_command_allowed_tools(tmp_path: Path) -> None:
    """上游 ``Se`` 只在 ``isSkillMode`` 时展开 SKILL_DIR；命令构建时为 false。"""
    _write_command(
        tmp_path,
        "deploy.md",
        _command(extra="allowed-tools: 'read_file ${CLAUDE_SKILL_DIR}/x'\n"),
    )

    entry = _discover(tmp_path).entries["deploy"]

    assert entry.allowed_tools == ("read_file", "${CLAUDE_SKILL_DIR}/x")


def test_skill_dir_placeholder_stays_literal_in_plugin_command(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path, "deploy-tools")
    _write(
        plugin / "commands" / "review.md",
        _command(extra="allowed-tools: 'read_file ${CLAUDE_SKILL_DIR}/x'\n"),
    )

    entry = _discover(tmp_path).entries["deploy-tools:review"]

    assert entry.allowed_tools == ("read_file", "${CLAUDE_SKILL_DIR}/x")


# --------------------------------------------------------------------------- #
# 4. disallowed-tools 不展开
# --------------------------------------------------------------------------- #
def test_disallowed_tools_placeholders_are_not_expanded(tmp_path: Path) -> None:
    """上游只改写 ``allowedTools``，``disallowed-tools`` 原样保留。"""
    _write_command(
        tmp_path,
        "deploy.md",
        _command(
            extra=(
                "allowed-tools: 'read_file ${CLAUDE_PROJECT_DIR}/a'\n"
                "disallowed-tools:\n"
                "  - 'write_file ${CLAUDE_PROJECT_DIR}/b'\n"
            )
        ),
    )

    entry = _discover(tmp_path).entries["deploy"]

    assert entry.allowed_tools == ("read_file", f"{_fwd(tmp_path)}/a")
    assert entry.disallowed_tools == ("write_file", "${CLAUDE_PROJECT_DIR}/b")


# --------------------------------------------------------------------------- #
# 5. 无占位符与多占位符
# --------------------------------------------------------------------------- #
def test_allowed_tools_without_placeholders_are_unchanged(tmp_path: Path) -> None:
    _write_command(
        tmp_path,
        "deploy.md",
        _command(extra="allowed-tools: 'read_file, Bash(git add)'\n"),
    )

    entry = _discover(tmp_path).entries["deploy"]

    assert entry.allowed_tools == ("read_file", "Bash(git add)")


def test_multiple_placeholders_in_one_tool_spec(tmp_path: Path) -> None:
    _write_command(
        tmp_path,
        "deploy.md",
        _command(
            extra=(
                "allowed-tools: "
                "'read_file ${CLAUDE_PROJECT_DIR}/a ${CLAUDE_PROJECT_DIR}/b'\n"
            )
        ),
    )

    entry = _discover(tmp_path).entries["deploy"]

    assert entry.allowed_tools == (
        "read_file",
        f"{_fwd(tmp_path)}/a",
        f"{_fwd(tmp_path)}/b",
    )


def test_plugin_data_placeholder_stays_literal_as_documented_deviation(tmp_path: Path) -> None:
    """kbgai 没有上游 ``${CLAUDE_PLUGIN_DATA}`` 的全局数据目录概念，保持字面量。"""
    _write_command(
        tmp_path,
        "deploy.md",
        _command(extra="allowed-tools: 'read_file ${CLAUDE_PLUGIN_DATA}/x'\n"),
    )

    entry = _discover(tmp_path).entries["deploy"]

    assert entry.allowed_tools == ("read_file", "${CLAUDE_PLUGIN_DATA}/x")
