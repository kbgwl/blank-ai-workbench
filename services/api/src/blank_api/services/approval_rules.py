"""审批前缀规则的落盘：重启或换任务之后，用户批准过的命令签名还认得。

Codex 把用户的"总是允许"写成 `~/.codex/rules/default.rules`，进程重启不失效。
我们此前只有两层记忆：会话内（`PermissionPolicy`）与任务内（从 `approval.granted`
事件回放），换一个任务就重新问一遍。这一层只持久化**前缀档**：

- 不落 `scope=tool`：那是"本会话总是允许某工具"的宽口径，写盘会把它变成整机永久放行；
- 不落 `scope=once`：一次性批准本来就只对那一次有效；
- 不可逆命令从不产生前缀规则（`memo_rule()` 已经拒了），所以文件里不会出现
  `git push --force` 这类条目；
- 文件坏了/读不出来 ⇒ 一条规则都不认（fail-closed），宁可多问一次。

**存储形状（v2）对齐 opencode 的 `permission-saved`**：磁盘上只有一份 `resources`
清单（`cmd:git push`），不再存原始 token 数组。v1 的 `tokens` 仍然能读——读的时候
用 `render_rule()` 转成同一个串，转换完还要**反过来解析验一遍**：往返不一致的规则
宁可丢掉，因为切残的规则比原命令更宽（PowerShell 里用两个双引号表示引号本身的写法，
就是这种切残形状）。
派生出来的 `tokens` 只在内存里给调用方用，不落盘，所以不存在两本账。

位置由 `BLANK_APPROVALS_FILE` 指定，默认与设置文件同目录（桌面壳把设置指到用户数据目录，
规则自然跟着走）。删除这个文件即"忘记所有已批准前缀"。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

VERSION = 2
LEGACY_VERSION = 1
# 文件是给人看得懂、也能手工编辑的；上限只防无限增长，不参与安全判定。
MAX_RULES = 200
SUPPORTED_TOOL = "run_shell"
# 资源串前缀：`cmd:` 表示一段命令前缀，与判定层的 resources_for() 同一种写法。
CMD = "cmd:"


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


def _encode(tool: object, tokens: object) -> tuple[str, str] | None:
    """token 元组 → 资源串。往返不精确就拒绝落盘（fail-closed 到"下次再问"）。"""
    from blank_api.services.permissions import prefix_rule, render_rule

    normalized = _normalize(tool, tokens)
    if normalized is None:
        return None
    text = render_rule(normalized[1])
    if not text or prefix_rule(text) != normalized[1]:
        return None
    return normalized[0], f"{CMD}{text}"


def _decode(tool: object, resources: object) -> tuple[str, tuple[str, ...], str] | None:
    """资源串 → (工具, token 元组, 命中的那条资源串)。只认 `cmd:`。"""
    from blank_api.services.permissions import prefix_rule

    if str(tool or "") != SUPPORTED_TOOL:
        return None
    if not isinstance(resources, (list, tuple)):
        return None
    for item in resources:
        text = str(item or "")
        if not text.startswith(CMD):
            continue
        parsed = prefix_rule(text[len(CMD) :])
        if parsed:
            return SUPPORTED_TOOL, parsed, text
    return None


def _read(path: Path) -> list[dict]:
    """读规则。文件缺失、损坏、版本不认识一律返回空表（fail-closed）。

    v1（`tokens` 数组）与 v2（`resources` 清单）都收，统一成 v2 形状返回，并附带
    由资源串还原的 `tokens`——那是派生值，不落盘。
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    if not isinstance(raw, dict) or raw.get("version") not in (VERSION, LEGACY_VERSION):
        return []
    items = raw.get("rules")
    if not isinstance(items, list):
        return []
    legacy = raw.get("version") == LEGACY_VERSION
    rules: list[dict] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        tool = item.get("tool")
        if legacy:
            encoded = _encode(tool, item.get("tokens"))
            if encoded is None:
                continue
            decoded = _decode(encoded[0], [encoded[1]])
        else:
            decoded = _decode(tool, item.get("resources"))
        if decoded is None:
            continue
        key = (decoded[0], decoded[1])
        if key in seen:
            continue
        seen.add(key)
        rules.append(
            {
                "tool": decoded[0],
                "resources": [decoded[2]],
                "tokens": list(decoded[1]),
                "created_at": str(item.get("created_at") or ""),
                "source_task": str(item.get("source_task") or ""),
            }
        )
    return rules[:MAX_RULES]


def load_rules() -> list[dict]:
    return _read(path())


def load_prefix_rules() -> set[tuple[str, ...]]:
    """给判定用的形态：只含 token 元组（与改造前逐字一致，判定不会变宽）。"""
    return {tuple(item["tokens"]) for item in load_rules()}


def add_rule(tool: str, tokens, source_task: str = "") -> bool:
    """记下一条前缀规则；返回是否真的落盘。

    写盘失败不让这次已获批准的调用失败——用户批的是"执行这条命令"，
    不是"写这个文件"。失败与否由调用方记进事件，证据链里看得见。
    """
    encoded = _encode(tool, tokens)
    if encoded is None:
        return False
    target = path()
    rules = [item for item in _read(target) if (item["tool"], item["resources"][0]) != encoded]
    rules.insert(
        0,
        {
            "tool": encoded[0],
            "resources": [encoded[1]],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_task": str(source_task or ""),
        },
    )
    payload = {
        "version": VERSION,
        # 派生的 tokens 不落盘：磁盘上只有一份事实（resources）。
        "rules": [
            {key: value for key, value in item.items() if key != "tokens"} for item in rules[:MAX_RULES]
        ],
    }
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
