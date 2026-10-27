"""审批与权限：四档权限模式、命令前缀规则、会话级批准记忆。

对标来源见 ``docs/REA-CODEX-CLAUDE.md``：

- Codex 的 ``approved_command_prefixes`` 与工具描述里的 ``prefix_rule``
  —— 用户批准一次 ``git status`` 之后，同类命令不再逐条打断。
- Claude Code 的 ``defaultMode``（default / acceptEdits / plan / bypassPermissions）
  与 ``user_approved_for_session`` —— 批准粒度分四档，其中"本会话不再询问"单独一档。

设计原则：默认收紧。放行顺序里任何一环都不能越过 ``IRREVERSIBLE``，
因为它是唯一无法靠工作区快照回滚的一类操作。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import posixpath
import re
from typing import TYPE_CHECKING, Any

from blank_api.services.approvals import approval_key
from blank_api.services.log_safety import contains_secret
from blank_api.services.permission_rules import PermissionRuleset, empty_ruleset
from blank_api.services.sandbox.tiers import DEFAULT_TIER, host_bypass_requested, normalize_tier

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查，运行时不会导入工具层（避免导入环）
    from blank_api.services.tools.base import ToolDefinition

MODE_DEFAULT = "default"
MODE_ACCEPT_EDITS = "accept_edits"
MODE_PLAN = "plan"
MODE_BYPASS = "bypass"
MODES = (MODE_DEFAULT, MODE_ACCEPT_EDITS, MODE_PLAN, MODE_BYPASS)

MODE_LABELS = {
    MODE_DEFAULT: "默认：高风险操作逐个确认",
    MODE_ACCEPT_EDITS: "接受编辑：文件写入自动放行，命令与联网仍需确认",
    MODE_PLAN: "计划模式：只读取证，不写文件、不执行命令",
    MODE_BYPASS: "全放行：所有操作不再询问（不可逆命令除外）",
}

ALLOW = "allow"
ASK = "ask"
DENY = "deny"

SCOPE_ONCE = "once"
SCOPE_PREFIX = "prefix"
SCOPE_TOOL = "tool"
SCOPES = (SCOPE_ONCE, SCOPE_PREFIX, SCOPE_TOOL)

# 「本会话总是允许这个工具」把免问范围定在工具本身。这一档只有当工具的风险
# 不由参数决定、或参数已经被路径前缀框住时才算说得出口：run_shell 的风险是
# `assess_risk(命令正文)` 算出来的，卡片上看到的是**这一条命令**的风险，
# 它替不了解释器收到的下一条（`-c` 换正文、`-m` 换模块、联网/破坏性词换掉）。
# 实物对照（#54 量的 Codex `~/.codex/rules/default.rules` 与 Claude Code 的
# `Bash(git push:*)`）：两边的免问粒度都落在「这一条命令」，没有「总是允许整个 shell」这种卡。
# 登记制：带 `risk_for` 的工具必须显式进这张表，否则 `test_tool_grant_scope_v52.py`
# 拿默认工具注册表逐条对账会红——漏登记在这条边界上是看得见的缺陷，不是静默放宽。
ARGUMENT_SCOPED_TOOLS = frozenset({"run_shell", "git", "apply_patch", "spawn_agents"})

# 计划模式唯一允许的一类工具：只看不写。
READ_ONLY_TOOLS = frozenset(
    {
        "list_files",
        "read_file",
        "search_code",
        "update_plan",
        "git",
        "request_user_input",
        "web_search",
        "web_fetch",
        "read_notes",
        "memory_search",
        # 团队状态只读，计划模式可以查；其余 team_* 写操作仍按普通规则拒绝
        "team_status",
        # 检索本身只读：计划模式要靠它把 git / 联网这些能力加载进来做只读取证
        "tool_search",
        # 模式开关本身不是写操作：计划模式下必须能"退出"，否则模型会卡死在里面。
        # 退出仍需人工批准，由 decide() 里的 mode:switch 特例负责。
        "enter_plan_mode",
        "exit_plan_mode",
    }
)
# "接受编辑"模式自动放行的工具：只含工作区内的文件写入（可回滚）。
EDIT_TOOLS = frozenset({"apply_patch"})

# 不可逆操作：不能靠工作区快照回滚，任何模式与任何前缀规则都不放行。
IRREVERSIBLE = re.compile(
    r"(?i)("
    r"\bmkfs(\.\w+)?\b"
    r"|\bdiskpart\b"
    r"|\bformat\s+[a-z]:"
    r"|\bshutdown\b|\breboot\b"
    r"|\bdrop\s+(database|schema)\b"
    r"|\btruncate\s+table\b"
    r"|\brm\s+-[a-z]*[rf][a-z]*\s+(/|~|\$HOME|\*|[a-z]:\\)"
    r"|\bdel\s+/[sfq]\s+[a-z]:\\"
    # Windows 原生删除形状。本机默认壳就是 cmd / PowerShell，缺这一组时
    # `powershell -Command "Remove-Item -Recurse C:\target"` 会被判成「可逆」——
    # 于是它既写进免问规则，又对用户承诺「可靠快照回滚」。两者都是谎。
    # 只认「动词 + 盘符路径」或「动词 + -Recurse/-Force」，不认裸 `*`：
    # 后者会撞上 `grep foo *` 这类无害命令，把逐条确认变成常态噪音。
    r"|\b(del|erase)\b\s+(?:/[a-z]+\s+)*[\"']?[a-z]:\\"
    r"|\b(remove-item|ri|rd|rmdir)\b[^\n]*(?:-recurse|-force|[a-z]:\\)"
    # 间接载荷：目标由环境变量/主目录展开，静态看不见具体路径。这类删除落在工作区
    # 之外，快照赎不回来；判成"可逆"等于允许 guardian 代批并发放"总是允许"。
    # 只挂在删除类动词上——`echo %USERPROFILE%` 这种读操作不许被连坐。
    r"|\b(del|erase|rd|rmdir|remove-item|ri)\b[^\n]*(?:%[a-z_][^%\n]*%|\$env:|\$home\b)"
    r"|\breg\s+delete\b"
    r"|\bgit\s+push\b[^\n]*--force"
    r"|\bdd\s+if="
    r"|\bchmod\s+-R\s+777\s+/"
    r"|>\s*/dev/sd[a-z]"
    r")"
)

# 命令分段符：只有整条命令的每一段都被前缀规则覆盖，才算命中。
_SEGMENT_BREAKS = ("&&", "||", ";", "|", "\n", "&")
# 规则串里必须带引号的字符：与分段符同源，另补重定向。
# 真宿主壳实测（本机 run_shell 就是 cmd.exe，见 E:/tmp/m87/separator_probe.py）：
# `|` 裸写 ⇒ 后一半被当成另一条命令，退出码 255；`&` 裸写 ⇒ 真的多跑了一条命令
# （`python -c print(1)&echo x` 退出码 0 并把 x 打印出来）；`;` 在 cmd 这一层实测不分段
# （PowerShell 的裸参数会被重新拼回一行，采到的形状里效果相同）。
# 三条里两条会变、剩下的那条只是"这台机器的壳恰好不吃"——所以一律包裹，不赌。
_RULE_METACHARS = frozenset(";&|<>")


def is_irreversible(command: str) -> bool:
    return bool(IRREVERSIBLE.search(command))


# --------------------------------------------------------------------------- #
# 命令前缀
# --------------------------------------------------------------------------- #
def split_segments(command: str) -> list[str]:
    """按 shell 分段符切开，跳过引号内的分隔符。"""
    segments: list[str] = []
    buffer: list[str] = []
    quote = ""
    index = 0
    length = len(command)
    while index < length:
        char = command[index]
        if quote:
            buffer.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "\"'":
            quote = char
            buffer.append(char)
            index += 1
            continue
        matched = next((brk for brk in _SEGMENT_BREAKS if command.startswith(brk, index)), None)
        if matched:
            segments.append("".join(buffer))
            buffer = []
            index += len(matched)
            continue
        buffer.append(char)
        index += 1
    segments.append("".join(buffer))
    return [item.strip() for item in segments if item.strip()]


def tokenize(segment: str) -> list[str]:
    """把一段命令切成 token。

    posix=False 是有意的：``C:\\Python312\\python.exe`` 这类 Windows 路径在 posix 模式下
    反斜杠会被当成转义符吃掉，结果变成 ``C:Python312python``。代价是引号会被保留，
    所以下面手动剥一层成对引号。
    """
    import shlex

    try:
        raw = shlex.split(segment, posix=False)
    except ValueError:
        raw = segment.split()
    tokens: list[str] = []
    for token in raw:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1]
        if token:
            tokens.append(token)
    return tokens


# 能当"子命令"看的第二段 token：git status / npm run / docker compose。
# 带斜杠、点、冒号或是绝对路径的一律不算——否则 curl https://x 会变成 "curl https://x"。
_SUBCOMMAND = re.compile(r"^[a-z][a-z0-9_-]{0,24}$")


def _basename(token: str) -> str:
    name = token.replace("\\", "/").rsplit("/", 1)[-1]
    lowered = name.lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return name


def command_prefix(command: str) -> str:
    """命令的前缀签名：可执行名 + 首个非选项参数（最多两段）。

    ``git status -s``             -> ``git status``
    ``npm run build``             -> ``npm run``
    ``C:\\Python\\python.exe -V`` -> ``python``
    ``curl https://x``            -> ``curl``
    """
    segments = split_segments(command)
    if not segments:
        return ""
    tokens = tokenize(segments[0])
    if not tokens:
        return ""
    exe = _basename(tokens[0])
    if len(tokens) > 1 and _SUBCOMMAND.match(tokens[1]):
        return f"{exe} {tokens[1]}"
    return exe


def prefix_rule(text: str) -> tuple[str, ...]:
    """把 ``"git status"`` 这类规则串解析成 token 元组；``*`` 表示占位通配。"""
    return tuple(token for token in tokenize(str(text)) if token)


def render_rule(rule: tuple[str, ...]) -> str:
    """规则串：带空白或 shell 控制符的 token 必须带引号。

    只按空白判断不够。真宿主壳实测（run_shell 在本机就是 cmd.exe）：
    内联正文里带 ``|`` 时裸写会得到退出码 255（后一半被当成另一条命令），
    带 ``&`` 时更糟——``python -c print(1)&echo x`` 退出码 0 且真的多跑了一条命令。
    卡片上"总是允许 X"的 X、事件里留痕的那一串，都必须是能一字不差跑回来的东西。

    用双引号而不是单引号：载荷里 `print('x')` 这种单引号很常见，
    用单引号包裹会写出 `'print('x')'` 这种语法上不存在、也读不回来的串；
    反过来 token 里已经带双引号时这里不再包裹（`"..."` 套 `"..."` 同样不存在），
    这种规则靠 :func:`_round_trips` 在入账前拒掉——实测样本里就是
    ``python -c 'print("a b")'`` 那一条，它记不下来，下次照样挂卡。
    """
    parts: list[str] = []
    for token in rule:
        if any(char.isspace() or char in _RULE_METACHARS for char in token) and '"' not in token:
            parts.append(f'"{token}"')
        else:
            parts.append(token)
    return " ".join(parts)


def _round_trips(rule: tuple[str, ...]) -> bool:
    """写出去还能一字不差读回来，才允许入账；读回来会变形的规则一律不记。

    落盘用的是 token 数组（`approvals.json`），本来不经过这个串；但事件留痕与卡片
    文案用的是它，任务级回放也按串解析。串一旦变形，「免问范围」就和用户批准的那条
    不是同一件事了——宁可下次照样挂卡。
    """
    return prefix_rule(render_rule(rule)) == rule


def _starts_with(argv: list[str], rule: tuple[str, ...]) -> bool:
    if len(argv) < len(rule):
        return False
    if _payload_terminated(rule) and len(argv) != len(rule):
        # 规则已经带到载荷（正文 / 模块 / 脚本）的解释器命令，免问范围只能等于那条命令，
        # 不能是「以它开头」：`python -c "…sys.argv[1]…"` 批准过一次，不该免问换参数的下一条。
        return False
    for index, token in enumerate(rule):
        if token == "*":
            continue
        actual = _basename(argv[index]) if index == 0 else argv[index]
        if actual.lower() != token.lower():
            return False
    return True


def _payload_terminated(rule: tuple[str, ...]) -> bool:
    """这条规则是否已经覆盖到「真正执行的载荷」——是则要求整段 argv 等长。"""
    return bool(rule) and len(rule) >= 2 and _basename(rule[0]).lower() in _CODE_EXECUTORS


def _rule_admissible(rule: tuple[str, ...]) -> bool:
    """这条规则配不配免问——写入侧拒收的形状，匹配侧同样不许放行。

    闸门必须排在匹配之前：`approvals.json` 是给人手工编辑的文件，
    :func:`memo_rule` 管不到它。没有这道闸，文件里一行 ``["powershell"]`` 就静默免问了
    任意 `-Command` 正文，而 :data:`_CODE_EXECUTORS` 的契约写的恰恰是"裸解释器名
    等于把整个会话的任意代码执行交给模型自选载荷"（见上面的注释块）。
    """
    if not rule:
        return False
    if _basename(rule[0]).lower() not in _CODE_EXECUTORS:
        return True
    if len(rule) < 2:
        return False  # 裸解释器名：载荷完全交给模型
    # 载荷是不可读文本（base64）的规则不认：批准它的人看不见自己签的是什么
    return not any(token.lower() in _OPAQUE_CODE_FLAGS for token in rule[1:])


def covered_by_rules(command: str, rules, *, refuse_inadmissible: bool = True) -> str | None:
    """整条命令的每个子命令都被规则覆盖时返回命中的规则串，否则返回 None。

    只覆盖第一段是不够的：``npm test && rm -rf src`` 若只对第一段放行，
    第二段就搭了顺风车。所以这里要求逐段命中。

    `refuse_inadmissible` 只对**免问**（批准）那一路为真；驳回那一路传 False——
    用户在本会话里明说"powershell 别问了"，一条不配免问的规则也得保住它的拒绝力。
    """
    segments = [tokenize(item) for item in split_segments(command)]
    if not segments or any(not segment for segment in segments):
        return None
    usable = [rule for rule in rules if rule and (not refuse_inadmissible or _rule_admissible(rule))]
    if not usable:
        return None
    hits: list[str] = []
    for segment in segments:
        matched = next((rule for rule in usable if _starts_with(segment, rule)), None)
        if matched is None:
            return None
        hits.append(render_rule(matched))
    return ", ".join(dict.fromkeys(hits))


# --------------------------------------------------------------------------- #
# 判定结果
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Decision:
    outcome: str
    risk: str = ""
    reason: str = ""
    rule: str = ""
    prefix: str = ""
    memo: str = ""

    @property
    def allowed(self) -> bool:
        return self.outcome == ALLOW

    @property
    def asks(self) -> bool:
        return self.outcome == ASK

    @property
    def denied(self) -> bool:
        return self.outcome == DENY

    def as_dict(self) -> dict:
        body = {"outcome": self.outcome, "risk": self.risk, "rule": self.rule}
        if self.prefix:
            body["prefix"] = self.prefix
        if self.memo:
            body["memo"] = self.memo
        if self.reason:
            body["reason"] = self.reason
        return body


def approve(risk: str, rule: str, prefix: str = "", memo: str = "") -> Decision:
    return Decision(ALLOW, risk=risk, rule=rule, prefix=prefix, memo=memo)


def ask(risk: str, rule: str, prefix: str = "", memo: str = "") -> Decision:
    return Decision(ASK, risk=risk, rule=rule, prefix=prefix, memo=memo)


def deny(risk: str, rule: str, reason: str, prefix: str = "", memo: str = "") -> Decision:
    return Decision(DENY, risk=risk, rule=rule, reason=reason, prefix=prefix, memo=memo)


def approval_options(tool_name: str, memo: str = "", *, tool_scope: bool) -> list[dict]:
    """审批卡上的按钮数据：本次 / 这条命令（仅 run_shell 且粒度够细时）/ 该工具本会话。

    ``memo`` 传的是 :func:`memo_rule` 算出来的那一串，不是命令本身的首段——
    卡片上写的"总是允许 X"必须与真正写进会话账本的那条规则逐字一致，
    否则用户批的是 ``python -c print(1)``、账本上记的却是 ``python``。

    ``tool_scope`` 取 :func:`tool_grant_too_wide` 的反面：**卡上只出现按得下去的钮**。
    前端把这份列表当成唯一的档位来源，所以这里少给一档界面上就少一档，
    不会留下"点了允许、账上却没记"的空按钮。

    这一档是**必填**的（#88）：默认值一旦是"开"，任何一个新发卡点忘记传它就自动长出
    工具档，而写入侧 :meth:`PermissionPolicy.remember` 照样拒记——
    失败方向是"界面给了个按了不留账的钮"，不是"当场报错"。
    现在漏传就是 ``TypeError``：把 fail-open 换成 fail-fast。
    """
    options = [{"scope": SCOPE_ONCE, "label": "仅本次允许"}]
    if memo:
        options.append(
            {
                "scope": SCOPE_PREFIX,
                "label": f"本会话总是允许 {memo}",
                "warning": "只免问这一条命令：换正文、换参数、换模块或换脚本路径都会重新挂卡",
            }
        )
    if tool_scope:
        options.append(
            {
                "scope": SCOPE_TOOL,
                "label": f"本会话总是允许 {tool_name}",
                # 工具档仍在不可逆判定之后生效：`git push --force` / `drop table` 照样逐条问。
                # 免问还按档位与路径分桶：换档位（尤其换到 off）或换路径都要重新问。
                "warning": "该工具在本档位、这片路径内不再询问；换档位、换路径或不可逆命令仍逐条确认",
            }
        )
    return options


# 允许"记住这个前缀"的可执行名（仅当命令没有子命令时才有意义）。
# git / rm / curl / bash 这类不在列：单独放行一个裸可执行名等于全放行。
MEMOIZABLE_EXES = frozenset(
    {
        "pytest",
        "python",
        "python3",
        "node",
        "npm",
        "pnpm",
        "yarn",
        "npx",
        "tsc",
        "ruff",
        "black",
        "mypy",
        "eslint",
        "prettier",
        "cargo",
        "go",
        "dotnet",
        "mvn",
        "gradle",
        "make",
        "cmake",
    }
)

# 名单里这几个是"能给什么代码就跑什么代码"的入口：它们和 pytest / tsc 不是一类。
# 记住一个裸名等于把整个会话的任意代码执行交给模型自选载荷，所以这类命令必须
# 带到真正被执行的那一段（`-c` 的正文、`-m` 的模块名、脚本路径），只准收紧到
# "这一条命令"，不准放宽到"这个解释器"。对照实物：Codex 落在
# ~/.codex/rules/default.rules 的那条规则，正文 `-Command` 整段都在 pattern 里。
#
# 会把「一段文本当代码执行」的解释器。本机默认壳是 cmd / PowerShell，所以
# powershell / pwsh / cmd 必须在表里：否则它们的 `-Command` 正文压根进不了免问判定，
# 「这一条命令免问」在 Windows 形状的命令上永远发不出档位。
_CODE_EXECUTORS = frozenset(
    {"python", "python3", "py", "node", "nodejs", "powershell", "pwsh", "cmd"}
)
_INLINE_CODE_FLAGS = frozenset(
    {"-c", "-e", "--command", "-command", "-commands", "/c", "/e", "--eval", "--print"}
)
_MODULE_FLAGS = frozenset({"-m", "--module"})
# 载荷是不可读文本（base64）的旗标：卡片上用户看到的是一串乱码，
# 记进免问规则等于让他盲签——和含密钥的规则一样，宁可不记。
_OPAQUE_CODE_FLAGS = frozenset({"-encodedcommand", "-enc", "/enc"})
# 一层之外的引号形状：PowerShell 用 `""` 表示引号本身，posix / cmd 转义用 `\"`。
_LOSSY_QUOTES = re.compile(r'""|\'\'|\\"')


def memo_rule(command: str) -> tuple[str, ...]:
    """这次批准该记成什么规则；空元组＝不值得记（下次照样挂卡）。

    规则必须覆盖到"真正被执行的那一段"：解释器命令要认得出载荷（``-c`` 的正文、
    ``-m`` 的模块名、脚本路径），并记下整段 argv；普通命令取
    :func:`command_prefix`。含密钥形态的规则不记——卡片上它是打码的，
    让用户批准一条自己看不见全文的规则，等于把打码变成盲签。
    """
    segments = split_segments(command)
    if not segments:
        return ()
    if is_irreversible(command):
        # 不可逆的命令压根不配有一条"总是允许"：`git push --force` 的前缀是 `git push`，
        # 记下来连正常推送也一起放行；`Remove-Item -Recurse` 更不可能靠快照回滚。
        # 闸门锁在这一层（而不是只排在 decide 的匹配之前）：任何一个调用点直接问
        # :func:`memoizable_prefix` 都不会给不可逆命令发出前缀档位。
        return ()
    tokens = tokenize(segments[0])
    if not tokens:
        return ()
    if _LOSSY_QUOTES.search(segments[0]):
        # tokenize() 只剥一层成对引号。PowerShell 的 `""` 与 posix 的 `\"` 这类转义它认不出来，
        # 载荷会被切残：`powershell -Command "Write-Host ""x"""` 读回来只剩 `Write-Host `。
        # 切残的规则比原命令更宽（不同的转义载荷会塌成同一条），所以宁可这条记不下来。
        return ()
    exe = _basename(tokens[0])
    rule: tuple[str, ...] = ()
    if exe.lower() in _CODE_EXECUTORS:
        # 跳过解释器自己的旗标（`-u` / `-I` / `--inspect`），先确认这一段里真有一个
        # 会被执行的载荷（`-c` 的正文、`-m` 的模块名、脚本路径）：裸解释器、只有旗标的
        # 调用（`python -V`）都不记——那等于把任意载荷交给模型自选。
        # 载荷一旦确认，规则取**整段 argv**：载荷后面的尾巴（`sys.argv`、脚本参数）
        # 会改变同一段代码的实际行为，让它搭顺风车就是扩权。实测形状：
        #   python -c "print(1)"                 ← 批准这条
        #   python -c "print(1)" https://a.example  旧实现把它一起免问了（截断成 3 段）
        # 配套：`_starts_with` 对载荷型规则要求整段等长，两者必须同时成立——
        # 只收紧匹配会得到「记下来的规则连批准它的那条命令都不覆盖」，下次照样挂卡。
        # 首段写 basename（与 command_prefix 一致），换一种路径写法也能命中。
        for index in range(1, len(tokens)):
            token = tokens[index]
            lowered = token.lower()
            if lowered in _OPAQUE_CODE_FLAGS:
                # `-EncodedCommand <base64>`：卡片上是串乱码，用户批的并不是他看见的东西。
                return ()
            if lowered in _INLINE_CODE_FLAGS or lowered in _MODULE_FLAGS:
                if index + 1 < len(tokens):
                    rule = (exe,) + tuple(tokens[1:])
                break
            if not token.startswith("-"):
                rule = (exe,) + tuple(tokens[1:])
                break
    else:
        rule = prefix_rule(command_prefix(command))
        if rule and len(rule) < 2 and rule[0].lower() not in MEMOIZABLE_EXES:
            rule = ()
    if rule and contains_secret(render_rule(rule)):
        return ()
    if rule and not _round_trips(rule):
        return ()
    return rule


def memoizable_prefix(tool_name: str, command: str) -> str:
    """审批卡上"本会话总是允许 X"里的 X：只有值得记的命令才给这一档。"""
    if tool_name != "run_shell":
        return ""
    return render_rule(memo_rule(command))


def parse_decision(raw: object) -> tuple[bool, str]:
    """兼容三种恢复入参：``True/False``、``{"decision": "approve"}``、``{"approve": true, "scope": ...}``。"""
    if isinstance(raw, bool):
        return raw, SCOPE_ONCE
    if isinstance(raw, dict):
        scope = str(raw.get("scope") or SCOPE_ONCE).strip().lower()
        if scope not in SCOPES:
            scope = SCOPE_ONCE
        if "approve" in raw:
            return bool(raw["approve"]), scope
        verdict = str(raw.get("decision") or "").strip().lower()
        # 空值或无法识别的措辞一律视为未批准：静默批准比多问一次危险得多。
        return verdict in ("approve", "allow", "yes", "true", "1", "ok"), scope
    if isinstance(raw, str):
        return raw.strip().lower() not in ("", "deny", "reject", "no", "false", "0"), SCOPE_ONCE
    if raw is None:
        return False, SCOPE_ONCE
    return bool(raw), SCOPE_ONCE


# --------------------------------------------------------------------------- #
# 策略
# --------------------------------------------------------------------------- #
_TOOL_PATH_KEYS = ("path", "file_path", "target")


def path_scope(arguments: dict | None) -> str:
    """这次调用落在哪个目标路径前缀上；没有路径参数的工具返回空串（整个工具）。

    用参数本身而不是父目录：`apply_patch path=src/a.py` 批的是那个文件，
    不是"整个 src 目录"。宁可多问一次，也不要把一次文件级批准扩成一次目录级批准。
    绝对路径原样保留——文件工具本来就会拒掉它，这里不为它额外开一条路。
    """
    data = arguments if isinstance(arguments, dict) else {}
    for key in _TOOL_PATH_KEYS:
        raw = data.get(key)
        if isinstance(raw, str) and raw.strip():
            text = raw.strip().replace("\\", "/")
            normalized = posixpath.normpath(text)
            return "" if normalized in ("", ".") else normalized
    return ""


def _covers_path(granted: str, target: str) -> bool:
    if not granted:
        return True
    if not target:
        return False
    return target == granted or target.startswith(granted.rstrip("/") + "/")


def render_tool_grant(grant: tuple[str, str, str]) -> str:
    tier, tool, prefix = grant
    return f"{tier}:{tool}:{prefix or '.'}"


def tool_grant_too_wide(tool_name: str, arguments: dict | None) -> bool:
    """这次批准能不能记成「这个工具以后都免问」。

    三道门槛：工具在 :data:`ARGUMENT_SCOPED_TOOLS` 里（风险由参数算出来），
    这次带的不是命令（命令类工具的免问粒度只能是那条命令，见 :func:`memo_rule`），
    且这一次的参数被路径前缀框住（`path_scope` 为空＝免问范围是整个工具）。
    ``apply_patch`` 是活例子：带 `path` 的批准只免问那片路径；
    而 `path` 省略、从补丁头推断的那一次，账本上会落成"整个工具都免问"——
    同一张卡、同一个按钮，免问范围却从「这个文件」悄悄变成「以后所有文件」。

    命令这一条挡的是模型多发的参数：``run_shell`` 的 schema 里没有 ``path``，
    ``validate_arguments`` 又不拒未知键，所以「带 command 也带 path」的调用是可达的。
    让这样一个对命令正文毫无影响的参数决定分桶，等于用户批一次之后，
    模型只要在后续调用里重复那个 ``path``，任意命令正文都免问。
    """
    if tool_name not in ARGUMENT_SCOPED_TOOLS:
        return False
    data = arguments if isinstance(arguments, dict) else {}
    if str(data.get("command") or "").strip():
        return True
    return not path_scope(data)


@dataclass
class PermissionPolicy:
    """一次任务运行期间的权限状态。可变：批准与拒绝会就地写入，供后续步骤复用。"""

    mode: str = MODE_DEFAULT
    # 工具档授权按 (档位, 工具, 目标路径前缀) 分桶。旧形状是一个裸工具名，
    # 等于"这个会话里随便哪个档位都不再问"——切到 off（宿主完整权限）仍然免问。
    approved_tools: set[tuple[str, str, str]] = field(default_factory=set)
    # 审批的免问范围是任务级事实，必须跟着 turn.context 一起被快照。
    sandbox_tier: str = DEFAULT_TIER
    approved_actions: set[str] = field(default_factory=set)
    approved_prefixes: set[tuple[str, ...]] = field(default_factory=set)
    # 落盘的前缀规则（跨任务、跨重启）。与会话内那一档分开存：追踪里要说得清
    # 这次放行是"这次会话刚批的"还是"以前记在 approvals.json 里的"。
    persistent_prefixes: set[tuple[str, ...]] = field(default_factory=set)
    denied_prefixes: set[tuple[str, ...]] = field(default_factory=set)
    denied_actions: set[str] = field(default_factory=set)
    approve_high_risk: bool = False
    # 人手写的 allow/deny 规则（permissions.rules）。与 approvals.json 分开：
    # 前者只读、可写注释；后者由程序原子替换。文件写坏时整份停用。
    ruleset: "PermissionRuleset" = field(default_factory=empty_ruleset)

    @classmethod
    def build(
        cls,
        mode: str = MODE_DEFAULT,
        *,
        approve_high_risk: bool = False,
        approved_tools=(),
        approved_actions=(),
        approved_prefixes=(),
        sandbox_tier: str = DEFAULT_TIER,
    ) -> "PermissionPolicy":
        tier = normalize_tier(sandbox_tier)
        grants: set[tuple[str, str, str]] = set()
        for item in approved_tools or ():
            if isinstance(item, (tuple, list)) and len(item) == 3:
                grants.add((str(item[0]), str(item[1]), str(item[2])))
            else:
                # 裸工具名是调用方在说"这个工具，本档位、整个工作区"。
                grants.add((tier, str(item), ""))
        return cls(
            mode=mode if mode in MODES else MODE_DEFAULT,
            sandbox_tier=tier,
            approve_high_risk=bool(approve_high_risk),
            approved_tools=grants,
            approved_actions=set(approved_actions or ()),
            approved_prefixes={prefix_rule(item) for item in (approved_prefixes or ()) if prefix_rule(item)},
        )

    # -- 状态变更 ----------------------------------------------------------
    def remember(self, tool: str, arguments: dict, scope: str = SCOPE_ONCE) -> None:
        """把一次批准写进会话记忆。

        前缀档记的是 :func:`memo_rule` 算出来的那一串（解释器类必须带到正文），
        不是命令首段——前端即使误传 scope=prefix，也记不出"整个 python"这种宽账。
        工具档同理：给不起的工具（:func:`tool_grant_too_wide`）即使硬传
        scope=tool 也不入账，免问范围不能由一个客户端参数放大。
        """
        self.approved_actions.add(approval_key(tool, arguments))
        # 单次越界只认"这一次"：就算客户端硬传 scope=prefix/tool，也不许把
        # 宿主执行记成会话级或工具级的免问范围。前缀档对它毫无意义。
        if host_bypass_requested(tool, arguments):
            return
        if scope == SCOPE_TOOL:
            if tool_grant_too_wide(tool, arguments):
                return
            # 记下"哪个档位、哪个工具、哪片路径"：档位换了就重新问，
            # 目标路径出了这片前缀也重新问。
            self.approved_tools.add((self.sandbox_tier, tool, path_scope(arguments)))
        elif scope == SCOPE_PREFIX:
            command = str(arguments.get("command") or "")
            # 不可逆命令不进前缀账：`git push --force` 的前缀是 `git push`，
            # 记下来等于把正常推送也放行。卡片上不给这一档，账本这层也必须自守。
            rule = memo_rule(command) if tool == "run_shell" and not is_irreversible(command) else ()
            if rule:
                self.approved_prefixes.add(rule)

    def forget(self, tool: str, arguments: dict, scope: str = SCOPE_ONCE) -> None:
        """驳回同样要记住，否则下一步模型会拿同一个动作再问一遍。"""
        self.denied_actions.add(approval_key(tool, arguments))
        if scope == SCOPE_PREFIX:
            command = str(arguments.get("command") or "")
            # 驳回 `git push --force` 不该连带把正常的 `git push` 也拒掉。
            rule = memo_rule(command) if tool == "run_shell" and not is_irreversible(command) else ()
            if rule:
                self.denied_prefixes.add(rule)
        elif scope == SCOPE_TOOL:
            # 驳回到工具档就把同一档位/同一片路径上的旧授权撤掉；
            # 否则"刚才明明点了拒绝，下一步又静默跑了"。
            target = path_scope(arguments)
            self.approved_tools = {
                grant
                for grant in self.approved_tools
                if not (
                    grant[0] == self.sandbox_tier
                    and grant[1] == tool
                    and _covers_path(grant[2], target)
                )
            }

    def snapshot(self) -> dict:
        return {
            "mode": self.mode,
            # 免问范围本身就是边界的一部分：只写"批过哪些工具"回答不了
            # "切到 off 之后这些批准还算不算"。三段的形状让读的人一眼看见档位与路径。
            "sandbox_tier": self.sandbox_tier,
            "tool_scope": "tier+tool+target_path_prefix",
            "approved_tools": sorted(render_tool_grant(grant) for grant in self.approved_tools),
            "approved_prefixes": sorted(render_rule(rule) for rule in self.approved_prefixes),
            "persistent_prefixes": sorted(render_rule(rule) for rule in self.persistent_prefixes),
            "denied_prefixes": sorted(render_rule(rule) for rule in self.denied_prefixes),
            # 手写规则也进快照：规则文件坏了/被改过，是事后解释"当时为什么没问"
            # 或"当时为什么被拒"的第一手材料。
            "ruleset": self.ruleset.summary(),
        }

    # -- 判定 --------------------------------------------------------------
    def decide(self, tool: "ToolDefinition", arguments: dict, risk: str | None = None) -> Decision:
        risk = tool.resolve_risk(dict(arguments or {})) if risk is None else risk
        name = tool.name
        command = str((arguments or {}).get("command") or "")
        # 卡片上"总是允许 X"的 X 与账本里真正记下的那条规则必须是同一串，所以在这里算一次。
        memo = memoizable_prefix(name, command)

        # 手写 deny 规则优先于一切：绕过模式、计划模式、会话里刚批过的动作都挡不住它。
        # 这正是用户写 deny 的目的（例如「这个目录永远别碰」），也是它唯一有用的形状。
        denied_rule = self.ruleset.match("deny", name, arguments or {})
        if denied_rule is not None:
            return deny(
                risk,
                "rule:deny",
                f"权限规则拒绝了该操作：{denied_rule.render()}",
                command_prefix(command),
            )

        # 单次越界的部署开关：关闭时连审批卡都不该出现——否则用户点了允许，
        # 命令仍然不执行，卡片本身就在撒谎。这道判定排在手写 deny 之后，
        # 让「永远别碰」的规则继续拿到它应有的归因。
        if host_bypass_requested(name, arguments):
            from blank_api.services.sandbox.policy import host_bypass_enabled

            if not host_bypass_enabled():
                return deny(
                    risk,
                    "sandbox:host_bypass_disabled",
                    "单次越界宿主执行未开启（部署开关 BLANK_SANDBOX_ALLOW_HOST_BYPASS）："
                    "该参数被忽略且命令不执行。",
                    command_prefix(command),
                )

        # 计划模式的进出是方向相反的两个开关：
        # * 进入只收紧（从什么模式进来都只可能更安全），不需要确认；
        # * 退出会放宽，计划模式下必须经过人工批准——这是模型唯一能主动请求放宽的入口。
        # 两条都排在计划模式闸门之前：否则 enter 会被当成普通写操作、exit 会被直接拒，
        # 模型就再也出不来了。
        if name == "enter_plan_mode":
            return approve(risk, "mode:already_plan" if self.mode == MODE_PLAN else "mode:enter_plan")
        if name == "exit_plan_mode":
            if self.mode != MODE_PLAN:
                return approve(risk, "mode:exit_noop")
            key = approval_key(name, arguments)
            if key in self.denied_actions:
                return deny(risk, "action:denied", "退出计划模式已在本次会话中被拒绝。")
            if key in self.approved_actions:
                return approve(risk, "action:approved")
            target = path_scope(arguments)
            if any(
                tier == self.sandbox_tier and granted_tool == name and _covers_path(prefix, target)
                for tier, granted_tool, prefix in self.approved_tools
            ):
                return approve(risk, f"tool:{self.sandbox_tier}:{name}")
            return ask(risk, "mode:switch")

        # 计划模式：只读取证，其余一律拒绝（不是询问——计划模式下不该有写动作的机会）。
        if self.mode == MODE_PLAN:
            if name in READ_ONLY_TOOLS and not is_irreversible(command):
                return approve(risk, "mode:plan")
            return deny(
                risk,
                "mode:plan",
                f"计划模式只允许只读取证，已拒绝执行 {name}。切换到默认模式后可以执行。",
                command_prefix(command),
            )

        # 单次越界宿主执行：部署开关已开时也必须逐条人工批准。
        # 它排在计划模式闸门之后（计划模式不允许执行命令）、不可逆闸门之前：
        # 越界本身就是要用户看见的那件事，卡片归因不能被 `irreversible` 顶掉。
        # 全放行模式与 approve_high_risk 都不代批——沙箱是能力边界，不是审批节奏。
        # 会话前缀 / 落盘规则 / 工具档在这里一律不生效：越界只能一条一条批。
        if host_bypass_requested(name, arguments):
            key = approval_key(name, arguments)
            if key in self.denied_actions:
                return deny(risk, "action:denied", "该单次越界执行已在本次会话中被拒绝。")
            if key in self.approved_actions:
                return approve(risk, "sandbox:host_bypass", command_prefix(command))
            return ask(risk, "sandbox:host_bypass", command_prefix(command), memo="")

        # 不可逆操作：任何模式、任何前缀规则都不放行，必须逐个确认。
        # 也不给"总是允许"这一档：`git push --force` 的前缀是 `git push`，记下来
        # 等于连正常的推送也一起放行，而它本来必须一次一确认。
        if is_irreversible(command):
            return ask(risk, "irreversible", command_prefix(command))

        if self.mode == MODE_BYPASS:
            return approve(risk, "mode:bypass", command_prefix(command))
        # 归因要和来源一致：开关就叫 approve_high_risk，别在追踪里冒充"全放行模式"。
        if self.approve_high_risk:
            return approve(risk, "flag:approve_high_risk", command_prefix(command))

        key = approval_key(name, arguments)
        if key in self.denied_actions:
            return deny(risk, "action:denied", f"该操作已在本次会话中被拒绝：{name}")
        if key in self.approved_actions:
            return approve(risk, "action:approved", command_prefix(command))

        if name == "run_shell" and command:
            prefix = command_prefix(command)
            denied = covered_by_rules(command, self.denied_prefixes, refuse_inadmissible=False)
            if denied:
                return deny(
                    risk,
                    "prefix:denied",
                    f"命令前缀已被本次会话拒绝：{denied}",
                    prefix,
                )
            hit = covered_by_rules(command, self.approved_prefixes)
            if hit:
                return approve(risk, f"prefix:{hit}", prefix)
            # 落盘规则排在会话规则之后：同一串两边都有时，追踪仍念"这次会话批的"。
            # 拒绝规则先于它生效（上面已经判过），所以持久化不会覆盖本会话的驳回。
            durable = covered_by_rules(command, self.persistent_prefixes)
            if durable:
                return approve(risk, f"prefix:persistent:{durable}", prefix)

        # 手写 allow 规则排在会话驳回之后：用户刚刚点过"不"的那一条，不该被
        # 一份通用规则立刻放行。它也不能越过上面的 IRREVERSIBLE 闸门。
        allowed_rule = self.ruleset.match("allow", name, arguments or {})
        if allowed_rule is not None:
            return approve(risk, f"rule:allow:{allowed_rule.selector()}", command_prefix(command))

        target = path_scope(arguments)
        if any(
            tier == self.sandbox_tier and granted_tool == name and _covers_path(prefix, target)
            for tier, granted_tool, prefix in self.approved_tools
        ):
            return approve(risk, f"tool:{self.sandbox_tier}:{name}", command_prefix(command))

        # "接受编辑"：普通文件写入自动放行（有备份可回滚）。
        # 但敏感路径 / 含密钥的补丁仍是高风险，不能被这一档静默吞掉。
        if self.mode == MODE_ACCEPT_EDITS and name in EDIT_TOOLS and risk not in ("high", "critical"):
            return approve(risk, "mode:accept_edits")

        if risk not in ("high", "critical"):
            return approve(risk, "risk:auto", command_prefix(command))

        return ask(risk, "risk:ask", command_prefix(command), memo=memo)


__all__ = [
    "ALLOW",
    "ARGUMENT_SCOPED_TOOLS",
    "ASK",
    "DENY",
    "Decision",
    "IRREVERSIBLE",
    "MEMOIZABLE_EXES",
    "MODE_ACCEPT_EDITS",
    "MODE_BYPASS",
    "MODE_DEFAULT",
    "MODE_LABELS",
    "MODE_PLAN",
    "MODES",
    "PermissionPolicy",
    "SCOPES",
    "SCOPE_ONCE",
    "SCOPE_PREFIX",
    "SCOPE_TOOL",
    "approval_options",
    "approve",
    "ask",
    "command_prefix",
    "covered_by_rules",
    "deny",
    "is_irreversible",
    "memoizable_prefix",
    "parse_decision",
    "prefix_rule",
    "render_rule",
    "split_segments",
    "tokenize",
    "tool_grant_too_wide",
]
