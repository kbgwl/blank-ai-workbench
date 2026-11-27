"""任务创建与派发快照：从 HTTP 路由层拆出的第一批领域动作。

这一层只依赖模型、设置与派发服务，不反向导入路由。路由层保留同名导入，
现有调用方（worker、IM 入站、测试）不需要同时改地址。
"""

from __future__ import annotations

import logging
import traceback
import uuid
from dataclasses import replace

from sqlalchemy import select

from blank_api.api.errors import ApiError
from blank_api.db import db_retry, get_session_factory
from blank_api.deps import DbSession
from blank_api.models.plan_template import PlanTemplate
from blank_api.models.task import AgentEvent, Task
from blank_api.models.trigger import AgentTrigger
from blank_api.schemas.task import TaskCreate
from blank_api.services.agent import notify
from blank_api.services.agent.dispatch import Budget, NOTIFY_APPROVAL_ONLY, explain, route
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.background_policy import budget_override, resolve_background
from blank_api.services.features import is_enabled
from blank_api.services.plan import normalize_criteria, normalize_goal
from blank_api.services.settings import SettingsStore, quiet_hours_active
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


async def _record_run_failure(task_id: str, run_id: str, error: BaseException) -> None:
    """把「跑不起来」写进事件流和任务状态。

    之前这里只把 agent_runs 标成 failed，任务自己停在 queued、事件流里一条都没有——
    界面上就是「输入之后没有任何返回」，用户既看不到原因也点不动。凡是异常都必须
    变成一条 task.failed 事件 + failed 状态。
    """
    message = str(error) or error.__class__.__name__
    detail = "".join(traceback.format_exception(type(error), error, error.__traceback__))[-4000:]
    try:
        # runtime 正常收尾时自己就会写 task.failed（带 reason），别写第二条。
        async with get_session_factory()() as session:
            already = (
                await session.execute(
                    select(AgentEvent.id).where(
                        AgentEvent.task_id == task_id,
                        AgentEvent.run_id == run_id,
                        AgentEvent.type == "task.failed",
                    )
                )
            ).first()
        if already is None:
            await EventRecorder(get_session_factory(), task_id, run_id=run_id).add(
                "task.failed",
                "error",
                {
                    "error": message,
                    "reason": message,
                    "error_type": error.__class__.__name__,
                    "traceback": detail,
                },
            )
    except Exception:  # 记事件本身失败也不能把进程带崩
        log.exception("写入 task.failed 事件失败 task=%s", task_id)

    async def mark_failed() -> None:
        async with get_session_factory()() as session:
            task = await session.get(Task, task_id)
            if task is not None and task.status not in ("cancelled", "succeeded"):
                task.status = "failed"
                task.failure_reason = message[:2000]
                await session.commit()

    try:
        # 连接池里可能躺着一条被取消打断的旧连接，换新连接重试即可
        await db_retry(mark_failed)
    except Exception:
        log.exception("更新任务失败状态出错 task=%s", task_id)


# 审批决定里真正交给 LangGraph 的字段。``approval_id`` / ``decided_seq`` 是内部
# 记账，不能混进 Command(resume=...)，否则图的恢复载荷会多出它不认识的键。
_RESUME_GRAPH_KEYS = ("approve", "scope", "feedback", "step_id")


def _graph_decision(decision: dict) -> dict:
    return {key: decision[key] for key in _RESUME_GRAPH_KEYS if key in decision}


async def _resume_decision_delivered(task_id: str, decision: dict) -> bool:
    """这条审批决定是否已经被投递给图。

    唯一凭据是 ``task.resumed`` 事件的序号：它由 ``runtime._resume`` 在
    ``graph.ainvoke`` 之前写，且位于对应 ``approval.decided`` 之后。没有这个
    序号（老数据）时按“尚未投递”处理——宁可重投一次决定，也不能让用户批过的
    任务停住；新写入的决定一定带 ``decided_seq``。
    """
    decided_seq = int(decision.get("decided_seq") or 0)
    if decided_seq <= 0:
        return False
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(AgentEvent.id)
                .where(
                    AgentEvent.task_id == task_id,
                    AgentEvent.type == "task.resumed",
                    AgentEvent.seq > decided_seq,
                )
                .limit(1)
            )
        ).first()
    return row is not None


def _notify_kind(status: str) -> str:
    """任务终态对应哪一类通知事件。

    waiting_approval 走审批（永远放行），failed/cancelled 走失败，succeeded 走交付物。
    其余中间态算进度——策略配了 always 才会打扰。
    """
    if status == "waiting_approval":
        return notify.EVENT_APPROVAL
    if status in ("failed", "cancelled"):
        return notify.EVENT_FAILURE
    if status == "succeeded":
        return notify.EVENT_DELIVERABLE
    return notify.EVENT_PROGRESS


