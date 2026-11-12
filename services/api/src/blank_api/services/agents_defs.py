"""File-based sub-agent definitions — the ``.claude/agents/*.md`` equivalent.

Claude Code lets a project describe its own agent types in Markdown files and
then pick one by ``subagent_type``. The interesting part is not the file format
but the *rules around it*:

* there are two layers — the operator's own agents and the project's — and the
  project wins on a name collision;
* a file without ``name`` is not a definition at all and is skipped silently;
  a file that declares ``name`` but fails validation never loads and is named
  in the error list (``missing description`` is the classic case);
* ``name`` must not start with ``-`` and must not contain ``:`` (reserved for
  plugin namespacing);
* ``tools`` / ``disallowedTools`` accept a comma string or a YAML list, and
  ``*`` in ``tools`` means "no restriction";
* ``skills`` names Skills to preload into the child's startup messages. A
  missing name is reported but never stops the spawn (upstream only warns);
* ``permissionMode`` / ``isolation`` / ``background`` are parsed like upstream
  (``Vf`` / ``fw``, ``worktree|remote``, ``true|false``). A declared mode may
  only *tighten* the child (see ``subagent._child_permission_mode``);
  ``isolation: worktree`` always wins over a caller's ``isolate: false``;
  ``background: true`` wins over ``run_in_background: false``;
* ``maxTurns`` is a positive integer; anything else is ignored with a warning;
* plugin agents fall back to the file name for ``name`` and to
  ``description`` → ``when_to_use`` → ``when-to-use`` → ``Agent from <plugin>
  plugin`` for the description, and ignore ``permissionMode`` / ``hooks`` /
  ``mcpServers`` with a warning (upstream ``Skn``);
* one bad file never takes the whole catalog down.

What a definition still may *not* do here is widen the boundary. We parse
``model`` / ``hooks`` / ``mcpServers`` (and a few other Claude-only knobs) but
never apply them; they are reported per definition so the omission is visible
instead of silently changing what the user thinks they configured. Tool lists
only ever narrow the child pool — a custom agent that lists ``run_shell`` stays
read-only unless it was spawned with ``isolate``. Permission modes only move
toward stricter; a declaration looser than the spawn baseline is ignored.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from blank_api.services.plugins_defs import discover_plugins
from blank_api.services.skills_defs import _tool_list as _parse_tool_specs
from blank_api.services.workspace import mask_path

BUILTIN_AGENT = "general-purpose"
# Claude 的 Agent 工具内建 `fork` 类型：子代理继承主会话到目前为止的全部对话，
# 用同一套系统提示、同一个模型，只是换一个新的上下文窗口接着干。它不是文件型
# 定义——名字留在这里，项目里同名文件会像 general-purpose 一样覆盖内置项。
BUILTIN_FORK = "fork"
# Claude skips files that are not regular files or exceed this byte limit.
MAX_FILE_BYTES = 1_048_576
# The body becomes part of every child system prompt. Claude uses it verbatim;
# we clip it so one huge definition cannot eat the child's whole window.
MAX_INSTRUCTIONS_CHARS = 20_000
PROJECT_AGENTS_DIR = Path(".blank") / "agents"
# Claude 的合法 permissionMode（``fw``）；``Vf`` 把 ``manual`` 当成 ``default``
# 的别名。``auto`` / ``dontAsk`` 我们这边没有对应审批档，解析时按合法值识别
# 但不落进定义，记一条告警并进 ignored 审计。
PERMISSION_MODES = (
    "default",
    "plan",
    "acceptEdits",
    "bypassPermissions",
    "dontAsk",
    "auto",
)
PERMISSION_MODE_ALIASES = {"manual": "default"}
# 本机真有的四种审批档（permissions.MODES 的 Claude 名字）。其余合法值只解析、
# 不生效。
MAPPABLE_PERMISSION_MODES = (
    "default",
    "plan",
    "acceptEdits",
    "bypassPermissions",
)
# Claude applies these; we cannot without widening/altering the parent boundary,
# so a definition that sets them gets them named in the audit instead.
UNSUPPORTED_FIELDS = (
    "model",
    "hooks",
    "mcpServers",
    "memory",
    "effort",
    "color",
    "initialPrompt",
)


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    description: str
    instructions: str = ""
    # None = no restriction (key absent or Claude's ``tools: "*"``), else a whitelist.
    tools: tuple[str, ...] | None = None
    disallowed_tools: tuple[str, ...] = ()
    # Skill 名字，按 Claude 的 ``OI`` 解析：缺省与 null 都是空表，``*`` 保留成
    # 字面名字（它不会解析成功，运行时会按"没找到"告警，与上游一致）。
    skills: tuple[str, ...] = ()
    # Claude 的 ``permissionMode``（原样保存，含大小写）。派发时只能相对基线收紧。
    permission_mode: str = ""
    # ``isolation: worktree`` 时强制隔离；``remote`` 未移植，解析时告警并丢弃。
    isolation: str = ""
    # 上游只认 ``background: true``（``false`` 等同于没写）；true 会压过调用方
    # 的 ``run_in_background=false``。
    background: bool = False
    max_turns: int | None = None
    source: str = "project"
    path: str = ""
    ignored: tuple[str, ...] = ()

    @property
    def builtin(self) -> bool:
        return self.source == "built-in"

    def as_dict(self) -> dict:
        """Audit view: enough to point at the file without copying its body."""
        payload = {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "chars": len(self.instructions),
        }
        if self.path:
            payload["path"] = mask_path(self.path)
        if self.tools is not None:
            payload["tools"] = list(self.tools)
        if self.disallowed_tools:
            payload["disallowedTools"] = list(self.disallowed_tools)
        if self.skills:
            payload["skills"] = list(self.skills)
        if self.permission_mode:
            payload["permissionMode"] = self.permission_mode
        if self.isolation:
            payload["isolation"] = self.isolation
        if self.background:
            payload["background"] = True
        if self.max_turns is not None:
            payload["maxTurns"] = self.max_turns
        if self.ignored:
            payload["ignored"] = list(self.ignored)
        return payload


BUILTIN_GENERAL_PURPOSE = AgentDefinition(
    name=BUILTIN_AGENT,
    description="通用子代理：只读探索；isolate=true 时在临时副本里动手。",
    source="built-in",
)

BUILTIN_FORK_AGENT = AgentDefinition(
    name=BUILTIN_FORK,
    description="当前会话的分身：继承到此为止的完整对话与工具权限，只能前台运行。",
    source="built-in",
)


@dataclass
class AgentCatalog:
    """Resolved definitions plus the files that could not be loaded."""

    entries: dict[str, AgentDefinition] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.entries)

    def __contains__(self, name: object) -> bool:
        return name in self.entries

    def get(self, name: str) -> AgentDefinition | None:
        return self.entries.get(str(name or "").strip())

    def names(self) -> list[str]:
        # Built-ins first, default name leading: it is the type the model should
        # reach for when none is stated, and the error list reads better with it
        # up front. fork comes second because it is a tool-shaped choice, not a
        # general exploration default.
        ordered = [
            name for name in (BUILTIN_AGENT, BUILTIN_FORK) if name in self.entries
        ]
        ordered.extend(
            sorted(name for name in self.entries if name not in (BUILTIN_AGENT, BUILTIN_FORK))
        )
        return ordered

    def custom(self) -> list[AgentDefinition]:
        return [self.entries[name] for name in self.names() if not self.entries[name].builtin]

    def listing(self) -> list[dict]:
        return [
            {
                "name": item.name,
                "description": item.description,
                "source": item.source,
            }
            for item in (self.entries[name] for name in self.names())
        ]

    def as_dict(self) -> dict:
        payload = {
            "count": len(self.entries),
            "agents": [self.entries[name].as_dict() for name in self.names()],
            "errors": list(self.errors),
        }
        if self.warnings:
            payload["warnings"] = list(self.warnings)
        return payload


def user_agents_dir(env: dict[str, str] | None = None) -> Path:
    """Where this operator's own agents live.

    Follows the same override as user-level instructions so a test or a
    portable install can move both with one variable.
    """
    env = env if env is not None else os.environ
    override = str(env.get("BLANK_USER_AGENTS_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    base = str(env.get("BLANK_USER_INSTRUCTIONS_DIR") or "").strip()
    if base:
        return Path(base).expanduser() / "agents"
    return Path.home() / ".blank" / "agents"


def _split_frontmatter(text: str) -> tuple[dict | None, str]:
    """Return (frontmatter, body). ``None`` means the file has no frontmatter."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, normalized.strip()
    end = -1
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() in ("---", "..."):
            end = index
            break
    if end < 0:
        raise ValueError("frontmatter 没有闭合（缺少第二个 ---）")
    raw = "\n".join(lines[1:end])
    try:
        meta = yaml.safe_load(raw) if raw.strip() else {}
    except yaml.YAMLError as exc:
        raise ValueError(f"frontmatter 不是合法 YAML：{exc.__class__.__name__}") from None
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise ValueError("frontmatter 必须是键值映射")
    return meta, "\n".join(lines[end + 1 :]).strip()


