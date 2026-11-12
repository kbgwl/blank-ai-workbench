"""LSP client support: one language server process per configured extension.

Importing this package spawns nothing. ``LSPServerManager`` starts a server on
the first request for a file whose extension it claims, and the tool layer owns
the manager per run so the process is closed when the run ends.

The config lives next to ``settings.json`` as ``lsp.json`` (``BLANK_LSP_FILE``
overrides it). **The file is the switch**: no file means no process is spawned
and the tool reports "No LSP server available for file type: .x" instead of
guessing a launcher.
"""

from blank_api.services.lsp.client import (
    DEFAULT_TIMEOUT,
    LSPError,
    LSPServerConfig,
    LSPServerManager,
    config_path,
    parse_servers,
    path_to_uri,
    uri_to_path,
)

__all__ = [
    "DEFAULT_TIMEOUT",
    "LSPError",
    "LSPServerConfig",
    "LSPServerManager",
    "config_path",
    "parse_servers",
    "path_to_uri",
    "uri_to_path",
]
