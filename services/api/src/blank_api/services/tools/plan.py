"""计划 / 目标作为可更新对象。

对标（REA 拆解证据见 docs/REA-CODEX-CLAUDE.md）：
- Codex：`update_plan` 工具
- Claude Code：`EnterPlanMode` / `ExitPlanMode`

两家的共识是：计划不是一次性输出，而是执行过程中可增删、可勾选、可按用户插话追加的对象。
设计里"执行中插话并入当前计划"的交互就落在这个工具上。

与旧版的差别：步骤带上了稳定 id，并能归到里程碑下。id 稳定的意义是**引用能成立**——
里程碑按 id 收步骤、验收条件按 id 指步骤，每次都换一批随机 id 就等于把它们全指空。
归一化逻辑全在 `blank_api.services.plan`，这里只负责工具契约。
"""

from __future__ import annotations

from blank_api.services.permissions import MODE_DEFAULT, MODE_PLAN
from blank_api.services.plan import (
    MAX_STEPS,
    MILESTONE_STATUSES,
    PLAN_STATUSES,
    link_milestones,
    normalize_goal,
    normalize_milestones,
    normalize_steps,
    plan_progress,
    render_plan,
    resolve_milestone_status,
)
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok


def _current_plan(context: ToolContext) -> tuple[list[dict], list[dict]]:
    """取当前图状态里的计划。拿不到就当空——这是"第一次给计划"的正常路径。"""
    state = getattr(context, "plan_state", None)
    if isinstance(state, dict):
        steps = state.get("steps")
        milestones = state.get("milestones")
        return (
            list(steps) if isinstance(steps, list) else [],
            list(milestones) if isinstance(milestones, list) else [],
        )
    return [], []


async def _update_plan(context: ToolContext, arguments: dict) -> ToolOutcome:
    previous_steps, previous_milestones = _current_plan(context)

    # 里程碑：没给就沿用上一版（模型常常只改状态，不想重述一遍阶段划分）
    raw_milestones = arguments.get("milestones")
    milestones = (
        normalize_milestones(raw_milestones)
        if isinstance(raw_milestones, list)
        else list(previous_milestones)
    )

    steps = normalize_steps(arguments.get("steps"), previous=previous_steps)
    if not steps:
        return fail("error", "update_plan 至少需要一个步骤")

    milestones = resolve_milestone_status(link_milestones(steps, milestones), steps)
    progress = plan_progress(steps, milestones)

    return ok(
        render_plan(steps, milestones),
        steps=steps,
        milestones=milestones,
        explanation=str(arguments.get("explanation") or "").strip()[:300],
        progress=progress,
        total=progress["total"],
        completed=progress["completed"],
    )


async def _set_goal(context: ToolContext, arguments: dict) -> ToolOutcome:
    """把目标结构化：为什么做、算不算做完、不做什么。

    刻意不做成 update_plan 的一个字段：目标是**判据**，计划是**路径**。
    混在一起时，模型改计划顺手就把验收标准也改了，而用户从头到尾没看见。
    """
    spec = normalize_goal(arguments)
    if not spec["objective"]:
        return fail("error", "set_goal 需要 objective：这一轮到底要达成什么")

    lines = [f"目标：{spec['objective']}"]
    if spec["done_when"]:
        lines.append("算做完的标准：")
        lines.extend(f"  - {item}" for item in spec["done_when"])
    if spec["constraints"]:
        lines.append("约束：")
        lines.extend(f"  - {item}" for item in spec["constraints"])
    if spec["out_of_scope"]:
        lines.append("这次不做：")
        lines.extend(f"  - {item}" for item in spec["out_of_scope"])

    return ok("\n".join(lines), **spec)


def _mode_policy(context: ToolContext):
    return getattr(context, "permissions", None)


async def _enter_plan_mode(context: ToolContext, arguments: dict) -> ToolOutcome:
    """进入计划模式：只读调研，写操作与命令一律拒绝。

    这是**收紧**方向，所以策略层直接放行；工具只负责把状态改到位。
    """
    policy = _mode_policy(context)
    if policy is None:
        return fail("error", "当前运行时没有权限策略，无法切换计划模式")
    previous = policy.mode
    if previous == MODE_PLAN:
        return ok(
            "已经在计划模式：只读取证，写文件与执行命令都会被拒绝。",
            mode=MODE_PLAN,
            previous=previous,
            changed=False,
        )
    policy.mode = MODE_PLAN
    return ok(
        "已进入计划模式：接下来只做只读取证，写文件与执行命令都会被拒绝。"
        "调研完成后调用 exit_plan_mode 附上计划，交用户确认后再开始修改。",
        mode=MODE_PLAN,
        previous=previous,
        changed=True,
    )


