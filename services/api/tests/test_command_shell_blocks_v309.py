"""#309：`!` shell 执行块（对齐 Claude Code 命令文件里的 ``!command``）。

Claude Code 在渲染命令文件正文时，会把行首 ``!`` 开头的行当 shell 命令跑，
stdout 嵌回正文替换掉 ``!command`` 那一行。本批把这个行为移植进来。

判据设计原则：每条都必须「能失败」——实现缺了、写错了、或压根没接线，
对应判据就红。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blank_api.services.commands_defs import expand_user_input
from blank_api.services.prompt_args import execute_shell_blocks, render_prompt


# --------------------------------------------------------------------------- #
# 帮助
# --------------------------------------------------------------------------- #
def _command(body: str) -> str:
    return f"---\ndescription: 命令说明\n---\n{body}"


def _write_command(root: Path, relative: str, text: str) -> Path:
    path = root / ".blank" / "commands" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _expand(root: Path, text: str, **kwargs):
    env_map = {"BLANK_USER_COMMANDS_DIR": str(root.parent / "no-user-commands")}
    return expand_user_input(text, root=root, env=env_map, **kwargs)


# --------------------------------------------------------------------------- #
# 1. execute_shell_blocks：核心执行
# --------------------------------------------------------------------------- #
def test_shell_block_executes_and_embeds_stdout(tmp_path: Path) -> None:
    """`!echo hello` 被执行，stdout `hello` 嵌回正文。"""
    result = execute_shell_blocks("前文\n!echo hello\n后文", root=tmp_path)
    assert "前文" in result
    assert "后文" in result
    assert "hello" in result
    assert "!echo hello" not in result


def test_multiple_shell_blocks_all_execute(tmp_path: Path) -> None:
    """多条 `!` 行各自独立执行。"""
    result = execute_shell_blocks("!echo first\n!echo second", root=tmp_path)
    assert "first" in result
    assert "second" in result
    assert "!echo first" not in result
    assert "!echo second" not in result


def test_shell_block_with_arguments_expands_first_then_executes(tmp_path: Path) -> None:
    """`$ARGUMENTS` 先展开，再执行 `!` 行。"""
    # expand_arguments 先把 $ARGUMENTS 换成参数值，execute_shell_blocks 再执行
    text = "!echo $ARGUMENTS"
    expanded = render_prompt(text, "世界", project_dir=str(tmp_path))
    assert "世界" in expanded
    assert "!echo" not in expanded


def test_shell_block_with_project_dir_substituted_first(tmp_path: Path) -> None:
    """`${CLAUDE_PROJECT_DIR}` 先替换，再执行 `!` 行。"""
    posix_root = str(tmp_path).replace("\\", "/")
    text = "!echo ${CLAUDE_PROJECT_DIR}"
    expanded = render_prompt(text, "", project_dir=str(tmp_path))
    assert posix_root in expanded
    assert "${CLAUDE_PROJECT_DIR}" not in expanded
    assert "!echo" not in expanded


def test_non_line_start_bang_is_not_executed(tmp_path: Path) -> None:
    """行中 `!` 不是 shell 块，原样保留。"""
    text = "这不是 !echo hello 命令"
    result = execute_shell_blocks(text, root=tmp_path)
    assert "!echo hello" in result
    assert "这不是" in result


def test_failed_command_embeds_error_output(tmp_path: Path) -> None:
    """命令失败时，错误信息嵌回正文。"""
    result = execute_shell_blocks("!nonexistent_cmd_xyz_123", root=tmp_path)
    assert "!nonexistent_cmd_xyz_123" not in result
    # 失败命令的输出（stderr 或错误标记）必须嵌回来
    assert "nonexistent_cmd_xyz_123" in result.lower() or "失败" in result or "error" in result.lower()


def test_empty_root_skips_execution(tmp_path: Path) -> None:
    """root 为空时不执行，`!` 行原样保留。"""
    text = "!echo hello"
    result = execute_shell_blocks(text, root="")
    assert result == text
    assert "!echo hello" in result


def test_bang_alone_is_not_executed(tmp_path: Path) -> None:
    """单独一个 `!`（后面没有命令）不算 shell 块，原样保留。"""
    text = "前文\n!\n后文"
    result = execute_shell_blocks(text, root=tmp_path)
    assert "!\n" in result or result == text


# --------------------------------------------------------------------------- #
# 2. expand_user_input：端到端接线
# --------------------------------------------------------------------------- #
def test_expand_user_input_executes_shell_blocks(tmp_path: Path) -> None:
    """通过 `/name` 展开命令时，`!` 行被执行并嵌回。"""
    _write_command(tmp_path, "ping.md", _command(body="!echo pong\n执行命令"))

    expanded, info = _expand(tmp_path, "/ping")

    assert info is not None
    assert info["name"] == "ping"
    assert "pong" in expanded
    assert "执行命令" in expanded
    assert "!echo pong" not in expanded


def test_render_prompt_without_project_dir_leaves_bang_literal() -> None:
    """project_dir 为空时，`!` 行原样留在正文。"""
    text = "!echo hello\n正文"
    expanded = render_prompt(text, "", project_dir="")
    assert "!echo hello" in expanded
    assert "正文" in expanded
