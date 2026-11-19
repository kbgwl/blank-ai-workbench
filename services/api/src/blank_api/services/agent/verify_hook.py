"""改完就跑一次校验：成功沉默，失败把原文塞回循环。

社区口径（HumanLayer / Addy Osmani）：`success is silent, failures are verbose`——
过了就什么都不说，挂了把错误原文注入下一轮。它的价值不在"多一道检查"，而在
**模型不必自己想起来验证**：真机里它连调两次 `run_tests` 都超时，最后干脆不验了。

为什么不给默认命令：跑什么校验是项目知识（pytest / npm test / tsc / 自定义脚本），
猜错会变成"每次改文件都跑一遍全仓 25 分钟单测"——那正是我们判错过一次的东西。
所以这里只提供**机制**，命令从 `BLANK_POST_EDIT_CHECK` 读；没配就整段跳过。

三条纪律：

1. **节流**：同一段时间内只跑一次（默认为每 3 次改动 / 至少隔 4 步），否则一次重构
   能把测试跑成刷屏；
2. **静默成功**：过了不写任何东西，不占上下文；
3. **失败只带尾巴**：错误原文截 2,000 字符，注进 hook_context，只进下一次调用。
"""

from __future__ import annotations

import os
import shlex

# 连续改几次才跑一次：一次重构常常连改三五个文件，逐个跑纯属浪费。
EDITS_BETWEEN_CHECKS = 3
# 两次检查之间至少隔几步，避免"改一步跑一次"。
STEPS_BETWEEN_CHECKS = 4
# 失败原文最多带多少字符。
ERROR_TAIL_CHARS = 2_000


def check_command() -> str:
    """配置里的校验命令；没配就是空串（整段跳过）。"""
    return (os.environ.get("BLANK_POST_EDIT_CHECK") or "").strip()


def should_check(
    *,
    command: str,
    edits_since_check: int,
    steps_since_check: int,
    changed_in_this_step: bool,
) -> bool:
    """这一步要不要跑校验。四条同时满足才跑。"""
    if not command:
        return False
    if not changed_in_this_step:
        return False
    if edits_since_check < EDITS_BETWEEN_CHECKS:
        return False
    return steps_since_check >= STEPS_BETWEEN_CHECKS


def argv_of(command: str) -> list[str] | None:
    """拆命令。Windows 下用 shell 跑更稳（引号、内置命令），所以这里只做非空校验。"""
    return [command] if command.strip() else None


def failure_note(command: str, exit_code: int, output: str) -> str:
    """失败时的注入文本：说清是哪条命令、退出码、以及错误末尾。

    成功时**什么都不返回**——这是这条机制的重点：安静的那一半才是它能长期开着的原因。
    """
    tail = (output or "").strip()[-ERROR_TAIL_CHARS:]
    head = f"【系统：改完自动校验失败】`{command}` 退出码 {exit_code}。按下面的报错改，改完继续；不要跳过它。"
    return f"{head}\n{tail}" if tail else f"{head}（没有输出：命令可能没找到，或它需要的环境没就绪）"
