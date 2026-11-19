"""上下文重置：把会话拆掉，用一份交接单重建。

依据（`docs/harness-borrowings.md` 第 1 条）：Ralph Loop 的核心不是"更聪明的提示词"，
而是**每轮全新上下文**，状态只走磁盘；Anthropic 自己也承认光靠压缩不够，长任务要
"拆掉会话、用一份紧凑的交接文件重建"。

我们为什么需要它：26 步的任务里 prompt 从 19K 涨到 83K，而**后 14 步没有产生任何
新证据**（同一文件写了 6 次、同一条命令跑了 9 次）。那不是"上下文不够大"，是
"上下文里全是自己刚写下的东西"——模型在重读自己的草稿，而不是在看世界。

判据是"值不值得重开"，三条同时成立才算：

* 步数够多（否则重开只是把上下文丢掉重来）；
* 上下文够大（小会话没有腐烂的问题）；
* 最近若干步没有新证据（有进展时重开会打断它）。

重开不等于失忆：交接单里必须写明目标、改过哪些文件、哪些还没验证、最近一次失败、
以及"不要复述，直接继续"。
"""

from __future__ import annotations

import json

# 步数门槛：低于它，重开的损耗大于收益。
RESTART_MIN_STEPS = 30
# 上下文门槛（token）。低于它说明还谈不上"腐烂"。
RESTART_MIN_TOKENS = 60_000
# 连续多少步没有新证据才算"在原地"。
RESTART_IDLE_STEPS = 8

HANDOFF_HEADER = "【上下文已重置：这是一个新会话，下面是接手这份工作需要的全部状态】"


def should_restart(*, steps: int, tokens: int, idle_steps: int) -> str:
    """要不要重开（返回原因，空串表示不重开）。"""
    if steps < RESTART_MIN_STEPS:
        return ""
    if tokens < RESTART_MIN_TOKENS:
        return ""
    if idle_steps < RESTART_IDLE_STEPS:
        return ""
    return "steps+tokens+no-progress"


def build_restart_messages(
    *,
    system: str,
    goal: str,
    changed: list[str] | dict[str, int],
    unverified: list[str] | set[str],
    last_error: str = "",
    notes: str = "",
    plan: list[str] | None = None,
) -> list[dict]:
    """生成重置后的消息列表：一条 system + 一条交接单。

    刻意只给两条：任何"顺带保留"的历史都会把腐烂一起搬过来。细节的出路是文件
    （改过的文件、笔记、`.blank/tool-results/` 里的完整输出），不是对话记录。
    """
    changed_lines = (
        [f"- {path}（改过 {count} 次）" for path, count in sorted(changed.items(), key=lambda kv: -kv[1])]
        if isinstance(changed, dict)
        else [f"- {path}" for path in changed]
    )
    blocks: list[str] = [HANDOFF_HEADER, "", "目标：", goal.strip() or "（未记录）"]

    if plan:
        blocks.extend(["", "计划："] + [f"{index}. {item}" for index, item in enumerate(plan, 1)])
    if changed_lines:
        blocks.extend(["", "改过的文件："] + changed_lines)
    pending = sorted(str(item) for item in (unverified or []))
    if pending:
        blocks.extend(
            ["", "**还没有验证过**（先跑一次验证，再继续改）："]
            + [f"- {path}" for path in pending]
        )
    if last_error.strip():
        blocks.extend(["", "最近一次失败：", last_error.strip()[:600]])
    if notes.strip():
        blocks.extend(["", "交接笔记：", notes.strip()[:2_000]])
    blocks.extend(
        [
            "",
            "现在接着做。不要复述这份交接单，也不要重新总结任务；"
            "需要细节时按上面提到的文件路径去读（完整工具输出在 .blank/tool-results/）。",
        ]
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(blocks)},
    ]


def summarize_changed(writes: dict[str, int], limit: int = 8) -> dict[str, int]:
    """改过的文件最多列几个：交接单是给"接手的人"看的，不是台账。"""
    ranked = sorted(writes.items(), key=lambda item: -item[1])
    return dict(ranked[:limit])


def as_json(value: object) -> str:
    """事件里记一份可核对的快照（不写消息正文）。"""
    return json.dumps(value, ensure_ascii=False)[:2_000]
