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
import re
from typing import TYPE_CHECKING

from blank_api.services.approvals import approval_key
from blank_api.services.log_safety import contains_secret

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
    r"|\bgit\s+push\b[^\n]*--force"
    r"|\bdd\s+if="
    r"|\bchmod\s+-R\s+777\s+/"
    r"|>\s*/dev/sd[a-z]"
    r")"
)

# 命令分段符：只有整条命令的每一段都被前缀规则覆盖，才算命中。
_SEGMENT_BREAKS = ("&&", "||", ";", "|", "\n", "&")


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
    """规则串：带空格的 token 必须带引号，否则 `"a b"` 记下来再读回去会多出一段。"""
    return " ".join(f"'{token}'" if any(char.isspace() for char in token) else token for token in rule)


def _starts_with(argv: list[str], rule: tuple[str, ...]) -> bool:
    if len(argv) < len(rule):
        return False
    for index, token in enumerate(rule):
        if token == "*":
            continue
        actual = _basename(argv[index]) if index == 0 else argv[index]
        if actual.lower() != token.lower():
            return False
    return True


def covered_by_rules(command: str, rules) -> str | None:
    """整条命令的每个子命令都被规则覆盖时返回命中的规则串，否则返回 None。

    只覆盖第一段是不够的：``npm test && rm -rf src`` 若只对第一段放行，
    第二段就搭了顺风车。所以这里要求逐段命中。
    """
    segments = [tokenize(item) for item in split_segments(command)]
    if not segments or any(not segment for segment in segments):
        return None
    usable = [rule for rule in rules if rule]
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


