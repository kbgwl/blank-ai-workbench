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
* ``maxTurns`` is a positive integer; anything else is ignored with a warning;
* one bad file never takes the whole catalog down.

What a definition may *not* do here is widen the boundary. We parse
``model`` / ``permissionMode`` / ``hooks`` / ``mcpServers`` (and a few other
Claude-only knobs) but never apply them; they are reported per definition so
the omission is visible instead of silently changing what the user thinks they
configured. Tool lists only ever narrow the child pool — a custom agent that
lists ``run_shell`` stays read-only unless it was spawned with ``isolate``.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from blank_api.services.plugins_defs import discover_plugins
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
# Claude applies these; we cannot without widening/altering the parent boundary,
# so a definition that sets them gets them named in the audit instead.
UNSUPPORTED_FIELDS = (
    "model",
    "permissionMode",
    "hooks",
    "mcpServers",
    "skills",
    "isolation",
    "background",
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
    # None = no restriction (Claude's ``tools: "*"``), otherwise a whitelist.
    tools: tuple[str, ...] | None = None
    disallowed_tools: tuple[str, ...] = ()
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


def _tool_list(value) -> tuple[tuple[str, ...] | None, str]:
    """Parse ``tools`` / ``disallowedTools`` into names (``None`` = absent).

    An invalid shape yields ``()`` — the same fail-closed "this definition
    grants nothing" reading Claude gets from its list parser.
    """
    if value is None:
        return None, ""
    warning = ""
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list):
        items = []
        for item in value:
            if isinstance(item, str):
                items.append(item)
            else:
                warning = "列表里只能写工具名字符串；非字符串项已忽略"
    else:
        return (), "必须是逗号分隔的字符串或字符串列表；按没有可用工具处理"
    return tuple(item.strip() for item in items if item.strip()), warning


def _positive_int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _definition_from_file(path: Path, source: str) -> tuple[AgentDefinition | None, list[str]]:
    """Parse one file. Returns (definition, errors); a file with no name is silent."""
    try:
        if not path.is_file():
            return None, [f"Agent 文件 {mask_path(str(path))} 不是常规文件，已跳过"]
        if path.stat().st_size > MAX_FILE_BYTES:
            return None, [
                f"Agent 文件 {mask_path(str(path))} 超过 {MAX_FILE_BYTES} 字节上限，已跳过"
            ]
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, [f"Agent 文件 {mask_path(str(path))} 读取失败：{exc.__class__.__name__}"]
    try:
        meta, body = _split_frontmatter(text)
    except ValueError as exc:
        return None, [f"Agent 文件 {mask_path(str(path))}：{exc}"]
    if meta is None:
        # No frontmatter = no declaration. Claude skips these silently too.
        return None, []
    raw_name = meta.get("name")
    if raw_name is None:
        return None, []
    if not isinstance(raw_name, str):
        return None, [f"Agent 文件 {mask_path(str(path))} 的 name 必须是字符串"]
    name = raw_name.strip()
    if not name:
        return None, [f"Agent 文件 {mask_path(str(path))} 的 name 为空"]
    if name.startswith("-"):
        return None, [
            f"Agent 文件 {mask_path(str(path))} 的名称 {name!r} 不能以 \"-\" 开头"
        ]
    if ":" in unicodedata.normalize("NFKC", name):
        return None, [
            f"Agent 文件 {mask_path(str(path))} 的名称 {name!r} 不能包含 \":\""
            "（冒号保留给插件命名空间）"
        ]
    raw_description = meta.get("description")
    if not isinstance(raw_description, str) or not raw_description.strip():
        return None, [
            f"Agent 文件 {mask_path(str(path))} 缺少必需的 description"
        ]
    description = raw_description.strip()
    warnings: list[str] = []

    tools, problem = _tool_list(meta.get("tools"))
    if problem:
        warnings.append(f"Agent 文件 {mask_path(str(path))} 的 tools {problem}")
    if tools is not None and "*" in tools:
        tools = None
    disallowed, problem = _tool_list(meta.get("disallowedTools"))
    if problem:
        warnings.append(
            f"Agent 文件 {mask_path(str(path))} 的 disallowedTools {problem}"
        )
    disallowed = disallowed or ()

    max_turns = None
    if meta.get("maxTurns") is not None:
        max_turns = _positive_int(meta.get("maxTurns"))
        if max_turns is None:
            warnings.append(
                f"Agent 文件 {mask_path(str(path))} 的 maxTurns 必须是正整数，已忽略"
            )

    if len(body) > MAX_INSTRUCTIONS_CHARS:
        body = body[:MAX_INSTRUCTIONS_CHARS].rstrip() + "\n…（定义正文过长，已截断）"
    ignored = tuple(key for key in UNSUPPORTED_FIELDS if meta.get(key) is not None)
    return (
        AgentDefinition(
            name=name,
            description=description,
            instructions=body,
            tools=tools,
            disallowed_tools=tuple(disallowed),
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
