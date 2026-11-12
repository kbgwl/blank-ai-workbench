"""Agent runtime settings: guardian mode, context water line, default permission mode.

The endpoint returns the *normalized* value, not what was sent: if a client posts
``trigger_ratio: 3`` it gets back the clamped number, so the settings page can
never display a value the runtime is not actually using.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query

from blank_api.api.auth import require_token
from blank_api.api.errors import ApiError
from blank_api.api.routes.projects import load_project
from blank_api.deps import DbSession
from blank_api.schemas.settings import AgentSettings, AgentSettingsPatch
from blank_api.services import features as feature_registry
from blank_api.services.agent.automation import (
    AUTOMATION_MODES,
    MODE_LABELS as AUTOMATION_MODE_LABELS,
)
from blank_api.services.guardian import MODES as GUARDIAN_MODES
from blank_api.services.mcp import approvals as mcp_approvals
from blank_api.services.mcp import manager as mcp_manager
from blank_api.services.mcp.bridge import SCOPE_LABELS, scoped_entries
from blank_api.services.permissions import MODE_LABELS, MODES as PERMISSION_MODES
from blank_api.services.sandbox import docker_probe
from blank_api.services.sandbox.policy import SandboxPolicy
from blank_api.services.safety import SafetyPolicy, backend_info
from blank_api.services.settings import (
    BACKGROUND_MAX_RECOVERIES,
    BACKGROUND_MAX_SECONDS,
    BACKGROUND_MAX_STEPS_CAP,
    BACKGROUND_MAX_TOKENS,
    BACKGROUND_MIN_SECONDS,
    BACKGROUND_MIN_TOKENS,
    BACKGROUND_MODEL_POLICIES,
    BACKGROUND_NOTIFY_POLICIES,
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
        # v165 §8：前端自动化模式下拉框的合法取值与中文标签。和归一化、PATCH
        # 校验共用 automation.py 的那份枚举，界面上选得到的必然存得上。
        "automation_modes": list(AUTOMATION_MODES),
        "automation_mode_labels": AUTOMATION_MODE_LABELS,
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
        # 后台面板的边界：用户只能往更保守的方向调，这里的 max 就是硬上限。
        "background": {
            "notify_policies": list(BACKGROUND_NOTIFY_POLICIES),
            "model_policies": list(BACKGROUND_MODEL_POLICIES),
            "max_seconds": {"min": BACKGROUND_MIN_SECONDS, "max": BACKGROUND_MAX_SECONDS},
            "max_steps": {"min": 1, "max": BACKGROUND_MAX_STEPS_CAP},
            "token_budget": {"min": BACKGROUND_MIN_TOKENS, "max": BACKGROUND_MAX_TOKENS},
            "auto_recoveries": {"min": 0, "max": BACKGROUND_MAX_RECOVERIES},
        },
    }


def _render(payload: dict, store: SettingsStore) -> AgentSettings:
    body = {
        k: v
        for k, v in payload.items()
        if k in ("guardian", "compaction", "permissions", "tools", "safety", "background", "output_style")
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


@router.get("/features")
async def read_features() -> dict:
    """注册表全量 + 每个开关当前生效的值与来源。

    这个端点存在的意义是「解释」：用户说某个能力没生效时，运维不用去翻进程
    环境变量和设置文件对比，这里直接说清楚是哪一层把值定成了现在这样。
    """
    return feature_registry.describe()


@router.patch("/features")
async def patch_features(payload: dict[str, Any] = Body(...)) -> dict:
    try:
        return feature_registry.apply_patch(payload)
    except feature_registry.UnknownFeature as exc:
        raise ApiError("unknown_feature", f"未注册的特性开关：{exc.name}") from exc
    except ValueError as exc:
        raise ApiError("invalid_feature_value", str(exc)) from exc


async def _workspace_root(db: DbSession, project_id: str) -> str | None:
    workspace = (await load_project(db, project_id)).active_workspace
    return workspace.path if workspace is not None else None


@router.get("/mcp")
async def read_mcp(db: DbSession, project_id: str = Query(default="")) -> dict:
    """外部工具服务端接上没有、每个为什么没接上。

    配置分三层（用户 ``mcp.json`` < 项目 ``.mcp.json`` < 本机 ``.blank/mcp.local.json``），
    带上 ``project_id`` 才能读到工作区下那两层。用户改错一个字段时界面上唯一的
    症状是「工具变少了」，所以这个端点把每一行的判定结果原样吐出来，省得靠猜。
    项目层/本机层没批准的服务端只出现在状态里（``approval=pending``），不会启动。
    """
    root = await _workspace_root(db, project_id) if project_id else None
    manager = mcp_manager()
    bundle = await manager.bundle(root)
    return {
        "config": str(manager.path),
        "projectId": project_id,
        "workspace": root is not None,
        **bundle.as_dict(),
    }


@router.post("/mcp/approvals")
async def approve_mcp_server(db: DbSession, payload: dict[str, Any] = Body(...)) -> dict:
    """批准项目层/本机层的一条服务端定义（按整条定义的哈希记录）。

    只允许批准当前真正生效的那一层：如果本机层同名条目把项目层盖住了，批准项目层
    那条不会有任何效果，所以直接说清是被谁盖住的，而不是假装成功。
    """
    scope = str(payload.get("scope") or "").strip().lower()
    name = str(payload.get("name") or "").strip()
    project_id = str(payload.get("project_id") or "").strip()
    if scope not in mcp_approvals.APPROVAL_SCOPES:
        raise ApiError("invalid_scope", "只有项目层与本机层需要批准")
    if not name:
        raise ApiError("invalid_server", "缺少服务端名称")
    if not project_id:
        raise ApiError("project_required", "批准项目层/本机层服务端必须指定项目")
    root = await _workspace_root(db, project_id)
    if root is None:
        raise ApiError("workspace_required", "该项目没有绑定工作区，没有项目层/本机层配置可批准")
    entry = scoped_entries(root).get(name)
    if entry is None:
        raise ApiError("server_not_found", f"工作区配置里没有服务端：{name}", status_code=404)
    entry_scope, definition = entry
    if entry_scope != scope:
        raise ApiError(
            "scope_overridden",
            f"服务端 {name} 当前由{SCOPE_LABELS.get(entry_scope, entry_scope)}覆盖，"
            f"批准{scope}那条不会生效",
            status_code=409,
        )
    digest = mcp_approvals.approve(scope, root, name, definition)
    # 批准后立刻重连：下一次任务启动就该看到这些工具，不能等进程重启
    await mcp_manager().reload(root)
    return {"approved": True, "scope": scope, "name": name, "digest": digest}


@router.delete("/mcp/approvals/{name}")
async def revoke_mcp_server(
    name: str,
    db: DbSession,
    scope: str = Query(...),
    project_id: str = Query(...),
) -> dict:
    """撤销批准：下一次解析时这条服务端会退回 ``pending``，进程不再启动。"""
    scope = scope.strip().lower()
    if scope not in mcp_approvals.APPROVAL_SCOPES:
        raise ApiError("invalid_scope", "只有项目层与本机层有批准记录")
    root = await _workspace_root(db, project_id)
    if root is None:
        raise ApiError("workspace_required", "该项目没有绑定工作区")
    existed = mcp_approvals.revoke(scope, root, name)
    await mcp_manager().reload(root)
    return {"revoked": existed, "scope": scope, "name": name}


@router.get("/safety/runtime")
async def read_safety_runtime() -> dict:
    """当前这台机器到底是怎么隔离的，以及策略的生效值。

    设置页要能说清「容器隔离」「本机实测边界」「无隔离」的区别：客户端没有 Docker 是常态，
    含糊地写一句"已开启沙箱"只会让客户以为跑不可信代码是安全的。
    """
    policy = SafetyPolicy.load()
    settings = SettingsStore().read()
    # 「这一页不许回答尚未探测」只对**拿这个读数做决定的形态**成立。开了沙箱时不补探不行：
    # `_backend()` 在读到 pending 时会按 PATH 兜底，实测那种情况下这一页在从没测过的机器上
    # 报出 `isolation=container`——比少一行读数坏得多。
    # 全局关着沙箱时读数只是资料（启动探测照样把它填上），为它把请求卡在启动路径上等一条
    # 子进程不值得：实测 ENABLED=1 第一次 0.112s、ENABLED=0 第一次 0.034s（TTL 内第二次都 0.001s）。
    if SandboxPolicy().enabled:
        await docker_probe.ensure_fresh()
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


@router.post("/safety/runtime/reprobe")
async def reprobe_safety_runtime() -> dict:
    """强制重探一次执行后端，把新读数原样端回去。

    客户装完或刚启动 Docker 时不该被要求重启服务：这条把 TTL 跳过，重新起一次
    `docker version`，界面上那行读数跟着换。
    """
    info = await docker_probe.refresh()
    return {"probe": info.as_dict(), "backend": backend_info().as_dict()}
