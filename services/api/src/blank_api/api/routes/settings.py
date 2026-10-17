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
from blank_api.services.settings import (
    DEFAULTS,
    GUARDIAN_MAX_TIMEOUT,
    GUARDIAN_MIN_TIMEOUT,
    MAX_RESERVE_TOKENS,
    MAX_TRIGGER_RATIO,
    MIN_RESERVE_TOKENS,
    MIN_TRIGGER_RATIO,
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
    }


def _render(payload: dict, store: SettingsStore) -> AgentSettings:
    body = {k: v for k, v in payload.items() if k in ("guardian", "compaction", "permissions", "tools")}
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
