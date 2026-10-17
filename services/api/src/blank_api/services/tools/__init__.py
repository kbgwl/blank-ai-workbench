from blank_api.services.tools.ask import ask_tools
from blank_api.services.tools.base import (
    ApprovalPolicy,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
    validate_arguments,
)
from blank_api.services.tools.filesystem import filesystem_tools
from blank_api.services.tools.gateway import ToolGateway
from blank_api.services.tools.git import git_tools
from blank_api.services.tools.delegate import delegate_tools
from blank_api.services.tools.memory_tools import memory_tools
from blank_api.services.tools.patch import patch_tools
from blank_api.services.tools.plan import plan_tools
from blank_api.services.tools.shell import shell_tools
from blank_api.services.tools.testing import test_tools
from blank_api.services.tools.web import web_tools


def build_default_tools() -> list[ToolDefinition]:
    return [
        *plan_tools(),
        *ask_tools(),
        *delegate_tools(),
        *filesystem_tools(),
        *memory_tools(),
        *patch_tools(),
        *shell_tools(),
        *git_tools(),
        *test_tools(),
        *web_tools(),
    ]


__all__ = [
    "ApprovalPolicy",
    "ToolContext",
    "ToolDefinition",
    "ToolGateway",
    "ToolOutcome",
    "build_default_tools",
    "validate_arguments",
]
