"""计划作为可更新对象。

对标（REA 拆解证据见 docs/REA-CODEX-CLAUDE.md）：
- Codex：`update_plan` 工具
- Claude Code：`EnterPlanMode` / `ExitPlanMode`

两家的共识是：计划不是一次性输出，而是执行过程中可增删、可勾选、可按用户插话追加的对象。
设计里"执行中插话并入当前计划"的交互就落在这个工具上。
"""

from __future__ import annotations

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
        )
    ]
