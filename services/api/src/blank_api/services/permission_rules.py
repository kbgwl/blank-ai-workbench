"""手写权限规则：让人能直接写 allow / deny，而不必点一遍审批卡。

与 `approvals.json` 的分工：

* `approvals.json` 是程序写的（用户在卡片上点「总是允许」），格式固定、原子替换；
* 本文件是**人写的**，纯文本、可带注释，程序只读不写。把它做成文本是因为
  手工编辑 JSON 容易写坏一个逗号，而写坏之后最危险的结果是"规则静默失效"。

语法（一行一条，`#` 开头为注释）::

    # 永远不问
    allow run_shell(npm run test *)
    allow read_file(src/**)
    # 永远拒绝（deny 优先于 bypass 与任何 allow）
    deny run_shell(rm -rf *)
    deny read_file(secrets/**)
    # 每次都问（ask 压过 allow、bypass 与会话批准；deny 仍压过 ask）
    ask run_shell(git push)

* 括号里是限制；不写括号表示"这个工具的所有调用"。
* `run_shell` 的限制按**逐段**匹配（`git status && rm -rf src` 不会被
  `allow run_shell(git status)` 放行），语义与 `approvals.json` 的前缀规则一致。
  `run_shell` 与 `monitor` 的规则互相通用：两者执行同一条命令，只是输出投递不同。
* 文件工具（`read_file` / `apply_patch` / `list_files` / `search_code` / `git` 等）
  的限制按工作区相对路径通配，`*` 可以跨目录。指向工作区外的规则不生效。
* 任何一行解析失败 ⇒ **整份文件停用**并带出错误。宁可多问一次，也不能让一条
  写坏的 deny 静默消失。

位置由 `BLANK_PERMISSION_RULES` 指定，默认与设置文件同目录 `permissions.rules`。

**第二来源是 settings.json 的 `permissions.allow/deny/ask`**（Claude Code 的形状）。
两处语义差异由 :func:`parse_settings_rules` 负责抹平：

* 工具名是 Claude 的（``Bash`` / ``Read`` / ``Edit`` …），映射到我们的本地工具；
* ``Bash(cmd:*)`` 是前缀，``Bash(cmd)`` 是**整串**（上游用 glob 比对整条子命令），
  而文本文件里的裸 ``cmd`` 是前缀——所以这里给规则带上 ``mode``；
* 写不出来的形状（MCP 工具、``domain:`` 规则、工作区外路径）**对 allow 只记 note
  并跳过**（少放行＝更安全），**对 deny/ask 则整份 settings 规则停用**——一份
  看起来防住了、其实没生效的 deny 比没有更糟。
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

MAX_RULE_LEN = 400
MAX_RULES = 500
_ACTIONS = ("allow", "deny", "ask")
_SETTINGS_ACTIONS = ("allow", "deny", "ask")
_TOOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# settings.json 里 Claude 的工具名 → 本地工具名。只收语义能一一对上的：
# Bash 的执行/审批语义与 run_shell 相同；Read/Edit/Write 等是同一种"按路径通配"。
# Task 在 Claude 里派子代理，我们对应 spawn_agent。
CLAUDE_TOOL_ALIASES: dict[str, str] = {
    "Bash": "run_shell",
    "Read": "read_file",
    "Edit": "edit_file",
    "MultiEdit": "edit_file",
    "Write": "write_file",
    "LS": "list_files",
    "NotebookEdit": "notebook_edit",
    "Glob": "glob",
    "Grep": "search_code",
    "WebFetch": "web_fetch",
    "WebSearch": "web_search",
    "Task": "spawn_agent",
    "Agent": "spawn_agent",
    "Skill": "skill",
    "TodoWrite": "update_plan",
    "AskUserQuestion": "request_user_input",
    "EnterPlanMode": "enter_plan_mode",
    "ExitPlanMode": "exit_plan_mode",
}

# 规则括号里的串该拿去比哪个参数。命令类工具自己有逐段逻辑，不在这张表里。
_SPEC_FIELDS: dict[str, str] = {
    "read_file": "path",
    "edit_file": "path",
    "write_file": "path",
    "list_files": "path",
    "apply_patch": "path",
    "notebook_edit": "notebook_path",
    "glob": "pattern",
    "search_code": "pattern",
}

_COMMAND_TOOLS = ("run_shell", "monitor")

# 本地工具名（与 services/tools.build_default_tools() 对齐，测试钉住）。
# settings 里直接写本地名字也认——用户会照着文档里的工具名写。
LOCAL_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "apply_patch", "cron_create", "cron_delete", "cron_list", "edit_file",
        "enter_plan_mode", "enter_worktree", "exit_plan_mode", "exit_worktree",
        "git", "glob", "list_agents", "list_files", "list_mcp_resources", "lsp",
        "memory_search", "memory_write", "monitor", "notebook_edit", "propose_goal",
        "propose_skills", "read_file", "read_mcp_resource", "read_mcp_resource_dir",
        "read_notes", "refresh_mcp_tools", "repl", "report_findings",
        "request_user_input", "run_shell", "run_tests", "schedule_wakeup",
        "search_code", "send_message", "set_goal", "skill", "spawn_agent",
        "spawn_agents", "task_create", "task_get", "task_list", "task_output",
        "task_stop", "task_update", "team_add_member", "team_assign_task",
        "team_create_bot", "team_remove_member", "team_status", "team_update_bot",
        "tool_search", "update_plan", "web_fetch", "web_search", "workflow",
        "write_file", "write_notes",
    }
)


def rules_path() -> Path:
    override = str(os.environ.get("BLANK_PERMISSION_RULES") or "").strip()
    if override:
        return Path(override)
    from blank_api.services.settings import settings_path

    return settings_path().with_name("permissions.rules")


@dataclass(frozen=True)
class PermissionRule:
    action: str
    tool: str
    spec: str = ""
    line: int = 0
    # 命令类规则的比对口径：``prefix`` 是文本文件的裸串（逐段"以它开头"），
    # ``glob`` 是 settings.json 里的 Bash 串（整条子命令做 glob，``cmd:*`` 除外）。
    # 路径类工具不看这个字段，一律按 glob 比路径。
    mode: str = "prefix"

    @property
    def tokens(self) -> tuple[str, ...]:
        """run_shell 规则的前缀 token 形态；解析口径与 approvals.json 同源。"""
        from blank_api.services.permissions import prefix_rule

        return prefix_rule(self.spec)

    def render(self) -> str:
        return f"{self.action} {self.selector()}"

    def selector(self) -> str:
        """不带动作的名字形态：追踪里 `rule:allow:run_shell(git status)` 用这个。"""
        return f"{self.tool}({self.spec})" if self.spec else self.tool

    def canonical(self) -> str:
        """指纹用：动作 + 口径 + 选择子。口径必须进去，否则同一串规则换个
        “精确/前缀”的读法会算出同一个 sha，事后分不清是哪一版。"""
        return f"{self.action} {self.mode} {self.selector()}"

    def matches(self, tool: str, arguments: dict) -> bool:
        # 为 run_shell / monitor 写下的规则互相通用：两者执行的是同一条命令，只是
        # 输出投递方式不同。不并过来，`deny run_shell(git push)` 换个工具名就能绕过。
        command_tool = tool in _COMMAND_TOOLS and self.tool in _COMMAND_TOOLS
        if tool != self.tool and not command_tool:
            return False
        if not self.spec:
            return True
        if command_tool:
            from blank_api.services.permissions import (
                covered_by_any,
                covered_by_rules,
                glob_covered_by_any,
                glob_covered_by_rules,
            )

            command = str((arguments or {}).get("command") or "")
            if not command:
                return False
            if self.mode == "glob":
                if self.action == "allow":
                    return glob_covered_by_rules(command, {self.spec}) is not None
                return glob_covered_by_any(command, {self.spec}) is not None
            # deny 是"只要有一处命中就拒"，包括壳载荷里展开出来的内层命令；
            # allow 仍要求每一段都被覆盖（含壳载荷展开），不许搭顺风车。
            if self.action == "deny":
                return covered_by_any(command, {self.tokens}) is not None
            return covered_by_rules(command, {self.tokens}) is not None
        key = _SPEC_FIELDS.get(self.tool, "path")
        return _path_matches(self.spec, str((arguments or {}).get(key) or ""))


def _strip_current_dir(text: str) -> str:
    """只剥掉真正的 `./` 前缀；`lstrip("./")` 会把 `.git` 也削成 `git`。"""
    while text.startswith("./"):
        text = text[2:]
    return text


def _path_matches(spec: str, value: str) -> bool:
    target = value.replace("\\", "/").strip()
    if not target:
        return False
    pattern = spec.replace("\\", "/").strip()
    # 规则不许指向工作区外：即使写了也不生效，避免"看起来防住了其实没防"。
    if pattern.startswith("/") or re.match(r"^[A-Za-z]:/", pattern) or ".." in Path(pattern).parts:
        return False
    if target.startswith("/") or ".." in Path(target).parts:
        return False
    return fnmatch.fnmatchcase(_strip_current_dir(target), _strip_current_dir(pattern))


@dataclass(frozen=True)
class PermissionRuleset:
    rules: tuple[PermissionRule, ...] = ()
    error: str = ""
    path: str = ""
    # 生效规则的版本号（按渲染后的规则算，注释/空行/空白不算）。快照里带上它，
    # 事后才能回答「这一步被哪一版手写规则管着」；只记路径和条目数，改一条 allow
    # 与换一版文件看起来会一模一样。空串 = 没加载过规则文件，与「文件存在但为空」
    # 的 sha 不同——后者是一个真实的策略事实。
    sha: str = ""
    # settings.json 里认不出/写不出的条目。只对 allow 记在这里（跳过＝少放行）；
    # deny/ask 写不出来时整份 settings 规则停用，错误进 ``error``。
    notes: tuple[str, ...] = ()

    def match(self, action: str, tool: str, arguments: dict) -> PermissionRule | None:
        for rule in self.rules:
            if rule.action == action and rule.matches(tool, arguments):
                return rule
        return None

    def summary(self) -> dict:
        allow = [rule.render() for rule in self.rules if rule.action == "allow"]
        deny = [rule.render() for rule in self.rules if rule.action == "deny"]
        ask = [rule.render() for rule in self.rules if rule.action == "ask"]
        payload = {
            "path": self.path,
            "allow": allow,
            "deny": deny,
            "error": self.error,
            "sha": self.sha,
        }
        if ask:
            payload["ask"] = ask
        if self.notes:
            payload["notes"] = list(self.notes)
        return payload


def empty_ruleset() -> PermissionRuleset:
    return PermissionRuleset()


def _rules_sha(rules: list[PermissionRule]) -> str:
    """生效规则的指纹：只算「哪条规则、什么顺序」，注释和空白不参与。

    顺序要算进去：判定取第一条命中，同样两条规则换个顺序就可能换一个答案。
    比对口径（前缀 vs glob）也要算：同一串规则换一种读法就是换了一条规则。
    """
    canonical = "\n".join(rule.canonical() for rule in rules)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def parse_rules(text: str, source: str = "") -> PermissionRuleset:
    rules: list[PermissionRule] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if len(line) > MAX_RULE_LEN:
            return _failed(source, f"第 {number} 行超过 {MAX_RULE_LEN} 字符")
        action, _, rest = line.partition(" ")
        if action not in _ACTIONS or not rest.strip():
            return _failed(source, f"第 {number} 行无法解析：{line[:80]}")
        rest = rest.strip()
        spec = ""
        if "(" in rest:
            tool, _, tail = rest.partition("(")
            if not tail.endswith(")"):
                return _failed(source, f"第 {number} 行括号没有闭合：{line[:80]}")
            spec = tail[:-1].strip()
            if not spec:
                return _failed(source, f"第 {number} 行括号里是空的")
        else:
            tool = rest
        tool = tool.strip()
        if not _TOOL.match(tool):
            return _failed(source, f"第 {number} 行工具名不合法：{tool[:40]}")
        if len(rules) >= MAX_RULES:
            return _failed(source, f"规则超过 {MAX_RULES} 条")
        rules.append(PermissionRule(action=action, tool=tool, spec=spec, line=number))
    return PermissionRuleset(rules=tuple(rules), path=source, sha=_rules_sha(rules))


def _failed(source: str, error: str, notes: tuple[str, ...] = ()) -> PermissionRuleset:
    return PermissionRuleset(rules=(), error=error, path=source, notes=notes)


# --------------------------------------------------------------------------- #
# settings.json 的 permissions.allow/deny/ask
# --------------------------------------------------------------------------- #
def _path_spec_ok(spec: str) -> bool:
    """settings 里的路径通配必须落在工作区内；指出去的一律不认。

    认下来又匹配不到，比不认更糟：``deny Read(/etc/**)`` 看起来防住了，
    实际那条 deny 永远不会命中。
    """
    text = spec.replace("\\", "/").strip()
    if not text or text.startswith(("/", "~")):
        return False
    if re.match(r"^[A-Za-z]:/", text):
        return False
    return ".." not in Path(text).parts


def _map_tool(name: str) -> str | None:
    if name in LOCAL_TOOL_NAMES:
        return name
    return CLAUDE_TOOL_ALIASES.get(name)


def _split_entry(text: str) -> tuple[str, str]:
    tool, sep, tail = text.partition("(")
    if not sep:
        return text.strip(), ""
    if not tail.endswith(")"):
        raise ValueError("括号没有闭合")
    spec = tail[:-1].strip()
    if not spec:
        raise ValueError("括号里是空的")
    return tool.strip(), spec


def _unsupported(
    action: str, message: str, source: str, notes: list[str]
) -> PermissionRuleset | None:
    """写不出来的条目：allow 记 note 跳过（少放行＝安全），deny/ask 整份停用。

    一份「看起来防住了、其实没生效」的 deny 是最危险的形状——它让人放心地
    把危险命令留在配置里。所以这里宁可整份 settings 规则不生效，也要把错误
    摆到事件上。
    """
    if action == "allow":
        notes.append(message)
        return None
    return _failed(source, message, notes=tuple(notes))


def _command_rule(action: str, tool: str, spec: str) -> PermissionRule:
    from blank_api.services.permissions import prefix_rule

    if spec == "*":
        return PermissionRule(action=action, tool=tool)
    if spec.endswith(":*"):
        prefix = spec[:-2].strip()
        if not prefix or "*" in prefix or not prefix_rule(prefix):
            # 上游的 `:*` 是按字面串做前缀的；我们把 `*` 当通配，`*:*` 会变成
            # "放行一切命令"。这种写法不认，交回 note/error。
            raise ValueError("前缀里不能带 * 或为空")
        return PermissionRule(action=action, tool=tool, spec=prefix, mode="prefix")
    return PermissionRule(action=action, tool=tool, spec=spec, mode="glob")


def parse_settings_rules(permissions: Any, source: str = "settings") -> PermissionRuleset:
    """把 Claude 形状的 ``permissions`` 段翻成我们的规则集。

    只翻语义能一比一对上的部分（见模块 docstring）。返回的规则带着 ``mode``：
    ``Bash(cmd:*)`` 是前缀，``Bash(cmd)`` 是按 glob 比对整条子命令。
    """
    if not permissions:
        return PermissionRuleset(path=source)
    if not isinstance(permissions, Mapping):
        return _failed(source, "permissions 不是对象")
    rules: list[PermissionRule] = []
    notes: list[str] = []
    for action in _SETTINGS_ACTIONS:
        entries = permissions.get(action)
        if entries is None:
            continue
        if not isinstance(entries, list):
            return _failed(source, f"permissions.{action} 不是列表", notes=tuple(notes))
        for raw in entries:
            if not isinstance(raw, str):
                failure = _unsupported(
                    action, f"permissions.{action} 里有非字符串条目", source, notes
                )
                if failure is not None:
                    return failure
                continue
            text = raw.strip()
            if not text:
                continue
            if len(text) > MAX_RULE_LEN:
                failure = _unsupported(
                    action,
                    f"permissions.{action} 有条目超过 {MAX_RULE_LEN} 字符",
                    source,
                    notes,
                )
                if failure is not None:
                    return failure
                continue
            try:
                tool_name, spec = _split_entry(text)
            except ValueError as exc:
                failure = _unsupported(
                    action,
                    f"permissions.{action} 条目无法解析（{exc}）：{text[:80]}",
                    source,
                    notes,
                )
                if failure is not None:
                    return failure
                continue
            tool = _map_tool(tool_name)
            if tool is None:
                kind = "MCP 工具" if tool_name.startswith("mcp__") else "未知工具名"
                failure = _unsupported(
                    action, f"{kind}规则未接入：{text[:80]}", source, notes
                )
                if failure is not None:
                    return failure
                continue
            try:
                if not spec:
                    rule = PermissionRule(action=action, tool=tool)
                elif tool in _COMMAND_TOOLS:
                    rule = _command_rule(action, tool, spec)
                elif tool in _SPEC_FIELDS:
                    if not _path_spec_ok(spec):
                        raise ValueError(f"路径不在工作区内：{spec[:80]}")
                    rule = PermissionRule(action=action, tool=tool, spec=spec, mode="glob")
                else:
                    raise ValueError(f"{tool_name} 不支持带参数的规则")
            except ValueError as exc:
                failure = _unsupported(
                    action, f"permissions.{action} 条目无法生效（{exc}）：{text[:80]}", source, notes
                )
                if failure is not None:
                    return failure
                continue
            if len(rules) >= MAX_RULES:
                return _failed(source, f"规则超过 {MAX_RULES} 条", notes=tuple(notes))
            rules.append(rule)
    return PermissionRuleset(
        rules=tuple(rules), path=source, sha=_rules_sha(rules), notes=tuple(notes)
    )


def merge_rulesets(*rulesets: PermissionRuleset) -> PermissionRuleset:
    """把几份规则集按顺序合起来（先出现的先命中）。"""
    rules: list[PermissionRule] = []
    errors: list[str] = []
    notes: list[str] = []
    paths: list[str] = []
    shas: list[str] = []
    for item in rulesets:
        if item is None:
            continue
        rules.extend(item.rules)
        if item.error:
            errors.append(item.error)
        notes.extend(item.notes)
        if item.path:
            paths.append(item.path)
        if item.sha:
            shas.append(item.sha)
    sha = _rules_sha(rules) if rules else (shas[0] if shas else "")
    return PermissionRuleset(
        rules=tuple(rules),
        error="; ".join(errors),
        path=" + ".join(paths),
        sha=sha,
        notes=tuple(notes),
    )


def load_ruleset(path: Path | None = None) -> PermissionRuleset:
    target = path or rules_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return PermissionRuleset(path=str(target))
    except (OSError, UnicodeDecodeError) as exc:
        return _failed(str(target), f"规则文件读取失败：{exc.__class__.__name__}")
    parsed = parse_rules(text, source=str(target))
    return parsed


__all__ = [
    "CLAUDE_TOOL_ALIASES",
    "LOCAL_TOOL_NAMES",
    "MAX_RULE_LEN",
    "MAX_RULES",
    "PermissionRule",
    "PermissionRuleset",
    "empty_ruleset",
    "load_ruleset",
    "merge_rulesets",
    "parse_rules",
    "parse_settings_rules",
    "rules_path",
]
