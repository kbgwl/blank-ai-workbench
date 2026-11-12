"""File-based Skill definitions — the ``.claude/skills/<name>/SKILL.md`` equivalent.

Claude Code lets a project (and the operator) keep Skills as files instead of
rows in a database. The format is one directory per Skill with a ``SKILL.md``
inside; the body is the instructions and the frontmatter carries a description
plus a handful of knobs. What matters for us is which of those knobs we can
honour without silently changing behaviour:

* the user layer (``~/.blank/skills``) is loaded first, the project layer
  (``<root>/.blank/skills``) wins on a name collision;
* the invocation name is the **directory name**, exactly like Claude's
  ``/commit`` from ``.claude/skills/commit/SKILL.md``; a frontmatter ``name``
  is kept as a display name only;
* a Skill whose frontmatter fails to parse still loads (Claude drops every
  field instead of skipping the file) — name falls back to the directory and
  description to the first body line; the problem is recorded as a warning;
* a missing ``SKILL.md``, a non-file, a file over 1 MiB and the reserved
  ``synced`` directory are skipped the way Claude skips them;
* ``disable-model-invocation`` is honoured: the Skill is not listed to the
  model and the ``skill`` tool refuses to load it (there is no user slash
  entry point in this app yet, which is reported as a deviation);
* ``allowed-tools`` / ``disallowed-tools`` are parsed and audited but not
  enforced — our tool pool is decided by the gateway, not by a prompt file;
* one bad Skill never takes the whole catalogue down.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from blank_api.services.prompt_args import argument_names
from blank_api.services.workspace import mask_path

# Claude skips Skill files that are not regular files or exceed this limit.
MAX_FILE_BYTES = 1_048_576
# The body is loaded into the conversation; Claude uses it verbatim, we clip
# it so one huge Skill cannot eat the model's window (same cap as ProposeSkills).
MAX_BODY_CHARS = 20_000
# One-line description cost in the system prompt, same as the DB catalogue.
SUMMARY_CHARS = 120
PROJECT_SKILLS_DIR = Path(".blank") / "skills"
# Claude's sync-owned root: not a Skill the user wrote, and loading it here
# would duplicate whatever the sync layer already installed.
RESERVED_DIRS = frozenset({"synced"})
# Parsed for audit only; we do not apply them (see module docstring).
UNSUPPORTED_FIELDS = (
    "model",
    "when_to_use",
    "version",
    "paths",
    "hooks",
    "context",
    "agent",
    "background",
    "fallback",
    "effort",
    "shell",
    "created_by",
    "improved_by",
    "mcpServers",
    "lspServers",
)

_LEADING_MARKS = re.compile(r"^[#>*\-\s]+")


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    description: str
    instructions: str
    display_name: str = ""
    argument_hint: str = ""
    argument_names: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    model_invocable: bool = True
    user_invocable: bool = True
    source: str = "project"
    path: str = ""
    ignored: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_entry(self) -> dict:
        """Shape consumed by ``tools.skills.SkillCatalog``."""
        return {
            "name": self.name,
            "description": self.description,
            "instructions": self.instructions,
            "display_name": self.display_name,
            "argument_hint": self.argument_hint,
            "argument_names": list(self.argument_names),
            "kind": "skill",
            "base_dir": str(Path(self.path).parent) if self.path else "",
            "allowed_tools": list(self.allowed_tools),
            "disallowed_tools": list(self.disallowed_tools),
            "model_invocable": self.model_invocable,
            "user_invocable": self.user_invocable,
            "source": self.source,
            "path": self.path,
            "ignored": list(self.ignored),
        }

    def as_dict(self) -> dict:
        """Audit view: point at the file without copying its body."""
        payload = {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "chars": len(self.instructions),
            "modelInvocable": self.model_invocable,
            "userInvocable": self.user_invocable,
        }
        if self.display_name and self.display_name != self.name:
            payload["displayName"] = self.display_name
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
class SkillDiscovery:
    """Resolved disk Skills plus the files/directories that were rejected."""

    entries: dict[str, SkillDefinition] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "count": len(self.entries),
            "skills": [item.as_dict() for item in self.entries.values()],
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def user_skills_dir(env: dict[str, str] | None = None) -> Path:
    """Where this operator's own Skills live (mirrors ``user_agents_dir``)."""
    env = env if env is not None else os.environ
    override = str(env.get("BLANK_USER_SKILLS_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    base = str(env.get("BLANK_USER_INSTRUCTIONS_DIR") or "").strip()
    if base:
        return Path(base).expanduser() / "skills"
    return Path.home() / ".blank" / "skills"


def _parse_frontmatter(text: str) -> tuple[dict, str, str]:
    """Return ``(frontmatter, body, problem)``; a bad block degrades, never skips.

    Claude's Skill parser keeps the body and drops every field when the YAML
    block is broken. We return an empty mapping plus a warning so the caller
    can still load the file under its directory name.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, normalized.strip(), ""
    end = -1
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() in ("---", "..."):
            end = index
            break
    if end < 0:
        return {}, normalized.strip(), "frontmatter 没有闭合（缺少第二个 ---），字段已全部忽略"
    body = "\n".join(lines[end + 1 :]).strip()
    raw = "\n".join(lines[1:end])
    try:
        meta = yaml.safe_load(raw) if raw.strip() else {}
    except yaml.YAMLError as exc:
        return {}, body, f"frontmatter 不是合法 YAML（{exc.__class__.__name__}），字段已全部忽略"
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        return {}, body, "frontmatter 必须是键值映射，字段已全部忽略"
    return meta, body, ""


def _tool_list(value) -> tuple[tuple[str, ...], str]:
    """Parse a comma string or YAML list; invalid shapes yield ``()``."""
    if value is None:
        return (), ""
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list):
        items = []
        for item in value:
            if isinstance(item, str):
                items.append(item)
            else:
                return (), "列表里只能写工具名字符串；非字符串项已忽略"
    else:
        return (), "必须是逗号分隔的字符串或字符串列表；按没有工具处理"
    return tuple(item.strip() for item in items if item.strip()), ""


def _flag(value, default: bool) -> tuple[bool, str]:
    if value is None:
        return default, ""
    if isinstance(value, bool):
        return value, ""
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "1", "on"):
            return True, ""
        if lowered in ("false", "no", "0", "off"):
            return False, ""
    return default, f"不是布尔值（{value!r}），按 {str(default).lower()} 处理"


def _summary(text: str) -> str:
    for raw in str(text or "").splitlines():
        line = _LEADING_MARKS.sub("", raw).strip()
        if not line:
            continue
        return line if len(line) <= SUMMARY_CHARS else line[: SUMMARY_CHARS - 1] + "…"
    return ""


def _skill_file(directory: Path) -> Path | None:
    """Locate ``SKILL.md`` case-insensitively; Claude's matcher is ``/^skill\\.md$/i``."""
    exact = directory / "SKILL.md"
    if exact.is_file():
        return exact
    try:
        for item in sorted(directory.iterdir(), key=lambda path: path.name.lower()):
            if item.is_file() and item.name.lower() == "skill.md":
                return item
    except OSError:
        return None
    return None


def _definition_from_dir(
    directory: Path, source: str
) -> tuple[SkillDefinition | None, list[str], list[str]]:
    """Parse one ``<name>/SKILL.md``. Returns (definition, errors, warnings)."""
    name = directory.name.strip()
    if name in RESERVED_DIRS:
        return None, [], [f"Skill 目录 {mask_path(str(directory))} 是保留目录名，已跳过"]
    path = _skill_file(directory)
    if path is None:
        return None, [], []
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None, [
                f"Skill 文件 {mask_path(str(path))} 超过 {MAX_FILE_BYTES} 字节上限，已跳过"
            ], []
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, [f"Skill 文件 {mask_path(str(path))} 读取失败：{exc.__class__.__name__}"], []

    meta, body, problem = _parse_frontmatter(text)
    warnings: list[str] = []
    if problem:
        warnings.append(f"Skill 文件 {mask_path(str(path))}：{problem}")

    raw_name = meta.get("name")
    display_name = ""
    if raw_name is not None:
        if isinstance(raw_name, str) and raw_name.strip():
            display_name = raw_name.strip()
        else:
            warnings.append(f"Skill 文件 {mask_path(str(path))} 的 name 不是非空字符串，已忽略")

    raw_description = meta.get("description")
    description = ""
    if isinstance(raw_description, str) and raw_description.strip():
        description = raw_description.strip()
    elif raw_description is not None:
        warnings.append(f"Skill 文件 {mask_path(str(path))} 的 description 不是字符串，已忽略")
    if not description:
        # Claude falls back to the first line of the body when the field is
        # absent — the Skill stays matchable instead of becoming invisible.
        description = _summary(body)

    allowed_tools, problem = _tool_list(meta.get("allowed-tools"))
    if problem:
        warnings.append(f"Skill 文件 {mask_path(str(path))} 的 allowed-tools {problem}")
    raw_disallowed = meta.get("disallowed-tools", meta.get("disallowedTools"))
    disallowed_tools, problem = _tool_list(raw_disallowed)
    if problem:
        warnings.append(f"Skill 文件 {mask_path(str(path))} 的 disallowed-tools {problem}")

    raw_disable = meta.get("disable-model-invocation")
    model_invocable = True
    if raw_disable is not None:
        disabled, problem = _flag(raw_disable, False)
        if problem:
            warnings.append(
                f"Skill 文件 {mask_path(str(path))} 的 disable-model-invocation {problem}"
            )
        model_invocable = not disabled
    user_invocable, problem = _flag(meta.get("user-invocable"), True)
    if problem:
        warnings.append(f"Skill 文件 {mask_path(str(path))} 的 user-invocable {problem}")

    raw_hint = meta.get("argument-hint")
    argument_hint = "" if raw_hint is None else str(raw_hint)
    names = argument_names(meta.get("arguments"))

    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS].rstrip() + "\n…（Skill 正文过长，已截断）"
    ignored = tuple(key for key in UNSUPPORTED_FIELDS if meta.get(key) is not None)
    return (
        SkillDefinition(
            name=name,
            description=description,
            instructions=body,
            display_name=display_name,
            argument_hint=argument_hint,
            argument_names=names,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            model_invocable=model_invocable,
            user_invocable=user_invocable,
            source=source,
            path=str(path),
            ignored=ignored,
            warnings=tuple(warnings),
        ),
        [],
        warnings,
    )


def _skill_dirs(root: Path) -> list[Path]:
    try:
        if not root.is_dir():
            return []
        return sorted(
            (item for item in root.iterdir() if item.is_dir()),
            key=lambda item: item.name,
        )
    except OSError:
        return []


def discover_skills(root: str | Path, *, env: dict[str, str] | None = None) -> SkillDiscovery:
    """Load the user layer, then the project layer; project wins by name."""
    discovery = SkillDiscovery()
    layers = (
        ("user", user_skills_dir(env)),
        ("project", Path(root) / PROJECT_SKILLS_DIR),
    )
    for source, directory in layers:
        for skill_dir in _skill_dirs(directory):
            definition, errors, warnings = _definition_from_dir(skill_dir, source)
            discovery.errors.extend(errors)
            discovery.warnings.extend(warnings)
            if definition is not None:
                discovery.entries[definition.name] = definition
    return discovery


def load_skill(
    path: str | Path, *, source: str = "project"
) -> tuple[SkillDefinition | None, list[str], list[str]]:
    """Single-file entry point used by tests and by future editing tools."""
    return _definition_from_dir(Path(path).parent, source)


def merge_skill_entries(*groups) -> list[dict]:
    """Merge catalogues in precedence order (earlier first, later wins).

    A Skill the model may not invoke cannot hide a same-named Skill it can
    invoke: Claude's command de-duplication keeps the invocable copy in that
    case (``aNs``), and doing otherwise would silently remove a capability
    because a lower layer added a user-only file.
    """
    merged: dict[str, dict] = {}
    for group in groups:
        for entry in group or ():
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            prior = merged.get(name)
            if (
                prior is not None
                and bool(prior.get("model_invocable", True))
                and not bool(entry.get("model_invocable", True))
            ):
                continue
            merged[name] = dict(entry, name=name)
    return list(merged.values())
