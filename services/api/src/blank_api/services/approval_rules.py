"""审批前缀规则的落盘：重启或换任务之后，用户批准过的命令签名还认得。

Codex 把用户的"总是允许"写成 `~/.codex/rules/default.rules`，进程重启不失效。
我们此前只有两层记忆：会话内（`PermissionPolicy`）与任务内（从 `approval.granted`
事件回放），换一个任务就重新问一遍。这一层只持久化**前缀档**：

- 不落 `scope=tool`：那是"本会话总是允许某工具"的宽口径，写盘会把它变成整机永久放行；
- 不落 `scope=once`：一次性批准本来就只对那一次有效；
- 不可逆命令从不产生前缀规则（`memo_rule()` 已经拒了），所以文件里不会出现
  `git push --force` 这类条目；
- 文件坏了/读不出来 ⇒ 一条规则都不认（fail-closed），宁可多问一次。

位置由 `BLANK_APPROVALS_FILE` 指定，默认与设置文件同目录（桌面壳把设置指到用户数据目录，
规则自然跟着走）。删除这个文件即"忘记所有已批准前缀"。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

VERSION = 1
# 文件是给人看得懂、也能手工编辑的；上限只防无限增长，不参与安全判定。
MAX_RULES = 200
SUPPORTED_TOOL = "run_shell"


def path() -> Path:
    override = str(os.environ.get("BLANK_APPROVALS_FILE") or "").strip()
    if override:
        return Path(override)
    from blank_api.services.settings import settings_path

    return settings_path().with_name("approvals.json")


def _normalize(tool: object, tokens: object) -> tuple[str, tuple[str, ...]] | None:
    if str(tool or "") != SUPPORTED_TOOL:
        return None
    if not isinstance(tokens, (list, tuple)):
        return None
    cleaned = tuple(str(item) for item in tokens if str(item or ""))
    if not cleaned:
        return None
    return SUPPORTED_TOOL, cleaned


def _read(path: Path) -> list[dict]:
    """读规则。文件缺失、损坏、版本不认识一律返回空表（fail-closed）。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    if not isinstance(raw, dict) or raw.get("version") != VERSION:
        return []
    items = raw.get("rules")
    if not isinstance(items, list):
        return []
    rules: list[dict] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        normalized = _normalize(item.get("tool"), item.get("tokens"))
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        rules.append(
            {
                "tool": normalized[0],
                "tokens": list(normalized[1]),
                "created_at": str(item.get("created_at") or ""),
                "source_task": str(item.get("source_task") or ""),
            }
        )
    return rules[:MAX_RULES]


def load_rules() -> list[dict]:
    return _read(path())


def load_prefix_rules() -> set[tuple[str, ...]]:
    """给判定用的形态：只含 token 元组。"""
    return {tuple(item["tokens"]) for item in load_rules()}


def add_rule(tool: str, tokens, source_task: str = "") -> bool:
    """记下一条前缀规则；返回是否真的落盘。

    写盘失败不让这次已获批准的调用失败——用户批的是"执行这条命令"，
    不是"写这个文件"。失败与否由调用方记进事件，证据链里看得见。
    """
    normalized = _normalize(tool, tokens)
    if normalized is None:
        return False
    target = path()
    rules = _read(target)
    rules = [item for item in rules if (item["tool"], tuple(item["tokens"])) != normalized]
    rules.insert(
        0,
        {
            "tool": normalized[0],
            "tokens": list(normalized[1]),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_task": str(source_task or ""),
        },
    )
    payload = {"version": VERSION, "rules": rules[:MAX_RULES]}
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
