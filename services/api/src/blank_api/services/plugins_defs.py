"""Plugin bundles — the ``.claude-plugin/plugin.json`` equivalent.

Claude Code plugins are folders with a manifest at
``.claude-plugin/plugin.json`` that bundle components. Every component is
namespaced under the plugin: an agent ``reviewer`` in plugin ``deploy-tools``
is addressed as ``deploy-tools:reviewer``.

Our boundaries (S7):

* user layer ``~/.blank/plugins/<plugin>/.claude-plugin/plugin.json``
  (override ``BLANK_USER_PLUGINS_DIR``), project layer
  ``<root>/.blank/plugins/<plugin>/...``; the project wins on a name collision;
* only prompt components are wired in: ``commands``, ``agents``, ``skills``,
  ``outputStyles``. ``hooks`` / ``mcpServers`` / ``lspServers`` and a bundled
  ``.mcp.json`` are reported as ``ignored`` — a repository that drops a plugin
  folder must not gain process execution or outbound connections;
* enablement follows Claude's ``enabledPlugins`` map (``name`` or
  ``name@marketplace`` keys, boolean values). A plugin with no explicit entry
  uses the manifest's ``defaultEnabled`` (Claude's default is ``true``);
* component path overrides must be relative, start with ``./`` and stay inside
  the plugin folder; anything else is reported and the default folder is used;
* a broken manifest skips that one plugin and is named in ``errors``; one bad
  plugin never takes the catalogue down.
"""

from __future__ import annotations

import json
import os
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from blank_api.services.workspace import mask_path

PLUGIN_MANIFEST = Path(".claude-plugin") / "plugin.json"
MAX_MANIFEST_BYTES = 1_048_576
PROJECT_PLUGINS_DIR = Path(".blank") / "plugins"

# Component name → manifest key. The manifest uses camelCase for output styles.
COMPONENTS: tuple[str, ...] = ("commands", "agents", "skills", "output-styles")
MANIFEST_COMPONENT_KEYS: dict[str, str] = {
    "commands": "commands",
    "agents": "agents",
    "skills": "skills",
    "output-styles": "outputStyles",
}
# Components Claude can carry that we deliberately do not wire in.
IGNORED_COMPONENTS: dict[str, str] = {
    "hooks": "插件 hooks 未执行（本应用不做插件级钩子）",
    "mcpServers": "插件 MCP 服务端未加载（请写进 mcp.json）",
    "lspServers": "插件 LSP 服务端未加载",
}


@dataclass(frozen=True)
class PluginDefinition:
    name: str
    path: str
    source: str
    description: str = ""
    version: str = ""
    author: str = ""
    enabled: bool = True
    default_enabled: bool = True
    # component → directories, in the order they should be scanned
    components: tuple[tuple[str, tuple[str, ...]], ...] = ()
    ignored: tuple[tuple[str, str], ...] = ()
    warnings: tuple[str, ...] = ()

    def component_dirs(self, component: str) -> tuple[Path, ...]:
        for name, dirs in self.components:
            if name == component:
                return tuple(Path(item) for item in dirs)
        return ()

    def as_dict(self) -> dict:
        payload = {
            "name": self.name,
            "source": self.source,
            "path": mask_path(self.path),
            "enabled": self.enabled,
            "defaultEnabled": self.default_enabled,
        }
        if self.description:
            payload["description"] = self.description
        if self.version:
            payload["version"] = self.version
        if self.author:
            payload["author"] = self.author
        counts = {
            name: _count_component(name, dirs)
            for name, dirs in self.components
        }
        if counts:
            payload["components"] = counts
        if self.ignored:
            payload["ignored"] = [{"section": key, "reason": reason} for key, reason in self.ignored]
        if self.warnings:
            payload["warnings"] = list(self.warnings)
        return payload


