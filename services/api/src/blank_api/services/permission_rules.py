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

* 括号里是限制；不写括号表示"这个工具的所有调用"。
* `run_shell` 的限制按**逐段**匹配（`git status && rm -rf src` 不会被
  `allow run_shell(git status)` 放行），语义与 `approvals.json` 的前缀规则一致。
* 文件工具（`read_file` / `apply_patch` / `list_files` / `search_code` / `git` 等）
  的限制按工作区相对路径通配，`*` 可以跨目录。指向工作区外的规则不生效。
* 任何一行解析失败 ⇒ **整份文件停用**并带出错误。宁可多问一次，也不能让一条
  写坏的 deny 静默消失。

位置由 `BLANK_PERMISSION_RULES` 指定，默认与设置文件同目录 `permissions.rules`。
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path

MAX_RULE_LEN = 400
MAX_RULES = 500
_ACTIONS = ("allow", "deny")
_TOOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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

    @property
    def tokens(self) -> tuple[str, ...]:
        """run_shell 规则的 token 形态；解析口径与 approvals.json 同源。"""
        from blank_api.services.permissions import prefix_rule

        return prefix_rule(self.spec)

    def render(self) -> str:
        return f"{self.action} {self.selector()}"

    def selector(self) -> str:
        """不带动作的名字形态：追踪里 `rule:allow:run_shell(git status)` 用这个。"""
        return f"{self.tool}({self.spec})" if self.spec else self.tool

    def matches(self, tool: str, arguments: dict) -> bool:
        if tool != self.tool:
            return False
        if not self.spec:
            return True
        if tool == "run_shell":
            from blank_api.services.permissions import covered_by_rules

            command = str((arguments or {}).get("command") or "")
            if not command:
                return False
            # allow 必须逐段覆盖且规则本身可免问；deny 保留拒绝力（哪怕规则
            # 本身不适合当"免问"——比如裸解释器名）。
            return (
                covered_by_rules(
                    command, {self.tokens}, refuse_inadmissible=self.action == "allow"
                )
                is not None
            )
        return _path_matches(self.spec, str((arguments or {}).get("path") or ""))


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

    def match(self, action: str, tool: str, arguments: dict) -> PermissionRule | None:
        for rule in self.rules:
            if rule.action == action and rule.matches(tool, arguments):
                return rule
        return None

    def summary(self) -> dict:
        allow = [rule.render() for rule in self.rules if rule.action == "allow"]
        deny = [rule.render() for rule in self.rules if rule.action == "deny"]
        return {
            "path": self.path,
            "allow": allow,
            "deny": deny,
            "error": self.error,
        }


def empty_ruleset() -> PermissionRuleset:
    return PermissionRuleset()


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
    return PermissionRuleset(rules=tuple(rules), path=source)


def _failed(source: str, error: str) -> PermissionRuleset:
    return PermissionRuleset(rules=(), error=error, path=source)


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
    "MAX_RULE_LEN",
    "MAX_RULES",
    "PermissionRule",
    "PermissionRuleset",
    "empty_ruleset",
    "load_ruleset",
    "parse_rules",
    "rules_path",
]
