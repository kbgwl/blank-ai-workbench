"""Runtime settings the user can change without restarting.

Everything here is *agent behaviour*, not secrets: the guardian's mode, where the
context water line sits, and the default permission mode. Secrets stay in the
providers table, where they are encrypted.

Two rules keep this safe:

* **Defaults are the conservative ones.** Guardian ``veto`` (can only tighten),
  compaction triggers at 82% of the window, permission mode ``default``.
* **Validation clamps instead of trusting.** A settings file edited by hand, or a
  request with ``trigger_ratio: 3``, can never push the water line past the
  window or let the guardian auto-approve by accident.

The file lives next to the other run state (``data/settings.json``), outside the
workspace, so it never shows up in the user's repository.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

from blank_api.services.permissions import MODE_DEFAULT, MODES as PERMISSION_MODES

GUARDIAN_MODES = ("off", "veto", "auto")

# 老位置：cwd 下的 data/settings.json（桌面版即包内），只作为搬迁来源
_LEGACY_SETTINGS = Path("data/settings.json")

DEFAULTS: dict[str, Any] = {
    "guardian": {
        # off = 不审查（默认）；veto = 只能拒绝不能放行（推荐）；auto = 可放行可逆动作。
        # 默认关：开审查意味着每次高风险动作多一次模型调用，这件事必须由用户显式选择，
        # 不能在升级后悄悄给他们加延迟与"被拒绝"的意外。
        "mode": "off",
        "timeout_seconds": 8.0,
    },
    "compaction": {
        # 上下文水位：到这个比例开始压缩，reserve 是给模型回复留的余量
        "trigger_ratio": 0.82,
        "reserve_tokens": 16_384,
    },
    "permissions": {
        # 新任务的默认权限模式
        "default_mode": MODE_DEFAULT,
    },
    "tools": {
        # 工具超过一定数量后按需检索加载（tool_search）
        "search": True,
    },
    "safety": {
        # 能力边界，与 permissions.default_mode（审批节奏）是两回事：
        # 这里决定"能不能做"，那里决定"要不要问"。默认全部取最保守的一档。
        "shell_enabled": False,
        "network_enabled": False,
        "confirm_all_commands": True,
        "restrict_to_workspace": True,
        "timeout_seconds": 120.0,
        "allowed_roots": [],
        "denied_commands": [],
    },
}

SAFETY_MIN_TIMEOUT = 10.0
SAFETY_MAX_TIMEOUT = 600.0
SAFETY_MAX_ENTRIES = 50

GUARDIAN_MIN_TIMEOUT = 1.0
GUARDIAN_MAX_TIMEOUT = 60.0
MIN_TRIGGER_RATIO = 0.5
MAX_TRIGGER_RATIO = 0.95
MIN_RESERVE_TOKENS = 1_024
MAX_RESERVE_TOKENS = 131_072


def _path() -> Path:
    """设置文件位置。桌面壳用 BLANK_SETTINGS_FILE 指到用户数据目录。

    历史遗留：设置原本落在 cwd 的 data/（桌面版就是包内），升级换包即重置。
    对权限设置来说这不是"丢了个偏好"，而是"客户的边界悄悄变回默认值"，
    所以这里做一次搬迁，老用户升级后不用重配。
    """
    # 环境变量优先：pydantic 的 get_settings() 带 lru_cache，测试与桌面壳在运行期
    # 改环境变量时必须立刻生效，不能被启动时缓存的那份顶掉。
    override = str(os.environ.get("BLANK_SETTINGS_FILE") or "").strip()
    if not override:
        try:
            from blank_api.config import get_settings

            override = str(get_settings().settings_file or "").strip()
        except Exception:  # noqa: BLE001 - 配置读不出来时按老位置走，不影响启动
            override = ""
    target = Path(override) if override else _LEGACY_SETTINGS
    _migrate_legacy(target)
    return target


@lru_cache(maxsize=8)
def _migrate_legacy(target: Path) -> None:
    """目标位置还没有设置文件时，把老位置那份接过来（只跑一次）。"""
    try:
        if target.exists() or _LEGACY_SETTINGS.resolve() == target.resolve():
            return
        if not _LEGACY_SETTINGS.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_LEGACY_SETTINGS.read_bytes())
    except OSError:
        # 搬迁失败不致命：下面读不到就退回默认值，用户在设置页重新配一次即可
        pass


def _clamp(value: float, low: float, high: float, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number != number:  # NaN
        return fallback
    return max(low, min(high, number))


def _as_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return fallback


def normalize(raw: Any) -> dict[str, Any]:
    """Merge a partial payload over the defaults and clamp every number."""
    data = raw if isinstance(raw, dict) else {}
    guardian = data.get("guardian") if isinstance(data.get("guardian"), dict) else {}
    compaction = data.get("compaction") if isinstance(data.get("compaction"), dict) else {}
    permissions = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
    tools = data.get("tools") if isinstance(data.get("tools"), dict) else {}
    safety = data.get("safety") if isinstance(data.get("safety"), dict) else {}

    mode = str(guardian.get("mode") or "").strip().lower()
    if mode not in GUARDIAN_MODES:
        mode = DEFAULTS["guardian"]["mode"]
    default_mode = str(permissions.get("default_mode") or "").strip().lower()
    if default_mode not in PERMISSION_MODES:
        default_mode = MODE_DEFAULT

    return {
        "guardian": {
            "mode": mode,
            "timeout_seconds": round(
                _clamp(
                    guardian.get("timeout_seconds"),
                    GUARDIAN_MIN_TIMEOUT,
                    GUARDIAN_MAX_TIMEOUT,
                    DEFAULTS["guardian"]["timeout_seconds"],
                ),
                1,
            ),
        },
        "compaction": {
            "trigger_ratio": round(
                _clamp(
                    compaction.get("trigger_ratio"),
                    MIN_TRIGGER_RATIO,
                    MAX_TRIGGER_RATIO,
                    DEFAULTS["compaction"]["trigger_ratio"],
                ),
                3,
            ),
            "reserve_tokens": int(
                _clamp(
                    compaction.get("reserve_tokens"),
                    MIN_RESERVE_TOKENS,
                    MAX_RESERVE_TOKENS,
                    DEFAULTS["compaction"]["reserve_tokens"],
                )
            ),
        },
        "permissions": {"default_mode": default_mode},
        "tools": {"search": _as_bool(tools.get("search"), DEFAULTS["tools"]["search"])},
        "safety": {
            # 能力开关一律按"没写就是关"处理：手改设置文件漏了字段，不能变成放行。
            "shell_enabled": _as_bool(safety.get("shell_enabled"), DEFAULTS["safety"]["shell_enabled"]),
            "network_enabled": _as_bool(safety.get("network_enabled"), DEFAULTS["safety"]["network_enabled"]),
            "confirm_all_commands": _as_bool(
                safety.get("confirm_all_commands"), DEFAULTS["safety"]["confirm_all_commands"]
            ),
            "restrict_to_workspace": _as_bool(
                safety.get("restrict_to_workspace"), DEFAULTS["safety"]["restrict_to_workspace"]
            ),
            "timeout_seconds": round(
                _clamp(
                    safety.get("timeout_seconds"),
                    SAFETY_MIN_TIMEOUT,
                    SAFETY_MAX_TIMEOUT,
                    DEFAULTS["safety"]["timeout_seconds"],
                ),
                1,
            ),
            "allowed_roots": _clean_paths(safety.get("allowed_roots")),
            "denied_commands": _clean_paths(safety.get("denied_commands")),
        },
    }


def _clean_paths(raw: Any) -> list[str]:
    """路径/命令白黑名单：去重、去空、限条数。顺序保留，用户心里的顺序有意义。"""
    if isinstance(raw, str):
        items: list[Any] = raw.replace("\n", ",").split(",")
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return []
    cleaned: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned[:SAFETY_MAX_ENTRIES]


class SettingsStore:
    """File-backed settings. Missing or corrupt file falls back to defaults."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else _path()

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, NotADirectoryError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return deepcopy(DEFAULTS)
        return normalize(raw)

    def raw(self) -> dict[str, Any]:
        """文件里的原样内容。用来区分「没配过」与「配成了默认值」。"""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, NotADirectoryError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def has_section(self, name: str) -> bool:
        """这一段是不是被真的显式设置过（而不是 normalize 出来的默认值）。

        只写了一段设置文件（比如只配了安全策略）就重置掉其它段的语义是不对的：
        没写的那几段仍然应该按"未配置"处理，该回落到环境变量的继续回落。
        """
        return isinstance(self.raw().get(name), dict)

    def write(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize(payload)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)
        return normalized

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Deep-merge a partial payload: sending only guardian.mode keeps the rest."""
        current = self.read()
        merged = deepcopy(current)
        for section, value in (patch or {}).items():
            if not isinstance(value, dict) or section not in merged:
                continue
            merged[section].update({k: v for k, v in value.items() if k in merged[section]})
        return self.write(merged)

    def reset(self) -> dict[str, Any]:
        return self.write(deepcopy(DEFAULTS))


def guardian_settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = settings if isinstance(settings, dict) else SettingsStore().read()
    return payload.get("guardian") or deepcopy(DEFAULTS["guardian"])


def compaction_settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = settings if isinstance(settings, dict) else SettingsStore().read()
    return payload.get("compaction") or deepcopy(DEFAULTS["compaction"])
