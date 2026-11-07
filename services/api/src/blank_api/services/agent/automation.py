"""v165 §8 三种自动化模式 + §13 审批矩阵。

两组概念必须分清，v0.9.1 的设置页把它们混在一起才让人觉得「全放行没生效」：

* ``permissions.default_mode`` = **要不要问**（审批节奏，四档）
* ``background.automation_mode`` = **要不要自己动**（自动化程度，三档）

自动驾驶只能放宽「要不要自己动」，绝不能放宽「要不要问」。所有硬闸都写在
``approval_matrix()`` 里，且不读权限档位以外的任何可放宽开关——这样即使
将来某个调用点忘了传 mode，高风险动作仍然会落到 ask。
"""

from __future__ import annotations

from typing import Any

AUTOMATION_MODES = ("manual", "assist", "autonomous")
MODE_LABELS = {
    "manual": "手动：我点执行才动，高风险逐步审批",
    "assist": "辅助（默认）：低风险自动做，高风险仍需审批",
    "autonomous": "自动驾驶：连续执行与自动修复，高风险仍需审批",
}

DECISION_ALLOW = "allow"
DECISION_ASK = "ask"
DECISION_DENY = "deny"
HIGH_RISKS = frozenset({"high", "critical"})

# §1.3：本版本默认不开启的动作。判 deny 而不是 ask，是因为「审批一次就放行」
# 会让它们以「用户点过同意」的名义常态化，那不是这一版要给的承诺。
DEFAULT_OFF_ACTIONS = frozenset(
    {
        "outside_workspace",
        "mass_delete",
        "external_send",
        "prod_deploy",
        "privilege_escalation",
        "sensitive_upload",
    }
)

# §13 高风险动作：任何模式、任何权限档位都要审批。
HIGH_RISK_ACTIONS = frozenset(
    {
        "delete_file",
        "install_dependency",
        "db_migration",
        "network_access",
        "git_push",
        "deploy",
        "permission_change",
    }
)


def resolve_mode(settings: dict[str, Any] | None, goal_mode: str = "") -> str:
    """设置优先于目标上的存档值；非法值一律回落 assist，不静默沿用脏数据。"""
    background = (settings or {}).get("background") or {}
    raw = str(background.get("automation_mode") or "").strip()
    if raw in AUTOMATION_MODES:
        return raw
    fallback = str(goal_mode or "").strip()
    return fallback if fallback in AUTOMATION_MODES else "assist"


def effective_policy(mode: str, permission_mode: str = "default") -> dict[str, Any]:
    """三模式 × 权限档位合成的执行边界。

    ``auto_start`` 与 ``allow_write`` 分开：手动模式是「不自动开跑」，
    plan 档是「压根不许写」，两件事被一个布尔表达就会互相顶掉。

    plan 档**仍然自动开跑**：用户选的是「只许读、不许写」，不是「别动」。
    让它连只读分析都要人点一下执行，正是 v165 要消灭的「一直要我催」。
    写动作由 PermissionPolicy 在 plan 档直接拒绝，不靠这里拦启动。
    """
    m = mode if mode in AUTOMATION_MODES else "assist"
    p = str(permission_mode or "default")
    plan_only = p == "plan"
    return {
        "mode": m,
        "permission_mode": p,
        "auto_start": m != "manual",
        "allow_write": not plan_only,
        "allow_background": m != "manual",
        "allow_auto_repair": m in ("assist", "autonomous"),
        "allow_multi_step": m in ("assist", "autonomous"),
        "high_risk_always_asks": True,
        "notes": [
            MODE_LABELS[m],
            "plan 档只读但会自动开始分析，写动作一律拒绝" if plan_only else "",
        ],
    }


def approval_matrix(
    *,
    mode: str = "assist",
    permission_mode: str = "default",
    risk: str = "low",
    action: str = "",
    irreversible: bool = False,
    automation_allowed: bool = True,
) -> dict[str, Any]:
    """v165 §13：意图 → 范围 → 工具 → 风险 → 审批 → 执行 的最后一道判据。

    返回 ``decision`` 与 ``bypassable``；``bypassable=False`` 表示这一条不许被
    任何「全放行 / 自动驾驶」设置改写，调用方应直接按 decision 执行。
    """
    m = mode if mode in AUTOMATION_MODES else "assist"

    if action in DEFAULT_OFF_ACTIONS:
        return {
            "decision": DECISION_DENY,
            "bypassable": False,
            "reason": f"v0.10.0 默认不自动执行「{action}」，需另行显式开启",
        }
    if not automation_allowed:
        return {
            "decision": DECISION_DENY,
            "bypassable": False,
            "reason": "自动化模式不允许执行此动作",
        }
    if irreversible or action in HIGH_RISK_ACTIONS or risk in HIGH_RISKS:
        # 关键：这一条排在 MODE_BYPASS 之前。bypass 只免「低风险要问」，
        # 不免「高风险必须问」；autonomous 同理。
        return {
            "decision": DECISION_ASK,
            "bypassable": False,
            "reason": "高风险操作需要用户审批，自动驾驶与全放行都不能跳过",
        }
    if permission_mode == "plan":
        return {
            "decision": DECISION_ASK,
            "bypassable": False,
            "reason": "计划模式只做只读，写操作需要退出计划模式",
        }
    if m == "manual" and risk == "medium":
        return {
            "decision": DECISION_ASK,
            "bypassable": True,
            "reason": "手动模式：中等风险动作先确认",
        }
    return {
        "decision": DECISION_ALLOW,
        "bypassable": True,
        "reason": f"{m} 模式：低风险动作自动执行",
    }


def is_high_risk(tool: str, arguments: dict[str, Any] | None = None) -> bool:
    """从工具名与参数文本粗判高风险，用于下发前的展示与告警。

    真正的判定仍在 PermissionPolicy（命令级、不可绕过），这里只是让监督者
    在「还没执行」时就能把 needs_approval 标出来，不要等工具撞墙再回。
    """
    return bool(classify_action(tool, arguments))


def classify_action(tool: str, arguments: dict[str, Any] | None = None) -> str:
    """把一次工具调用映射成 §13 的高风险动作名；低风险返回空串。

    这张表刻意只收**已经在 ``is_high_risk`` 里被认过的**那几个签名：普通
    ``curl``、``npm test``、``git status`` 都不在其中，否则每一条只读命令都会
    被自动驾驶模式额外弹一次审批。返回值直接喂给 ``approval_matrix(action=)``，
    因此必须落在 HIGH_RISK_ACTIONS 里，不能落到 DEFAULT_OFF_ACTIONS（那条会
    判 deny——批量删除在现有产品里由「不可逆命令」闸门负责，不在这里改语义）。
    """
    if tool == "delete_file":
        return "delete_file"
    if tool != "run_shell":
        return ""
    text = str((arguments or {}).get("command") or "").lower()
    if not text:
        return ""
    for mark, action in (
        ("rm -rf", "delete_file"),
        ("del /s", "delete_file"),
        ("pip install", "install_dependency"),
        ("npm i ", "install_dependency"),
        ("yarn add", "install_dependency"),
        ("migrate", "db_migration"),
        ("drop table", "db_migration"),
        ("git push", "git_push"),
        ("deploy", "deploy"),
    ):
        if mark in text:
            return action
    return ""
