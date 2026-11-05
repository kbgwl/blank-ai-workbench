# -*- coding: utf-8 -*-
"""场景系统：五场景决定模型看到的额外规则、可用工具与记忆权限。

设计红线（2026-10-21 的教训）：**用户的每一句话必须原样进意图判定与模型**。
场景绝不向用户 prompt 拼任何前缀——它的全部影响都落在三个地方：

1. 系统提示末尾追加一段场景规则（在安全规则之后，声明不得覆盖安全与审批规则）；
2. 工具注册表 ``registry.exclude()``：只读场景把写类工具彻底禁掉（检索也捞不回）；
3. 记忆权限：只有日常场景且用户选择"记住"时才开放 ``memory_write``。

因此闲聊被判成什么，只取决于用户那句话本身，与所选场景无关。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class Scenario:
    key: str
    label: str
    """前端选择器上显示的名字。"""
    summary: str
    """一句话说明这个场景做什么（列表接口返回，也进事件）。"""
    # 进系统提示的场景规则段。空串 = 开发场景，行为与没有场景系统时完全一致。
    system_prompt: str = ""
    # False 时把 WRITE_TOOLS 从注册表彻底禁掉（检索也捞不回）。
    allow_write: bool = True
    # 允许落盘的文件扩展名（小写、带点）。空集 = 不按后缀限制（仅开发场景）。
    #
    # 文档场景要的是"能写文档，不能改代码"：一刀切成只读会让它连 README 都写不了，
    # 一刀切成可写就变成可以改 main.py。按后缀授权才是真的权限分级（v164 §4）。
    write_extensions: frozenset[str] = frozenset()
    # 这个场景额外禁用的工具（在 WRITE_TOOLS 之外）。
    #
    # 文档场景保留 apply_patch（要写 .md），但跑测试、跑 shell、git 提交都不是写文档：
    # 它们绕过扩展名限制，一旦放开就能改任何东西。
    tool_denylist: frozenset[str] = frozenset()
    # True 时（且用户选择"记住"）开放 memory_write；其余场景一律禁用。
    memory_allowed: bool = False
    # 输入框占位提示（前端联动用，不进模型）。
    input_hint: str = ""


DEV = Scenario(
    key="dev",
    label="开发",
    summary="读写代码、跑测试、修到通过为止的完整执行场景",
    input_hint="说出目标：改什么、怎么算完成…",
)

DOCS = Scenario(
    key="docs",
    label="文档",
    summary="写文档、整理说明与发布内容；只能落文档类文件",
    system_prompt=(
        "当前场景：文档。产出以文字为主：结构、措辞与读者视角优先。\n"
        "· 写长文档前先给提纲让用户确认方向（一轮一问，不要连问）。\n"
        "· 代码只作为被引用的对象去读，不做无关重构。\n"
        "· 只能写文档类文件（.md/.txt/.rst/.adoc/.docx）；改源码请让用户切换开发场景。\n"
    ),
    # 文档场景的边界是文件类型，不是"能不能写"。写 README 是本职，改 main.py 不是。
    write_extensions=frozenset({".md", ".markdown", ".txt", ".rst", ".adoc", ".docx", ".doc"}),
    # 跑命令和提交不在"写文档"里，而且它们能绕开扩展名限制。
    tool_denylist=frozenset({"run_shell", "run_tests", "git"}),
    input_hint="要写给谁看？主题和大致篇幅…",
)

DAILY = Scenario(
    key="daily",
    label="日常",
    summary="问答与生活事务；只有这里提供记忆开关",
    system_prompt=(
        "当前场景：日常。只回答与建议，不修改任何文件、不执行命令。\n"
        "· 用户允许记忆时，只记跨会话仍有用的偏好（称呼、格式、常用位置），"
        "电话、住址、证件号这类敏感事实一律不记。\n"
    ),
    allow_write=False,
    memory_allowed=True,
    input_hint="问点什么，或让 AI 帮你想想…",
)

BRAINSTORM = Scenario(
    key="brainstorm",
    label="头脑风暴",
    summary="只读发散：多方案、多角度，不动工作区",
    system_prompt=(
        "当前场景：头脑风暴。给出多个可选项与取舍理由，不做唯一结论。\n"
        "· 每个方向一两句话，先数量后质量；不写文件、不执行命令。\n"
        "· 需要事实依据时先只读看一眼工作区，再回到发散。\n"
    ),
    allow_write=False,
    input_hint="抛一个问题或一个想法，一起把它展开…",
)

REVERSE = Scenario(
    key="reverse",
    label="逆向实验室",
    summary="代码与二进制分析：原始样本只读，结论写进独立分析区",
    system_prompt=(
        "当前场景：逆向实验室。目标是被分析对象的行为与结构，不是修复它。\n"
        "· 结论写进分析文档（.md），注明证据来自哪次读取或命令输出。\n"
        "· 不确定就标「未确认」，不把猜测写成事实。\n"
        "· 原始样本是只读的：所有派生文件写到独立分析目录，绝不改原始工作区。\n"
    ),
    # 分析产物只有笔记这一类；原始样本永远只读（写路径在运行时被改写到分析区）。
    write_extensions=frozenset({".md", ".markdown", ".txt"}),
    # 逆向不提交、不跑项目测试、不跑 shell：这三件事都作用在被分析的那个仓库上。
    # 分析要用命令时应该走"读样本 + 自己算"，而不是在人家目录里执行东西。
    tool_denylist=frozenset({"run_shell", "run_tests", "git"}),
    input_hint="给出要分析的目标文件或目录…",
)

SCENARIOS: dict[str, Scenario] = {
    item.key: item for item in (DEV, DOCS, DAILY, BRAINSTORM, REVERSE)
}

DEFAULT_SCENARIO = DEV.key

# 场景能拿来过滤的全部真实工具名（与 services/tools/ 的注册名一一对应）。
WRITE_TOOLS = frozenset({"apply_patch", "run_shell", "run_tests", "git"})
# 逆向与日常不联网检索：分析以本地工作区为准（web 工具按需可再放开）。
MEMORY_WRITE_TOOL = "memory_write"


def resolve(key: str | None) -> Scenario:
    """未知或空一律回开发场景：场景系统故障不能改变默认行为。"""
    return SCENARIOS.get(str(key or "").strip(), DEV)


def excluded_tools(scenario: Scenario, *, memory_opt_in: bool = False) -> frozenset[str]:
    """这个场景下要彻底禁用的工具名。

    写类工具跟随 ``allow_write``；再加上场景自己的 ``tool_denylist``
    （文档禁 shell、逆向禁 git 这类"不受扩展名约束的写入路径"）；
    memory_write 只在「日常 + 用户选了记住」时开放，其余场景（包括开发）一律禁用
    ——日常习惯不进开发会话的上下文。
    """
    denied: set[str] = set()
    if not scenario.allow_write:
        denied.update(WRITE_TOOLS)
    denied.update(scenario.tool_denylist)
    if not (scenario.memory_allowed and memory_opt_in):
        denied.add(MEMORY_WRITE_TOOL)
    return frozenset(denied)


def write_allowed(scenario: Scenario, relative_path: str) -> bool:
    """这个路径能不能在这个场景里写。

    空 ``write_extensions`` 表示不按后缀限制（开发场景），此时只看 ``allow_write``。
    用后缀判而不是文件内容判：后缀是用户和模型都看得见的那一层约定，
    拿内容嗅探决定权限只会让"为什么这个文件能写那个不能"变得没法解释。
    """
    if not scenario.allow_write:
        return False
    if not scenario.write_extensions:
        return True
    suffix = PurePosixPath(str(relative_path or "").replace("\\", "/")).suffix.lower()
    return suffix in scenario.write_extensions


def system_prompt(scenario: Scenario, *, memory_opt_in: bool = False) -> str:
    """系统提示的场景段。开发场景返回空串（行为与从前完全一致）。"""
    parts = [scenario.system_prompt]
    if scenario.memory_allowed and memory_opt_in:
        parts.append(
            "用户选择了「记住习惯」：遇到明确的偏好（输出长短、常用工具、"
            "固定的文件位置、惯用语言与格式）可以用 memory_write 记一条，"
            "一次只记一句；敏感事实（联系方式、住址、证件号）不记。\n"
        )
    return "".join(part for part in parts if part)


def list_scenarios() -> list[dict]:
    """给前端选择器的清单（保持固定顺序）。"""
    return [
        {
            "key": item.key,
            "label": item.label,
            "summary": item.summary,
            "allow_write": item.allow_write,
            "memory_allowed": item.memory_allowed,
            "input_hint": item.input_hint,
            # 这个场景能落盘的文件类型；空表示"不限"（只有开发场景是这样）。
            # 文档场景按扩展名授权：写 README.md 是本职，改 .py 不是。
            "write_extensions": sorted(item.write_extensions),
        }
        for item in (DEV, DOCS, DAILY, BRAINSTORM, REVERSE)
    ]


# 「要动文件/要跑命令」的信号词。与 intent.py 的动作词分开：那边判断"要不要干活"，
# 这里判断"这事只读能不能做完"。读、看、搜、列都不在其中——那些在只读场景里可以做。
_WRITE_INTENT = re.compile(
    r"(改|修改|修复|重写|重构|实现|新增|添加|加一个|加个|加上|删除|删掉|移除|替换|"
    r"升级|降级|安装|卸载|部署|发布|打包|构建|编译|运行|执行|跑一下|跑一遍|"
    r"清理|整理|迁移|提交|回滚|写入|写一个|写个|创建一个|新建一个|"
    r"refactor|fix|implement|build|deploy|install|migrate|run|create|write)",
    re.IGNORECASE,
)

# 只读场景收到"动手"请求时的固定回答。
#
# 为什么要有这段固定文案：写工具被禁之后，模型手里没有能用的工具，它不会说
# "我没权限"，而是顺着上文把上一轮的结果又讲一遍——用户看到的是答非所问，
# 还以为是模型坏了。与其让它自由发挥，不如在这里直接说清楚限制和出路。
REFUSAL_TEXT = {
    "daily": "日常场景不会直接修改文件，也不会执行命令。\n请切换到开发场景后再执行这个操作。",
    "brainstorm": "头脑风暴场景只做发散，不改动工作区。\n要动手请切换到开发场景。",
}


def needs_write(goal: str) -> bool:
    """这句话是不是非写不可。

    只读场景不能让"帮我看看这个文件"也吃一句拒绝——那是能做的。
    拿写动作词判一次，判不准时按只读放行：真的越界也会被网关拦下来，
    代价是一次明确的拒绝记录，而不是一句答非所问。
    """
    return bool(_WRITE_INTENT.search(goal or ""))
