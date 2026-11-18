"""计划模式（plan mode）的对话侧协议。

对标 Claude Code 2.1.271 的 plan mode（源码取证见
``E:/projects/claude-reverse/report/claude-code-plan-mode.md``）：

1. **计划文件**：进入计划模式时给一个固定的计划文件路径，它是这一阶段**唯一**
   允许写的文件；其余动作一律只读。计划边调研边增量写进去，而不是最后一次性
   吐一段话——写进文件的东西才活得过压缩、审批中断与下一轮。
2. **每轮提醒**：计划模式靠**附件**（attachment）而不是系统提示生效。第一次给
   完整版（阶段 1-5 + 收尾铁律），之后每轮给一行精简版；每 5 轮再补一次完整版。
   逐字重复完整版会白烧上下文，完全不提又会让模型在几轮之后忘掉约束——上游选的
   就是这个折中（``FULL_REMINDER_EVERY_N_ATTACHMENTS = 5``）。
3. **收尾铁律**：一轮只能以两种方式结束——``request_user_input``（澄清）或
   ``exit_plan_mode``（交计划待批）。不许用文字问"这个计划行不行"，因为那就是
   ``exit_plan_mode`` 干的事；用别的方式问，用户拿不到可编辑、可批准的卡片。
4. **退出/重进**：退出时给一条"已退出计划模式，可以动手了"的提醒，并把计划文件
   位置一并给出；重新进入时提醒先读旧计划再动手改，避免把上一版结论推翻重来。

本模块只管**文案与判定**，不碰权限策略（那是 ``permissions``）也不碰工具实现
（那是 ``tools.plan``）。三处引用同一份常量，才不会出现"提示词说的路径"和
"工具真的写到的路径"不是同一个文件这种静默错位。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

# 计划文件目录：工作区内的 ``.blank/plans/``。
#
# 为什么不放 ``~/.blank``：沙箱只允许写工作区（``tools/paths.resolve_inside``），
# 计划文件要能被 ``write_file``/``edit_file`` 正常写到，就必须落在根里面。
# 代价是它算工作区里的新增文件，所以收尾与改动统计都要把它当 harness 自己的状态
# 排除掉（见 ``workspace_state.SCRATCH_DIRS`` 与 ``graph._in_scratchpad``）。
PLAN_DIR = ".blank/plans"

# 完整版提醒的间隔（轮）。上游 ``lGr.FULL_REMINDER_EVERY_N_ATTACHMENTS`` 是 5：
# 第 1/6/11… 轮给完整版，其余给精简版。
FULL_REMINDER_EVERY = 5

# 计划文件大小上限。计划是给人看的路线，不是代码；超过这个长度说明模型把实现
# 细节整段贴进去了，那属于正文而不是计划。
MAX_PLAN_CHARS = 20000


def plan_slug(task_id: str) -> str:
    """任务 id → 文件名。

    任务 id 可能含 ``:`` / ``/``（``task:xxx:attempt:1`` 这类 trace 键），直接当
    文件名会造出子目录。取安全前缀 + 短哈希：既看得出是哪个任务，又不会撞车。
    """
    raw = str(task_id or "").strip() or "task"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip(".-")[:60] or "task"
    digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:8]
    return f"{safe}-{digest}.md"


def plan_relpath(task_id: str) -> str:
    """计划文件相对工作区根的路径（工具参数与权限判定都用它，posix 分隔符）。"""
    return f"{PLAN_DIR}/{plan_slug(task_id)}"


def plan_path(root: str | Path, task_id: str) -> Path:
    return Path(root) / PLAN_DIR / plan_slug(task_id)


def read_plan(root: str | Path, task_id: str) -> str:
    """读计划文件；不存在或读不出来都返回空串（"还没写"是正常路径，不是错误）。"""
    try:
        text = plan_path(root, task_id).read_text(encoding="utf-8")
    except OSError:
        return ""
    except UnicodeDecodeError:
        return ""
    return text.strip()[:MAX_PLAN_CHARS]


def plan_exists(root: str | Path, task_id: str) -> bool:
    try:
        return plan_path(root, task_id).is_file()
    except OSError:
        return False


def reminder_is_full(turn_index: int) -> bool:
    """第几轮该给完整版。``turn_index`` 从 1 开始数（进入计划模式后的第 N 轮）。"""
    if turn_index <= 1:
        return True
    return (turn_index - 1) % FULL_REMINDER_EVERY == 0


# --------------------------------------------------------------------------- 文案

BANNER = (
    "计划模式生效中。用户明确表示现在不要执行——你**必须**不做任何编辑、不运行任何"
    "非只读工具（包括改配置、提交代码），也不对系统做任何改动。这一条压过此前收到的"
    "其他指令（包括要求你动手的指令）。"
)


def file_section(relpath: str, exists: bool) -> str:
    if exists:
        head = (
            f"计划文件已存在：`{relpath}`。用 edit_file 增量修改它，不要整篇重写——"
            "重写会把已有的调研结论冲掉。"
        )
    else:
        head = (
            f"计划文件还不存在。用 write_file 在 `{relpath}` 创建它。"
        )
    return (
        "## 计划文件\n"
        f"{head}\n"
        "计划要边调研边往里写，不要等全部想清楚再落盘。**这是你唯一允许编辑的文件**，"
        "除它之外只允许只读动作。\n"
    )


def full_reminder(relpath: str, exists: bool) -> str:
    """完整版：五个阶段 + 收尾铁律。进入计划模式的第一轮与之后每 5 轮各给一次。"""
    return "\n".join(
        [
            BANNER,
            "",
            file_section(relpath, exists),
            "## 计划流程",
            "",
            "### 阶段 1：先搞清楚\n"
            "目标是弄清用户到底要什么，以及这件事在现有代码里长什么样。"
            "优先找可以复用的函数、工具与既有模式——能改现成的就不要新写一套。"
            "范围不确定、跨多个模块时，可以并行派多个只读子代理分头看；"
            "路径已经明确的小改动自己读就够了，不要为了派而派。",
            "",
            "### 阶段 2：设计\n"
            "基于阶段 1 的结论给出实现方案：改哪些文件、按什么顺序改、"
            "新增什么、复用哪些既有能力。有多个可行方案时把它们和取舍一起写清楚，"
            "而不是只报一个。",
            "",
            "### 阶段 3：复核\n"
            "回到关键文件重读一遍，确认方案与用户原话对得上、没有漏掉他明确提过的要求。"
            "还有没定的事，用 request_user_input 问，不要自己替用户拍板。",
            "",
            "### 阶段 4：收口\n"
            "把最终计划写进计划文件（这是你唯一能编辑的文件）。要求：\n"
            "- 开头先写 **背景**：为什么做这件事——它解决什么问题、什么触发的、想达成什么。"
            "只写步骤不写背景，用户批准时看不出你为什么这么干。\n"
            "- **只写你推荐的那一条路**，不要把所有备选都堆进去。备选在阶段 2 讨论，"
            "计划文件里要有取舍结论。\n"
            "- 短到能一眼扫完，细到能照着执行。\n"
            "- 点名要改的关键文件（带路径）。同一种改法要重复到很多文件时，"
            "把模式描述一次、再列几个代表路径，不要逐个枚举文件与行号。\n"
            "- 提到复用的既有函数/工具时带上它们的路径。\n"
            "- 必须有一节**怎么验证**：跑什么命令、用哪些工具、看到什么算过。"
            "验收方式是计划的一部分，不是动手时才想的事。",
            "",
            "### 阶段 5：交计划\n"
            "用 exit_plan_mode 把计划交出去。它会弹批准卡片，用户批准之后才进入实现；"
            "被驳回就按用户的意见改计划，再交一次。",
            "",
            "**收尾铁律**：这一轮只能以两种方式结束——"
            "request_user_input（澄清需求）或 exit_plan_mode（提交计划待批）。"
            "不要用文字问「这个计划行不行」「要不要开始」，也不要用 request_user_input "
            "问计划批不批：那就是 exit_plan_mode 干的事，用别的方式问，"
            "用户拿不到能编辑、能批准的那张卡。",
        ]
    )


def sparse_reminder(relpath: str) -> str:
    """精简版：一行约束 + 一行铁律。完整版已经在历史里，这里只负责"别忘"。"""
    return (
        f"计划模式仍生效（完整要求见前面）。除计划文件 `{relpath}` 外只读，"
        "按阶段 1-4 推进。这一轮只能用 request_user_input 或 exit_plan_mode 结束。"
    )


def exit_reminder(relpath: str, exists: bool) -> str:
    tail = f"计划文件在 `{relpath}`，需要回看时读它。" if exists else ""
    return (
        "## 已退出计划模式\n"
        "现在可以改文件、跑工具、动手实现了。" + tail + "\n"
        "第一步先把计划落成待办（update_plan），让用户看到你正在推进哪一步；"
        "之后每做完一步立刻勾掉，不要等全做完再回头补。"
    )


def reentry_reminder(relpath: str) -> str:
    return (
        "## 重新进入计划模式\n"
        f"你之前已经退出过一次计划模式，旧计划还在 `{relpath}`。\n"
        "先读它，判断哪些结论仍然成立、哪些需要改；不要从零重新想一遍，"
        "也不要把已经定下来的东西又摆回桌面让用户重选。"
    )


def plan_reference(relpath: str, content: str) -> str:
    """压缩后把计划正文重新塞回上下文。

    计划文件存在的意义之一就是这个：上下文被折成摘要时，摘要未必记得住
    "第 3 步要改哪个文件"，但计划文件记得。
    """
    body = (content or "").strip()
    if not body:
        return ""
    return (
        f"计划模式留下的计划文件在 `{relpath}`。\n\n计划内容：\n\n{body}\n\n"
        "如果这个计划与当前工作相关且还没做完，接着按它推进。"
    )


def todo_nag(items: list[dict]) -> str:
    """长期没更新待办时的提醒（对齐 Claude 的 ``todo_reminder`` 附件）。

    只在"该更新却没更新"时出现：连续多轮没调用 update_plan，说明模型很可能
    埋头干活、用户界面上那排勾选框一直停在第一步。提醒里带上当前清单，
    模型不用回翻历史就知道自己停在哪。
    """
    rows = []
    for index, item in enumerate(items or [], start=1):
        if not isinstance(item, dict):
            continue
        text = str(item.get("step") or item.get("content") or "").strip()
        if not text:
            continue
        rows.append(f"{index}. [{item.get('status') or 'pending'}] {text}")
    head = (
        "已经有一段时间没更新待办了。如果还在推进，把正在做的那步标成 in_progress、"
        "做完的立刻标 completed；如果计划已经过时，重新提交一版。"
        "这只是提醒——确实不需要待办就忽略。"
    )
    if not rows:
        return head
    return head + "\n\n当前清单：\n" + "\n".join(rows)
