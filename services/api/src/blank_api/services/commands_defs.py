"""Custom slash commands — the ``.claude/commands/**/*.md`` equivalent.

Claude keeps two kinds of prompt files that both answer to ``/name``:

* Skills (``.claude/skills/<name>/SKILL.md``) — already handled by
  ``skills_defs``;
* Commands (``.claude/commands/**/*.md``) — one Markdown file per command.

The naming rules are the surprising part and are copied verbatim from Claude's
``Hwr`` / ``mJs`` / ``pJs``:

* a nested file becomes a colon-joined name: ``commands/db/migrate.md`` →
  ``/db:migrate``;
* a file named ``SKILL.md`` in a subdirectory names the **directory**:
  ``commands/db/SKILL.md`` → ``/db``; a ``SKILL.md`` at the commands root has
  no name and is skipped;
* when one directory holds several Markdown files and one of them is
  ``SKILL.md``, only that one is used and a warning says which was chosen.

The frontmatter shares the Skill schema (``description``, ``argument-hint``,
``arguments``, ``allowed-tools`` / ``disallowed-tools``,
``disable-model-invocation``, ``user-invocable``, …). The body uses the same
``$ARGUMENTS`` / ``$1`` / ``$name`` substitution engine (``prompt_args``).

Our boundaries: user layer ``~/.blank/commands``, project layer
``<root>/.blank/commands``; project wins on a name collision; 1 MB per file;
a bad frontmatter degrades instead of skipping; a body over 20,000 chars is
clipped; ``allowed-tools`` / ``disallowed-tools`` are audited but not enforced;
shell execution blocks (``!`cmd` ``) are **not** executed — they stay in the
body as text.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from blank_api.services.prompt_args import argument_names, render_prompt
from blank_api.services.skills_defs import (
    MAX_BODY_CHARS,
    MAX_FILE_BYTES,
    UNSUPPORTED_FIELDS,
    _flag,
    _parse_frontmatter,
    _summary,
    _tool_list,
)
from blank_api.services.workspace import mask_path

PROJECT_COMMANDS_DIR = Path(".blank") / "commands"
# Claude's own scan asks ripgrep for `*.md`; the extension match is
# case-sensitive there (Linux), so we do the same.
COMMAND_SUFFIX = ".md"
SKILL_FILE = "skill.md"

_SLASH = re.compile(r"^/([^\s/]+)(?:\s+(.*))?$", re.DOTALL)


@dataclass(frozen=True)
class CommandDefinition:
    name: str
    description: str
    instructions: str
    argument_hint: str = ""
    argument_names: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    model_invocable: bool = True
    user_invocable: bool = True
    source: str = "project"
    path: str = ""
    base_dir: str = ""
    ignored: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_entry(self) -> dict:
        """Shape consumed by ``tools.skills.SkillCatalog``."""
        return {
            "name": self.name,
            "description": self.description,
            "instructions": self.instructions,
            "display_name": "",
            "argument_hint": self.argument_hint,
            "argument_names": list(self.argument_names),
            "kind": "command",
            "base_dir": self.base_dir,
            "allowed_tools": list(self.allowed_tools),
            "disallowed_tools": list(self.disallowed_tools),
            "model_invocable": self.model_invocable,
            "user_invocable": self.user_invocable,
            "source": self.source,
            "path": self.path,
            "ignored": list(self.ignored),
        }

    def as_dict(self) -> dict:
        payload = {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "chars": len(self.instructions),
            "modelInvocable": self.model_invocable,
            "userInvocable": self.user_invocable,
        }
        if self.argument_hint:
            payload["argumentHint"] = self.argument_hint
        if self.argument_names:
            payload["argumentNames"] = list(self.argument_names)
        if self.path:
            payload["path"] = mask_path(self.path)
        if self.allowed_tools:
            payload["allowedTools"] = list(self.allowed_tools)
        if self.disallowed_tools:
            payload["disallowedTools"] = list(self.disallowed_tools)
        if self.ignored:
            payload["ignored"] = list(self.ignored)
        return payload


@dataclass
class CommandDiscovery:
    entries: dict[str, CommandDefinition] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "count": len(self.entries),
            "commands": [item.as_dict() for item in self.entries.values()],
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def user_commands_dir(env: dict[str, str] | None = None) -> Path:
    env = env if env is not None else os.environ
    override = str(env.get("BLANK_USER_COMMANDS_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    base = str(env.get("BLANK_USER_INSTRUCTIONS_DIR") or "").strip()
    if base:
        return Path(base).expanduser() / "commands"
    return Path.home() / ".blank" / "commands"


def split_slash(text: str) -> tuple[str, str] | None:
    """Split ``/name args``; anything that is not a single-token command is ``None``."""
    match = _SLASH.match(str(text or "").strip())
    if match is None:
        return None
    return match.group(1), match.group(2) or ""


def _markdown_files(directory: Path) -> list[Path]:
    try:
        if not directory.is_dir():
            return []
        return sorted(
            (
                path
                for path in directory.rglob("*" + COMMAND_SUFFIX)
                if path.is_file()
            ),
            key=lambda path: str(path).lower(),
        )
    except OSError:
        return []


def _name_for(path: Path, base: Path) -> tuple[str, str] | None:
    """Return ``(name, base_dir)``; ``None`` means Claude gives this file no name."""
    relative = path.relative_to(base)
    parts = relative.parts
    if path.name.lower() == SKILL_FILE:
        if len(parts) < 2:
            return None
        return ":".join(parts[:-1]), str(path.parent)
    return ":".join(parts)[: -len(COMMAND_SUFFIX)], ""


def _definition(path: Path, base: Path, source: str) -> tuple[CommandDefinition | None, list[str], list[str]]:
    named = _name_for(path, base)
    if named is None:
        return None, [], []
    name, base_dir = named
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None, [
                f"命令文件 {mask_path(str(path))} 超过 {MAX_FILE_BYTES} 字节上限，已跳过"
            ], []
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, [f"命令文件 {mask_path(str(path))} 读取失败：{exc.__class__.__name__}"], []

    meta, body, problem = _parse_frontmatter(text)
    warnings: list[str] = []
    if problem:
        warnings.append(f"命令文件 {mask_path(str(path))}：{problem}")

    raw_description = meta.get("description")
    description = ""
    if isinstance(raw_description, str) and raw_description.strip():
        description = raw_description.strip()
    elif raw_description is not None:
        warnings.append(f"命令文件 {mask_path(str(path))} 的 description 不是字符串，已忽略")
    if not description:
        description = _summary(body)

    allowed_tools, problem = _tool_list(meta.get("allowed-tools"))
    if problem:
        warnings.append(f"命令文件 {mask_path(str(path))} 的 allowed-tools {problem}")
    raw_disallowed = meta.get("disallowed-tools", meta.get("disallowedTools"))
    disallowed_tools, problem = _tool_list(raw_disallowed)
    if problem:
        warnings.append(f"命令文件 {mask_path(str(path))} 的 disallowed-tools {problem}")

    raw_disable = meta.get("disable-model-invocation")
    model_invocable = True
    if raw_disable is not None:
        disabled, problem = _flag(raw_disable, False)
        if problem:
            warnings.append(
                f"命令文件 {mask_path(str(path))} 的 disable-model-invocation {problem}"
            )
        model_invocable = not disabled
    user_invocable, problem = _flag(meta.get("user-invocable"), True)
    if problem:
        warnings.append(f"命令文件 {mask_path(str(path))} 的 user-invocable {problem}")

    raw_hint = meta.get("argument-hint")
    argument_hint = "" if raw_hint is None else str(raw_hint)
    names = argument_names(meta.get("arguments"))

    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS].rstrip() + "\n…（命令正文过长，已截断）"
    ignored = tuple(key for key in UNSUPPORTED_FIELDS if meta.get(key) is not None)
    return (
        CommandDefinition(
            name=name,
            description=description,
            instructions=body,
            argument_hint=argument_hint,
            argument_names=names,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            model_invocable=model_invocable,
            user_invocable=user_invocable,
            source=source,
            path=str(path),
            base_dir=base_dir,
            ignored=ignored,
            warnings=tuple(warnings),
        ),
        [],
        warnings,
    )


def _layer(directory: Path, source: str, discovery: CommandDiscovery) -> None:
    grouped: dict[Path, list[Path]] = {}
    for path in _markdown_files(directory):
        grouped.setdefault(path.parent, []).append(path)
    for parent in sorted(grouped, key=lambda item: str(item).lower()):
        files = grouped[parent]
        skill_files = [path for path in files if path.name.lower() == SKILL_FILE]
        if skill_files:
            # Claude 的告警条件是"这个目录里不止一个 md"——用的是 SKILL.md，
            # 其余文件被它盖住，所以必须说出来。
            if len(files) > 1:
                discovery.warnings.append(
                    f"目录 {mask_path(str(parent))} 下有多个 markdown 文件，使用 {skill_files[0].name}"
                )
            files = skill_files[:1]
        for path in files:
            definition, errors, warnings = _definition(path, directory, source)
            discovery.errors.extend(errors)
            discovery.warnings.extend(warnings)
            if definition is not None:
                discovery.entries[definition.name] = definition


def discover_commands(
    root: str | Path | None, *, env: dict[str, str] | None = None
) -> CommandDiscovery:
    """Load the user layer, then the project layer; project wins by name.

    ``root=None`` means there is no workspace (yet): only the user layer is
    scanned, the same way Claude has no project commands outside a project.
    """
    discovery = CommandDiscovery()
    _layer(user_commands_dir(env), "user", discovery)
    if root is not None:
        _layer(Path(root) / PROJECT_COMMANDS_DIR, "project", discovery)
    return discovery


def user_prompt_catalog(
    root: str | Path | None, *, env: dict[str, str] | None = None
) -> dict[str, dict]:
    """Everything a user can type ``/name`` for: disk Skills plus commands.

    Claude unifies the two surfaces (``/<skill-name>`` works for both). A
    command wins over a same-named Skill because it is the file the user
    deliberately put under ``commands/``.
    """
    from blank_api.services.skills_defs import discover_skills

    merged: dict[str, dict] = {}
    for definition in discover_skills(root, env=env).entries.values():
        merged[definition.name] = definition.as_entry()
    for definition in discover_commands(root, env=env).entries.values():
        merged[definition.name] = definition.as_entry()
    return merged


def expand_user_input(
    text: str,
    *,
    root: str | Path | None,
    session_id: str = "",
    env: dict[str, str] | None = None,
) -> tuple[str, dict | None]:
    """Expand a user message that starts with a known, user-invocable name.

    Unknown names, non-slash text and ``user-invocable: false`` entries are all
    returned unchanged — this app has no "unknown slash command" surface, and
    treating ``/path/to/file`` as an error would break ordinary messages.
    """
    parsed = split_slash(text)
    if parsed is None:
        return text, None
    name, args = parsed
    entry = user_prompt_catalog(root, env=env).get(name)
    if entry is None or not entry.get("user_invocable", True):
        return text, None
    expanded = render_prompt(
        entry.get("instructions") or "",
        args,
        names=tuple(entry.get("argument_names") or ()),
        project_dir=str(root or ""),
        session_id=session_id,
        skill_dir=str(entry.get("base_dir") or ""),
    )
    info = {
        "name": name,
        "source": str(entry.get("source") or ""),
        "kind": str(entry.get("kind") or ""),
        "args_chars": len(args),
        "original_chars": len(str(text)),
        "expanded_chars": len(expanded),
    }
    if entry.get("path"):
        info["path"] = mask_path(str(entry["path"]))
    return expanded, info
