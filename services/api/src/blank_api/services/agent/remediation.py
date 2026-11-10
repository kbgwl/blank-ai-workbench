"""环境缺口补救：缺一条命令不是结论，是要做的工作。

真实读数（安装版 `blank.db`，任务 `4378855e…`，43 步）：agent 跑了 `where node`、
`where npm`，确认本机没有；随后一条 `where /R C:\\ node.exe`（在磁盘上找现成安装）
被空转断路器当成"又用了 node"拦掉；最终交付写「需要你：装上 Node 后重跑 `npm test`」。
任务原话是「为什么别的 Agent 就可以自己测试，你就不可以呢」——它知道该装，
但停在"需要你装"。

提示词只能改变概率（见 `unknowns.py` 的同一段说明）。这里把「拿环境缺口收尾」
做成可判定的三连：

1. 本任务确实有"命令不存在"的回执（不是模型自己说的）；
2. 最终答复是在把它交回用户（需要你装 / 装上后 / 装不了 / 做不了）；
3. 本任务没有任何一次"把它弄到这台机器上"的尝试（包管理器、安装包、下载解压）。

三条同时成立，执行节点把任务推回一轮，并在系统侧给出补齐顺序。上限两次：
第二次之后仍然没有动作，允许如实收尾——断网、没权限这类真装不上也是一种结论，
但必须带着"试过什么、报错是什么"，不能只剩一句"需要你装"。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

# 把活儿交回用户的说法。收得窄：只有这些句子才算"交付里让用户去装"。
# 「本机没有 node，所以我改用了 PowerShell 等价脚本并跑通」不算——那句里没有
# 交回动作，任务确实完成了；误判成收尾会把一次完成的交付再推一轮。
_HANDOFF = re.compile(
    r"(需要|请|建议|麻烦)[\s：:，,、]*(你|您)[\s：:，,、]*(先)?[\s]*(装|安装|下载|配置|准备)"
    r"|装上[\s\S]{0,8}(后|再)"
    r"|(未|没有|无法|不能|装不|做不|跑不)(安装|装上|安装好|继续|完成|运行)"
    r"|(装不了|做不了|做不到|没法继续|无法继续|只能先到这里|到此为止)",
    re.I,
)

# "把它弄到这台机器上"的尝试。只认真正会改变环境的动作：
# 包管理器安装、下载安装包、解压、官方 zip/msi/exe 链接。
# 刻意不把 `where` / `if exist` / `dir` 算进来——那是确认，不是补齐；
# 只有确认没有动作时才会被推回，而推回后模型第一件常做的事就是再确认一遍，
# 把确认算成尝试，等于这条闸门永远不生效。
_OBTAIN = re.compile(
    r"winget\s+(?:install|add)|choco\s+install|scoop\s+install|"
    r"msiexec\s+/|npm\s+(?:install|i)\s+-g|pnpm\s+add\s+-g|yarn\s+global\s+add|"
    r"Invoke-WebRequest|Start-BitsTransfer|Expand-Archive|"
    r"curl\b[^\n]*(?:\s-o\s|\s-O\b|--output)|wget\b|"
    r"https?://\S+\.(?:zip|msi|exe|7z|tar\.gz)|"
    r"(?:下载|download)[^\n]{0,20}(?:安装包|installer|\.zip|\.msi|\.exe|node)",
    re.I,
)

# 只留最近这些条命令进判定，避免检查点里囤一整个任务的全部命令行。
MAX_TRACKED_COMMANDS = 200


def is_handoff(answer: str) -> bool:
    return bool(_HANDOFF.search(str(answer or "")))


def is_obtain_attempt(command: str) -> bool:
    """这条命令是不是在把缺的东西弄到机器上（而不是又用一次）。"""
    return bool(_OBTAIN.search(str(command or "")))


def obtain_attempts(commands: Iterable[str]) -> list[str]:
    return [str(item) for item in commands if is_obtain_attempt(item)]


def gap_reason(
    *,
    answer: str,
    commands: Sequence[str],
    missing: Iterable[str],
) -> str:
    """三条都成立时返回缺的命令名（多个用顿号连），否则回空串。"""
    gaps = sorted({str(item).strip() for item in missing if str(item).strip()})
    if not gaps:
        return ""
    if not is_handoff(answer):
        return ""
    if obtain_attempts(commands):
        return ""
    return "、".join(gaps)


def hint(missing: str, round_number: int) -> str:
    """推回执行时给模型的系统侧指令。只进下一次模型调用，不改用户原话。"""
    return (
        f"【系统：环境缺口未补齐（第 {round_number} 轮）】这次任务里 `{missing}` 不存在，"
        "但还没有任何一次「把它弄到这台机器上」的尝试。不要用「需要你安装」收尾，按顺序做：\n"
        f"1. 现场确认：`where {missing}`、`{missing} --version`，以及常见安装目录；"
        "仓库注释、README、记忆里写着「本机没有它」只能当线索，不能当证据。\n"
        "2. 找现成的：包管理器（winget / choco / scoop）装，或下载官方 portable 包解压；"
        "也可以先在磁盘上找已有的可执行文件。\n"
        f"3. 装上后直接用绝对路径，或把目录加进 PATH，重跑原来失败的那一步并留下输出。\n"
        "联网、装软件需要审批或开关时，按审批规则申请，不要跳过。"
        "只有真的执行过并且失败，才在交付里写卡在哪、附上命令与报错、再说需要用户做什么。"
    )
