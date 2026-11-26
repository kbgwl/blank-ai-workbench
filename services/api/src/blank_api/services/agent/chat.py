# -*- coding: utf-8 -*-
"""无工作目录的纯聊天支撑。

产品上的分界只有一条：**没有目录 = 纯聊天，有目录 = 可以操作电脑。**
纯聊天不应该为了回答一句话先要求用户选目录，但也不能偷偷沿用项目上次的
活动目录——那会让「只是聊两句」变成一次看不见的文件操作。

这里的根只给运行时的上下文装配用（技能发现、上下文管家、记忆召回都要求一个
root），**不会暴露任何文件/命令工具**：runtime 在 chat_only 时把工具表清空，
create_plan 直接走一次回答，不拍快照、不进计划/校验/修复回路。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def chat_root(task_id: str) -> Path:
    """本次纯聊天任务的临时根。不可写时也返回路径，让上层报出可读错误。"""
    base = os.environ.get("BLANK_CHAT_DIR") or str(Path(tempfile.gettempdir()) / "blank-chat")
    safe = "".join(ch for ch in str(task_id or "") if ch.isalnum() or ch in "-_") or "task"
    path = Path(base) / safe
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


CHAT_ONLY_SYSTEM_RULE = (
    "当前对话没有选择工作目录，也没有任何文件或命令工具。\n"
    "· 直接回答用户的问题；不要声称读过、写过文件，也不要声称执行过命令。\n"
    "· 不要把自己描述成「工作在某个工作区」，也不要暗示当前能读写文件；"
    "按普通对话助手回答。\n"
    "· 如果用户要求读写文件、修改代码、跑命令或操作电脑，只说明："
    "这件事需要先选择一个工作目录，并提示用户点输入框下方的「选择目录」。\n"
    "  不要要求用户手填路径。不要假装已经做了，也不要编造执行结果。\n"
    "· 用户要求检查电脑/清理磁盘时，说明当前没有工作目录，无法访问文件或执行命令；"
    "引导选择目录，并说明单个目录只覆盖该目录，检查整机需要选盘根或把盘加入允许列表。\n"
)

LIST_ONLY_TOOLS = frozenset({"list_files", "glob"})

LIST_ONLY_SYSTEM_RULE = (
    "用户只要求列出文件或目录。\n"
    "· 只调用 list_files / glob 获取路径；不要读取文件内容，也不要分析用途。\n"
    "· 拿到路径后直接用 Markdown 列表输出，一项一行，文件/路径用反引号。\n"
    "· 不要写总结段、不要解释目录用途，除非用户明确问内容。\n"
)

NO_TOOLS_SYSTEM_RULE = (
    "用户明确要求本轮不要调用任何工具（不要读取/修改文件、不要执行命令，"
    "或只要求一段文字回答）。\n"
    "· 直接根据对话与已知信息回答；不要尝试读取/写入文件、执行命令或调用任何工具。\n"
    "· 不要声称已经读取、修改或执行过任何东西。\n"
    "· 如果仅凭现有信息无法回答，直接说明还需要什么信息，不要自行去查。\n"
)

