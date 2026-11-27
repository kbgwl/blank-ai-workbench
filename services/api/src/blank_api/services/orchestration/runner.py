"""任务创建与派发快照：从 HTTP 路由层拆出的第一批领域动作。

这一层只依赖模型、设置与派发服务，不反向导入路由。路由层保留同名导入，
现有调用方（worker、IM 入站、测试）不需要同时改地址。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace

from blank_api.api.errors import ApiError
from blank_api.db import get_session_factory
from blank_api.deps import DbSession
from blank_api.models.plan_template import PlanTemplate
from blank_api.models.task import Task
from blank_api.schemas.task import TaskCreate
from blank_api.services.agent.dispatch import Budget, explain, route
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.background_policy import budget_override, resolve_background
from blank_api.services.features import is_enabled
from blank_api.services.plan import normalize_criteria, normalize_goal
from blank_api.services.settings import SettingsStore
from blank_api.services.uploads import UploadStore

log = logging.getLogger(__name__)


async def persist_task(
    db: DbSession,
    project_id: str,
    payload: TaskCreate,
    *,
    thread_id: str | None = None,
    turn_index: int = 0,
    command_info: dict | None = None,
) -> Task:
    """把一条任务写进库并发一条 task.created。

    独立任务和"会话里的一轮"共用它：``thread_id`` / ``turn_index`` 在建行时就带上，
    而不是建完再 UPDATE 一次——那次 UPDATE 会把服务端生成的 ``updated_at`` 置为过期，
    紧接着序列化它就会在异步会话里触发一次同步懒加载（MissingGreenlet）。

    ``created_at``/``updated_at`` 是服务端生成的，插入后 ORM 并不知道它们的值。
    SQLite 会用 RETURNING 顺手带回来，MySQL 8.4 的 INSERT 没有 RETURNING：不先读回，
    序列化时同样是一次同步懒加载。所以这里统一 refresh，两种方言走同一条路。
    """
    # 附件先过一遍存在性检查：前端传来的 id 可能是上一次上传后又被清理掉的，
    # 那种直接丢掉，不要带着一个指向空气的引用进任务。
    attachments = [
        ref.as_dict()
        for ref in UploadStore().resolve(project_id, [item.model_dump() for item in payload.attachments])
    ]
    # 验收条件与目标在这里归一化，而不是等 Agent 去猜：
    # 建行这一刻就把形状定下来，后面验收循环、证据页、前端读到的才是同一份东西。
    criteria = normalize_criteria(payload.acceptance_criteria)
    goal_spec = normalize_goal(payload.goal_spec, fallback=payload.goal or payload.title)
    # v0.10.1 §4：创建时用户选的权限档位钉进快照链。_run_request 的回落顺序是
    # 运行请求 → goal_spec.dispatch.permission_mode → 设置页默认；落在中间一层，
    # 之后重试/恢复不传模式也不会漂回全局默认。（threads 路径随后会整份覆盖
    # goal_spec.dispatch，两处口径一致，互不冲突。）
    if payload.permission_mode:
        goal_spec = {
            **goal_spec,
            "dispatch": {
                **(goal_spec.get("dispatch") or {}),
                "permission_mode": payload.permission_mode,
            },
        }

    # 套用计划模板：步骤原样带过来，但状态一律重置为 pending。
    # 模板是"怎么做事"，不是"做到哪了"——把完成态一起带过来会显示一份假进度。
    plan_steps: list[dict] = []
    milestones: list[dict] = []
    if payload.plan_template_id:
        template = await db.get(PlanTemplate, payload.plan_template_id)
        if template is None or template.project_id != project_id:
            raise ApiError("plan_template_not_found", "计划模板不存在", 404)
        plan_steps = [
            {**step, "status": "pending", "evidence": ""}
            for step in (template.steps or [])
            if isinstance(step, dict)
        ]
        milestones = [
            {**item, "status": "pending"}
            for item in (template.milestones or [])
            if isinstance(item, dict)
        ]
        if not payload.goal_spec and template.goal_spec:
            goal_spec = normalize_goal(template.goal_spec, fallback=payload.goal or payload.title)
        template.usage_count = int(template.usage_count or 0) + 1

    # 纯聊天是这一轮的固定属性，跟着任务走：重试/恢复/审批续跑时不能因为
    # 项目后来绑了目录就突然变成可操作电脑。normalize_goal 只留四个已知键，
    # 所以必须在归一化之后单独补写。
    if payload.chat_only:
        goal_spec = {**goal_spec, "chat_only": True}
    # 用户明确要求「不要动工具」也要跟着任务走：重试/恢复时不能因为项目有目录
    # 就重新把工具挂上来。
    if payload.no_tools:
        goal_spec = {**goal_spec, "no_tools": True}
    if payload.read_only:
        goal_spec = {**goal_spec, "read_only": True}
    if payload.list_only:
        goal_spec = {**goal_spec, "list_only": True}

    task = Task(
        id=str(uuid.uuid4()),
        project_id=project_id,
        provider_id=payload.provider_id,
        scenario=payload.scenario or "dev",
        title=payload.title,
        goal=payload.goal or payload.title,
        status="draft",
        acceptance_criteria=criteria,
        acceptance_results=[],
        goal_spec=goal_spec,
        plan_steps=plan_steps,
        milestones=milestones,
        attachments=attachments,
        max_steps=payload.max_steps,
        thread_id=thread_id,
        turn_index=turn_index,
        # 展开事实随行落库（见 models/task.py 的 expansion 注释）：提交钩子要读
        # 未打码的 command_args，而审计事件里的那份是打过码的。
        expansion=command_info or {},
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    await EventRecorder(get_session_factory(), task.id).add(
        "task.created",
        "receive_task",
        {
            "title": task.title,
            "acceptance_criteria": list(task.acceptance_criteria or []),
            "attachments": len(attachments),
        },
    )
    # 斜杠命令展开留痕：用户输入被换成了文件正文，事后要能回答"这句话是怎么
    # 变成这段提示词的"，所以把命中项与长度差单独记一条。
    if command_info:
        await EventRecorder(get_session_factory(), task.id).add(
            "command.expanded",
            "receive_task",
            {"task_id": task.id, **command_info},
        )
    await _record_dispatch(db, task)
    return task


async def _record_dispatch(db: DbSession, task: Task) -> None:
    """创建时算一次派发快照并落库（v162，特性开关 super_agent_dispatch）。

    为什么在**创建**这一刻算：派发方式（现在跑 / 排队 / 等事件 / 先问人）是用户
    点发送后立刻要给出的反应，等到模型判完意图才决定"这条该不该排队"已经晚了。
    为什么落库而不是留在内存：执行可能发生在另一个进程（独立 worker）或几小时后
    （断点恢复），那时内存里那份早没了，只剩库能回答「当时按什么权限派的」。

    开关关着时整段跳过：不写快照、不发事件，行为与引入 Router 之前一致。
    """
    if not is_enabled("super_agent_dispatch"):
        return
    if (task.dispatch_snapshot or {}).get("snapshot_id"):
        # 已有快照就不再覆盖：重复调用 persist 时重新路由会让"当时那份决定"漂移。
        return
    # 沙箱档位也一起钉进快照：它是"这件事跑在哪"的决定，事后传参改不了它（v164 §5.3）。
    # 读设置失败按默认档位走——比让整个任务建不出来更合理。
    try:
        default_tier = str(
            (SettingsStore().read().get("sandbox") or {}).get("default_tier") or "workspace"
        )
    except Exception:  # noqa: BLE001 - 设置读不出来时按默认档位，不影响建任务
        default_tier = "workspace"
    snapshot = route(
        task.goal or task.title,
        scenario=task.scenario or "dev",
        max_steps=int(task.max_steps or 0) or None,
        sandbox_tier=default_tier,
    )
    # v165 §14：设置页的 max_steps / max_seconds / token_budget / auto_recoveries
    # 折进这份快照。budget_override 逐项取 min，只能收紧；设置读不出来时不改。
    # route() 保持纯函数（同样的入参永远得到同样的快照），夹取只发生在落库这一层。
    try:
        background = resolve_background(SettingsStore().read())
        tightened = budget_override(background, snapshot.budget.as_dict())
        snapshot = replace(snapshot, budget=Budget(**tightened))
        snapshot.guard()
    except Exception:  # noqa: BLE001 - 设置读坏/夹取异常都不该让任务建不出来
        log.warning("后台预算夹取失败，按发布默认预算继续", exc_info=True)
    task.dispatch_snapshot = snapshot.as_dict()
    await db.commit()
    # commit 会让 ORM 实例过期，之后序列化 created_at/updated_at 会触发同步懒加载
    # （异步会话里直接 MissingGreenlet）。和 persist_task 里同一条规矩：写完读回。
    await db.refresh(task)
    await EventRecorder(get_session_factory(), task.id).add(
        "dispatch.decided",
        "receive_task",
        {
            "snapshot_id": snapshot.snapshot_id,
            "intent": snapshot.intent,
            "dispatch": snapshot.dispatch,
            "tool_policy": snapshot.tool_policy,
            "notify_policy": snapshot.notify_policy,
            "memory_policy": snapshot.memory_policy,
            "priority": snapshot.priority,
            "high_risk": snapshot.high_risk,
            "budget": snapshot.budget.as_dict(),
            "why": explain(snapshot),
        },
    )
