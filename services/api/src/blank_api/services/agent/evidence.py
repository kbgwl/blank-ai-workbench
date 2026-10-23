"""交付证明包：验收条件、文件变更与 Diff、测试结论、用量与事件时间线。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from blank_api.api.errors import ApiError
from blank_api.models.model_provider import ModelProvider, ModelUsage
from blank_api.models.task import AgentEvent, FileChange, Task, ToolCall


async def load_task(session: AsyncSession, task_id: str) -> Task:
    task = await session.get(Task, task_id)
    if task is None:
        raise ApiError(code="task_not_found", message="任务不存在", status_code=404)
    return task


def _sole(values) -> str:
    """一条线程的父线程 / 根线程 / 分支路径按构造只有一个值。

    出现多个值说明归属本身在飘——那种时候宁可读成"没说"（空串），
    也不能从两个里随便挑一个冒充整条线程的出身。
    """
    distinct = sorted({str(value or "") for value in values} - {""})
    return distinct[0] if len(distinct) == 1 else ""


def _usage_totals(rows) -> dict:
    prompt = sum(row.prompt_tokens for row in rows)
    completion = sum(row.completion_tokens for row in rows)
    return {
        "calls": len(rows),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "cached_tokens": sum(row.cached_tokens for row in rows),
        "cache_write_tokens": sum(row.cache_write_tokens for row in rows),
        "reasoning_tokens": sum(row.reasoning_tokens for row in rows),
    }


def turn_rollup(rows) -> list[dict]:
    """按轮（run_id）把账本行合起来 —— "这一轮花了多少"就是这张表。

    `run_id` 为空的行**不进这里**：那是回合归属上线前落下的账，把它们并成"第 0 轮"
    等于把没记过的说成记过。这些行有多少条由 `unattributed_calls` 单独说出来。
    """
    grouped: dict[str, list] = {}
    for row in rows:
        run_id = str(getattr(row, "run_id", "") or "")
        if not run_id:
            continue
        grouped.setdefault(run_id, []).append(row)
    return [
        {
            "run_id": run_id,
            "threads": len({row.trace_id for row in group if row.trace_id}),
            "subagent_calls": sum(1 for row in group if str(getattr(row, "node", "") or "") == "subagent"),
            **_usage_totals(group),
        }
        for run_id, group in sorted(grouped.items())
    ]


def branch_rollup(rows) -> list[dict]:
    """按线程（trace_id）汇总，并当场说清"这条分支是谁引起的"。

    父子关系原来只写在事件 payload 里，钱表上只有一串看不出源头的 trace_id；
    现在 parent / root / agent_path 就在行上，不用把事件流走一遍才能回答。
    """
    grouped: dict[str, list] = {}
    for row in rows:
        trace_id = str(row.trace_id or "")
        if not trace_id:
            continue
        grouped.setdefault(trace_id, []).append(row)
    return [
        {
            "trace_id": trace_id,
            "agent_path": _sole(row.agent_path for row in group),
            "parent_thread_id": _sole(getattr(row, "parent_trace_id", "") for row in group),
            "root_thread_id": _sole(getattr(row, "root_trace_id", "") for row in group),
            "run_id": _sole(getattr(row, "run_id", "") for row in group),
            **_usage_totals(group),
        }
        for trace_id, group in sorted(grouped.items())
    ]


def parallel_group_rollup(rows) -> list[dict]:
    """按并行编排批次（parallel_group）把账本行合起来 —— "那一次 fan_out 一共花了多少"。

    原来这个问题只能靠事件流重放：`subagents.planned` 说了这批派了几个，
    每个 `subagent.completed` 说了自己花了多少，但"这批合计多少"没有任何一行直接答得出，
    而 `agent_path` 里那个 `par-{group}-{i}` 是要人再解析一遍的字符串。
    批次号 #45 起成为账本的一等列，这里就当场按它算，不再从别处推。

    `parallel_group` 为空的行**不进这里**：单发子代理、主线程、摘要都不属于任何一批，
    批次号上线前落下的老行同样是空（那是"当时没记"，不是"第 0 批"），把它们并成一批
    等于给它们编了一个批次号；这一类有多少条由 `usage.batched_calls` /
    `usage.unbatched_calls` 两个数分开说。
    """
    grouped: dict[str, list] = {}
    for row in rows:
        group = str(getattr(row, "parallel_group", "") or "")
        if not group:
            continue
        grouped.setdefault(group, []).append(row)
    return [
        {
            "group": group,
            "run_id": _sole(getattr(row, "run_id", "") for row in batch),
            "threads": len({row.trace_id for row in batch if row.trace_id}),
            "agent_paths": sorted({str(row.agent_path or "") for row in batch if row.agent_path}),
            **_usage_totals(batch),
        }
        for group, batch in sorted(grouped.items())
    ]


def window_turn_rollup(items) -> list[dict]:
    """把一窗留下的历史引用按出生轮合起来 —— "这一窗里谁写的话最多"就是这张表。

    窗口链（#42）和轮账（#43）原来各说各的：前者只知道"第几窗替换了第几窗"，后者只知道
    "哪一轮花了多少钱"，跨不过去 —— 这正是 §7.7/§7.8 各自列的那条未覆盖。引用里现在带了
    `run`，这里就能把它折回轮的身份：不用抄正文，也不用新增副本账（数都从引用当场算）。

    `run` 为空的条目**不进这里**：那是回合归属上线前写下的历史，并成"第 0 轮"就是把没记过
    的说成记过；它们有多少条由同级的 `unattributed` 单独说出来。
    """
    grouped: dict[str, dict] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        run_id = str(item.get("run") or "")
        if not run_id:
            continue
        entry = grouped.setdefault(
            run_id,
            {"run_id": run_id, "items": 0, "first_window": None, "max_windows_held": 0},
        )
        entry["items"] += 1
        # 同一轮里最老的那条决定这窗第一次见到谁；跨窗最多的那条给出"至少留了几窗"。
        since = item.get("retained_since")
        if isinstance(since, int) and (entry["first_window"] is None or since < entry["first_window"]):
            entry["first_window"] = since
        held = item.get("retained_windows")
        if isinstance(held, int) and held > entry["max_windows_held"]:
            entry["max_windows_held"] = held
    return [grouped[run_id] for run_id in sorted(grouped)]


async def build_evidence(
    session_factory: async_sessionmaker[AsyncSession], task_id: str
) -> dict[str, Any]:
    async with session_factory() as session:
        task = await load_task(session, task_id)
        events = (
            (
                await session.execute(
                    select(AgentEvent).where(AgentEvent.task_id == task_id).order_by(AgentEvent.seq)
                )
            )
            .scalars()
            .all()
        )
        calls = (
            (
                await session.execute(
                    select(ToolCall).where(ToolCall.task_id == task_id).order_by(ToolCall.created_at)
                )
            )
            .scalars()
            .all()
        )
        changes = (
            (
                await session.execute(
                    select(FileChange).where(FileChange.task_id == task_id).order_by(FileChange.created_at)
                )
            )
            .scalars()
            .all()
        )
        usage_rows = (
            (
                await session.execute(
                    select(ModelUsage, ModelProvider.name)
                    # 左连接而不是内连接：供应商行被删或指向空 provider_id 的用量记录，
                    # 钱是真花出去了。用内连接会让这些行从求和里静默消失，证据包就说
                    # "这个任务没花 token"，而事件里的任务级同期明明记着这笔账。
                    .outerjoin(ModelProvider, ModelProvider.id == ModelUsage.provider_id)
                    .where(ModelUsage.task_id == task_id)
                )
            )
            .all()
        )

    acceptance = list(task.acceptance_results or [])
    criteria = list(task.acceptance_criteria or [])
    if criteria and not acceptance:
        acceptance = [
            {"criterion": item, "covered": False, "evidence": "任务未产出验收结论"} for item in criteria
        ]
    validation = _latest_validation(events)
    # 审批请求里的权限上下文（模式 / 命中规则 / 建议前缀 / 作用域选项）在中断事件里；
    # 前端要拿它渲染「仅本次 / 总是允许 git status / 总是允许 run_shell」。
    approval_context: dict[str, Any] = {}
    for event in reversed(events):
        if event.type in ("approval.required", "approval.requested") and isinstance(event.payload, dict):
            approval_context = dict(event.payload)
            break
    pending_approvals = [
        {
            "tool_call_id": call.id,
            "tool": call.name,
            "arguments": call.arguments,
            "risk_level": call.risk_level,
            "message": call.result_summary,
            **_approval_lifecycle(approval_context.get("expires_at")),
            **_approval_hints(approval_context, call.name, call.arguments),
        }
        for call in calls
        if call.status == "awaiting_approval"
    ]
    if not pending_approvals and task.status == "waiting_approval":
        # 图在工具网关之前就被 interrupt() 打断时不会留下 ToolCall 行，退回最近一次审批事件。
        for event in reversed(events):
            if event.type not in ("approval.required", "approval.requested"):
                continue
            payload = event.payload or {}
            pending_approvals.append(
                {
                    "tool_call_id": str(payload.get("tool_call_id") or ""),
                    "tool": str(payload.get("tool") or ""),
                    "arguments": payload.get("arguments") or {},
                    "risk_level": str(payload.get("risk_level") or "high"),
                    "message": "高风险操作等待人工审批",
                    **_approval_lifecycle(payload.get("expires_at") if isinstance(payload, dict) else None),
                    **_approval_hints(payload, str(payload.get("tool") or ""), payload.get("arguments") or {}),
                }
            )
            break
    # 提问与审批共用同一条中断通道：等待中的问题单独取出来，前端据此渲染问题卡。
    pending_questions: list[dict] = []
    question_expires_at = ""
    if task.status == "waiting_approval":
        for event in reversed(events):
            if event.type != "question.required":
                continue
            payload = event.payload or {}
            pending_questions = [
                dict(item) for item in payload.get("questions") or [] if isinstance(item, dict)
            ]
            question_expires_at = str(payload.get("expires_at") or "")
            break
    context_summary = {
        "active_tokens": 0,
        "compactions": 0,
        "micro_trims": 0,
        "tokens_saved": 0,
        "boundary": 0,
        # 工具输出瘦身省下的字符：不是压缩省下的，是"根本没塞进去"。
        # 两个口径必须分开报，否则会把"少发一遍"算成"压缩得多"。
        "tool_chars_saved": 0,
        # 窗口链（对标 §4.1 的 window_number / window_id / previous_window_id）。
        # 一条记录 = 一次真压缩；老任务没有这些字段，链就是空的，不补一个"第 1 窗"进去。
        "windows": [],
        "dropped": 0,
    }
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.type == "context.measured":
            context_summary["active_tokens"] = int(payload.get("active_context_tokens_after") or 0)
        elif event.type == "context.compacted":
            context_summary["compactions"] += 1
            context_summary["tokens_saved"] += int(payload.get("tokens_saved") or 0)
            context_summary["boundary"] += int(payload.get("folded") or 0)
            record = payload.get("replacement") or {}
            # 只搬"数得出来的那部分"：逐条引用留在事件里，证据包里给条数与窗口身份。
            # 把 items 再抄一遍就是两处会写歪的账（与 #30 不给 ToolCall 加列同一个理由）。
            dropped = len(record.get("dropped") or []) if isinstance(record, dict) else 0
            context_summary["dropped"] += dropped
            window_number = payload.get("window_number")
            if window_number is not None:
                items = record.get("items") if isinstance(record, dict) else None
                items = items if isinstance(items, list) else []
                turns = window_turn_rollup(items)
                context_summary["windows"].append(
                    {
                        "seq": event.seq,
                        "window_number": int(window_number),
                        "window_id": str(payload.get("window_id") or ""),
                        "previous_window_id": str(payload.get("previous_window_id") or ""),
                        "compaction_response_id": str(payload.get("compaction_response_id") or ""),
                        "retained": int((record.get("retained") or 0) if isinstance(record, dict) else 0),
                        "dropped": dropped,
                        "items": int(len(items)),
                        # 这一窗留下的历史分别是哪几轮写的；缺的条数单独说，不当 0 轮。
                        "turns": turns,
                        "turns_unattributed": len(items) - sum(turn["items"] for turn in turns),
                    }
                )
        elif event.type == "context.microcompacted":
            context_summary["micro_trims"] += 1
            context_summary["tokens_saved"] += int(payload.get("tokens_saved") or 0)
        elif event.type == "context.trimmed":
            # 累计值只取最后一个，避免把每一步的累计数再加一遍。
            context_summary["tool_chars_saved"] = int(payload.get("total_saved_chars") or 0)
    # 每次调用跑在哪个后端，只有事件里才有（tool.completed 带 tool_call_id）。
    # 不给 ToolCall 表加列：台账记「做了什么」，执行边界记「在哪做的」，两者各自有主；
    # 证据包在读出时把边界贴回工具清单，省一次迁移，也少一处会写歪的副本。
    call_backend = {
        str(payload.get("tool_call_id")): {
            k: v for k, v in (("sandbox_backend", payload.get("sandbox_backend")),
                              ("sandbox_enforcement", payload.get("sandbox_enforcement")))
            if k in payload
        }
        for payload in (event.payload for event in events if event.type == "tool.completed")
        if isinstance(payload, dict)
        and "sandbox_backend" in payload
        and payload.get("tool_call_id")
    }
    prompt_tokens = sum(row.prompt_tokens for row, _ in usage_rows)
    # 治理快照：回看任务时要能回答「这一步当时被什么策略管着、中途换过几次」。
    # Codex 的 rollout 每轮都写 turn_context，靠的就是「策略变了要留痕」这件事。
    # 第一条给全量（含 sandbox/features/permissions），之后的变更只给差异，
    # 免得一份证据包里重复贴几十份同样的 300 字节。
    snap_events = [
        event for event in events if event.type == "turn.context" and isinstance(event.payload, dict)
    ]
    snapshots = [event.payload for event in snap_events]
    governance_summary: dict[str, Any] = {
        "snapshots": len(snapshots),
        "first": snapshots[0] if snapshots else None,
        "changes": [
            {
                "seq": event.seq,
                "step": event.payload.get("step"),
                "node": event.node,
                "model": event.payload.get("model"),
                "tier": event.payload.get("tier"),
                "permission_mode": event.payload.get("permission_mode"),
            }
            for event in snap_events[1:]
        ],
        "models": sorted({str(item.get("model") or "") for item in snapshots if item.get("model")}),
        "modes": sorted({str(item.get("permission_mode") or "") for item in snapshots}),
    }
    # 环境快照（对标 Codex world_state）：每条都是当时那个世界的**全量**，
    # 所以最后一条就是终态，不必往前拼增量；条数本身说明中途环境换过几次。
    world_events = [event for event in events if event.type == "world.state" and isinstance(event.payload, dict)]
    world_summary: dict[str, Any] = {
        "snapshots": len(world_events),
        "current": world_events[-1].payload if world_events else None,
        "changes": [
            {
                "seq": event.seq,
                "node": event.node,
                "date": (event.payload.get("state") or {}).get("current_date"),
            }
            for event in world_events[1:]
        ],
    }
    completion_tokens = sum(row.completion_tokens for row, _ in usage_rows)
    # 缓存与推理必须单独成项：§3 那条判断（"打开提示词缓存"不等于"缓存真的命中"）
    # 只有把 cached 摊开跟 prompt 并排放才证得出来。
    cached_tokens = sum(row.cached_tokens for row, _ in usage_rows)
    cache_write_tokens = sum(row.cache_write_tokens for row, _ in usage_rows)
    reasoning_tokens = sum(row.reasoning_tokens for row, _ in usage_rows)
    usage_events = [
        event for event in events if event.type == "model.usage" and isinstance(event.payload, dict)
    ]
    # 三级口径的事实形状只在事件里有（一次调用同时写单次/回合/任务），
    # 证据包取最后一条原样带出；老任务没有这类事件就是 None，不补一个像是算过的数。
    last_usage_record = usage_events[-1].payload if usage_events else None
    models = sorted({row.model for row, _ in usage_rows if row.model})
    providers = sorted({name for _, name in usage_rows if name})
    trace_ids = sorted({row.trace_id for row, _ in usage_rows if row.trace_id})
    unverified = [
        f"验收条件未验证：{item['criterion']}"
        for item in acceptance
        if not item.get("covered")
    ]
    if not validation:
        unverified.append("未执行测试验证")
    elif validation.get("status") == "skipped":
        # 跳过校验有很多种原因，有的无关紧要（没改文件，校验本就不适用），
        # 有的很要命（沙箱不可用、命令没授权、还在等审批）。后三类必须说出来：
        # 用户会以为测试跑过了，而实际上一行都没跑。
        reason = str(validation.get("skipped_reason") or "")
        if reason and "无需执行测试" not in reason:
            unverified.append(f"未做自动化校验：{reason}")
    if pending_approvals:
        unverified.append(
            f"存在待审批的高风险操作：{', '.join(sorted({a['tool'] for a in pending_approvals}))}"
        )
    return {
        "task": {
            "id": task.id,
            "project_id": task.project_id,
            "title": task.title,
            "goal": task.goal,
            "status": task.status,
            "steps_used": task.steps_used,
            "max_steps": task.max_steps,
            "result_summary": task.result_summary,
            "failure_reason": task.failure_reason,
            "created_at": _iso(task.created_at),
            "updated_at": _iso(task.updated_at),
        },
        "acceptance": acceptance,
        "changes": {
            "count": len(changes),
            "files": [change.path for change in changes],
            "reversible": all(bool(change.backup_path) or change.change_type == "created" for change in changes),
            "diff": "\n".join(change.diff for change in changes if change.diff),
            "items": [
                {
                    "path": change.path,
                    "change_type": change.change_type,
                    "backup_path": change.backup_path,
                    "created_at": _iso(change.created_at),
                }
                for change in changes
            ],
        },
        "validation": validation,
        "governance": governance_summary,
        "world": world_summary,
        "usage": {
            "providers": providers,
            "models": models,
            "trace_ids": trace_ids,
            "calls": len(usage_rows),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cached_tokens": cached_tokens,
            "cache_write_tokens": cache_write_tokens,
            "reasoning_tokens": reasoning_tokens,
            # 命中率按"输入里有多少走的是缓存价"算，prompt 为 0 时给 0（不是 null）：
            # 没有调用就没有命中率，这本身是事实，不是缺数据。
            "cache_hit_ratio": round(cached_tokens / prompt_tokens, 4) if prompt_tokens else 0.0,
            "records": len(usage_events),
            # 回合归属的两张汇总表：按轮（"这一轮花了多少"）与按线程（"这条分支是谁引起的"）。
            # 数字全部来自账本行本身，不是从事件 payload 里再推一遍——两处各推一次早晚会对不上。
            "turns": turn_rollup([row for row, _ in usage_rows]),
            "branches": branch_rollup([row for row, _ in usage_rows]),
            "unattributed_calls": sum(1 for row, _ in usage_rows if not str(getattr(row, "run_id", "") or "")),
            # 按并行编排批次汇总：一次 fan_out 花了多少，从这里直接读，不用把 branches 表加一遍。
            "parallel_groups": parallel_group_rollup([row for row, _ in usage_rows]),
            "batched_calls": sum(1 for row, _ in usage_rows if str(getattr(row, "parallel_group", "") or "")),
            "unbatched_calls": sum(1 for row, _ in usage_rows if not str(getattr(row, "parallel_group", "") or "")),
            # 派给子代理的那部分（#45 起也算进主线程的预算）。给两个口径是为了能核对：
            # 这里是按账本行现数，budget.* 事件里那一份是按图状态累计，两个数各说各的
            # 就没有人发现其中一个是假的。
            "delegated_calls": sum(1 for row, _ in usage_rows if str(row.node or "") == "subagent"),
            "delegated_tokens": sum(
                int(row.prompt_tokens or 0) + int(row.completion_tokens or 0)
                for row, _ in usage_rows
                if str(row.node or "") == "subagent"
            ),
            "levels": (
                {
                    "call": last_usage_record.get("call"),
                    "turn": last_usage_record.get("turn"),
                    "task": last_usage_record.get("task"),
                }
                if last_usage_record
                else None
            ),
            # 模型累计耗时（毫秒），前端「耗时」行读这个字段；不填就只会显示「暂无」。
            "latency_ms": sum(row.latency_ms for row, _ in usage_rows),
        },
        "tools": [
            {
                "id": call.id,
                "trace_id": call.trace_id,
                "node": call.node,
                "name": call.name,
                "arguments": call.arguments,
                "status": call.status,
                "risk_level": call.risk_level,
                "duration_ms": call.duration_ms,
                "summary": call.result_summary,
                "artifact_path": call.artifact_path,
                "created_at": _iso(call.created_at),
                **call_backend.get(call.id, {}),
            }
            for call in calls
        ],
        "pending_approvals": pending_approvals,
        "pending_questions": pending_questions,
        "question_expires_at": question_expires_at,
        # 问题卡和审批卡共用同一条中断通道、同一套 TTL，前端要的「还能不能答」
        # 也必须由同一个判定给，别让它自己解析时间串。
        "question_expired": approval_is_expired(question_expires_at),
        "context": context_summary,
        "unverified_risks": unverified,
        "timeline": [
            {
                "seq": event.seq,
                "node": event.node,
                "type": event.type,
                "payload": event.payload,
                "created_at": _iso(event.created_at),
            }
            for event in events
        ],
    }


def approval_is_expired(expires_at: object, *, now: datetime | None = None) -> bool:
    """审批/提问项是否已经过了 TTL。

    过期判定只能有一处：写侧（提交审批时拒绝并 409）和读侧（`/state` 里的
    `approval_state`）若各算各的，界面就会照「待审批」渲染一个必然 409 的按钮——
    真模型点验实测到了：成员挂起后 5 分钟没人点，`/state` 仍报「待审批」，
    再批就是 `approval_expired`，整轮看着还能救、其实只能重跑。

    没有时间戳＝不按 TTL 拦（写侧同样放行）；时间戳读不懂＝点不成，读侧算过期，
    写侧会先把它当成非法时间报 409，两侧都拿不到能批准的结论。
    """
    if not expires_at:
        return False
    try:
        expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= (now or datetime.now(timezone.utc))


def _approval_lifecycle(expires_at: object) -> dict[str, Any]:
    """审批卡的状态字段。读侧和写侧（`_reject_expired`）必须给同一个答案，
    否则界面会给出一个点了必然 409 的按钮。"""
    expired = approval_is_expired(expires_at)
    return {
        "approval_state": "已过期" if expired else "待审批",
        "expired": expired,
        "expires_at": expires_at or None,
    }


def _approval_hints(context: dict, tool: str, arguments) -> dict[str, Any]:
    """把审批事件里的权限上下文摘到待审批项上；上下文不属于该工具时留空，不编造。"""
    if not context or str(context.get("tool") or "") != tool:
        return {}
    hints: dict[str, Any] = {
        "permission_mode": context.get("permission_mode") or "",
        "permission_rule": context.get("permission_rule") or "",
        "command_prefix": context.get("command_prefix") or "",
        "suggested_prefix": context.get("suggested_prefix") or "",
        "approval_options": context.get("approval_options") or [],
    }
    if not context.get("arguments") and arguments:
        # 事件里没带参数时，用调用记录补一份，前端才能渲染「总是允许 X」的实际命令。
        hints["arguments_preview"] = arguments
    return hints


def _latest_validation(events: list[AgentEvent]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.type == "validation.completed":
            return dict(event.payload or {})
    return None


def _iso(value) -> str:
    return value.isoformat(timespec="seconds") if value else ""