_MISSING = object()


def _tool_list(value):
    """Parse ``tools`` / ``disallowedTools`` like upstream ``VW``.

    A missing key (``_MISSING``) means "no restriction"; an explicit ``null``
    or other falsy value means "no tools". The shared parser collapses any
    ``*`` to ``("*",)``; ``VW`` turns that into ``undefined`` — unrestricted
    for ``tools`` and an omitted deny list for ``disallowedTools``.
    """
    if value is _MISSING:
        return None, ""
    items, warning = _parse_tool_specs(value)
    if items == ("*",):
        return None, warning
    return items, warning


def _skills_list(value):
    """Parse ``skills`` like upstream ``OI``.

    Missing key, ``null`` and other falsy values all mean "nothing to preload".
    ``*`` stays a literal name here — unlike ``tools``, upstream does not read it
    as "all skills"; it just fails to resolve and warns at spawn time.
    """
    if value is _MISSING or not value:
        return (), ""
    items, warning = _parse_tool_specs(value)
    if warning:
        warning = warning.replace("工具名", "Skill 名").replace("没有工具", "没有 Skill")
    return items, warning


def _positive_int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _permission_mode(value) -> tuple[str, str, bool]:
    """Parse ``permissionMode`` like upstream ``Vf`` + ``fw``.

    Returns ``(value, warning, ignored)``. ``ignored`` means the key was written
    but nothing was applied, so it belongs in the audit list. ``manual`` is a
    Claude alias for ``default``; ``auto`` / ``dontAsk`` are upstream-valid but
    have no local approval tier, so they are reported rather than guessed at.
    """
    if value is _MISSING or value is None:
        return "", "", False
    if not isinstance(value, str):
        return "", "permissionMode 必须是字符串，已忽略", True
    raw = value.strip()
    canonical = PERMISSION_MODE_ALIASES.get(raw, raw)
    if canonical not in PERMISSION_MODES:
        return (
            "",
            f"permissionMode {raw!r} 无效（可选：{', '.join(PERMISSION_MODES)}），已忽略",
            True,
        )
    if canonical not in MAPPABLE_PERMISSION_MODES:
        return (
            "",
            f"permissionMode {raw!r} 在我们这里没有对应审批档（已移植："
            f"{', '.join(MAPPABLE_PERMISSION_MODES)}），已忽略",
            True,
        )
    return canonical, "", False


