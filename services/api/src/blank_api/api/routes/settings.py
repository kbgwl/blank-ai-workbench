"""Agent runtime settings: guardian mode, context water line, default permission mode.

The endpoint returns the *normalized* value, not what was sent: if a client posts
``trigger_ratio: 3`` it gets back the clamped number, so the settings page can
never display a value the runtime is not actually using.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from blank_api.api.auth import require_token
from blank_api.schemas.settings import AgentSettings, AgentSettingsPatch
from blank_api.services.guardian import MODES as GUARDIAN_MODES
from blank_api.services.permissions import MODE_LABELS, MODES as PERMISSION_MODES
from blank_api.services.safety import SafetyPolicy, backend_info
from blank_api.services.settings import (
    DEFAULTS,
    GUARDIAN_MAX_TIMEOUT,
    GUARDIAN_MIN_TIMEOUT,
    MAX_RESERVE_TOKENS,
    MAX_TRIGGER_RATIO,
    MIN_RESERVE_TOKENS,
    MIN_TRIGGER_RATIO,
    SAFETY_MAX_ENTRIES,
    SAFETY_MAX_TIMEOUT,
    SAFETY_MIN_TIMEOUT,
    SettingsStore,
)

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_token)])


def _limits() -> dict:
    return {
        "guardian_modes": list(GUARDIAN_MODES),
        "guardian_timeout": {
            "min": GUARDIAN_MIN_TIMEOUT,
            "max": GUARDIAN_MAX_TIMEOUT,
            "default": DEFAULTS["guardian"]["timeout_seconds"],
        },
        "permission_modes": list(PERMISSION_MODES),
        "permission_mode_labels": MODE_LABELS,
        "trigger_ratio": {
            "min": MIN_TRIGGER_RATIO,
            "max": MAX_TRIGGER_RATIO,
            "default": DEFAULTS["compaction"]["trigger_ratio"],
        },
        "reserve_tokens": {
            "min": MIN_RESERVE_TOKENS,
            "max": MAX_RESERVE_TOKENS,
            "default": DEFAULTS["compaction"]["reserve_tokens"],
        },
        # 安全面板要靠这段渲染输入边界，顺便让"被截断到哪了"可解释
        "safety_timeout": {
            "min": SAFETY_MIN_TIMEOUT,
            "max": SAFETY_MAX_TIMEOUT,
            "default": DEFAULTS["safety"]["timeout_seconds"],
        },
        "safety_max_entries": SAFETY_MAX_ENTRIES,
    }


def _render(payload: dict, store: SettingsStore) -> AgentSettings:
    body = {
        k: v
        for k, v in payload.items()
        if k in ("guardian", "compaction", "permissions", "tools", "safety")
    }
    return AgentSettings.model_validate({**body, "path": str(store.path), "limits": _limits()})


@router.get("/agent", response_model=AgentSettings)
async def read_agent_settings() -> AgentSettings:
    store = SettingsStore()
    return _render(store.read(), store)


@router.put("/agent", response_model=AgentSettings)
async def update_agent_settings(payload: AgentSettingsPatch) -> AgentSettings:
    store = SettingsStore()
    return _render(store.update(payload.model_dump(exclude_none=True)), store)


@router.post("/agent/reset", response_model=AgentSettings)
async def reset_agent_settings() -> AgentSettings:
    store = SettingsStore()
    return _render(store.reset(), store)


@router.get("/safety/runtime")
async def read_safety_runtime() -> dict:
    """当前这台机器到底是怎么隔离的，以及策略的生效值。

    设置页要能说清「本机受限执行」和「容器隔离」的区别：客户端没有 Docker 是常态，
    含糊地写一句"已开启沙箱"只会让客户以为跑不可信代码是安全的。
    """
    policy = SafetyPolicy.load()
    settings = SettingsStore().read()
    return {
        "backend": backend_info().as_dict(),
        "policy": {
            "shell_enabled": policy.shell_enabled,
            "network_enabled": policy.network_enabled,
            "confirm_all_commands": policy.confirm_all_commands,
            "restrict_to_workspace": policy.restrict_to_workspace,
            "timeout_seconds": policy.timeout_seconds,
            "allowed_roots": list(policy.allowed_roots),
            "denied_commands": list(policy.denied_commands),
        },
        "default_mode": (settings.get("permissions") or {}).get("default_mode", "default"),
        "settings_path": str(SettingsStore().path),
    }
