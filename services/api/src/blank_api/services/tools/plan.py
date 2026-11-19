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

from blank_api.services.permissions import MODE_ACCEPT_EDITS, MODE_DEFAULT, MODE_PLAN
from blank_api.services import plan_mode
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
    # 对齐 Claude TodoWrite：同一时刻只能有一个 in_progress。多个「正在做」等于
    # 没有正在做——用户看不出现在推进的是哪一步，模型也容易同时拉开几条线。
    running = [str(item.get("step") or "") for item in steps if item.get("status") == "in_progress"]
    if len(running) > 1:
        return fail(
            "error",
            f"同一时刻只能有一个 in_progress，收到 {len(running)} 个：{running}。"
            "把其余仍在做的步骤改回 pending，完成一步就立即把它标成 completed。",
        )

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


def _plan_file(context: ToolContext) -> str:
    """本任务的计划文件（工作区相对路径）。拿不到根就退化成空串，工具照常工作。"""
    root = getattr(context, "root", None)
    task_id = getattr(context, "task_id", "")
    if root is None:
        return ""
    return plan_mode.plan_relpath(task_id)


async def _enter_plan_mode(context: ToolContext, arguments: dict) -> ToolOutcome:
    """进入计划模式：只读调研 + 一个可写的计划文件。

    这是**收紧**方向，所以策略层直接放行；工具负责把两件事都改到位：
    模式，以及"计划模式下唯一允许写的路径"。少做第二件，模型就会在计划模式里
    被自己的写操作挡住，然后把整段计划塞进对话正文——下一轮压缩就没了。
    """
    policy = _mode_policy(context)
    if policy is None:
        return fail("error", "当前运行时没有权限策略，无法切换计划模式")
    previous = policy.mode
    plan_file = _plan_file(context)
    if previous == MODE_PLAN:
        # 已经在计划模式（直接以 plan 档启动、或审批恢复回来）：只补路径，不动模式。
        # 路径必须补——少了它，模型在计划模式里连计划文件都写不了，只能把计划
        # 塞进对话正文，下一轮压缩就没了。
        if plan_file and not getattr(policy, "plan_file", ""):
            policy.plan_file = plan_file
        return ok(
            "已经在计划模式：只读取证，写文件与执行命令都会被拒绝"
            + (f"（计划文件 `{plan_file}` 除外）。" if plan_file else "。"),
            mode=MODE_PLAN,
            previous=previous,
            changed=False,
            plan_path=plan_file,
        )
    # 记下从哪个模式进来的：退出时要回到它，而不是一律回 default。
    policy.pre_plan_mode = previous
    policy.plan_file = plan_file
    policy.mode = MODE_PLAN
    turn_state = getattr(context, "turn_state", None)
    if isinstance(turn_state, dict):
        # 本会话里退出过计划模式、现在又进来：提醒先读旧计划，别从零重想。
        turn_state["plan_reentry"] = bool(turn_state.pop("plan_ever_exited", False))
    exists = bool(plan_file) and plan_mode.plan_exists(context.root, context.task_id)
    head = plan_mode.file_section(plan_file, exists) if plan_file else ""
    return ok(
        "已进入计划模式：接下来只做只读取证，写文件与执行命令都会被拒绝"
        + (f"，唯一可写的是计划文件 `{plan_file}`。" if plan_file else "。")
        + "\n\n"
        + head
        + "调研完成后调用 exit_plan_mode 交计划：不传 plan 时它会直接读这个文件。"
        "这一轮只能用 request_user_input（澄清）或 exit_plan_mode（交计划）结束。",
        mode=MODE_PLAN,
        previous=previous,
        changed=True,
        plan_path=plan_file,
    )