def approval_options(tool_name: str, memo: str = "") -> list[dict]:
    """审批卡上的按钮数据：本次 / 这条命令（仅 run_shell 且粒度够细时）/ 该工具本会话。

    ``memo`` 传的是 :func:`memo_rule` 算出来的那一串，不是命令本身的首段——
    卡片上写的"总是允许 X"必须与真正写进会话账本的那条规则逐字一致，
    否则用户批的是 ``python -c print(1)``、账本上记的却是 ``python``。
    """
    options = [{"scope": SCOPE_ONCE, "label": "仅本次允许"}]
    if memo:
        options.append({"scope": SCOPE_PREFIX, "label": f"本会话总是允许 {memo}"})
    options.append(
        {"scope": SCOPE_TOOL, "label": f"本会话总是允许 {tool_name}", "warning": "该工具在本会话内不再询问"}
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
_CODE_EXECUTORS = frozenset({"python", "python3", "py", "node", "nodejs"})
_INLINE_CODE_FLAGS = frozenset(
    {"-c", "-e", "--command", "-command", "-commands", "/c", "/e", "--eval", "--print"}
)
_MODULE_FLAGS = frozenset({"-m", "--module"})


def memo_rule(command: str) -> tuple[str, ...]:
    """这次批准该记成什么规则；空元组＝不值得记（下次照样挂卡）。

    规则必须覆盖到"真正被执行的那一段"：解释器取到 ``-c`` 的正文或脚本路径为止，
    普通命令取 :func:`command_prefix`。含密钥形态的规则不记——卡片上它是打码的，
    让用户批准一条自己看不见全文的规则，等于把打码变成盲签。
    """
    segments = split_segments(command)
    if not segments:
        return ()
    tokens = tokenize(segments[0])
    if not tokens:
        return ()
    exe = _basename(tokens[0])
    rule: tuple[str, ...] = ()
    if exe.lower() in _CODE_EXECUTORS:
        # 跳过解释器自己的旗标（`-u` / `-I` / `--inspect`），停在载荷那一段：
        # 内联代码取到正文，`-m` 取到模块名，否则取到脚本路径。裸解释器不记。
        # 首段写 basename（与 command_prefix 一致），换一种路径写法也能命中。
        for index in range(1, len(tokens)):
            token = tokens[index]
            lowered = token.lower()
            if lowered in _INLINE_CODE_FLAGS or lowered in _MODULE_FLAGS:
                if index + 1 < len(tokens):
                    rule = (exe,) + tuple(tokens[1 : index + 2])
                break
            if not token.startswith("-"):
                rule = (exe,) + tuple(tokens[1 : index + 1])
                break
    else:
        rule = prefix_rule(command_prefix(command))
        if rule and len(rule) < 2 and rule[0].lower() not in MEMOIZABLE_EXES:
            rule = ()
    if rule and contains_secret(render_rule(rule)):
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
@dataclass
class PermissionPolicy:
    """一次任务运行期间的权限状态。可变：批准与拒绝会就地写入，供后续步骤复用。"""

    mode: str = MODE_DEFAULT
    approved_tools: set[str] = field(default_factory=set)
    approved_actions: set[str] = field(default_factory=set)
    approved_prefixes: set[tuple[str, ...]] = field(default_factory=set)
    denied_prefixes: set[tuple[str, ...]] = field(default_factory=set)
    denied_actions: set[str] = field(default_factory=set)
    approve_high_risk: bool = False

    @classmethod
    def build(
        cls,
        mode: str = MODE_DEFAULT,
        *,
        approve_high_risk: bool = False,
        approved_tools=(),
        approved_actions=(),
        approved_prefixes=(),
    ) -> "PermissionPolicy":
        return cls(
            mode=mode if mode in MODES else MODE_DEFAULT,
            approve_high_risk=bool(approve_high_risk),
            approved_tools=set(approved_tools or ()),
            approved_actions=set(approved_actions or ()),
            approved_prefixes={prefix_rule(item) for item in (approved_prefixes or ()) if prefix_rule(item)},
        )

    # -- 状态变更 ----------------------------------------------------------
    def remember(self, tool: str, arguments: dict, scope: str = SCOPE_ONCE) -> None:
        """把一次批准写进会话记忆。

        前缀档记的是 :func:`memo_rule` 算出来的那一串（解释器类必须带到正文），
        不是命令首段——前端即使误传 scope=prefix，也记不出"整个 python"这种宽账。
        """
        self.approved_actions.add(approval_key(tool, arguments))
        if scope == SCOPE_TOOL:
            self.approved_tools.add(tool)
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

    def snapshot(self) -> dict:
        return {
            "mode": self.mode,
            "approved_tools": sorted(self.approved_tools),
            "approved_prefixes": sorted(render_rule(rule) for rule in self.approved_prefixes),
            "denied_prefixes": sorted(render_rule(rule) for rule in self.denied_prefixes),
        }

    # -- 判定 --------------------------------------------------------------
    def decide(self, tool: "ToolDefinition", arguments: dict, risk: str | None = None) -> Decision:
        risk = tool.resolve_risk(dict(arguments or {})) if risk is None else risk
        name = tool.name
        command = str((arguments or {}).get("command") or "")
        # 卡片上"总是允许 X"的 X 与账本里真正记下的那条规则必须是同一串，所以在这里算一次。
        memo = memoizable_prefix(name, command)

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
            if covered_by_rules(command, self.denied_prefixes):
                return deny(
                    risk,
                    "prefix:denied",
                    f"命令前缀已被本次会话拒绝：{covered_by_rules(command, self.denied_prefixes)}",
                    prefix,
                )
            hit = covered_by_rules(command, self.approved_prefixes)
            if hit:
                return approve(risk, f"prefix:{hit}", prefix)

        if name in self.approved_tools:
            return approve(risk, f"tool:{name}", command_prefix(command))

        # "接受编辑"：普通文件写入自动放行（有备份可回滚）。
        # 但敏感路径 / 含密钥的补丁仍是高风险，不能被这一档静默吞掉。
        if self.mode == MODE_ACCEPT_EDITS and name in EDIT_TOOLS and risk not in ("high", "critical"):
            return approve(risk, "mode:accept_edits")

        if risk not in ("high", "critical"):
            return approve(risk, "risk:auto", command_prefix(command))

        return ask(risk, "risk:ask", command_prefix(command), memo=memo)


__all__ = [
    "ALLOW",
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
]
