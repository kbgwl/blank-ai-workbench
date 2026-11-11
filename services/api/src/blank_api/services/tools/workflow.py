"""workflow 工具：用一段 JavaScript 确定性编排多个子代理（对齐 Claude Workflow）。

判据来自 Claude 的 Workflow 契约（工具原文）：只有用户明确要求多代理编排时才可
调用；脚本必须以 ``export const meta`` 纯字面量开头；``scriptPath`` 优先于
``script`` 与 ``name``；``args`` 原样暴露；``resumeFromRunId`` 同会话内复用未变的
``(prompt, options)``。执行本体在 ``services/agent/workflow.py``，这里只做工具面：
找运行时、判 opt-in、解析来源、落盘、启动后台运行并立即交还句柄。
"""

from __future__ import annotations

from blank_api.models.task import Task
from blank_api.services.agent import workflow as workflow_module
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok

# 缺 Node 时的补齐方式：与 K 批 run_tests 的结构化缺口同一口径——先说缺什么，
# 再说怎么补，最后才让人接手。
NODE_GAP = (
    "本机没有可用的 Node.js，工作流无法执行（不是脚本写错了）。"
    "先补齐再重跑：安装 Node 20+（winget install OpenJS.NodeJS.LTS / "
    "choco install nodejs-lts / 或从 nodejs.org 下载），"
    "或用环境变量 BLANK_NODE_BIN 指向已有的 node.exe。"
    "确实装不上，再告诉用户缺什么、怎么装；不要改用 spawn_agents 冒充工作流。"
)

# 与 registry.ALIASES 保持一致：这里只用于「用户原话有没有明确点这门能力」。
# opt-in 判定不读任务正文、不读模型自己的描述——只认用户写下的目标。
OPINION_NOTE = (
    "Workflow 只在用户明确要求多代理编排时使用：用户原话要出现 "
    "「use a workflow / 工作流 / 多代理 / fan out / orchestrate / ultracode」之类的请求，"
    "或者点名要跑某个已保存的工作流。仅仅「这件事适合并行」不算 opt-in。"
    "现在用户没有这样要求：先用一句话说明多代理工作流能做什么、大概花多少，"
    "问用户要不要跑——不要自行开工。"
)


def _text(arguments: dict, key: str) -> str:
    value = arguments.get(key)
    return value.strip() if isinstance(value, str) else ""


def _opted_in(goal: str) -> bool:
    haystack = (goal or "").lower()
    return any(word in haystack for word in workflow_module.OPT_IN_WORDS)


async def _current_task(context: ToolContext) -> Task | None:
    if context.session_factory is None or not context.task_id:
        return None
    async with context.session_factory() as session:
        return await session.get(Task, context.task_id)


def _node_gap() -> ToolOutcome:
    return ToolOutcome(
        ok=False,
        status="skipped",
        message=NODE_GAP,
        data={
            "missing_command": "node",
            "skipped_reason": "本机没有 Node.js，工作流没有执行",
        },
    )


def _resolve_script(context: ToolContext, arguments: dict) -> tuple[str, str]:
    """按 scriptPath > script > name 取脚本文本，返回 ``(脚本, 来源说明)``。"""
    script_path = _text(arguments, "scriptPath")
    if script_path:
        return workflow_module.read_script_path(context.root, script_path), script_path
    script = arguments.get("script")
    if isinstance(script, str) and script.strip():
        return script, "script"
    name = _text(arguments, "name")
    if name:
        return workflow_module.load_named_script(context.root, name), f"name={name}"
    raise workflow_module.WorkflowError(
        "需要 script（内联脚本）、scriptPath（工作区里的 .mjs 文件）或 name（预定义工作流）之一"
    )


