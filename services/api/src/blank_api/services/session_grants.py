"""会话级审批授权的落盘：同一会话里“这类动作不再逐条问”活过一轮任务。

任务级记忆（``PermissionPolicy.session_grants``）只活一次运行；而用户说的
“本会话”是界面上的一条对话（``threads.id``），一轮任务结束不该让授权失效。
这里按 ``thread_id + 动作类别`` 存一份小账本：

- **只存人工批准过的固定类别**（``SESSION_GRANT_LABELS``）。bypass 档本身
  不写任何东西——它只免低风险，不能让“删库”悄悄变成默认动作。
- **默认 24 小时过期**（``BLANK_SESSION_GRANT_TTL_SECONDS`` 可改，最小 60 秒）。
  会话可以开很久，授权不能永远有效；过期后重新挂卡。
- **文件坏了/读不出来 ⇒ 一条授权都不认**（fail-closed），宁可多问一次。
- 写盘用临时文件 + ``os.replace``，与 ``approvals.json`` 同一套做法；
  写失败不让这次已获批的调用失败，调用方把“没落盘”记进事件。

形状::

    {"version": 1, "grants": [
      {"thread": "<uuid>", "category": "git_push", "granted_at": "...",
       "expires_at": "...", "source_task": "..."}
    ]}
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from blank_api.services.permissions import SESSION_GRANT_LABELS

VERSION = 1
MAX_GRANTS = 500
DEFAULT_TTL_SECONDS = 24 * 60 * 60
MIN_TTL_SECONDS = 60

_LOCK = threading.Lock()


def ttl_seconds() -> int:
    raw = str(os.environ.get("BLANK_SESSION_GRANT_TTL_SECONDS") or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_TTL_SECONDS
    return max(MIN_TTL_SECONDS, value)


def path() -> Path:
    override = str(os.environ.get("BLANK_SESSION_GRANTS_FILE") or "").strip()
    if override:
        return Path(override)
    from blank_api.services.settings import settings_path

    return settings_path().with_name("session-grants.json")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expires_at(item: dict) -> str:
    raw = str(item.get("expires_at") or "").strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return ""


def _normalize(item: object, now: datetime) -> dict | None:
    if not isinstance(item, dict):
        return None
    thread = str(item.get("thread") or "").strip()
    category = str(item.get("category") or "").strip()
    if not thread or category not in SESSION_GRANT_LABELS:
        return None
    expires_at = _expires_at(item)
    if not expires_at:
        return None
    try:
        if datetime.fromisoformat(expires_at) <= now:
            return None
    except ValueError:  # pragma: no cover - _expires_at 已经解析过一次
        return None
    return {
        "thread": thread[:64],
        "category": category,
        "granted_at": str(item.get("granted_at") or ""),
        "expires_at": expires_at,
        "source_task": str(item.get("source_task") or "")[:64],
    }


def _read(target: Path) -> list[dict]:
    """读授权。文件缺失、损坏、版本不认识一律返回空表（fail-closed）。"""
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    if not isinstance(raw, dict) or raw.get("version") != VERSION:
        return []
    items = raw.get("grants")
    if not isinstance(items, list):
        return []
    now = _now()
    seen: set[tuple[str, str]] = set()
    grants: list[dict] = []
    for item in items:
        normalized = _normalize(item, now)
        if normalized is None:
            continue
        key = (normalized["thread"], normalized["category"])
        if key in seen:
            continue
        seen.add(key)
        grants.append(normalized)
    return grants[:MAX_GRANTS]


def _write(target: Path, grants: list[dict]) -> bool:
    payload = {"version": VERSION, "grants": grants[:MAX_GRANTS]}
    temp = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, target)
    except OSError:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def load_categories(thread_id: str) -> set[str]:
    """该会话当前有效的授权类别。空 thread_id（独立任务）没有会话级授权。"""
    thread = str(thread_id or "").strip()
    if not thread:
        return set()
    return {item["category"] for item in _read(path()) if item["thread"] == thread}


def list_grants(thread_id: str) -> list[dict]:
    thread = str(thread_id or "").strip()
    if not thread:
        return []
    return [dict(item) for item in _read(path()) if item["thread"] == thread]


def grant(thread_id: str, category: str, source_task: str = "") -> bool:
    """记下一次人工批准；返回是否真的落盘。

    写盘失败不让这次已获批的调用失败——用户批的是“执行这类动作”，
    不是“写这个文件”。调用方把 false 记进审批事件，证据链里看得见。
    """
    thread = str(thread_id or "").strip()
    name = str(category or "").strip()
    if not thread or name not in SESSION_GRANT_LABELS:
        return False
    target = path()
    now = _now()
    expires_at = (now + timedelta(seconds=ttl_seconds())).isoformat()
    with _LOCK:
        grants = [
            item for item in _read(target) if (item["thread"], item["category"]) != (thread, name)
        ]
        grants.insert(
            0,
            {
                "thread": thread[:64],
                "category": name,
                "granted_at": now.isoformat(),
                "expires_at": expires_at,
                "source_task": str(source_task or "")[:64],
            },
        )
        return _write(target, grants)


def revoke(thread_id: str, category: str) -> bool:
    thread = str(thread_id or "").strip()
    name = str(category or "").strip()
    if not thread or not name:
        return False
    target = path()
    with _LOCK:
        grants = _read(target)
        kept = [
            item for item in grants if (item["thread"], item["category"]) != (thread, name)
        ]
        if len(kept) == len(grants):
            return False
        return _write(target, kept)


def revoke_all(thread_id: str) -> int:
    """撤销该会话的全部授权，返回撤掉的条数。会话不存在时返回 0。"""
    thread = str(thread_id or "").strip()
    if not thread:
        return 0
    target = path()
    with _LOCK:
        grants = _read(target)
        kept = [item for item in grants if item["thread"] != thread]
        removed = len(grants) - len(kept)
        if removed <= 0:
            return 0
        return removed if _write(target, kept) else 0


def purge_thread(thread_id: str) -> bool:
    """会话被删除时顺手清账；留到过期也无害，只是不该让旧授权继续可见。"""
    thread = str(thread_id or "").strip()
    if not thread:
        return False
    target = path()
    with _LOCK:
        grants = _read(target)
        kept = [item for item in grants if item["thread"] != thread]
        if len(kept) == len(grants):
            return False
        return _write(target, kept)