def _isolation(value, *, plugin: bool) -> tuple[str, str, bool]:
    """Parse ``isolation`` like upstream (``worktree`` in effect, ``remote`` frozen).

    Upstream plugin agents accept only ``worktree`` and silently drop anything
    else; file agents warn on invalid values. ``remote`` is valid upstream but
    this build has no remote executor, so it is reported and treated as local.
    """
    if value is _MISSING or value is None:
        return "", "", False
    if not isinstance(value, str):
        if plugin:
            return "", "", True
        return "", "isolation 必须是字符串，已忽略", True
    raw = value.strip()
    if raw == "worktree":
        return "worktree", "", False
    if plugin:
        return "", "", True
    if raw == "remote":
        return "", "isolation 'remote'（远程子代理）尚未移植，已按本地运行", True
    return "", f"isolation {raw!r} 无效（可选：worktree, remote），已忽略", True


def _background(value, *, plugin: bool) -> tuple[bool, str, bool]:
    """Parse ``background`` like upstream: only an explicit true is meaningful."""
    if value is _MISSING or value is None:
        return False, "", False
    if value is True or value == "true":
        return True, "", False
    if value is False or value == "false":
        return False, "", False
    if plugin:
        # 上游插件 agent 对非法值静默忽略，不写 warning。
        return False, "", True
    return (
        False,
        f"background {value!r} 无效（只能是 'true' / 'false' / 布尔），已忽略",
        True,
    )


