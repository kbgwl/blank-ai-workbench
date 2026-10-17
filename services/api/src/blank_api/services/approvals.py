"""审批指纹：一次具体工具动作的稳定标识。

单独成模块是为了打断导入环——权限策略与工具层都要用它，而这个计算本身
不依赖任何一方（只有 hashlib 与 json）。
"""

from __future__ import annotations

import hashlib
import json


def approval_key(tool_name: str, arguments: dict) -> str:
    """Stable fingerprint for one exact tool action."""
    encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{tool_name}:{encoded}".encode("utf-8")).hexdigest()