async def _start(context: ToolContext, arguments: dict) -> ToolOutcome:
    runner = getattr(context, "subagent_runner", None)
    if runner is None or not hasattr(runner, "spawn_background"):
        return fail("error", "当前运行时没有启用后台子代理：工作流需要它来执行每个 agent() 阶段")
    row = await _current_task(context)
    if row is None:
        return fail("error", "找不到当前任务，无法确定工作流挂在哪个项目下")
    if not _opted_in(f"{row.title}\n{row.goal}"):
        return fail("error", OPINION_NOTE)

    node = workflow_module.find_node()
    if not node:
        return _node_gap()

    try:
        script, source = _resolve_script(context, arguments)
        meta, body = workflow_module.parse_meta(script)
    except workflow_module.WorkflowError as error:
        return fail("error", str(error))

    resume_id = _text(arguments, "resumeFromRunId")
    resume_cache: dict | None = None
    if resume_id:
        if workflow_module.RESUME_PATTERN.fullmatch(resume_id) is None:
            return fail("error", "resumeFromRunId 必须是 wf_ 开头的运行 id（^wf_[a-z0-9-]{6,}$）")
        prior = workflow_module.get_run(row.id, resume_id)
        if prior is not None and prior.status == "running":
            return fail("error", f"上一次运行 {resume_id} 还在跑：先 task_stop(task_id=\"{resume_id}\") 再续跑")
        cached = workflow_module.get_cache(resume_id)
        if cached is None:
            return fail(
                "error",
                f"进程内没有运行 {resume_id} 的缓存，无法续跑（服务重启或缓存已过期）；请从头运行",
            )
        resume_cache = dict(cached)

    workflow_id = workflow_module.new_id()
    try:
        script_path = workflow_module.persist_script(context.root, workflow_id, script)
    except OSError as error:
        return fail("error", f"工作流脚本写不进工作区：{error}")

    run = workflow_module.start_workflow(
        task_id=row.id,
        meta=meta,
        body=body,
        args=arguments.get("args"),
        script_path=script_path,
        runner=runner,
        recorder=context.recorder,
        node=node,
        resume_cache=resume_cache,
        workflow_id=workflow_id,
    )
    if context.recorder is not None:
        try:
            await context.recorder.add(
                "workflow.started",
                "workflow",
                {
                    "workflow_id": run.workflow_id,
                    "name": run.name,
                    "phases": [item.get("title") for item in meta.get("phases") or []],
                    "resumed_from": resume_id,
                    "args_provided": arguments.get("args") is not None,
                    "script_path": script_path,
                    "source": source,
                },
            )
        except Exception:  # 事件是旁路，不能把启动本身带崩
            pass

    phases = "、".join(item.get("title", "") for item in meta.get("phases") or [] if item.get("title"))
    phase_note = f"，阶段：{phases}" if phases else ""
    return ok(
        f"已在后台启动工作流 {run.workflow_id}（{run.name}）：{run.description}{phase_note}。"
        f"脚本已保存到 {script_path}，改脚本后可用 scriptPath 重跑。"
        f"它还在运行；用 task_output(task_id=\"{run.workflow_id}\") 取结论，"
        f"不需要了用 task_stop(task_id=\"{run.workflow_id}\") 取消。"
        f"结束时会收到系统通知；结论由你转述给用户。",
        workflow_id=run.workflow_id,
        name=run.name,
        description=run.description,
        status="running",
        script_path=script_path,
        phases=[item.get("title") for item in meta.get("phases") or []],
        resumed_from=resume_id,
    )


def workflow_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="workflow",
            description=(
                "执行一段 JavaScript 工作流脚本，确定性地编排多个子代理（对齐 Claude Workflow）。"
                "它在后台跑：立即返回 wf_ 开头的运行 id，结束时你会收到系统通知，"
                "结论用 task_output 取、task_stop 取消。"
                "【仅限用户明确要求多代理编排】用户原话要出现「use a workflow / 工作流 / 多代理 / "
                "fan out / orchestrate / ultracode」或点名某个已保存工作流；仅仅「适合并行」不算。"
                "没得到这样的要求时，先用一句话说明它能做什么、大概花多少，再问用户要不要跑。"
                "脚本必须以 `export const meta = { name, description, phases? }` 纯字面量开头"
                "（不要变量/调用/插值；phases 的 title 与 phase() 调用逐个对齐），"
                "正文用 agent() / parallel() / pipeline() / phase()。脚本是 JavaScript，不是 TypeScript。"
                "agent(prompt, {label, phase, schema, isolate, maxSteps}) 交给子代理："
                "schema 让结果按 JSON 返回并做骨架校验；parallel 接收函数数组并保序；"
                "pipeline 让每个 item 的各阶段串行、item 之间并发。"
                "脚本不能联网、不能写文件、不能起进程；需要外部资料交给 agent() 子代理。"
                "优先用 pipeline（一个维度审完就能立刻验），需要参数化时用 args 传真正的 JSON 值。"
                "默认规模指引：10 个子代理以内（硬上限 50）。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "script": {
                        "type": "string",
                        "maxLength": workflow_module.MAX_SCRIPT_CHARS,
                        "description": (
                            "自包含的工作流脚本：必须以 `export const meta = "
                            "{ name, description, phases }` 纯字面量开头，正文用 "
                            "agent()/parallel()/pipeline()/phase()"
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": "预定义工作流的名字（工作区 .blank/workflows/<name>.mjs）",
                    },
                    "description": {"type": "string", "description": "忽略：描述写在 meta 里"},
                    "title": {"type": "string", "description": "忽略：标题写在 meta 里"},
                    "args": {
                        "description": (
                            "原样暴露给脚本的全局 args；数组/对象要传真正的 JSON 值，"
                            "不要传 JSON 字符串（字符串会让 args.filter/args.map 失效）"
                        ),
                    },
                    "scriptPath": {
                        "type": "string",
                        "description": (
                            "工作区里的脚本路径（.mjs/.js），优先于 script 与 name。"
                            "每次运行都会把脚本存到 .blank/workflows/.runs/ 并在回执里返回路径，"
                            "迭代时改那个文件再用同一个 scriptPath 重跑"
                        ),
                    },
                    "resumeFromRunId": {
                        "type": "string",
                        "pattern": "^wf_[a-z0-9-]{6,}$",
                        "description": (
                            "从一次旧运行续跑：未变的 (prompt, options) 直接命中缓存，"
                            "只重跑改过或新增的调用。仅限本进程；续跑前先 task_stop 旧运行"
                        ),
                    },
                },
                "additionalProperties": False,
            },
            risk_level="high",
            handler=_start,
            timeout_seconds=60.0,
        )
    ]


__all__ = ["NODE_GAP", "workflow_tools"]
