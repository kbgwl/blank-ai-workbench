from blank_api.services.tools.ask import ask_tools
from blank_api.services.tools.base import (
    ApprovalPolicy,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
    validate_arguments,
)
from blank_api.services.tools.cron import cron_tools
from blank_api.services.tools.filesystem import filesystem_tools
from blank_api.services.tools.gateway import ToolGateway
from blank_api.services.tools.git import git_tools
from blank_api.services.tools.delegate import delegate_tools
from blank_api.services.tools.editing import editing_tools
from blank_api.services.tools.goals import goal_tools
from blank_api.services.tools.memory_tools import memory_tools
from blank_api.services.tools.lsp import lsp_tools
from blank_api.services.tools.mcp_resources import mcp_resource_tools
from blank_api.services.tools.monitor import monitor_tools
from blank_api.services.tools.notebook import notebook_tools
from blank_api.services.tools.patch import patch_tools
from blank_api.services.tools.plan import plan_tools
from blank_api.services.tools.repl import repl_tools
from blank_api.services.tools.review import review_tools
from blank_api.services.tools.search import search_tools
from blank_api.services.tools.shell import shell_tools
from blank_api.services.tools.skill_proposals import skill_proposal_tools
from blank_api.services.tools.skills import SkillCatalog, skill_tools
from blank_api.services.tools.tasks import task_tools
from blank_api.services.tools.team_admin import TEAM_ADMIN_TOOL_NAMES, team_admin_tools
from blank_api.services.tools.testing import test_tools
from blank_api.services.tools.web import web_tools
from blank_api.services.tools.workflow import workflow_tools
from blank_api.services.tools.worktree import worktree_tools


def build_default_tools() -> list[ToolDefinition]:
    return [
        *search_tools(),
        *plan_tools(),
        *task_tools(),
        *ask_tools(),
        *goal_tools(),
        *delegate_tools(),
        *filesystem_tools(),
        *memory_tools(),
        *patch_tools(),
        *editing_tools(),
        *notebook_tools(),
        *shell_tools(),
        *monitor_tools(),
        *repl_tools(),
        *git_tools(),
        *test_tools(),
        *web_tools(),
        *skill_tools(),
        *skill_proposal_tools(),
        *review_tools(),
        *cron_tools(),
        *workflow_tools(),
        *worktree_tools(),
        *mcp_resource_tools(),
        *lsp_tools(),
        *team_admin_tools(),
    ]


__all__ = [
    "ApprovalPolicy",
    "SkillCatalog",
    "TEAM_ADMIN_TOOL_NAMES",
    "ToolContext",
    "ToolDefinition",
    "ToolGateway",
    "ToolOutcome",
    "build_default_tools",
    "validate_arguments",
]
