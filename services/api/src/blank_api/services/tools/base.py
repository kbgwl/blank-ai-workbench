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
    # PermissionPolicy（延迟注解，避免与 permissions 模块循环导入）
    permissions: "PermissionPolicy | None" = None


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
