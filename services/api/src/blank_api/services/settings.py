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
from pathlib import Path
from typing import Any

from blank_api.services.permissions import MODE_DEFAULT, MODES as PERMISSION_MODES

GUARDIAN_MODES = ("off", "veto", "auto")

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
}

GUARDIAN_MIN_TIMEOUT = 1.0
GUARDIAN_MAX_TIMEOUT = 60.0
MIN_TRIGGER_RATIO = 0.5
MAX_TRIGGER_RATIO = 0.95
MIN_RESERVE_TOKENS = 1_024
MAX_RESERVE_TOKENS = 131_072


def _path() -> Path:
    override = str(os.environ.get("BLANK_SETTINGS_FILE") or "").strip()
    return Path(override) if override else Path("data/settings.json")


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
    }


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
