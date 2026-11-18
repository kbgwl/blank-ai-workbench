"""权限判定的唯一入口。

一次判定的顺序是**写死在一处**的，任何模块都不许自己再拼一遍：

    能力边界（safety） → 策略（permissions） → 严格复核（高危险不许自动放行）

以前这三步散在两处（网关的 `decide` 与图里的审批门），加一条规则要在两个地方对齐，
迟早分叉——「审查员把卡按没」那次就是分叉的样子：权限层算出该出卡，审查员另算一遍
算出该拒，最后卡不出现，而没有任何一处记着"这张卡被谁取消了"。

另外引入 **resources**：把"这次动作碰了什么"变成数据（`path:src/x.py` / `cmd:curl`），
而不是散落在各处的正则。规则匹配、审批卡、审计都用同一份资源清单。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from blank_api.services.permissions import (
    ALLOW,
    Decision,
    ask,
    approve,
    command_prefix,
    deny,
    path_scope,
)

__all__ = ["authorize", "enforce_strict_review", "resources_for"]

# 这些"放行"不是人给的：策略自动放行、旧口径自动放行、以及没有依据的空规则。
# 高危动作靠它们过关，等于没有闸门——严格复核专治这一档。
_AUTOMATIC_ALLOW_RULES = frozenset({"policy:auto", "legacy:auto", ""})

# 写盘类工具：资源就是它碰的那个路径。
_FILE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch", "notebook_edit"})
# 命令类工具：资源是命令首段（可执行体），不是整条命令——整条等于没抽象。
_SHELL_TOOLS = frozenset({"run_shell", "monitor"})


def resources_for(tool: str, arguments: dict | None) -> tuple[str, ...]:
    """这次动作碰了什么。形状对齐 opencode 的 `resources: string[]`。

    只做"能一眼看懂、能稳定比对"的切分：路径类给 `path:`，命令类给 `cmd:`，
    其余给 `tool:`。不在这里解析管道、重定向与 shell 语法——那是策略层的事，
    资源清单越聪明越容易和真实执行对不上。
    """
    args = dict(arguments or {})
    if tool in _FILE_TOOLS:
        return (f"path:{path_scope(args) or '*'}",)
    if tool in _SHELL_TOOLS:
        command = str(args.get("command") or "").strip()
        if not command:
            return ("cmd:*",)
        return (f"cmd:{command_prefix(command) or command.split()[0]}",)
    return (f"tool:{tool}",)


def _with_resources(decision: Decision, tool: str, arguments: dict | None) -> Decision:
    if decision.resources:
        return decision
    return replace(decision, resources=resources_for(tool, arguments))


def enforce_strict_review(decision: Decision, risk: str, mode: str) -> Decision:
    """高危动作不许被**自动**路径静默放行（对齐 Codex 的 strictReviewRequired）。

    返回值语义：
    - 原本就 deny/ask 的，原样返回（不改变已有判定）；
    - allow 但依据是人给的（rule 不在自动清单里），原样返回；
    - allow 且没有依据，风险又是 high/critical → 升级成 ask。

    bypass 模式是用户显式关掉询问，不算静默放行。
    """
    if decision.outcome != ALLOW:
        return decision
    if risk not in ("high", "critical"):
        return decision
    if mode == "bypass":
        return decision
    if decision.rule not in _AUTOMATIC_ALLOW_RULES:
        return decision
    return ask(risk, "policy:strict_review")


def authorize(
    tool: Any,
    arguments: dict | None,
    context: Any,
    *,
    risk: str | None = None,
) -> Decision:
    """唯一判定入口：任何"要不要问人"的答案都从这一个函数出。

    `tool` 只要求有 `name` 与 `resolve_risk`；`context` 只要求有
    `permissions` / `safety` / `approval_policy` / `root` —— 这样单测与子代理
    都能用最简单的替身构造，不必背上整个运行时。
    """
    args = dict(arguments or {})
    resolved = tool.resolve_risk(args) if risk is None else risk
    permissions = getattr(context, "permissions", None)
    mode = (getattr(permissions, "mode", "") or "default") if permissions is not None else "default"

    # ① 能力边界：这里拒绝的动作，就算人在审批卡上点了"允许"也不执行，
    #    所以它必须排在最前面——否则会变成"点了允许还是失败"。
    safety = getattr(context, "safety", None)
    if safety is not None:
        verdict = safety.gate(tool.name, args, getattr(context, "root", ""), mode=mode)
        if verdict.denied:
            return _with_resources(deny(resolved, verdict.rule, verdict.reason, verdict.prefix), tool.name, args)
        # 「每条命令都要确认」不另造状态机：抬高风险即可，审批、会话记忆、
        # 前缀白名单全部复用同一套既有逻辑。
        if verdict.asks and resolved not in ("high", "critical"):
            resolved = "high"

    # ② 策略：模式、规则、会话批准、前缀账、不可逆闸门都在这里判。
    if permissions is not None:
        decided = permissions.decide(tool, args, resolved)
        return _with_resources(enforce_strict_review(decided, resolved, mode), tool.name, args)

    # ③ 没有任何策略对象时的旧口径（单测与外部集成会走到）：有没有人批过 → 风险兜底。
    policy = getattr(context, "approval_policy", None)
    if policy is not None:
        try:
            allowed = bool(policy(tool, args))
        except Exception:
            allowed = False
        if allowed:
            return _with_resources(approve(resolved, "legacy:approved"), tool.name, args)
    outcome = ask(resolved, "legacy:ask") if resolved in ("high", "critical") else approve(resolved, "legacy:auto")
    return _with_resources(outcome, tool.name, args)
