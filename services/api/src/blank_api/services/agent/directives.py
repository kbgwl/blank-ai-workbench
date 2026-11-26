# -*- coding: utf-8 -*-
"""用户消息里的硬指令：这一轮到底许不许动工具。

动作词判定有一个真实缺陷：**否定句里也含动作词**。
「只回复 OK，不要读取或修改任何文件」会被 `_ACTION` 里的「读取/修改」
判成 act，Agent 于是真去翻代码——用户要的却是一句回答。真机探针复现过
20 多秒的空转。

这里只做一件事：在进入意图判定之前，先认出三种明确指令。

- ``no_tools``：这一轮不要碰任何文件/命令，只直接回答。
  例：只回复、只解释、不要读取、不要调用工具。
- ``read_only``：可以看，但不要改、不要执行。
  例：不要修改代码、帮我看看哪里错；不要执行命令，只分析。
- ``none``：没有这类硬指令，按原意图链路走。

刻意不解析复杂的自然语言否定（「不是不能改」这类）。规则只收明确、可解释的
说法；认不出时交回原链路，避免把一次真任务误判成纯聊天。
"""

from __future__ import annotations

import re

NO_TOOLS = "no_tools"
READ_ONLY = "read_only"
NONE = "none"

# 「不要忘了运行测试」是在催办，不是在禁止运行。先把这类反向说法拿掉，
# 否则下面的「不要 + 运行」会把一次真任务判成只读。
_FORGET = re.compile(r"(?:千万)?(?:不要|别)忘了?|(?:千万)?不要忘记")

_ONLY_ANSWER = re.compile(r"(?:只|仅|只要)\s*(?:回复|回答|输出|说|告诉)")
# 「只解释 / 只分析」不禁止读代码，只禁止动手改。归到 read_only，
# 否则一次「只解释这段代码」会被误判成连文件都不能看。
_ONLY_REASON = re.compile(r"(?:只|仅|只要)\s*(?:解释|说明|分析|评估|判断)")
_NO_READ = re.compile(
    r"(?:不要|别|不许|不准|禁止|无需|不用|不必|切勿)\s*(?:再)?\s*"
    r"(?:读取|读文件|读代码|读一下|查看文件|打开文件|浏览文件)"
)
_NO_TOOL = re.compile(
    r"(?:不要|别|不许|不准|禁止|无需|不用|不必|切勿)\s*(?:再)?\s*"
    r"(?:调用|使用|启用)\s*(?:任何)?\s*(?:工具|函数|命令)"
)
_NO_COMPUTER = re.compile(
    r"(?:不要|别|不许|不准|禁止|无需|不用|不必|切勿)\s*(?:再)?\s*"
    r"(?:操作|碰|动)\s*(?:电脑|系统|文件|代码|仓库)"
)

_NO_WRITE = re.compile(
    r"(?:不要|别|不许|不准|禁止|无需|不用|不必|切勿)\s*(?:再)?\s*"
    # 不收裸「改」：「不要改变对外 API 名称」是修复约束，不是「不要改代码」。
    # 旧实现把「改变」当成「改」，把整轮锁成只读，代码一处不改必失败。
    r"(?:修改|改动|改写|改代码|改文件|改实现|写|编辑|删除|移除|替换|提交|推送|部署|安装|升级|降级)"
)
_NO_RUN = re.compile(
    r"(?:不要|别|不许|不准|禁止|无需|不用|不必|切勿)\s*(?:再)?\s*"
    r"(?:执行|运行|跑|启动|调用命令|动代码)"
)

# 作用域约束不是“只读”：「不要修改工作区之外的文件」仍允许改工作区内。
# 旧实现把它当成「不要修改」，整轮锁只读；B7 真机任务因此 0 文件改动、
# 验收三条全 false 却 succeeded。先把这类整句拿掉，再判 _NO_WRITE / _NO_RUN。
_OUT_OF_SCOPE = re.compile(
    r"(?:不要|别|不许|不准|禁止|无需|不用|不必|切勿)\s*(?:再)?\s*"
    r"(?:修改|改动|改|写|编辑|删除|移除|替换|执行|运行|跑|启动|操作|碰|动)\s*"
    r"(?:任何)?\s*(?:工作区|仓库|项目|当前目录|目录|代码库|repo)\s*之外"
)