async def _exit_plan_mode(context: ToolContext, arguments: dict) -> ToolOutcome:
    """退出计划模式：把计划交出去、恢复默认权限。

    放宽方向，策略层在计划模式下会为此弹审批卡（`mode:switch`）；工具本身不判断
    用户批没批——走到这里就说明审批门已经过了。
    """
    policy = _mode_policy(context)
    if policy is None:
        return fail("error", "当前运行时没有权限策略，无法切换计划模式")
    if policy.mode != MODE_PLAN:
        return ok(
            "当前不在计划模式，无需退出。",
            mode=policy.mode,
            previous=policy.mode,
            changed=False,
        )
    plan = str(arguments.get("plan") or "").strip()[:4000]
    previous = policy.mode
    policy.mode = MODE_DEFAULT
    head = f"计划已提交：\n{plan}\n\n" if plan else ""
    return ok(
        head + "已退出计划模式，恢复默认权限；接下来的写操作按默认规则逐条确认。",
        mode=MODE_DEFAULT,
        previous=previous,
        changed=True,
        plan=plan,
    )


def plan_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="update_plan",
            description=(
                "创建或更新当前任务的执行计划。计划是可更新对象："
                "开始执行前先给出步骤；执行中把已完成的标为 completed、把正在做的标为 "
                "in_progress；用户中途插话要求新内容时追加步骤。"
                "每次提交**完整步骤列表**（覆盖旧计划），不要只提交变化的部分。"
                "步骤可以带 id 保持身份（不改 id 就能保住里程碑与验收条件的引用）。"
                "工作分阶段时用 milestones 把步骤归组，用户才能看到"
                "「调研完了、正在实现」这种阶段感，而不是一串平铺的勾选框。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "maxItems": MAX_STEPS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "description": "沿用上一版的 id 以保住引用；新建步骤可省略",
                                },
                                "step": {"type": "string"},
                                "status": {"type": "string", "enum": list(PLAN_STATUSES)},
                                "milestone_id": {
                                    "type": "string",
                                    "description": "归到哪个里程碑（milestones 里的 id）",
                                },
                                "evidence": {
                                    "type": "string",
                                    "description": "标 completed 时说明凭什么算完成：改了哪个文件、跑了什么命令",
                                },
                            },
                            "required": ["step"],
                        },
                    },
                    "milestones": {
                        "type": "array",
                        "maxItems": 6,
                        "description": "阶段性目标。省略则沿用上一版。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "title": {"type": "string"},
                                "status": {"type": "string", "enum": list(MILESTONE_STATUSES)},
                                "step_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "也可以不写，改由步骤的 milestone_id 指过来",
                                },
                            },
                            "required": ["title"],
                        },
                    },
                    "explanation": {"type": "string"},
                },
                "required": ["steps"],
            },
            risk_level="low",
            handler=_update_plan,
        ),
        ToolDefinition(
            name="set_goal",
            description=(
                "把这一轮的目标结构化：objective（要达成什么）、done_when（怎么算做完）、"
                "constraints（必须遵守的限制）、out_of_scope（明确不做什么）。"
                "接到多步骤的工程任务时先调用它，再给 update_plan——"
                "验收阶段会拿 done_when 逐条判定，写清楚了才不会做完才发现理解偏了。"
                "用户的原话不要改写，objective 用你自己的话概括执行意图。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "objective": {"type": "string", "description": "一句话说清要达成什么"},
                    "done_when": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可判定的完成标准，最好是能跑命令验证的",
                    },
                    "constraints": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "技术选型、不许改的文件、兼容性要求等",
                    },
                    "out_of_scope": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "这次明确不做的部分，防止范围蔓延",
                    },
                },
                "required": ["objective"],
            },
            risk_level="low",
            handler=_set_goal,
        ),
        ToolDefinition(
            name="enter_plan_mode",
            description=(
                "进入计划模式：只做只读调研（读文件、搜索、看 git 状态），"
                "不写文件、不改配置、不执行命令。这是收紧操作，不需要审批。"
                "调研完成后调用 exit_plan_mode 附上计划，等用户确认后再动手。"
            ),
            input_schema={"type": "object", "properties": {}},
            risk_level="low",
            handler=_enter_plan_mode,
        ),
        ToolDefinition(
            name="exit_plan_mode",
            description=(
                "退出计划模式并把计划交给用户确认。参数 plan 写完整计划（步骤、"
                "将要改哪些文件、验收方式）。计划模式下调用会弹审批卡；"
                "用户批准后恢复默认权限，本步骤里不再执行其他写操作，下一步再开始。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "plan": {
                        "type": "string",
                        "description": "完整计划：步骤、涉及的文件、验收方式",
                    }
                },
            },
            risk_level="low",
            handler=_exit_plan_mode,
        ),
    ]
