"""MCP client support: external tool servers become ordinary tools.

Importing this package does not spawn anything. Connections happen on first
:func:`blank_api.services.mcp.bridge.manager` call, which is why a missing or
broken ``mcp.json`` can never keep the app from starting.
"""

from blank_api.services.mcp.bridge import (
    MCPBundle,
    MCPManager,
    MCPStatus,
    config_path,
    manager,
    parse_servers,
    reset_manager,
)
from blank_api.services.mcp.client import MCPError, ServerConfig

__all__ = [
    "MCPBundle",
    "MCPError",
    "MCPManager",
    "MCPStatus",
    "ServerConfig",
    "config_path",
    "manager",
    "parse_servers",
    "reset_manager",
]
