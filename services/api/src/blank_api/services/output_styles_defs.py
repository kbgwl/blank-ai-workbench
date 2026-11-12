"""Output styles — the ``.claude/output-styles/*.md`` equivalent.

Claude Code lets the user keep whole personas/working styles as Markdown files
and pick one at a time. Semantics copied from the CLI:

* files live under ``<root>/.blank/output-styles`` (project) and
  ``~/.blank/output-styles`` (operator), scanned recursively; the project layer
  wins on a name collision;
* a style is one ``.md`` file (the extension match is case-insensitive, but
  only a lowercase ``.md`` is stripped from the name — ``Foo.MD`` keeps its
  suffix just like Claude's ``replace(/\\.md$/,"")``);
* ``name`` defaults to the file name, ``description`` to the first body line;
* ``keep-coding-instructions`` decides whether the default coding discipline
  stays in the system prompt when the style is active;
* a regular file over 1 MiB is skipped; a broken frontmatter degrades (fields
  dropped, body kept) instead of removing the style;
* ``force-for-plugin`` is only meaningful for plugin-bundled styles — on a
  user/project style it is ignored with a warning (Claude logs one too).

Boundaries: plugin-bundled styles are discovered through ``.claude-plugin``
manifests but only when the plugin is enabled (``plugins.enabled``), and
``forceForPlugin`` is parsed but not applied. There is no marketplace install
and no separate per-turn "style is active" meta reminder. Our system prompt is
rebuilt for every task/turn, so the style is re-resolved (and re-applied) at
that point.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from blank_api.services.plugins_defs import discover_plugins
from blank_api.services.skills_defs import (
    MAX_FILE_BYTES,
    _parse_frontmatter,
    _summary,
)
from blank_api.services.workspace import mask_path

PROJECT_OUTPUT_STYLES_DIR = Path(".blank") / "output-styles"
STYLE_SUFFIX = ".md"
# Claude's recursive scan stops at 32 levels and 4096 directories.
SCAN_MAX_DEPTH = 32
SCAN_MAX_DIRS = 4096
DEFAULT_STYLE = "default"

_KNOWN_KEYS = ("name", "description", "keep-coding-instructions", "force-for-plugin")
_ALIASES = {
    "keepCodingInstructions": "keep-coding-instructions",
    "forceForPlugin": "force-for-plugin",
}


@dataclass(frozen=True)
class OutputStyle:
    name: str
    description: str
    prompt: str
    source: str = "project"
    path: str = ""
    keep_coding_instructions: bool = False
    force_for_plugin: bool = False
    ignored: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_prompt(self) -> dict:
        """Shape consumed by ``graph.compose_system_prompt``."""
        return {
            "name": self.name,
            "prompt": self.prompt,
            "keep_coding_instructions": self.keep_coding_instructions,
        }

    def as_dict(self) -> dict:
        """Audit view: no body, only a size."""
        payload = {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "chars": len(self.prompt),
            "keepCodingInstructions": self.keep_coding_instructions,
        }
        if self.path:
            payload["path"] = mask_path(self.path)
        if self.ignored:
            payload["ignored"] = list(self.ignored)
        return payload


@dataclass
class OutputStyleDiscovery:
    entries: dict[str, OutputStyle] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def resolve(self, name: str | None) -> OutputStyle | None:
        """The style the settings asked for; unknown names fall back silently.

        Claude does ``allOutputStyles[settings.outputStyle] ?? null`` — a stale
        name in the settings file means "default", not an error.
        """
        key = "" if name is None else str(name)
        if not key.strip():
            return None
        return self.entries.get(key)

    def as_dict(self) -> dict:
        return {
            "count": len(self.entries),
            "styles": [item.as_dict() for item in self.entries.values()],
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def user_output_styles_dir(env: dict[str, str] | None = None) -> Path:
    """Where the operator's own output styles live (mirrors Skills/commands)."""
    env = env if env is not None else os.environ
    override = str(env.get("BLANK_USER_OUTPUT_STYLES_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    base = str(env.get("BLANK_USER_INSTRUCTIONS_DIR") or "").strip()
    if base:
        return Path(base).expanduser() / "output-styles"
    return Path.home() / ".blank" / "output-styles"


def _walk_markdown(directory: Path) -> tuple[list[Path], list[str]]:
    """Recursive ``*.md`` scan with Claude's depth/count guards."""
    files: list[Path] = []
    warnings: list[str] = []
    if not directory.is_dir():
        return files, warnings
    stack: list[tuple[Path, int]] = [(directory, 0)]
    seen = 0
    while stack:
        current, depth = stack.pop()
        seen += 1
        if seen > SCAN_MAX_DIRS:
            warnings.append(
                f"输出样式扫描到 {SCAN_MAX_DIRS} 个目录后停止（root={mask_path(str(directory))}）"
            )
            break
        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            continue
        children: list[Path] = []
        for item in entries:
            if item.is_dir():
                if depth + 1 >= SCAN_MAX_DEPTH:
                    warnings.append(
                        f"输出样式扫描跳过超过 {SCAN_MAX_DEPTH} 层的目录：{mask_path(str(item))}"
                    )
                    continue
                children.append(item)
            elif item.is_file() and item.name.lower().endswith(STYLE_SUFFIX):
                files.append(item)
        stack.extend((child, depth + 1) for child in reversed(children))
    files.sort(key=lambda item: str(item).lower())
    return files, warnings


def _style_name(path: Path) -> str:
    """``L0o(d).replace(/\\.md$/,"")`` — only a lowercase suffix is removed."""
    filename = path.name
    return filename[: -len(STYLE_SUFFIX)] if filename.endswith(STYLE_SUFFIX) else filename


def _style_flag(value, default: bool) -> tuple[bool, str]:
    """Claude's ``m1``: bool as-is, strings/numbers through the truthy sets.

    Numbers go through ``String(value)`` upstream, so ``1.0`` reads as ``"1"``.
    Anything else stays undefined and falls back to the field default.
    """
    if value is None:
        return default, ""
    if isinstance(value, bool):
        return value, ""
    if isinstance(value, (int, float)):
        number = float(value)
        text = str(int(number)) if number.is_integer() else str(number)
    elif isinstance(value, str):
        text = value.strip().lower()
    else:
        text = ""
    if text in ("true", "yes", "1", "on"):
        return True, ""
    if text in ("false", "no", "0", "off"):
        return False, ""
    return default, f"不是布尔值（{value!r}），按 {str(default).lower()} 处理"


def _description(value, path: Path, body: str) -> tuple[str, list[str]]:
    """Claude's ``q2``: strings trimmed, scalars stringified, shapes rejected."""
    warnings: list[str] = []
    if value is None:
        return _summary(body), warnings
    if isinstance(value, bool):
        return ("true" if value else "false"), warnings
    if isinstance(value, str):
        return value.strip() or _summary(body), warnings
    if isinstance(value, (int, float)):
        return str(value), warnings
    warnings.append(
        f"输出样式文件 {mask_path(str(path))} 的 description 不是字符串，已忽略（改用正文首行）"
    )
    return _summary(body), warnings


def _definition(path: Path, source: str, *, plugin: bool = False) -> tuple[OutputStyle | None, list[str], list[str]]:
    warnings: list[str] = []
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None, [
                f"输出样式文件 {mask_path(str(path))} 超过 {MAX_FILE_BYTES} 字节上限，已跳过"
            ], []
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, [
            f"输出样式文件 {mask_path(str(path))} 读取失败：{exc.__class__.__name__}"
        ], []

    meta, body, problem = _parse_frontmatter(text)
    if problem:
        warnings.append(f"输出样式文件 {mask_path(str(path))}：{problem}")

    normalized: dict = {}
    ignored: list[str] = []
    for key, value in meta.items():
        canonical = _ALIASES.get(str(key), str(key))
        if canonical in _KNOWN_KEYS:
            normalized[canonical] = value
        elif str(key).strip():
            ignored.append(str(key))
    if ignored:
        warnings.append(
            f"输出样式文件 {mask_path(str(path))} 含未知字段 {', '.join(ignored)}，已忽略（样式仍加载）"
        )

    fallback = _style_name(path)
    raw_name = normalized.get("name")
    name = ("" if raw_name is None else str(raw_name)) or fallback

    description, extra = _description(normalized.get("description"), path, body)
    warnings.extend(extra)

    keep, issue = _style_flag(normalized.get("keep-coding-instructions"), False)
    if issue:
        warnings.append(
            f"输出样式文件 {mask_path(str(path))} 的 keep-coding-instructions {issue}"
        )
    force, issue = _style_flag(normalized.get("force-for-plugin"), False)
    if issue:
        warnings.append(f"输出样式文件 {mask_path(str(path))} 的 force-for-plugin {issue}")
    if "force-for-plugin" in normalized:
        warnings.append(
            f"输出样式 {name} 的 force-for-plugin 只对 plugin 样式生效，已忽略"
            if not plugin
            else f"输出样式 {name} 的 force-for-plugin 已解析，但本应用不做强制样式"
        )

    return (
        OutputStyle(
            name=name,
            description=description,
            prompt=body.strip(),
            source=source,
            path=str(path),
            keep_coding_instructions=keep,
            force_for_plugin=force,
            ignored=tuple(ignored),
            warnings=tuple(warnings),
        ),
        [],
        warnings,
    )


def _layer(directory: Path, source: str, discovery: OutputStyleDiscovery, *, prefix: str = "") -> None:
    files, warnings = _walk_markdown(directory)
    discovery.warnings.extend(warnings)
    for path in files:
        style, errors, issues = _definition(path, source, plugin=bool(prefix))
        discovery.errors.extend(errors)
        discovery.warnings.extend(issues)
        if style is not None:
            if prefix:
                style = replace(style, name=prefix + style.name)
            discovery.entries[style.name] = style


def discover_output_styles(
    root: str | Path | None, *, env: dict[str, str] | None = None
) -> OutputStyleDiscovery:
    """Load the user layer, the project layer, then enabled plugins.

    Plugin styles are addressed as ``<plugin>:<style>`` (Claude's namespace), so
    a user/project style keeps its bare name and cannot shadow them by accident.
    """
    discovery = OutputStyleDiscovery()
    _layer(user_output_styles_dir(env), "user", discovery)
    if root is not None:
        _layer(Path(root) / PROJECT_OUTPUT_STYLES_DIR, "project", discovery)

    catalog = discover_plugins(root, env=env)
    discovery.errors.extend(catalog.errors)
    discovery.warnings.extend(catalog.warnings)
    for plugin in catalog.enabled():
        for directory in plugin.component_dirs("output-styles"):
            _layer(directory, f"plugin:{plugin.name}", discovery, prefix=f"{plugin.name}:")
    return discovery
