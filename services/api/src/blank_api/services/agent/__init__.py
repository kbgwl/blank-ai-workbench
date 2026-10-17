"""Agent 运行时的门面包。

这里**故意不**在模块级导入 runtime/evidence：runtime → graph → tools.gateway，
而 gateway 又依赖 agent.artifacts，任何"先导入 tools 再碰 agent"的顺序都会绕回来
撞上半初始化的模块。用 PEP 562 的模块级 __getattr__ 做延迟解析，导入顺序就不再
是隐性约束。
"""

from importlib import import_module
from typing import Any

_LAZY = {
    "AgentRuntime": ("blank_api.services.agent.runtime", "AgentRuntime"),
    "RunRequest": ("blank_api.services.agent.runtime", "RunRequest"),
    "resolve_status": ("blank_api.services.agent.runtime", "resolve_status"),
    "build_evidence": ("blank_api.services.agent.evidence", "build_evidence"),
}

__all__ = list(_LAZY)


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module, attribute = target
    return getattr(import_module(module), attribute)


def __dir__() -> list[str]:
    return sorted(_LAZY)