async def _origin_from_trigger(session, task_id: str) -> str:
    """快照里没有 ``intent`` 时，从触发器行反推"这一次是谁起的"。

    ``super_agent_dispatch`` 关着时快照是空的（见 ``_record_notify_decision``
    里那段注释），但"定时到点"和"盯着工作区"起的那一轮，触发器行里明明白白
    写着 intent。不补这一步，所有桌面通知都只能写"做完了"——用户设了九点的
    提醒，到点弹出来的却是一句"任务完成"，读起来根本不像提醒。

    ``release_one`` 要求触发器必须已关联 task_id 才放行，所以这个反查是可靠的：
    查不到的说明它不是触发器起的（人自己发的），返回空串即可。
    """
    try:
        row = (
            await session.execute(
                select(AgentTrigger)
                .where(AgentTrigger.task_id == task_id)
                .order_by(AgentTrigger.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
    except Exception:  # noqa: BLE001 - 反查失败只影响文案分档，不该影响通知本身
        return ""
    if row is None:
        return ""
    return str((row.payload or {}).get("intent") or "")


async def _record_notify_decision(task_id: str, status: str) -> None:
    """把「这一次该不该打扰用户」写成事件，并把它真的投递出去（任务书 §5）。

    判断与投递分开，但**都在这里收口**：``agent/notify.py`` 只回答该不该出声，
    ``channels/delivery.py`` 只负责发。任何一个通道发失败都不影响任务终态，
    也不会写坏判定——判定是事实（"策略说要打扰"），投递结果另记一条事件。

    v0.10.3 起这里多了一件事：IM 里问的那句话，结论要回到原会话。它不看
    notify 策略——用户在聊天里问了一句，答案就该出现在聊天里，那不叫打扰。
    """
    title = goal = detail = ""
    disturb = False
    kind = notify.EVENT_PROGRESS
    origin = ""
    try:
        async with get_session_factory()() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            snapshot = dict(task.dispatch_snapshot or {})
            background = SettingsStore().read().get("background") or {}
            window = str(background.get("quiet_hours") or "")
            quiet = quiet_hours_active(window)
            kind = _notify_kind(status)
            # 策略的来源顺序（2026-11-08 修）：任务快照 > 设置里的「通知时机」> approval_only。
            #
            # 以前只有第一档，没有就硬兜 approval_only——而 ``super_agent_dispatch``
            # 默认是**关**的（BETA），关着时快照恒为 ``{}``。于是设置页那个「通知时机」
            # 下拉框选了 milestone / always 也毫无作用，桌面通知永远按"只报审批"走，
            # 用户选了"全都说"却什么都收不到。这是"设置项没人消费"的典型，
            # 不是"默认保守"就能解释过去的。
            policy = str(snapshot.get("notify_policy") or "").strip().lower()
            if not policy:
                policy = str(background.get("notify_policy") or "").strip().lower() or NOTIFY_APPROVAL_ONLY
            decision = notify.explain(policy, kind, quiet_hours=quiet)
            # 先把要发的内容取出来：会话关掉之后还要用它拼消息，而 Task 实例
            # 在会话外读字段会触发一次同步懒加载。
            title = str(task.title or "")
            goal = str(task.goal or "")
            detail = str(task.failure_reason or task.result_summary or "")
            # 策略说要打扰 ≠ 可以主动发。只有"用户自己设的定时/触发器"或"他显式改了
            # 通知策略"才允许推；默认的 approval_only 是产品兜底，不是用户的请求。
            disturb = notify.may_push(
                disturb=bool(decision["disturb"]), policy=policy, origin=origin
            )
            # 「这一次是谁起的」——定时到点、工作区变了、还是人自己发的。
            # 桌面通知要靠它决定标题写「提醒：检查部署」还是「任务已完成」：
            # 同一句"任务完成"扣在定时提醒上，读起来就不像提醒了。
            origin = str(snapshot.get("intent") or "")
            if not origin:
                origin = await _origin_from_trigger(session, task_id)
            await EventRecorder(get_session_factory(), task_id).add(
                "notify.decided",
                "finish",
                {
                    **decision,
                    "status": status,
                    "origin": origin,
                    "would_disturb": bool(decision.get("disturb")),
                    # 推送闸的结论单独记：事后能回答"策略说要发，为什么没发"。
                    "allowed_to_push": bool(disturb),
                },
            )
    except Exception:  # 通知判定是旁路，坏了也不能把任务的终态带走
        log.warning("记录通知判定失败 task=%s", task_id, exc_info=True)
        return

    from blank_api.services.channels import delivery

    # ── 1. 提醒推到开了推送的通道 ────────────────────────────────────────
    results: list[dict] = []
    if disturb:
        try:
            results = await delivery.fanout(
                delivery.notification_text(title=title, goal=goal, kind=kind, detail=detail)
            )
        except Exception:  # noqa: BLE001 - 投递失败只影响"有没有收到"，不影响任务
            log.warning("通道投递失败 task=%s", task_id, exc_info=True)
    if results:
        try:
            await EventRecorder(get_session_factory(), task_id).add(
                "notify.delivered",
                "finish",
                {
                    "delivered": results,
                    "ok": sum(1 for item in results if item.get("ok")),
                    "failed": sum(1 for item in results if not item.get("ok")),
                },
            )
        except Exception:
            log.warning("记录投递结果失败 task=%s", task_id, exc_info=True)

    # ── 2. 结论回到消息进来的那个会话 ────────────────────────────────────
    if status in ("succeeded", "failed", "cancelled", "waiting_approval"):
        try:
            sent = await delivery.reply_to_source(
                task_id,
                delivery.reply_text(
                    status=status, summary=detail if status == "succeeded" else "",
                    failure=detail if status != "succeeded" else "", task_id=task_id,
                ),
            )
            if sent is not None:
                await EventRecorder(get_session_factory(), task_id).add(
                    "channel.replied", "finish", sent
                )
        except Exception:  # noqa: BLE001
            log.warning("回话失败 task=%s", task_id, exc_info=True)
