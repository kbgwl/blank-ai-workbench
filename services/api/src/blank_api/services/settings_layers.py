"""设置层级：用户 < 项目 < 本机 < 管理策略（对齐 Claude 的 settings 级联）。

Claude Code 的 settings 从低到高叠加：用户级、项目级（``.claude/settings.json``，
随仓库提交）、本机级（``.claude/settings.local.json``，通常不进 git）、企业
管理策略。高一层只覆盖它写了的键；数组整段替换而不是拼接。

本机统一用 ``.blank``：

* 用户级：``BLANK_SETTINGS_FILE``（桌面壳指到用户数据目录）；
* 项目级：``<workspace>/.blank/settings.json``；
* 本机级：``<workspace>/.blank/settings.local.json``；
* 管理策略：``BLANK_MANAGED_SETTINGS_FILE``（没配就没有这一层）。

**安全段只认用户层与管理层。** 项目/本机文件躺在一个可能被克隆、被随手提交的
仓库里，让它改 ``safety`` / ``permissions`` / ``sandbox`` / ``guardian`` /
``features`` 等于把「打开这个仓库」变成一次提权。被忽略的段进 ``ignored``
并带在审计事件里，不静默吞掉。

单层文件写坏（坏 JSON、顶层不是对象、超过 1 MiB）只跳过**这一层**：其余层
照常叠加，错误进 ``errors``。设置读不出来时整任务起不来，比少一层偏好更糟。
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from blank_api.services.settings import DEFAULTS, normalize
from blank_api.services.workspace import mask_path

LAYER_USER = "user"
LAYER_PROJECT = "project"
LAYER_LOCAL = "local"
LAYER_MANAGED = "managed"

# 从低到高：后面的层覆盖前面的层。
LAYERS: tuple[str, ...] = (LAYER_USER, LAYER_PROJECT, LAYER_LOCAL, LAYER_MANAGED)

PROJECT_DIR = ".blank"
PROJECT_FILE = "settings.json"
LOCAL_FILE = "settings.local.json"
MANAGED_ENV = "BLANK_MANAGED_SETTINGS_FILE"
MAX_FILE_BYTES = 1024 * 1024

# 这些段只能由用户层或管理策略层决定。项目层/本机层写了也只进 ignored。
PROTECTED_SECTIONS: frozenset[str] = frozenset(
    {"guardian", "permissions", "sandbox", "safety", "features"}
)
_PROTECTED_REASON = "安全设置只能由用户层或管理策略决定"


@dataclass
class Layer:
    """一层设置文件的读取结果。``data`` 只在 ``ok`` 时才是有效对象。"""

    name: str
    path: Path | None
    exists: bool = False
    ok: bool = False
    data: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ignored: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "layer": self.name,
            "path": mask_path(self.path) if self.path is not None else "",
            "exists": self.exists,
            "ok": self.ok,
        }
        if self.errors:
            payload["errors"] = list(self.errors)
        if self.warnings:
            payload["warnings"] = list(self.warnings)
        if self.ignored:
            payload["ignored"] = [dict(item) for item in self.ignored]
        return payload


@dataclass
class LayeredSettings:
    """叠加后的生效设置 + 每一层干了什么（审计用，不含原始值）。"""

    settings: dict[str, Any]
    layers: list[Layer]
    # 顶层段 → 最后一层写它的层名；没写的段不在表里（运行时按默认值）
    sources: dict[str, str]

    @property
    def warnings(self) -> list[str]:
        return [item for layer in self.layers for item in layer.warnings]

    @property
    def errors(self) -> list[str]:
        return [item for layer in self.layers for item in layer.errors]

    def changed(self) -> bool:
        """用户层之外有没有东西（或有没有坏层）。没有就不必写事件。"""
        return any(layer.name != LAYER_USER and (layer.exists or layer.errors) for layer in self.layers)

    def as_dict(self) -> dict[str, Any]:
        return {
            "layers": [layer.as_dict() for layer in self.layers],
            "sources": dict(sorted(self.sources.items())),
            "active": [layer.name for layer in self.layers if layer.ok],
            "warnings": self.warnings,
            "errors": self.errors,
        }


def managed_path(env: Mapping[str, str] | None = None) -> Path | None:
    """管理策略文件位置。只在显式配置时才有这一层。"""
    source = os.environ if env is None else env
    raw = str(source.get(MANAGED_ENV) or "").strip()
    return Path(raw) if raw else None


def project_paths(workspace: str | Path | None) -> tuple[Path | None, Path | None]:
    """``(项目层, 本机层)`` 路径。没有工作区时两层都不存在。"""
    if workspace is None or not str(workspace).strip():
        return None, None
    root = Path(workspace)
    return root / PROJECT_DIR / PROJECT_FILE, root / PROJECT_DIR / LOCAL_FILE


def _user_path() -> Path:
    # 延迟导入：settings.py 的 read_layered 反过来要调本模块，顶层互相导入会成环。
    from blank_api.services.settings import settings_path

    return settings_path()


def _read_layer(name: str, path: Path | None) -> Layer:
    layer = Layer(name=name, path=path)
    if path is None:
        return layer
    try:
        if not path.is_file():
            return layer
        size = path.stat().st_size
    except OSError as exc:
        layer.errors.append(f"设置文件 {mask_path(path)} 无法访问：{exc.__class__.__name__}")
        return layer
    layer.exists = True
    if size > MAX_FILE_BYTES:
        layer.errors.append(f"设置文件 {mask_path(path)} 超过 1 MiB，已跳过")
        return layer
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError) as exc:
        layer.errors.append(f"设置文件 {mask_path(path)} 读取失败：{exc.__class__.__name__}")
        return layer
    except json.JSONDecodeError as exc:
        layer.errors.append(
            f"设置文件 {mask_path(path)} 不是合法 JSON（第 {exc.lineno} 行），已跳过"
        )
        return layer
    if not isinstance(raw, dict):
        layer.errors.append(f"设置文件 {mask_path(path)} 的顶层不是对象，已跳过")
        return layer
    layer.data = raw
    layer.ok = True
    return layer


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """键级覆盖；数组与标量整段替换（与 Claude 的叠加口径一致）。"""
    result = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _merge_layer(merged: dict[str, Any], sources: dict[str, str], layer: Layer) -> None:
    if not layer.ok:
        return
    for key, value in layer.data.items():
        if key in PROTECTED_SECTIONS and layer.name in (LAYER_PROJECT, LAYER_LOCAL):
            layer.ignored.append({"section": key, "reason": _PROTECTED_REASON})
            continue
        if key not in DEFAULTS:
            layer.ignored.append({"section": key, "reason": "未知设置段"})
            continue
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
        sources[key] = layer.name


def load_layered_settings(
    workspace: str | Path | None = None,
    *,
    user_path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> LayeredSettings:
    """按 用户 < 项目 < 本机 < 管理 叠加一次，返回生效值与审计面。

    ``user_path`` 供 ``SettingsStore`` 用它自己的路径（测试与桌面壳可能换位置）；
    不传时用应用默认的用户设置文件。
    """
    user = Path(user_path) if user_path is not None else _user_path()
    project, local = project_paths(workspace)
    plan = (
        (LAYER_USER, user),
        (LAYER_PROJECT, project),
        (LAYER_LOCAL, local),
        (LAYER_MANAGED, managed_path(env)),
    )
    layers = [_read_layer(name, path) for name, path in plan]
    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for layer in layers:
        _merge_layer(merged, sources, layer)
    return LayeredSettings(settings=normalize(merged), layers=layers, sources=sources)
