"""沙箱档位：`off` / `workspace` / `workspace-net` 的任务级选择与诚实降级账。

档位回答「这次任务想被什么边界管」，边界强度回答「这台机器此刻真的能强制哪几条」。
两件事必须分开报：把 `workspace` 当成"文件/网络一定被拒"就是设计稿 §2.5 要消灭的谎。
本模块只做解析与账，不执行命令；执行仍在 `runner.py`，两处读同一份 `TierResolution`。

失败方向永远是收紧：
- 认不出的档位在严格模式下抛错，宽松模式下退回 `workspace`（不是 `off`）；
- 部署开关关闭时，任何档位都落成 `off` 并写明原因，不假装还受着限；
- `workspace-net` 在测不出网络强制的机器上**不提供**，也不降级成 `off`
  （`off` 的权限严格更大：网络开着、还没有资源上限）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from blank_api.services.sandbox import boundary, docker_probe
from blank_api.services.sandbox.policy import SandboxPolicy

TIER_OFF = "off"
TIER_WORKSPACE = "workspace"
TIER_WORKSPACE_NET = "workspace-net"
TIERS = (TIER_OFF, TIER_WORKSPACE, TIER_WORKSPACE_NET)
DEFAULT_TIER = TIER_WORKSPACE
# `run_shell(sandbox="host")` 的取值。它是一个**单次**请求，不改变任务档位。
HOST_BYPASS = "host"

# 这些 reasons 是稳定判据，界面与测试都读它，不读文案。
REASON_OFF = "tier_off"
REASON_GLOBAL_DISABLED = "sandbox_disabled"
REASON_BACKEND_UNAVAILABLE = "sandbox_backend_unavailable"
REASON_NET_UNAVAILABLE = "workspace_net_unavailable"
REASON_JOB_ONLY = "measured_job_only"
REASON_OS_ENFORCED = "measured_os_enforced"


class UnknownTier(ValueError):
    """档位不是三档之一。调用方要么 422，要么在宽松模式下退回 workspace。"""


class TierDowngrade(ValueError):
    """子代理/续跑拿到了比父级更宽的边界。这不是配置错误，是越权。"""


# 边界宽窄：数字越大，执行体拿到的权限越多。`off` 是宿主完整权限；`workspace-net`
# 比 `workspace` 多一条出网。降档 = 往更大的数字走，必须失败。
PERMISSIVENESS = {TIER_WORKSPACE: 0, TIER_WORKSPACE_NET: 1, TIER_OFF: 2}


def permissiveness(tier: str) -> int:
    return PERMISSIVENESS.get(normalize_tier(tier), 0)


def host_bypass_requested(tool_name: str, arguments: dict | None) -> bool:
    """这次调用是不是在请求单次越界宿主执行。

    只有 `run_shell` 有这个参数；判定必须在权限层、工具入口与执行层共用同一份，
    否则一处认 `"HOST"`、另一处只认 `"host"`，就会出现"过了审批却没按越界记账"。
    """
    if tool_name != "run_shell":
        return False
    data = arguments if isinstance(arguments, dict) else {}
    return str(data.get("sandbox") or "").strip().lower() == HOST_BYPASS


def assert_not_looser(parent: str, child: str) -> str:
    """子级不得比父级更宽；任一方为空（当时没记）就不冒充知道。返回规范化后的子级。"""
    if not str(parent or "").strip() or not str(child or "").strip():
        return normalize_tier(child)
    chosen = normalize_tier(child)
    if permissiveness(chosen) > permissiveness(parent):
        raise TierDowngrade(
            f"子级沙箱档位不得比父级更宽：父级 {normalize_tier(parent)} → 子级 {chosen}"
        )
    return chosen


def normalize_tier(value: object, *, strict: bool = False) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return DEFAULT_TIER
    if text in TIERS:
        return text
    if strict:
        raise UnknownTier(f"未知沙箱档位：{text}（可选：{', '.join(TIERS)}）")
    return DEFAULT_TIER


def policy_for(tier: str, base: SandboxPolicy | None = None) -> SandboxPolicy:
    """把档位折成执行策略。`off` 直接把 enabled 关掉——执行层据此走宿主路径。"""
    chosen = base or SandboxPolicy()
    if tier == TIER_OFF:
        return replace(chosen, enabled=False)
    return replace(chosen, allow_network=tier == TIER_WORKSPACE_NET)


@dataclass(frozen=True)
class TierResolution:
    requested: str
    tier: str
    strength: str
    label: str
    backend: str
    global_enabled: bool
    available: bool
    degraded: bool
    limits_enforced: bool
    filesystem_enforced: bool
    network_enforced: bool
    notice: str
    reason: str
    policy: SandboxPolicy

    def as_dict(self) -> dict[str, Any]:
        """事件/证据里只放事实，不放 policy 对象。"""
        return {
            "requested": self.requested,
            "tier": self.tier,
            "strength": self.strength,
            "label": self.label,
            "backend": self.backend,
            "global_enabled": self.global_enabled,
            "available": self.available,
            "degraded": self.degraded,
            "limits_enforced": self.limits_enforced,
            "filesystem_enforced": self.filesystem_enforced,
            "network_enforced": self.network_enforced,
            "notice": self.notice,
            "reason": self.reason,
        }


def _backend(policy: SandboxPolicy) -> str:
    from blank_api.services.sandbox.runner import get_sandbox

    try:
        return get_sandbox(policy).name
    except Exception:  # noqa: BLE001 - 起不来也是事实，由 available=False 表达
        return "none"


def resolve_tier(
    requested: object = "",
    *,
    policy: SandboxPolicy | None = None,
    cap: boundary.Capability | None = None,
    strict: bool = False,
) -> TierResolution:
    """解析一次任务/命令的档位与生效边界。所有字段都来自策略或现场探测，不猜。"""
    name = normalize_tier(requested, strict=strict)
    base = policy or SandboxPolicy()
    cap = cap if cap is not None else boundary.measure()

    if not base.enabled:
        return TierResolution(
            requested=name,
            tier=TIER_OFF,
            strength="none",
            label=boundary.strength_label("none"),
            backend="none",
            global_enabled=False,
            available=True,
            degraded=name != TIER_OFF,
            limits_enforced=False,
            filesystem_enforced=False,
            network_enforced=False,
            notice="部署策略关闭了沙箱：本任务无隔离，命令直接以宿主完整权限执行",
            reason=REASON_GLOBAL_DISABLED,
            policy=policy_for(TIER_OFF, base),
        )

    if name == TIER_OFF:
        return TierResolution(
            requested=name,
            tier=TIER_OFF,
            strength="none",
            label=boundary.strength_label("none"),
            backend="none",
            global_enabled=True,
            available=True,
            degraded=False,
            limits_enforced=False,
            filesystem_enforced=False,
            network_enforced=False,
            notice="无隔离：命令以宿主完整权限执行（off 档，每次调用都会记录这一条）",
            reason=REASON_OFF,
            policy=policy_for(TIER_OFF, base),
        )

    chosen = policy_for(name, base)
    backend = _backend(chosen)
    if backend == "none":
        return TierResolution(
            requested=name,
            tier=name,
            strength="none",
            label=boundary.strength_label("none"),
            backend="none",
            global_enabled=True,
            available=False,
            degraded=True,
            limits_enforced=False,
            filesystem_enforced=False,
            network_enforced=False,
            notice=(
                "沙箱后端不可用：命令被拒绝，不会退回宿主执行"
                + docker_probe.notice_suffix()
            ),
            reason=REASON_BACKEND_UNAVAILABLE,
            policy=chosen,
        )

    docker = backend == "docker"
    strength = "os_enforced" if docker else cap.strength
    label = "容器隔离（由 Docker 强制）" if docker else cap.label
    os_level = strength == "os_enforced"
    limits = docker or strength in ("job_only", "os_enforced")
    filesystem = docker or os_level
    network = docker or os_level

    if name == TIER_WORKSPACE_NET and not network:
        return TierResolution(
            requested=name,
            tier=name,
            strength=strength,
            label=label,
            backend=backend,
            global_enabled=True,
            available=False,
            degraded=True,
            limits_enforced=limits,
            filesystem_enforced=filesystem,
            network_enforced=False,
            notice=(
                "workspace-net 不提供：本机实测边界强度为 {}，无法由系统强制网络边界；"
                "该档不会降级成 off（off 的权限更大）"
            ).format(strength),
            reason=REASON_NET_UNAVAILABLE,
            policy=chosen,
        )

    degraded = not (docker or os_level)
    if degraded:
        notice = (
            "资源与生命周期隔离：内存/进程数/进程树由内核强制；"
            "文件与网络边界未由系统强制（边界强度={}），越界拦截靠路径闸门、审批与证据链"
        ).format(strength)
        reason = REASON_JOB_ONLY if strength == "job_only" else "measured_" + strength
    else:
        notice = "内核强制边界：文件与网络由系统拒绝（边界强度={}）".format(strength)
        reason = REASON_OS_ENFORCED if not docker else "docker_container"

    return TierResolution(
        requested=name,
        tier=name,
        strength=strength,
        label=label,
        backend=backend,
        global_enabled=True,
        available=True,
        degraded=degraded,
        limits_enforced=limits,
        filesystem_enforced=filesystem,
        network_enforced=network,
        notice=notice,
        reason=reason,
        policy=chosen,
    )


__all__ = [
    "DEFAULT_TIER",
    "HOST_BYPASS",
    "PERMISSIVENESS",
    "REASON_BACKEND_UNAVAILABLE",
    "REASON_GLOBAL_DISABLED",
    "REASON_JOB_ONLY",
    "REASON_NET_UNAVAILABLE",
    "REASON_OFF",
    "REASON_OS_ENFORCED",
    "TIERS",
    "TIER_OFF",
    "TIER_WORKSPACE",
    "TIER_WORKSPACE_NET",
    "TierDowngrade",
    "TierResolution",
    "UnknownTier",
    "assert_not_looser",
    "host_bypass_requested",
    "normalize_tier",
    "permissiveness",
    "policy_for",
    "resolve_tier",
]