async def _exit_plan_mode(context: ToolContext, arguments: dict) -> ToolOutcome:
    """退出计划模式：把计划交出去、回到进入之前的权限模式。

    放宽方向，策略层在计划模式下会为此弹审批卡（`mode:switch`）；工具本身不判断
    用户批没批——走到这里就说明审批门已经过了。

    计划来源有两处，按可靠程度取：卡片上带来的 ``plan`` 参数优先（用户可能改过它），
    其次是计划文件（模型边调研边写的那一份）。两处都空说明它根本没写计划，
    此时**不退出**：让一张空计划过审，用户看到的就是一张没有内容的批准卡。
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
    plan_file = str(getattr(policy, "plan_file", "") or "") or _plan_file(context)
    plan = str(arguments.get("plan") or "").strip()[: plan_mode.MAX_PLAN_CHARS]
    from_file = False
    if not plan and plan_file:
        plan = plan_mode.read_plan(context.root, context.task_id)
        from_file = bool(plan)
    if not plan:
        return fail(
            "error",
            "计划是空的：先把计划写进计划文件"
            + (f" `{plan_file}`" if plan_file else "")
            + "（步骤、要改的文件、怎么验证），再调用 exit_plan_mode。"
            "不传 plan 参数时工具会自动读这个文件。",
        )
    previous = policy.mode
    restore = str(getattr(policy, "pre_plan_mode", "") or "")
    policy.mode = restore if restore in (MODE_DEFAULT, MODE_ACCEPT_EDITS, MODE_PLAN) else MODE_DEFAULT
    policy.pre_plan_mode = ""
    # 退出提醒靠 turn_state 传递：工具拿不到运行时会话，而"刚退出"这个事实必须
    # 在这一轮的下一次模型调用里被说出来，否则模型会继续按只读那套自我约束。
    turn_state = getattr(context, "turn_state", None)
    if isinstance(turn_state, dict):
        turn_state["plan_exited"] = plan_file
        turn_state["plan_ever_exited"] = True
    return ok(
        "计划已提交：\n" + plan + "\n\n"
        + f"已退出计划模式，恢复到 `{policy.mode}`；接下来的写操作按该模式的规则判定。"
        + "第一步先把这份计划落成待办（update_plan），之后每完成一步立刻勾掉。",
        mode=policy.mode,
        previous=previous,
        changed=True,
        plan=plan,
        plan_path=plan_file,
        plan_from_file=from_file,
    )


def plan_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="update_plan",
            description=(
                "创建或更新当前任务的执行计划。计划是可更新对象："
                "开始执行前先给出步骤；执行中把已完成的标为 completed、把正在做的标为 "
                "in_progress；用户中途插话要求新内容时追加步骤。"
                "同一时刻**只有一个步骤是 in_progress**；一步做完就立刻标 completed，"
                "不要等都做完才回头补勾。"
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
                        # 两种写法都收：一句话（给人看），或 {text, check}（能自动核对）。
                        # check 只认三种，全部由验收器只读事实核对：文件在不在、
                        # 文件里有没有那段字、某条命令这轮跑过且退出码为 0。
                        "items": {
                            "anyOf": [
                                {"type": "string"},
                                {
                                    "type": "object",
                                    "properties": {
                                        "text": {"type": "string", "description": "这条判据说的是什么"},
                                        "check": {
                                            "type": "object",
                                            "properties": {
                                                "type": {
                                                    "type": "string",
                                                    "enum": ["path_exists", "file_contains", "command"],
                                                },
                                                "path": {"type": "string", "description": "工作区相对路径"},
                                                "text": {"type": "string", "description": "file_contains 要找的文字"},
                                                "cmd": {"type": "string", "description": "command 要跑的命令，需与本轮实际执行的完全一致"},
                                            },
                                            "required": ["type"],
                                        },
                                    },
                                    "required": ["text"],
                                },
                            ]
                        },
                        "description": (
                            "可判定的完成标准。能写成可执行检查的就写成对象，验收时会逐条核对并告诉你"
                            "哪条没过；写不成检查的（比如「体验好」）就用字符串，验收时标为无法自动核对。"
                        ),
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
                "进入计划模式：先只读调研、把方案写成计划交用户批准，再动手实现。"
                "非平凡的实现任务优先用它——先对齐方案比写完再返工便宜。\n"
                "**什么时候用**（命中任意一条就该用）：\n"
                "1. 加新功能（按钮放哪、点击后做什么、错误怎么提示都可能要定）；\n"
                "2. 存在多种可行方案（加缓存可以是 Redis / 内存 / 文件）；\n"
                "3. 要改现有行为（登录流程、重构某个组件）；\n"
                "4. 需要做架构选择（WebSocket vs SSE vs 轮询）；\n"
                "5. 预计要动 3 个以上文件；\n"
                "6. 需求不清楚，得先看清代码才能定范围；\n"
                "7. 实现方式取决于用户偏好——本来想用 request_user_input 问方案，"
                "改用这个：计划模式能先看代码再带着上下文给选项。\n"
                "**什么时候不用**：单行/几行的明确修改、加一个需求写死的函数、"
                "用户已经给了非常具体的要求、纯调研（只看不改，直接答）。\n"
                "拿不准就进计划模式：对齐一次比返工一次便宜。\n"
                "进入后只允许只读动作，唯一可写的是计划文件；这一轮只能用 "
                "request_user_input（澄清）或 exit_plan_mode（交计划）结束。"
            ),
            input_schema={"type": "object", "properties": {}},
            risk_level="low",
            handler=_enter_plan_mode,
        ),
        ToolDefinition(
            name="exit_plan_mode",
            description=(
                "退出计划模式，把计划交给用户批准后开始实现。仅在计划模式里可用。\n"
                "**用法**：把计划写进计划文件后调用它；不传 plan 参数时它会读计划文件。"
                "计划里要有：步骤、将要改哪些文件、怎么验证（跑什么命令、看到什么算过）。\n"
                "**不要用它做别的事**：只是读代码回答问题、没有要实现的东西时不要调用；"
                "纯调研任务不要调用。\n"
                "**不要用 request_user_input 问「这个计划行不行」**——那正是这个工具做的事，"
                "而且它会给出可编辑、可批准的卡片。用户批准后按计划开始实现，"
                "第一步先用 update_plan 把计划落成待办。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "plan": {
                        "type": "string",
                        "description": (
                            "完整计划：步骤、涉及的文件、验收方式。"
                            "省略时读取计划文件的内容。"
                        ),
                    }
                },
            },
            risk_level="low",
            handler=_exit_plan_mode,
        ),
    ]
