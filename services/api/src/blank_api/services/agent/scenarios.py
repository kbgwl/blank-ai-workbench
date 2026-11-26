# -*- coding: utf-8 -*-
"""场景系统：产品只开两个场景——编程（dev）与日常（daily）。

``SCENARIOS`` 里另外三个（docs / brainstorm / reverse）是历史遗留，只为让按旧场景
建成的任务按原样跑完；它们不在选择器里，新建与重跑都不接受。

两个场景都是**全能 agent**：dev 按 Claude 的编程纪律跑，daily 按 Codex 的通用
agent 方法论跑。差别在提示词风格与默认定位，不在能力——两者都能读写文件、
跑命令、跑测试。历史遗留的 docs / brainstorm / reverse 仍是只读，按原样跑完。

设计红线（2026-10-21 的教训）：**用户的每一句话必须原样进意图判定与模型**。
场景绝不向用户 prompt 拼任何前缀——它的全部影响都落在三个地方：

1. 系统提示末尾追加一段场景规则（在安全规则之后，声明不得覆盖安全与审批规则）；
2. 工具注册表 ``registry.exclude()``：只读场景（历史三场景）把写类工具彻底禁掉；
3. 记忆权限：只有日常场景且用户选择"记住"时才开放 ``memory_write``。

因此闲聊被判成什么，只取决于用户那句话本身，与所选场景无关。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

PLANNING_STYLES = ("code", "general")
VERIFICATION_STYLES = ("code", "general")


@dataclass(frozen=True)
class ScenarioPolicy:
    """场景的执行策略，不只是提示词。

    ``planning`` / ``verification`` 是给运行时的显式口径；``budget_scale`` 直接
    收缩这类任务的默认步数与时长。两个场景共享同一套安全与审批下限，策略只
    在“怎么做事”这一层不同——不能拿场景当放宽权限的开关。
    """

    planning: str = "code"
    verification: str = "code"
    budget_scale: float = 1.0

    def __post_init__(self) -> None:
        if self.planning not in PLANNING_STYLES:
            raise ValueError(f"未知规划风格：{self.planning}")
        if self.verification not in VERIFICATION_STYLES:
            raise ValueError(f"未知验证风格：{self.verification}")
        if not 0.25 <= float(self.budget_scale) <= 1.0:
            raise ValueError(f"预算比例越界：{self.budget_scale}")


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
    # 执行策略：规划/验证口径与预算比例。默认值 = 代码场景的严格口径。
    policy: ScenarioPolicy = field(default_factory=ScenarioPolicy)


DEV = Scenario(
    key="dev",
    label="编程",
    summary="读写代码、跑测试、修到通过；改动前先读，交付前必须真验",
    system_prompt=(
        "当前场景：编程（主场景）。按 Claude Code 级的编程纪律执行：\n"
        "· 改动前先读相关代码与现有测试，只做最小必要 diff，不顺手重构无关文件。\n"
        "· 改完必须真跑受影响的测试；没跑、没跑通、或环境不允许，都要如实写出来。\n"
        "· 不把未完成说成完成，不用注释、占位或“下一步”掩盖缺口。\n"
        "· 交付文档：代码仓库里的说明/README 用 Markdown；面向同事的计划书、报告、"
        "方案默认 .docx（WPS/Office 可直接打开），用户明确要 PDF/PPT 时再换格式。\n"
        "· 不要为生成文档反复安装第三方包：能用标准库或无依赖方案就用；"
        "装依赖失败两次就停，改用无依赖方案或把缺什么写进交付。\n"
        "· 长文件分段落盘：单次 write_file 的内容超过约 4000 字时，先写第一节，"
        "再用 write_file(append=true) 追加后续；不要把整份长文塞进一次调用。\n"
    ),
    input_hint="说出目标：改什么、怎么算完成…",
    policy=ScenarioPolicy(planning="code", verification="code", budget_scale=1.0),
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
    # monitor 也执行命令，只是把输出逐行投递——不跟着禁就是换条通道跑 shell。
    tool_denylist=frozenset({"run_shell", "run_tests", "git", "monitor"}),
    input_hint="要写给谁看？主题和大致篇幅…",
)

DAILY = Scenario(
    key="daily",
    label="日常",
    summary="通用任务：读写文件、跑命令、把事情做到完；可记住你的习惯",
    # 日常场景按 Codex 通用 agent 的口径跑：能读写、能跑命令、做事做到完，
    # 与编程场景（Claude 编程纪律）的差别在方法论与措辞，不在能力。
    # "先问再干"的授权边界仍由权限层管，场景段不碰它。
    #
    # 2026-11-23 对标 Codex 收紧四条：直接动手少问、先最简单做法、做完核对结果、
    # 交付先给结论。这四条是 Codex 日常体感与"会聊天但不动手"之间最明显的分界。
    system_prompt=(
        "当前场景：日常（主场景）。你是对照 Codex 的通用 agent：先判断用户意图，"
        "能自己查到的自己查、能自己定的自己定，把事情做到真正完成再收尾。\n"
        "· 直接动手，少问：缺关键信息（对象、目录、权限）或要做不可逆操作时才问，"
        "一次只问一个；其余情况按最合理的默认值先做，并在结果里说明假设。\n"
        "· 先用最简单可靠的做法：能直接读写文件就不写脚本；需要脚本时优先标准库；"
        "不要为一次性任务安装依赖，装包失败两次就改用无依赖方案。\n"
        "· 做完必须核对结果：文件写没写进去、命令退出码是不是 0、产物能不能打开；"
        "没验证的部分在交付里明说，不把未完成写成完成。\n"
        "· 交付先给结论，再给必要的改动清单与验证结果；不复述思考过程，不写客套话。"
        "涉及代码或页面时仍要真跑验证，不因为「日常」降低验证下限。\n"
        "· 默认用简体中文交付，用户明确要其他语言时除外。\n"
        "· 用户允许记忆时，只记跨会话仍有用的偏好（称呼、格式、常用位置），"
        "电话、住址、证件号这类敏感事实一律不记。\n"
    ),
    allow_write=True,
    memory_allowed=True,
    input_hint="说点什么，或让 AI 帮你把一件事做完…",
    # 日常场景不做“为了凑步骤而拆细”的工程表演；预算也略收紧，
    # 但代码/页面的验证下限不降（见 system_prompt 的验证段）。
    policy=ScenarioPolicy(planning="general", verification="general", budget_scale=0.8),
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
    # monitor 同样是"在人家目录里执行东西"，只是换了投递方式。
    tool_denylist=frozenset({"run_shell", "run_tests", "git", "monitor"}),
    input_hint="给出要分析的目标文件或目录…",
)

SCENARIOS: dict[str, Scenario] = {
    item.key: item for item in (DEV, DOCS, DAILY, BRAINSTORM, REVERSE)
}

DEFAULT_SCENARIO = DEV.key

# 场景能拿来过滤的全部真实工具名（与 services/tools/ 的注册名一一对应）。
# monitor 也在里面：它执行命令，只是把输出逐行投递；不跟着 run_shell 一起禁，
# 「文档/逆向场景不跑命令」就只剩一句提示词。
# repl 同理：它本身不写文件，但内层可以调写工具与命令；只读场景里连入口都不该有。
# 文档/逆向没有把它加进 tool_denylist：内层 run_shell 已被场景排除，
# 留下的读/算能力正是这两个场景需要的。
WRITE_TOOLS = frozenset(
    {
        "apply_patch",
        "write_file",
        "edit_file",
        "notebook_edit",
        "run_shell",
        "run_tests",
        "git",
        "monitor",
        "repl",
    }
)
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
    if scenario.policy.planning == "general":
        parts.append(
            "执行方式：简单任务直接做，不必为了凑步骤而拆细；"
            "只有多步骤、需要跨文件或跨命令的任务才先列计划。\n"
        )
    if scenario.policy.verification == "code":
        parts.append(
            "验证口径：涉及代码必须真跑受影响的测试；涉及页面必须在最新代码上"
            "用 browser_check 验过，没跑通就如实说，不把未验证写成完成。\n"
        )
    else:
        parts.append(
            "验证口径：如实说明做了什么、结果如何；涉及代码或页面时仍要真跑验证，"
            "不把未验证写成完成。\n"
        )
    if scenario.memory_allowed and memory_opt_in:
        parts.append(
            "用户选择了「记住习惯」：遇到明确的偏好（输出长短、常用工具、"
            "固定的文件位置、惯用语言与格式）可以用 memory_write 记一条，"
            "一次只记一句；敏感事实（联系方式、住址、证件号）不记。\n"
        )
    return "".join(part for part in parts if part)


def list_scenarios() -> list[dict]:
    """给前端选择器的清单（保持固定顺序）。

    只列编程（主）与日常（辅）两个。``SCENARIOS`` 里另外三个（文档 / 头脑风暴 /
    逆向）留给已有任务按原场景跑完——它们不出现在选择器里，也就不会再被新建出来。
    """
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
            # 执行策略也带出去：界面/证据页不用再自己猜“两个场景到底差在哪”。
            "planning": item.policy.planning,
            "verification": item.policy.verification,
            "budget_scale": item.policy.budget_scale,
        }
        for item in (DEV, DAILY)
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
    # daily 放开为 Codex 全能 agent 后不再拒任何动作：读写、命令、测试都可做。
    "brainstorm": "头脑风暴场景只做发散，不改动工作区。\n要动手请切换到开发场景。",
}


def needs_write(goal: str) -> bool:
    """这句话是不是非写不可。

    只读场景不能让"帮我看看这个文件"也吃一句拒绝——那是能做的。
    拿写动作词判一次，判不准时按只读放行：真的越界也会被网关拦下来，
    代价是一次明确的拒绝记录，而不是一句答非所问。
    """
    return bool(_WRITE_INTENT.search(goal or ""))
