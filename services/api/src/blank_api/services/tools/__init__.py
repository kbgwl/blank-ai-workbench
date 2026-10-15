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
from blank_api.services.tools.patch import patch_tools
from blank_api.services.tools.shell import shell_tools
from blank_api.services.tools.testing import test_tools


def build_default_tools() -> list[ToolDefinition]:
    return [*filesystem_tools(), *patch_tools(), *shell_tools(), *git_tools(), *test_tools()]


__all__ = [
    "ApprovalPolicy",
    "ToolContext",
    "ToolDefinition",
    "ToolGateway",
    "ToolOutcome",
    "build_default_tools",
    "validate_arguments",
]
