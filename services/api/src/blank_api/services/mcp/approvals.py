"""Which project/local MCP servers the user has actually approved.

Claude Code gates ``.mcp.json`` servers behind an explicit first-use approval:
a cloned repo must not be able to start a process just because you opened it.
We keep the same rule, with one difference in *what* is remembered.

Each approval is a hash of the full server entry, not the entry itself. The
config may hold ``${GITHUB_TOKEN}``-style placeholders or literal secrets in
``env``/``headers``; copying it into an approvals file would duplicate the
secret into a second place with weaker expectations. Hashing also gives the
right invalidation for free: change one byte of the command, args or env and
the digest no longer matches, so the server reads as unapproved again.

Approvals are scoped to a workspace *and* a scope name. Approving
``.mcp.json`` in repo A says nothing about repo B, and says nothing about a
``.blank/mcp.local.json`` entry with the same name. The stored key is
``scope:workspace-hash:name`` — the workspace is hashed because the path is
machine-local and the file is meant to be readable without leaking it.

A corrupt approvals file means "nothing is approved", not "everything is":
failing open here would turn a truncated write into a silent process launch.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 只有随仓库/工作区走的层需要批准；用户层是本人亲手写的，不设闸门。
APPROVAL_SCOPES = ("project", "local")


def approvals_path() -> Path:
    override = str(os.environ.get("BLANK_MCP_APPROVALS_FILE") or "").strip()
    if override:
        return Path(override)
    from blank_api.services.settings import SettingsStore

    return SettingsStore().path.parent / "mcp-approvals.json"


def entry_digest(entry: Any) -> str:
    """整条服务端定义 → sha256。排序键 + 紧凑分隔符，保证进程间口径一致。"""
    text = json.dumps(entry, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def workspace_key(root: str | Path | None) -> str:
    """工作区 → 稳定短哈希。None（未绑定项目）返回空串，不与任何工作区撞键。"""
    if root is None:
        return ""
    try:
        text = str(Path(root).resolve()).casefold()
    except OSError:
        text = str(root).casefold()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def approval_key(scope: str, root: str | Path | None, name: str) -> str:
    return f"{scope}:{workspace_key(root)}:{name}"


def load_approvals(path: Path | None = None) -> dict[str, str]:
    """key → 已批准时那条定义的摘要。

    文件缺失＝一条都没批准；文件坏了同样返回空表（fail-closed），但不去改写它，
    留给下一次 ``approve``/``revoke`` 覆盖成合法内容。
    """
    target = path or approvals_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    entries = payload.get("approvals") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in entries.items():
        digest = value.get("digest") if isinstance(value, dict) else value
        if isinstance(digest, str) and digest:
            result[str(key)] = digest
    return result


def _write(path: Path, approvals: dict[str, str]) -> None:
    body = {
        "approvals": {
            key: {"digest": digest, "approved_at": datetime.now(timezone.utc).isoformat()}
            for key, digest in approvals.items()
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    # 原子替换：半截 JSON 会让下一次启动把所有批准读丢
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=path.name + ".", suffix=".tmp", delete=False
    )
    try:
        with handle:
            json.dump(body, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def is_approved(
    scope: str,
    root: str | Path | None,
    name: str,
    entry: Any,
    path: Path | None = None,
) -> bool:
    """定义逐字节一致才算批准过；改一个字段就得重新点一次。"""
    if scope not in APPROVAL_SCOPES:
        return False
    approved = load_approvals(path).get(approval_key(scope, root, name))
    return bool(approved) and approved == entry_digest(entry)


def approve(
    scope: str,
    root: str | Path | None,
    name: str,
    entry: Any,
    path: Path | None = None,
) -> str:
    if scope not in APPROVAL_SCOPES:
        raise ValueError(f"只有 {APPROVAL_SCOPES} 需要批准（收到 {scope}）")
    target = path or approvals_path()
    approvals = load_approvals(target)
    digest = entry_digest(entry)
    approvals[approval_key(scope, root, name)] = digest
    _write(target, approvals)
    return digest


def revoke(
    scope: str,
    root: str | Path | None,
    name: str,
    path: Path | None = None,
) -> bool:
    """撤掉一条批准；返回是否真的删掉了记录（没批准过也算成功撤销）。"""
    target = path or approvals_path()
    approvals = load_approvals(target)
    existed = approvals.pop(approval_key(scope, root, name), None) is not None
    if existed:
        _write(target, approvals)
    return existed


__all__ = [
    "APPROVAL_SCOPES",
    "approval_key",
    "approvals_path",
    "approve",
    "entry_digest",
    "is_approved",
    "load_approvals",
    "revoke",
    "workspace_key",
]
