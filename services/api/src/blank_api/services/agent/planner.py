"""v165 §4 Autonomous Planner：把执行链已有的计划升级为可审计的执行计划。

设计取舍：**不额外调模型**。步骤文本由 ``create_plan_node`` 产出，本模块只补齐
v165 要求的结构（kind / tools / risk / depends_on / success_condition / 预算 / 审批点），
并在拿到真实失败结果时改版。再调一次模型换不来更可靠的结构，却让每个任务多花
一轮上下文；真正决定「自主」的是「失败之后计划有没有变」，不是计划写得像不像 JSON。

所有函数都是纯函数：不查库、不写事件、不读磁盘。事件与状态由调用方收口。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

STEP_KINDS = ("inspect", "edit", "verify", "repair")
MAX_PLAN_REVISIONS = 3
ROLLBACK_STRATEGY = "workspace_diff"

# 纯规则推断。顺序有意义：「修复测试失败」必须先命中 repair，才会落到 edit。
_KIND_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("repair", ("修复", "修好", "改正失败", "处理报错", "fix ", "repair")),
    ("verify", ("测试", "验证", "校验", "跑一遍", "检查是否", "test", "verify", "lint", "构建")),
    ("inspect", ("读取", "查看", "定位", "分析", "排查", "找", "看", "read", "inspect", "search", "locate")),
    ("edit", ("修改", "改写", "实现", "新增", "添加", "写入", "更新", "删除", "change", "write", "edit", "implement")),
)

_TOOLS_BY_KIND: dict[str, tuple[str, ...]] = {
    "inspect": ("list_files", "read_file", "search_code"),
    "edit": ("apply_patch", "write_file"),
    "verify": ("run_tests", "run_shell"),
    "repair": ("read_file", "apply_patch", "run_tests"),
}

# 这些动作无论落在哪一步都必须审批，v165 §7.2 / §13。
_HIGH_RISK_MARKS = (
    "删除", "移除", "安装依赖", "装依赖", "迁移", "部署", "发布", "push", "上传",
    "发送邮件", "发消息", "网络请求", "权限", "delete", "install", "migrate", "deploy",
)


def _text(step: Any) -> str:
    if isinstance(step, str):
        return step.strip()
    if isinstance(step, dict):
        return str(step.get("step") or step.get("text") or step.get("title") or "").strip()
    return ""


def _stable_id(*parts: str) -> str:
    raw = "\x1f".join(parts).encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()[:16]


def step_kind(title: str) -> str:
    """从步骤文本推断类型。命中不了的一律按 edit：宁可高估风险，不可漏审。"""
    low = title.lower()
    for kind, marks in _KIND_RULES:
        if any(mark in low for mark in marks):
            return kind
    return "edit"


def step_risk(title: str, kind: str) -> str:
    low = title.lower()
    if any(mark in low for mark in _HIGH_RISK_MARKS):
        return "high"
    if kind in ("edit", "repair"):
        return "medium"
    return "low"


def build_plan(
    steps: Iterable[Any],
    *,
    goal_id: str = "",
    scenario: str = "dev",
    max_steps: int = 30,
    max_seconds: int = 1800,
    token_budget: int = 150_000,
) -> dict[str, Any]:
    """生成 v165 §4.2 的计划结构。

    ``steps`` 同时吃 ``list[str]``（意图节点刚产出）和 ``list[dict]``
    （``update_plan`` 回写的 plan_steps），旧数据不需要转换。
    """
    items = [item for item in steps if _text(item)][:12]
    plan_id = _stable_id("plan", goal_id, scenario, *(_text(item) for item in items))
    previous_id = ""
    structured: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        title = _text(item)[:200]
        kind = step_kind(title)
        risk = step_risk(title, kind)
        step_id = f"step-{index}"
        structured.append(
            {
                "id": step_id,
                "title": title,
                "kind": kind,
                "tools": list(_TOOLS_BY_KIND[kind]),
                "risk": risk,
                "depends_on": [previous_id] if previous_id else [],
                "success_condition": _success_condition(kind, title),
            }
        )
        previous_id = step_id
    return {
        "goal_id": goal_id,
        "plan_id": plan_id,
        "scenario": scenario,
        "steps": structured,
        "max_steps": max_steps,
        "max_seconds": max_seconds,
        "token_budget": token_budget,
        "approval_points": [item["id"] for item in structured if item["risk"] == "high"],
        "rollback_strategy": ROLLBACK_STRATEGY,
        "revision": 0,
        "early_verify": False,
    }


def _success_condition(kind: str, title: str) -> str:
    if kind == "inspect":
        return f"拿到「{title[:40]}」所需的文件与上下文"
    if kind == "verify":
        return "测试或校验命令真实执行且退出码为 0"
    if kind == "repair":
        return "上一轮失败不再复现，且重新验证通过"
    return f"「{title[:40]}」已落盘且可在 diff 中确认"


def plan_signature(plan: dict[str, Any] | None) -> str:
    """只按步骤签名：预算/审批点变化不算「计划改版」，否则每轮收尾都会 +1。"""
    steps = [(_text(item.get("title")), item.get("kind")) for item in (plan or {}).get("steps", [])]
    return hashlib.sha1(json.dumps(steps, ensure_ascii=False).encode()).hexdigest()[:16]


def revise_plan(
    plan: dict[str, Any] | None,
    *,
    failure: dict[str, Any] | None = None,
    repair_round: int = 1,
    goal_already_met: bool = False,
) -> dict[str, Any]:
    """按真实执行结果改版（v165 §4.3），带硬上限 §4.4。

    返回**新 dict**，不改动传入对象：图状态靠替换传递，原地改会让 LangGraph
    的 checkpoint 比较失效。
    """
    current = dict(plan or build_plan([]))
    next_plan = {**current, "steps": [dict(item) for item in current.get("steps", [])]}
    revision = int(current.get("revision") or 0)

    if goal_already_met:
        # 已经满足目标就提前进验证，不继续无意义执行。
        return {**next_plan, "early_verify": True, "revision_reason": "目标已满足，提前验证"}

    if repair_round < 1 or not failure:
        return next_plan

    if revision >= MAX_PLAN_REVISIONS:
        return {
            **next_plan,
            "exhausted": True,
            "revision_reason": f"计划改版已达上限 {MAX_PLAN_REVISIONS}，停止自动修复",
        }

    failure_kind = str(failure.get("kind") or "")
    detail = str(failure.get("detail") or failure.get("reason") or "")[:160]
    failure_key = f"{failure_kind}:{detail[:80]}"
    if str(next_plan.get("last_failure_key") or "") == failure_key:
        # v165 §4.4「每次循环必须产生新证据」：同一个失败原样再来一次，
        # 说明上一轮修复什么也没改变。再追加一条换个编号的步骤只是把预算烧完。
        return {
            **next_plan,
            "no_new_evidence": True,
            "exhausted": True,
            "revision_reason": "相同失败重复出现且没有新证据，停止自动修复",
        }
    if failure_kind in ("file_missing", "not_found") or "不存在" in detail or "not found" in detail.lower():
        title = "重新定位目标文件，不重复盲读原路径"
        kind, tools = "inspect", list(_TOOLS_BY_KIND["inspect"])
    elif failure_kind in ("test_failed", "validation_failed", "timeout") or "测试" in detail:
        title = f"按失败输出定位根因并做最小范围修复（第 {repair_round} 轮）"
        kind, tools = "repair", list(_TOOLS_BY_KIND["repair"])
    elif failure_kind in ("dependency", "risk_expanded"):
        title = "暂停当前计划，等待用户补充依赖信息或重新审批"
        kind, tools = "inspect", ["request_user_input"]
    else:
        title = f"读取失败证据并调整执行路径（第 {repair_round} 轮）"
        kind, tools = "inspect", list(_TOOLS_BY_KIND["inspect"])

    # 同一类失败只保留一条修复步骤，更新轮次而不是叠加。否则预算内会塞满
    # 「第 1 轮 / 第 2 轮 / 第 3 轮」三条同义步骤，看起来在改版，其实没有新路径。
    existing = next(
        (
            item
            for item in next_plan["steps"]
            if item.get("source_failure") == failure_kind and item.get("kind") == kind
        ),
        None,
    )
    if existing is not None:
        existing["title"] = title
        existing["success_condition"] = _success_condition(kind, title)
    else:
        step_id = f"step-{len(next_plan['steps']) + 1}"
        next_plan["steps"].append(
            {
                "id": step_id,
                "title": title,
                "kind": kind,
                "tools": tools,
                "risk": step_risk(title, kind),
                "depends_on": [next_plan["steps"][-1]["id"]] if next_plan["steps"] else [],
                "success_condition": _success_condition(kind, title),
                "source_failure": failure_kind,
            }
        )
        if step_risk(title, kind) == "high":
            next_plan["approval_points"] = sorted({*(next_plan.get("approval_points") or []), step_id})
    next_plan["revision"] = revision + 1
    next_plan["revision_reason"] = f"{failure_kind or 'unknown'}：{detail}"[:240]
    next_plan["last_failure_key"] = failure_key
    next_plan["exhausted"] = False
    next_plan["no_new_evidence"] = False
    return next_plan


def repair_hint(failure: dict[str, Any], repair_round: int) -> str:
    """给执行节点的系统侧补充指令。

    红线：这是**追加**给模型的系统提示，绝不改写用户原话，也不拼到用户消息前面
    （v164 删掉场景模式前缀就是因为这个）。
    """
    detail = str(failure.get("detail") or failure.get("reason") or "")[:300]
    return (
        f"【系统：第 {repair_round} 轮自动修复】上一轮验证没有通过，失败证据：{detail or '未提供'}。"
        "请先用只读工具确认根因，再改最小范围；不要重跑完全相同的失败命令，"
        "不要声称已修复；改完必须重新跑一次验证。"
    )