def _definition_from_file(path: Path, source: str) -> tuple[AgentDefinition | None, list[str]]:
    """Parse one file. Returns (definition, errors); a file with no name is silent.

    Plugin sources (``plugin:<name>``) follow upstream ``Skn`` instead of the
    file-agent contract: the name falls back to the file stem, the description
    falls back to ``when_to_use`` / ``when-to-use`` / a generated line, and
    ``permissionMode`` / ``hooks`` / ``mcpServers`` are named as ignored.
    """
    plugin_source = source.startswith("plugin:")
    plugin_name = source.split(":", 1)[1] if plugin_source else ""
    label = "插件 Agent 文件" if plugin_source else "Agent 文件"
    where = mask_path(str(path))
    try:
        if not path.is_file():
            return None, [f"{label} {where} 不是常规文件，已跳过"]
        if path.stat().st_size > MAX_FILE_BYTES:
            return None, [
                f"{label} {where} 超过 {MAX_FILE_BYTES} 字节上限，已跳过"
            ]
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, [f"{label} {where} 读取失败：{exc.__class__.__name__}"]
    try:
        meta, body = _split_frontmatter(text)
    except ValueError as exc:
        return None, [f"{label} {where}：{exc}"]
    if meta is None:
        # No frontmatter = no declaration. Claude skips these silently too.
        return None, []
    raw_name = meta.get("name")
    if plugin_source:
        # 上游 Skn：``String(name)`` 为空时退回文件名（去掉 .md）。
        if raw_name is None:
            name = path.stem
        elif isinstance(raw_name, str):
            name = raw_name.strip() or path.stem
        else:
            name = str(raw_name).strip() or path.stem
    else:
        if raw_name is None:
            return None, []
        if not isinstance(raw_name, str):
            return None, [f"{label} {where} 的 name 必须是字符串"]
        name = raw_name.strip()
        if not name:
            return None, [f"{label} {where} 的 name 为空"]
    if name.startswith("-"):
        return None, [f"{label} {where} 的名称 {name!r} 不能以 \"-\" 开头"]
    if ":" in unicodedata.normalize("NFKC", name):
        return None, [
            f"{label} {where} 的名称 {name!r} 不能包含 \":\""
            "（冒号保留给插件命名空间）"
        ]
    if plugin_source:
        description = ""
        for candidate in (
            meta.get("description"),
            meta.get("when_to_use"),
            meta.get("when-to-use"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                description = candidate.strip()
                break
        if not description:
            description = f"Agent from {plugin_name} plugin"
    else:
        raw_description = meta.get("description")
        if not isinstance(raw_description, str) or not raw_description.strip():
            return None, [f"{label} {where} 缺少必需的 description"]
        description = raw_description.strip()
    warnings: list[str] = []
    ignored_extra: set[str] = set()

    tools, problem = _tool_list(meta["tools"] if "tools" in meta else _MISSING)
    if problem:
        warnings.append(f"{label} {where} 的 tools {problem}")
    disallowed, problem = _tool_list(
        meta["disallowedTools"] if "disallowedTools" in meta else _MISSING
    )
    if problem:
        warnings.append(f"{label} {where} 的 disallowedTools {problem}")
    disallowed = disallowed or ()

    skills, problem = _skills_list(meta["skills"] if "skills" in meta else _MISSING)
    if problem:
        warnings.append(f"{label} {where} 的 skills {problem}")

    permission_mode = ""
    if plugin_source:
        # 上游对插件 agent 的 permissionMode/hooks/mcpServers 一律告警忽略：
        # 这三类控制只认 .blank/agents/ 里的定义。
        for key in ("permissionMode", "hooks", "mcpServers"):
            if meta.get(key) is not None:
                ignored_extra.add(key)
                warnings.append(
                    f"{label} {where} 设置了 {key}，插件 agent 会忽略它；"
                    "需要这类控制请放到 .blank/agents/"
                )
    else:
        permission_mode, problem, unsupported = _permission_mode(
            meta["permissionMode"] if "permissionMode" in meta else _MISSING
        )
        if problem:
            warnings.append(f"{label} {where} 的 {problem}")
        if unsupported:
            ignored_extra.add("permissionMode")

    isolation, problem, unsupported = _isolation(
        meta["isolation"] if "isolation" in meta else _MISSING,
        plugin=plugin_source,
    )
    if problem:
        warnings.append(f"{label} {where} 的 {problem}")
    if unsupported:
        ignored_extra.add("isolation")

    background, problem, unsupported = _background(
        meta["background"] if "background" in meta else _MISSING,
        plugin=plugin_source,
    )
    if problem:
        warnings.append(f"{label} {where} 的 {problem}")
    if unsupported:
        ignored_extra.add("background")

    max_turns = None
    if meta.get("maxTurns") is not None:
        max_turns = _positive_int(meta.get("maxTurns"))
        if max_turns is None:
            warnings.append(
                f"{label} {where} 的 maxTurns 必须是正整数，已忽略"
            )

    if len(body) > MAX_INSTRUCTIONS_CHARS:
        body = body[:MAX_INSTRUCTIONS_CHARS].rstrip() + "\n…（定义正文过长，已截断）"
    governed = tuple(
        dict.fromkeys([*UNSUPPORTED_FIELDS, *sorted(ignored_extra)])
    )
    ignored = tuple(key for key in governed if meta.get(key) is not None)
    return (
        AgentDefinition(
            name=name,
            description=description,
            instructions=body,
            tools=tools,
            disallowed_tools=tuple(disallowed),
            skills=skills,
            permission_mode=permission_mode,
            isolation=isolation,
            background=background,
            max_turns=max_turns,
            source=source,
            path=str(path),
            ignored=ignored,
        ),
        warnings,
    )


def _files_under(root: Path) -> list[Path]:
    try:
        if not root.is_dir():
            return []
        return sorted(
            (item for item in root.rglob("*.md") if item.is_file()),
            key=lambda item: (len(item.parts), str(item)),
        )
    except OSError:
        return []


def discover_agents(root: str | Path, *, env: dict[str, str] | None = None) -> AgentCatalog:
    """Load the user layer, the project layer, then enabled plugins.

    Plugin agents are addressed as ``<plugin>:<agent>`` (Claude's namespace).
    """
    catalog = AgentCatalog(
        entries={BUILTIN_AGENT: BUILTIN_GENERAL_PURPOSE, BUILTIN_FORK: BUILTIN_FORK_AGENT}
    )
    layers = (
        ("user", user_agents_dir(env)),
        ("project", Path(root) / PROJECT_AGENTS_DIR),
    )
    for source, directory in layers:
        # Sorted traversal + "later wins" mirrors Claude's map insertion order:
        # a deeper file in the same layer overrides a shallower one.
        for path in _files_under(directory):
            definition, problems = _definition_from_file(path, source)
            catalog.errors.extend(problems)
            if definition is not None:
                catalog.entries[definition.name] = definition

    plugins = discover_plugins(root, env=env)
    catalog.errors.extend(plugins.errors)
    catalog.warnings.extend(plugins.warnings)
    for plugin in plugins.enabled():
        for directory in plugin.component_dirs("agents"):
            for path in _files_under(directory):
                definition, problems = _definition_from_file(
                    path, f"plugin:{plugin.name}"
                )
                catalog.errors.extend(problems)
                if definition is not None:
                    definition = replace(
                        definition, name=f"{plugin.name}:{definition.name}"
                    )
                    catalog.entries[definition.name] = definition
    return catalog


def load_definition(path: str | Path, *, source: str = "project") -> tuple[AgentDefinition | None, list[str]]:
    """Single-file entry point used by tests and by future editing tools."""
    return _definition_from_file(Path(path), source)
