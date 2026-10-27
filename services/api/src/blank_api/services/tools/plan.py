"""计划作为可更新对象。

对标（REA 拆解证据见 docs/REA-CODEX-CLAUDE.md）：
- Codex：`update_plan` 工具
- Claude Code：`EnterPlanMode` / `ExitPlanMode`

两家的共识是：计划不是一次性输出，而是执行过程中可增删、可勾选、可按用户插话追加的对象。
设计里"执行中插话并入当前计划"的交互就落在这个工具上。
"""

from __future__ import annotations

from blank_api.services.permissions import MODE_DEFAULT, MODE_PLAN
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok

MAX_STEPS = 12
MAX_STEP_CHARS = 200
PLAN_STATUSES = ("pending", "in_progress", "completed")
_MARKS = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}


def normalize_steps(raw: object) -> list[dict]:
    """容错解析：既接受 ["步骤一", "步骤二"]，也接受 [{"step":..., "status":...}]。"""
    steps: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            text, status = item, "pending"
        elif isinstance(item, dict):
            text = str(item.get("step") or item.get("text") or "")
            status = str(item.get("status") or "pending")
        else:
            continue
        text = text.strip()[:MAX_STEP_CHARS]
        if not text:
            continue
        steps.append({"step": text, "status": status if status in PLAN_STATUSES else "pending"})
    return steps[:MAX_STEPS]


def render_plan(steps: list[dict]) -> str:
    return "\n".join(
        f"{_MARKS[step['status']]} {index}. {step['step']}" for index, step in enumerate(steps, start=1)
    )


async def _update_plan(context: ToolContext, arguments: dict) -> ToolOutcome:
    steps = normalize_steps(arguments.get("steps"))
    if not steps:
        return fail("error", "update_plan 至少需要一个步骤")
    completed = sum(1 for step in steps if step["status"] == "completed")
    return ok(
        render_plan(steps),
        steps=steps,
        explanation=str(arguments.get("explanation") or "").strip()[:300],
        total=len(steps),
        completed=completed,
    )


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
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "step": {"type": "string"},
                                "status": {"type": "string", "enum": list(PLAN_STATUSES)},
                            },
                            "required": ["step"],
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