# 文件白名单 / 单文件保护也不是整轮只读。两类真实写法：
#   「只允许创建或修改这些文件：…。其余文件（尤其测试文件）一律不要改动」
#   「不要修改 data.csv」（同一任务还要生成 汇总.md）
# 不先摘掉，「不要改动」会命中 _NO_WRITE，把整个动作任务锁成只读——
# 基准题的 daily-001/002/003 都因此 0 文件改动却 succeeded。
_SCOPE_BOUNDARY = re.compile(
    r"(?:其余|其他|其它|剩余|剩下)(?:的)?\s*(?:文件|内容|部分)"
    r"[^。；\n]{0,24}?(?:不要|别|不许|不准|禁止|无需|不用|不必|切勿)\s*(?:再)?\s*"
    r"(?:修改|改动|改写|写|编辑|删除|移除|替换)"
    r"|"
    r"(?:不要|别|不许|不准|禁止|无需|不用|不必|切勿)\s*(?:再)?\s*"
    r"(?:修改|改动|改写|写|编辑|删除|移除|替换)\s*(?:文件\s*)?"
    r"[`'\"“”]?[\w一-鿿][\w一-鿿.\-/]*\.[A-Za-z0-9]{1,8}[`'\"“”]?"
)

# 只列文件/目录的请求。用户要的是路径清单，不是文件内容；这类任务不该
# 把每个文件读一遍再写一段总结——既慢，又答非所问。
_LIST_REQUEST = re.compile(
    r"(?:列出|列一下|列个|列出来|列一份|给我列|看看|查看|显示|整理|汇总)"
    r".{0,16}(?:文件|目录|文件夹|清单|列表|结构)"
    r"|(?:有(?:哪些|什么|几个))?(?:文件|目录|文件夹).{0,8}(?:清单|列表|结构)"
    r"|有(?:哪些|什么|几个)(?:文件|目录|文件夹)"
)
# 只要出现“内容/用途/分析”等词，就说明用户不是只要路径清单。
_LIST_CONTENT = re.compile(
    r"(?:内容|正文|代码|作用|用途|做什么|干什么|分析|解释|说明|介绍|检查|审查|"
    r"哪一行|为什么|怎么用|依赖|关系|质量|问题)"
)


def directive_mode(text: str) -> str:
    """返回 ``no_tools`` / ``read_only`` / ``none``。"""
    body = _FORGET.sub("", str(text or ""))
    # 「不要修改工作区之外的文件」是范围边界，不是「不要改代码」。
    body = _OUT_OF_SCOPE.sub("", body)
    # 文件白名单 / 单文件保护同理：只限制对象，不禁止整轮动手。
    body = _SCOPE_BOUNDARY.sub("", body)
    if _ONLY_ANSWER.search(body) or _NO_READ.search(body):
        return NO_TOOLS
    if _NO_TOOL.search(body) or _NO_COMPUTER.search(body):
        return NO_TOOLS
    if _ONLY_REASON.search(body) or _NO_WRITE.search(body) or _NO_RUN.search(body):
        return READ_ONLY
    return NONE


def listing_only(text: str) -> bool:
    """用户是不是只要文件/目录清单（不含内容分析）。"""
    body = _FORGET.sub("", str(text or ""))
    return bool(_LIST_REQUEST.search(body)) and not _LIST_CONTENT.search(body)


# 只动文档、不动源码的任务。它不是"不能写"，而是"只写文档"：工具表应把
# 浏览器、任务台账、子代理、notebook/REPL 等无关入口收掉，减少每轮 schema
# 成本和误选；README/文档编辑仍需 write_file/edit_file 与 run_tests。
_DOC_TARGET = re.compile(
    r"(?:README|readme|文档|docs?/|\.md\b|说明文件|使用说明|变更记录|CHANGELOG)",
    re.IGNORECASE,
)
_DOC_BOUNDARY = re.compile(
    r"(?:不改动?源码|不要修改?源码|不改代码|不要改代码|只改文档|只改\s*README|"
    r"仅改文档|文档任务|只更新文档)"
)
_DOC_ACCEPTANCE = re.compile(r"(?:未修改|不改|不要改).{0,8}(?:src|源码|代码)")

# 文档-only 任务的执行纪律：防止模型为了"自证"读遍仓库、写临时脚本、查 git，
# 把一次一文件改动做成十几轮。放在系统提示的协作规则段，不覆盖安全边界。
DOC_ONLY_INSTRUCTION = (
    "本轮是文档-only任务：只改用户指定的文档（README/.md），不要改源码。"
    "只读取目标文档和它直接引用的源码，不要扫描整个仓库；不要写临时校验脚本；"
    "除非用户明确要求，不要查 git / 浏览器 / 任务台账。需要核对函数输出时用一次只读命令，"
    "最后用 run_tests 验证一次即可。"
)


def documentation_only(goal: str, acceptance: object = ()) -> bool:
    """这条任务是不是“只改文档、不动源码”。

    两个条件同时成立才算：目标里点到 README/文档，且用户明确写了“不改源码/
    只改文档”，或验收里写了“未修改 src”。只出现 README 不算——修 README 里
    的代码示例也可能要改源码，不能把它误收成文档任务。
    """
    text = str(goal or "")
    if not _DOC_TARGET.search(text):
        return False
    if _DOC_BOUNDARY.search(text):
        return True
    if isinstance(acceptance, str):
        acceptance = (acceptance,)
    return any(_DOC_ACCEPTANCE.search(str(item or "")) for item in acceptance or ())
