"""Tool layer primitives. The Agent never touches the filesystem or shell directly."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from blank_api.services.approvals import approval_key
from blank_api.services.models.base import ToolSpecLite

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查时导入，运行时无依赖
    from blank_api.services.permissions import PermissionPolicy
    from blank_api.services.safety import SafetyPolicy

ApprovalPolicy = Callable[["ToolDefinition", dict], Any]


TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}


@dataclass(frozen=True)
class ToolContext:
    root: Path
    task_id: str
    session_factory: async_sessionmaker[AsyncSession]
    # 旧的单值审批回调：只有这一层时按"高风险才拦"的老口径判定，保留给外部集成与单测。
    # 新代码请用 permissions（四档模式 + 前缀规则 + 会话记忆）。
    approval_policy: ApprovalPolicy | None = None
    extra_secrets: tuple[str, ...] = ()
    approved_actions: frozenset[str] = frozenset()
    trace_id: str = ""
    # 事件记录器：少数工具会写自己的领域事件（worktree.entered 等），
    # 拿不到时为空，工具本身照常工作——事件是事实的旁路，不是执行前提。
    recorder: Any = None
    # PermissionPolicy（延迟注解，避免与 permissions 模块循环导入）
    permissions: "PermissionPolicy | None" = None
    # 能力边界（能不能做），与 permissions（要不要问）是两回事，判定时先过后者
    safety: "SafetyPolicy | None" = None
    # 子代理运行器（延迟注入：它要引用创建它的 AgentSession）
    subagent_runner: Any = None
    # 本任务的交接笔记 / 项目长期记忆：工具层只读路径约定，不关心实现
    notes: Any = None
    memory: Any = None
    # 本线程的 shell 快照：run_shell 用它决定子进程环境
    shell: Any = None
    # 工具注册表：tool_search 靠它判断"哪些工具还没加载"，激活状态按会话隔离
    tool_registry: Any = None
    # 本任务的工具网关：repl 的内层调用必须回到同一条管线（场景/权限/安全/审计/沙箱），
    # 而不是自己另开一套。子代理与非 runtime 入口拿不到网关，repl 会明确拒绝。
    tool_gateway: Any = None
    # 团队群聊身份：group_id / bot_id / is_admin / project_id。空表示非团队任务，
    # 团队管理工具只在主理人的任务里可见。
    team_scope: dict = field(default_factory=dict)
    # 本任务的 Skill 目录（tools/skills.py）。为空表示没有可加载的 Skill，
    # `skill` 工具也会从这一轮的工具表里被拿掉——模型连"有这个工具"都不该知道。
    skills: Any = None
    # 本任务的沙箱档位（off / workspace / workspace-net）。命令类工具与子代理读它，
    # 审批的"工具档"授权也按它分桶：在 workspace 下批过的工具，切到 off 不再免问。
    sandbox_tier: str = "workspace"
    # 任务级隔离（J3）：本任务的 AppContainer 包名，空串表示没开隔离。
    # 非空时命令与文件工具必须在这个包内执行——任务根已收紧 ACL，宿主令牌仍读得到，
    # 只有换身份才让「另一个任务用绝对路径也读不到」成立。
    isolation_profile: str = ""
    # 当前计划状态 {steps, milestones, goal}。update_plan 靠它拿到"上一版步骤"，
    # 才能让重新提交时沿用原 id——否则里程碑和验收条件按 id 建起来的引用会被换空。
    plan_state: dict = field(default_factory=dict)
    # 本会话的读后写状态（tools/file_state.py）。非空时写工具执行前必须过
    # 「读过没有 / 读过之后被改过没有」两道闸门；生产路径由 runtime 与 subagent 注入，
    # 直接构造 ToolContext 的单测默认不启用，既有行为不变。
    file_state: Any = None
    # 后台命令（run_shell(run_in_background=true)）：子代理的工作副本在本轮结束时会被
    # 清理，后台进程活不到有人取结果的时候，所以子代理上下文显式关掉这个入口。
    allow_background_shell: bool = True


@dataclass
class ToolOutcome:
    ok: bool
    status: str = "ok"
    output: str = ""
    data: dict = field(default_factory=dict)
    message: str = ""

    def as_dict(self) -> dict:
        body = {"ok": self.ok, "status": self.status, "output": self.output}
        if self.message:
            body["message"] = self.message
        if self.data:
            body["data"] = self.data
        return body


def ok(output: str = "", **data) -> ToolOutcome:
    return ToolOutcome(ok=True, output=output, data=data)


def fail(status: str, message: str, output: str = "") -> ToolOutcome:
    return ToolOutcome(ok=False, status=status, output=output, message=message)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict
    risk_level: str
    handler: Callable[[ToolContext, dict], Awaitable[ToolOutcome]]
    timeout_seconds: float = 30.0
    risk_for: Callable[[dict], str] | None = None

    @property
    def requires_approval(self) -> bool:
        return self.risk_level in ("high", "critical")

    def resolve_risk(self, arguments: dict) -> str:
        if self.risk_for is None:
            return self.risk_level
        try:
            return self.risk_for(arguments)
        except Exception:
            return self.risk_level

    def spec(self) -> ToolSpecLite:
        return ToolSpecLite(
            name=self.name, description=self.description, input_schema=self.input_schema
        )


def validate_arguments(schema: dict, arguments: dict) -> str | None:
    """Return a Chinese error message when the arguments do not match the schema."""
    if not isinstance(arguments, dict):
        return "工具参数必须是 JSON 对象"
    properties: dict = schema.get("properties") or {}
    for required in schema.get("required") or []:
        if required not in arguments:
            return f"缺少必填参数：{required}"
    for key, value in arguments.items():
        rule = properties.get(key)
        if rule is None:
            continue
        expected = rule.get("type")
        if expected and expected in TYPE_CHECKS:
            allowed = TYPE_CHECKS[expected]
            if expected != "integer" and not isinstance(value, allowed):
                return f"参数 {key} 类型应为 {expected}"
            if expected == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
                return f"参数 {key} 类型应为 {expected}"
        enum = rule.get("enum")
        if enum and value not in enum:
            return f"参数 {key} 只能是 {'/'.join(map(str, enum))} 之一"
    return None