@dataclass
class PluginCatalog:
    entries: dict[str, PluginDefinition] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def enabled(self) -> list[PluginDefinition]:
        return [item for item in self.entries.values() if item.enabled]

    def as_dict(self) -> dict:
        return {
            "count": len(self.entries),
            "plugins": [item.as_dict() for item in self.entries.values()],
            "enabled": [item.name for item in self.entries.values() if item.enabled],
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def user_plugins_dir(env: dict[str, str] | None = None) -> Path:
    """Where this operator's plugin bundles live (mirrors Skills/commands)."""
    env = env if env is not None else os.environ
    override = str(env.get("BLANK_USER_PLUGINS_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    base = str(env.get("BLANK_USER_INSTRUCTIONS_DIR") or "").strip()
    if base:
        return Path(base).expanduser() / "plugins"
    return Path.home() / ".blank" / "plugins"


def project_plugins_dir(root: str | Path | None) -> Path | None:
    if root is None or not str(root).strip():
        return None
    return Path(root) / PROJECT_PLUGINS_DIR


def enabled_flags(
    root: str | Path | None, *, env: dict[str, str] | None = None
) -> dict[str, bool]:
    """``enabledPlugins`` resolved through the settings cascade, keyed by base name.

    Claude stores ``"name@marketplace": true``; we have no marketplace layer, so
    the marketplace suffix is dropped when matching. Several keys for the same
    base name resolve to the last one written (dict order = file order).
    """
    from blank_api.services.settings import SettingsStore

    layered = SettingsStore().read_layered(root, env=env)
    raw = layered.settings.get("plugins", {}).get("enabled", {})
    resolved: dict[str, bool] = {}
    for key, value in raw.items():
        base = str(key).split("@", 1)[0].strip()
        if base:
            resolved[base] = bool(value)
    return resolved


def discover_plugins(
    root: str | Path | None, *, env: dict[str, str] | None = None
) -> PluginCatalog:
    """Scan the user and project plugin roots once.

    ``root=None`` means "no workspace yet": only user plugins are scanned, the
    same way the other definition loaders treat a missing workspace.
    """
    catalog = PluginCatalog()
    try:
        flags = enabled_flags(root, env=env)
    except (OSError, ValueError) as exc:
        catalog.warnings.append(f"读取插件启用设置失败，按清单默认值处理：{exc.__class__.__name__}")
        flags = {}

    layers = (("user", user_plugins_dir(env)), ("project", project_plugins_dir(root)))
    seen: dict[str, str] = {}
    for source, directory in layers:
        if directory is None:
            continue
        for plugin_dir in _plugin_dirs(directory, catalog):
            definition = _read_plugin(plugin_dir, source, flags, catalog)
            if definition is None:
                continue
            prior = seen.get(definition.name)
            if prior is not None:
                if prior == source:
                    catalog.warnings.append(
                        f"插件 {definition.name} 在 {source} 层重复出现，保留先发现的那份"
                    )
                    continue
                catalog.warnings.append(
                    f"插件 {definition.name} 同时存在于 {prior} 层与 {source} 层，使用 {source} 层"
                )
            catalog.entries[definition.name] = definition
            seen[definition.name] = source

    for name, flag in flags.items():
        if flag and name not in catalog.entries:
            catalog.warnings.append(f"设置在 enabledPlugins 里启用了插件 {name}，但本机没有找到它的目录")
    return catalog


def _plugin_dirs(root: Path, catalog: PluginCatalog) -> list[Path]:
    try:
        if not root.is_dir():
            return []
        items = sorted(root.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        return []
    found: list[Path] = []
    for item in items:
        if not item.is_dir() or item.name.startswith("."):
            continue
        if (item / PLUGIN_MANIFEST).is_file():
            found.append(item)
        else:
            catalog.warnings.append(
                f"插件目录 {mask_path(item)} 缺少 {PLUGIN_MANIFEST.as_posix()}，已跳过"
            )
    return found


def _read_plugin(
    plugin_dir: Path,
    source: str,
    flags: dict[str, bool],
    catalog: PluginCatalog,
) -> PluginDefinition | None:
    manifest_path = plugin_dir / PLUGIN_MANIFEST
    # 只报插件目录名（打码后是叶子名），用户才知道是哪一份清单坏了。
    label = mask_path(plugin_dir)
    try:
        if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            catalog.errors.append(f"插件 {label} 的清单超过 1 MiB，已跳过")
            return None
        raw = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError) as exc:
        catalog.errors.append(
            f"插件 {label} 的清单读取失败：{exc.__class__.__name__}"
        )
        return None
    except json.JSONDecodeError as exc:
        catalog.errors.append(
            f"插件 {label} 的清单不是合法 JSON（第 {exc.lineno} 行），已跳过"
        )
        return None
    if not isinstance(raw, dict):
        catalog.errors.append(f"插件 {label} 的清单顶层不是对象，已跳过")
        return None

    raw_name = raw.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        catalog.errors.append(f"插件 {label} 的清单缺少 name，已跳过")
        return None
    name = raw_name.strip()
    if name.startswith("-"):
        catalog.errors.append(
            f"插件 {label} 的名称 {name!r} 不能以 \"-\" 开头，已跳过"
        )
        return None
    if ":" in unicodedata.normalize("NFKC", name):
        catalog.errors.append(
            f"插件 {label} 的名称 {name!r} 不能包含 \":\"（冒号保留给插件命名空间），已跳过"
        )
        return None

    warnings: list[str] = []
    description = _text_field(raw, "description", label, warnings)
    version = _text_field(raw, "version", label, warnings)
    author = _author(raw.get("author"), label, warnings)

    default_enabled = True
    if raw.get("defaultEnabled") is not None:
        value = raw.get("defaultEnabled")
        if isinstance(value, bool):
            default_enabled = value
        else:
            warnings.append(
                f"插件 {name} 的 defaultEnabled 不是布尔值（{value!r}），按 true 处理"
            )

    ignored: list[tuple[str, str]] = []
    for key, reason in IGNORED_COMPONENTS.items():
        value = raw.get(key)
        if value not in (None, {}, [], ""):
            ignored.append((key, reason))
    if (plugin_dir / ".mcp.json").is_file():
        ignored.append((".mcp.json", "插件自带的 MCP 配置未加载（请写进 mcp.json）"))

    components: list[tuple[str, tuple[str, ...]]] = []
    for component in COMPONENTS:
        dirs = _component_dirs(plugin_dir, component, raw, name, warnings)
        if dirs:
            components.append((component, tuple(str(item) for item in dirs)))

    enabled = flags.get(name, default_enabled)
    return PluginDefinition(
        name=name,
        path=str(plugin_dir),
        source=source,
        description=description,
        version=version,
        author=author,
        enabled=enabled,
        default_enabled=default_enabled,
        components=tuple(components),
        ignored=tuple(ignored),
        warnings=tuple(warnings),
    )


def _text_field(raw: dict, key: str, label: str, warnings: list[str]) -> str:
    value = raw.get(key)
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    warnings.append(f"插件 {label} 的 {key} 不是字符串，已忽略")
    return ""


def _author(value, label: str, warnings: list[str]) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        name = value.get("name")
        email = value.get("email")
        parts = [str(name).strip()] if isinstance(name, str) and name.strip() else []
        if isinstance(email, str) and email.strip():
            parts.append(f"<{email.strip()}>")
        return " ".join(parts)
    warnings.append(f"插件 {label} 的 author 形状不对，已忽略")
    return ""


def _component_dirs(
    plugin_dir: Path,
    component: str,
    raw: dict,
    name: str,
    warnings: list[str],
) -> list[Path]:
    """Resolve one component's directories (manifest override or default)."""
    manifest_key = MANIFEST_COMPONENT_KEYS[component]
    value = raw.get(manifest_key)
    if value is None:
        directory = plugin_dir / component
        return [directory] if directory.is_dir() else []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        items = list(value)
    else:
        warnings.append(
            f"插件 {name} 的 {manifest_key} 必须是路径或路径数组，按默认目录 {component}/ 处理"
        )
        directory = plugin_dir / component
        return [directory] if directory.is_dir() else []

    resolved: list[Path] = []
    for item in items:
        candidate, problem = _resolve_component_path(plugin_dir, item)
        if problem:
            warnings.append(f"插件 {name} 的 {manifest_key} {problem}")
            continue
        if not candidate.is_dir():
            warnings.append(
                f"插件 {name} 的 {manifest_key} 指向的目录不存在：{item}"
            )
            continue
        resolved.append(candidate)
    return resolved


def _resolve_component_path(plugin_dir: Path, item: str) -> tuple[Path, str]:
    text = item.strip()
    if not text.startswith("./"):
        return Path(), f"路径 {item!r} 必须以 \"./\" 开头"
    try:
        root = plugin_dir.resolve()
        candidate = (plugin_dir / text).resolve()
    except (OSError, RuntimeError) as exc:
        return Path(), f"路径 {item!r} 无法解析：{exc.__class__.__name__}"
    if candidate != root and root not in candidate.parents:
        return Path(), f"路径 {item!r} 跳出了插件目录"
    return candidate, ""


def _count_component(component: str, directories: tuple[str, ...]) -> int:
    total = 0
    for raw in directories:
        directory = Path(raw)
        try:
            if component == "skills":
                total += sum(
                    1
                    for item in directory.iterdir()
                    if item.is_dir() and _has_skill_file(item)
                )
            else:
                total += sum(1 for item in directory.rglob("*.md") if item.is_file())
        except OSError:
            continue
    return total


def _has_skill_file(directory: Path) -> bool:
    try:
        return any(
            item.is_file() and item.name.lower() == "skill.md"
            for item in directory.iterdir()
        )
    except OSError:
        return False
