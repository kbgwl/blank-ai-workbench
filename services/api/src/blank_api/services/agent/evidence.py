"""交付证明包：验收条件、文件变更与 Diff、测试结论、用量与事件时间线。"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from blank_api.api.errors import ApiError
from blank_api.models.model_provider import ModelProvider, ModelUsage
from blank_api.models.task import AgentEvent, FileChange, Task, ToolCall
from blank_api.services.plan import normalize_criteria

# 证据包默认只带最近这么多条事件进 `timeline`。原来的做法是"全部塞进去"：
# 一个 116 步、581 条事件的任务，一次 `/evidence` 就要序列化几 MB——前端渲染卡死，
# 而其中用户真正要看的就是最后那一段。分页参数在路由上（`timeline_limit` /
# `timeline_before_seq`），要全量照样能要（`timeline_limit=2000`）。
DEFAULT_TIMELINE_LIMIT = 200
MAX_TIMELINE_LIMIT = 2000

# 工具调用与文件变更同样可能随长任务膨胀：一个几百步的任务会留下上千条 ToolCall、
# 成百个文件 diff。原来它们是「全量塞进证据包」，与早期时间线同一个坑（几 MB JSON、
# 前端渲染卡死）。这里给它们各自一个默认上限 + 游标分页，`*_meta` 把「还有多少没加载」
# 数出来，翻页界面才不会误以为看完了。要全量仍可显式要大上限。
DEFAULT_TOOLS_LIMIT = 500
MAX_TOOLS_LIMIT = 2000
DEFAULT_CHANGES_LIMIT = 500
MAX_CHANGES_LIMIT = 2000
# 拼接后的全量 diff 字符串同样会爆：单文件 diff 动辄几万字符，几百个文件就是数 MB。
# 给一个字符预算，超出部分不进 `diff` 字段，由 `changes_meta.diff_truncated` 说明漏了几条。
DIFF_CHAR_BUDGET = 200_000

# 汇总类字段需要的事件类型。这份清单不是"性能优化"，是**必须**：审批状态、验收结论、
# 上下文水位、边界归属、用量明细全部从事件里读，漏一个类型等于那一块静默变空。
# 守卫见 `tests/test_evidence_paging_v152.py::test_summary_types_cover_every_type_read_by_evidence`，
# 它扫这个文件的源码，新增的 `event.type == "..."` 若不在集合里会直接红。
SUMMARY_EVENT_TYPES = frozenset(
    {
        "approval.required",
        "approval.requested",
        "context.compacted",
        "context.measured",
        "context.microcompacted",
        "context.trimmed",
        "guardian.auto_approved",
        "guardian.reviewed",
        "model.usage",
        "question.required",
        "task.started",
        "tool.completed",
        "turn.context",
        "validation.completed",
        "world.state",
    }
)

# 汇总事件的单次装载上限（条）。类型有界但轮数无界；消费方全是「取最近」语义，
# 超限时保尾部（倒序取上限再回正序），小任务结果与不设限完全一致。
SUMMARY_EVENT_LIMIT = 2000



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


_ATTEMPT_RUN_ID = re.compile(r"^task:[^:\s]+:attempt:\d+$")


def run_id_kind(run_id: object) -> str:
    """给 `run_id` 定形状：空串 / 群轮 / 同步入口的 attempt 键 / 普通运行。

    账本里实测四种形状共存：空串（回合归属上线前）、``team-<轮>-<消息>``（群聊发言）、
    ``task:<uuid>:attempt:N``（同步入口兜底）、``agent_runs.id``（排队入口）。
    这里只做**读侧命名**，不改任何存量值——空串仍然是"当时没记"，不是第 0 轮。
    """
    value = str(run_id or "")
    if not value:
        return "unrecorded"
    if value.startswith("team-"):
        return "team"
    if _ATTEMPT_RUN_ID.match(value):
        return "attempt"
    return "run"


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
            "kind": run_id_kind(run_id),
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


def origin_rollup(rows) -> list[dict]:
    """按发起轮（origin_run_id）把账本行合起来 —— 跨成员任务的一整轮花销就是这张表。

    与 `turns` 的区别：一轮团队里有几个成员、几个 Task、几条 run，`turns` 会给出好几行；
    `origins` 把同一轮引起的全部钱收成一行。团队之外（顶层运行）发起轮就是 run 本身，
    两张表恰好同形——这不是巧合，是"发起轮=引起这棵树的那一轮"的定义。

    `origin_run_id` 为空的行**不进这里**：那是这一列上线前落下的账，并成"第 0 轮"
    等于替它编了一个归属。有多少条由 `usage.origin_unattributed_calls` 单独说。
    """
    grouped: dict[str, list] = {}
    for row in rows:
        origin = str(getattr(row, "origin_run_id", "") or "")
        if not origin:
            continue
        grouped.setdefault(origin, []).append(row)
    return [
        {
            "origin_run_id": origin,
            "run_ids": sorted({str(row.run_id) for row in group if row.run_id}),
            "threads": len({row.trace_id for row in group if row.trace_id}),
            "tasks": len({str(row.task_id) for row in group if row.task_id}),
            "subagent_calls": sum(1 for row in group if str(getattr(row, "node", "") or "") == "subagent"),
            **_usage_totals(group),
        }
        for origin, group in sorted(grouped.items())
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


def boundary_report(events: list[AgentEvent], started: dict) -> dict[str, Any]:
    """一次任务到底有哪些动作落在边界内、哪些在边界外。

    `boundary_kind` 是 M3 起每个 `tool.completed` 都带的字段。老任务没有这个键，
    它们的调用落进 `unattributed` —— 不猜成 `in_process`，也不从工具名反推：
    「当时没记」和「记了是进程内」是两件事。
    """
    counts = {"blocked": 0, "mcp_host_process": 0, "process": 0, "in_process": 0, "fs_broker": 0,
              "unattributed": 0}
    modes: set[str] = set()
    mcp_calls = 0
    profile: dict[str, Any] = {}
    # 单次越界（#115）：宿主执行的调用里，哪些是“绕过沙箱”而不是“本来就没沙箱”。
    # 计数与 tool_call_id 一起留，审计才能从证据包回到具体那一条命令。
    bypass_calls = 0
    bypass_tool_call_ids: list[str] = []
    # fs-broker（#105）单独三笔账：成功次数、失败按层计数、IPC 延迟的样本数与最大 p95。
    # 失败必须留层名（spawn/timeout/killed/protocol/...），"调用失败了"不够——要知道是哪一层坏的。
    broker_calls = 0
    broker_failures: dict[str, int] = {}
    broker_unknown_effects = 0
    broker_latency = {"samples": 0, "max_p95_ms": 0.0, "max_last_ms": 0.0}
    for event in events:
        if event.type == "turn.context" and isinstance(event.payload, dict):
            # 取最后一条治理快照的逐条授权：中途改过「仅限工作区」或换过档位，
            # 末次就是任务结束时生效的那一份。老任务没有这一列。
            item = event.payload.get("permission_profile")
            if isinstance(item, dict) and item:
                profile = item
            continue
        if event.type != "tool.completed" or not isinstance(event.payload, dict):
            continue
        kind = str(event.payload.get("boundary_kind") or "")
        if kind in counts and kind != "unattributed":
            counts[kind] += 1
        else:
            counts["unattributed"] += 1
        mode = str(event.payload.get("sandbox_mode") or "")
        if mode:
            modes.add(mode)
        if kind == "mcp_host_process":
            mcp_calls += 1
        if event.payload.get("sandbox_bypass") is True:
            bypass_calls += 1
            call_id = str(event.payload.get("tool_call_id") or "")
            if call_id:
                bypass_tool_call_ids.append(call_id)
        latency = event.payload.get("fs_broker_latency")
        if isinstance(latency, dict):
            broker_calls += 1
            broker_latency["samples"] = max(broker_latency["samples"], int(latency.get("samples") or 0))
            broker_latency["max_p95_ms"] = max(
                broker_latency["max_p95_ms"], float(latency.get("p95_ms") or 0.0)
            )
            broker_latency["max_last_ms"] = max(
                broker_latency["max_last_ms"], float(latency.get("last_ms") or 0.0)
            )
        error = event.payload.get("fs_broker_error")
        if isinstance(error, dict):
            layer = str(error.get("layer") or "unknown")
            broker_failures[layer] = broker_failures.get(layer, 0) + 1
            if str(error.get("side_effects") or "") == "unknown":
                # 请求进过 broker 却没拿到响应：动作可能已经落盘。证据包必须单列这一格，
                # 否则"broker 失败 = 这次什么都没干"会被读成结论。
                broker_unknown_effects += 1
    sandbox = started.get("sandbox") if isinstance(started, dict) else {}
    sandbox = sandbox if isinstance(sandbox, dict) else {}
    mcp = started.get("mcp") if isinstance(started, dict) else {}
    mcp = mcp if isinstance(mcp, dict) else {}
    return {
        "sandbox": {
            "tier": sandbox.get("tier", ""),
            "requested": sandbox.get("requested", ""),
            "backend": sandbox.get("backend", ""),
            "strength": sandbox.get("strength", ""),
            "degraded": sandbox.get("degraded"),
            "filesystem_enforced": sandbox.get("filesystem_enforced"),
            "network_enforced": sandbox.get("network_enforced"),
            "notice": sandbox.get("notice", ""),
        },
        "mcp": {
            # stdio 服务端尽量挂进 Job；挂不进去是必须红字说出来的事实。
            # http 服务端的进程在远端。证据包把边界种类与未纳入的服务端都原样带走，
            # 否则"所有工具都在边界内"就是一句假话。
            "boundary": mcp.get("boundary", ""),
            "note": mcp.get("boundary_note", ""),
            "servers": mcp.get("servers") or [],
            "calls": mcp_calls,
            "by_boundary": mcp.get("by_boundary") or {},
            "unbounded_servers": mcp.get("unbounded_servers") or [],
        },
        "tool_calls": counts,
        # 单次越界宿主执行：只统计带 `sandbox_bypass=true` 的调用。缺这个键的老任务
        # 一律按 0 读，绝不从 `sandbox_backend=null` 反推——那会把整档 off 也算成越界。
        "sandbox_bypass": {
            "calls": bypass_calls,
            "tool_call_ids": bypass_tool_call_ids,
        },
        # fs-broker 汇总：延迟是实测出来的（samples/p95 来自每次调用带出的字段），
        # 失败按层计数——"broker 坏了多少次、坏在哪一层"要能一眼回答。
        "fs_broker": {
            "calls": broker_calls,
            "failures_by_layer": broker_failures,
            # 失败里"请求已进 broker、响应没拿到"的次数：这些调用的磁盘动作不可知，
            # 不能读成"没执行"。字面值与 fs_broker.SIDE_EFFECTS_UNKNOWN 同值，由测试钉住。
            "failures_with_unknown_side_effects": broker_unknown_effects,
            "latency": broker_latency,
        },
        # 一次任务里出现过的档位。子代理/团队成员的档位继承由写入侧保证；
        # 这里读的是"事件实际写了哪些"，两者对不上就是继承链路断了。
        "sandbox_modes": sorted(modes),
        # 对标 Codex `turn_context.permission_profile`：末次治理快照里「这一步能碰哪些路径」。
        # 老任务没有这一条就报 unrecorded，绝不按档位反推一份假授权出来。
        "permission_profile": profile
        or {
            "type": "unrecorded",
            "note": "该任务没有权限快照（M4 之前的老任务），不按档位反推",
        },
    }


def questions_can_default(questions: object) -> bool:
    """空答案能不能放行：**每道题**都得有推荐项。

    只有偏好题有推荐项。事实题（真实电话、邮箱）没有，所以空答案必须被拒——
    否则「用户没填」会被读成「这处留空」，而服务器替用户编一个号码是更坏的结果。
    混合场景（一题有默认、一题没有）也不放行：漏掉的那题正是不能猜的那题。
    """
    if not questions or not isinstance(questions, list):
        return False
    for question in questions:
        if not isinstance(question, dict):
            return False
        options = question.get("options") or []
        if not isinstance(options, list):
            return False
        if not any(isinstance(option, dict) and option.get("recommended") for option in options):
            return False
    return True


async def build_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
    *,
    timeline_limit: int = DEFAULT_TIMELINE_LIMIT,
    timeline_before_seq: int | None = None,
    include_timeline: bool = True,
    tools_limit: int = DEFAULT_TOOLS_LIMIT,
    tools_offset: int = 0,
    changes_limit: int = DEFAULT_CHANGES_LIMIT,
    changes_offset: int = 0,
) -> dict[str, Any]:
    """交付证据包。

    两段读，各自有明确的上界：

    * ``events``（内部变量）——只取 :data:`SUMMARY_EVENT_TYPES` 里的类型。验收、审批、
      上下文、边界、用量这些汇总字段都从它们算出来，所以这一段必须**完整**但不能
      顺手把工具输出、节点流水一起拖回来。
    * ``timeline_events``——时间线本身，默认最近 :data:`DEFAULT_TIMELINE_LIMIT` 条，
      用 ``timeline_before_seq`` 往前翻页。``include_timeline=False`` 时一条都不读。

    条数统计走 ``GROUP BY type`` 的一次聚合，所以"还有多少条没加载"是数出来的，
    不是估的——分页界面最怕的就是"以为看完了"。
    """
    limit = max(1, min(int(timeline_limit or DEFAULT_TIMELINE_LIMIT), MAX_TIMELINE_LIMIT))
    tools_limit = max(0, min(int(tools_limit or 0), MAX_TOOLS_LIMIT))
    tools_offset = max(0, int(tools_offset or 0))
    changes_limit = max(0, min(int(changes_limit or 0), MAX_CHANGES_LIMIT))
    changes_offset = max(0, int(changes_offset or 0))
    async with session_factory() as session:
        task = await load_task(session, task_id)
        # 汇总事件（计划/交付/验收等）按类型过滤后仍要给个硬上限：类型有界
        # （每轮各几条）但轮数本身无界。这些事件的消费方全都是「取最近」语义
        # （最新验收结论、最近审批上下文、最后一次用量），所以倒序取上限再回正序——
        # 小任务结果与不设限完全一致，大任务保住的是尾部而不是头部。
        events = (
            (
                await session.execute(
                    select(AgentEvent)
                    .where(
                        AgentEvent.task_id == task_id,
                        AgentEvent.type.in_(sorted(SUMMARY_EVENT_TYPES)),
                    )
                    .order_by(AgentEvent.seq.desc())
                    .limit(SUMMARY_EVENT_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        events = list(reversed(events))
        type_rows = (
            await session.execute(
                select(AgentEvent.type, func.count())
                .where(AgentEvent.task_id == task_id)
                .group_by(AgentEvent.type)
            )
        ).all()
        timeline_events: list[AgentEvent] = []
        earlier_count = 0
        if include_timeline:
            query = select(AgentEvent).where(AgentEvent.task_id == task_id)
            if timeline_before_seq is not None:
                query = query.where(AgentEvent.seq < int(timeline_before_seq))
            rows = (
                await session.execute(query.order_by(AgentEvent.seq.desc()).limit(limit))
            ).scalars().all()
            # 取尾部再翻回正序：时间线永远是"从旧到新"，倒着渲染会让工具卡对不上步号。
            timeline_events = list(reversed(rows))
            # 本页之前还有多少条：一次 COUNT，比让前端"猜着翻"便宜得多。
            if timeline_events:
                earlier_count = int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(AgentEvent)
                            .where(
                                AgentEvent.task_id == task_id,
                                AgentEvent.seq < timeline_events[0].seq,
                            )
                        )
                    ).scalar_one()
                )
        calls_total = int(
            (
                await session.execute(
                    select(func.count()).select_from(ToolCall).where(ToolCall.task_id == task_id)
                )
            ).scalar_one()
        )
        calls_query = (
            select(ToolCall).where(ToolCall.task_id == task_id).order_by(ToolCall.created_at)
        )
        if tools_limit:
            calls_query = calls_query.limit(tools_limit).offset(tools_offset)
        calls = (await session.execute(calls_query)).scalars().all()
        changes_total = int(
            (
                await session.execute(
                    select(func.count()).select_from(FileChange).where(FileChange.task_id == task_id)
                )
            ).scalar_one()
        )
        changes_query = (
            select(FileChange).where(FileChange.task_id == task_id).order_by(FileChange.created_at)
        )
        if changes_limit:
            changes_query = changes_query.limit(changes_limit).offset(changes_offset)
        changes = (await session.execute(changes_query)).scalars().all()
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
    # 验收条件可能已经是结构化字典（{id, criterion, step_ids}）：这里只取文字，
    # 但要连引用一起带上——证据页据此能把"没通过"指到具体步骤。
    criteria = normalize_criteria(task.acceptance_criteria)
    if criteria and not acceptance:
        acceptance = [
            {
                "criterion": item["criterion"],
                "covered": False,
                "evidence": "任务未产出验收结论",
                "criterion_id": item["id"],
                "milestone_id": item["milestone_id"],
                "step_ids": item["step_ids"],
            }
            for item in criteria
        ]
    validation = _latest_validation(events)
    # 审批请求里的权限上下文（模式 / 命中规则 / 建议前缀 / 作用域选项）在中断事件里；
    # 前端要拿它渲染「仅本次 / 总是允许 git status / 总是允许 run_shell」。
    approval_context: dict[str, Any] = {}
    for event in reversed(events):
        if event.type in ("approval.required", "approval.requested") and isinstance(event.payload, dict):
            approval_context = dict(event.payload)
            break
    # 对账：同一工具在这次等待之后又落了新的账（无论成败），说明那次等待已被
    # 批准并执行完毕，这条旧的「待审批」是重放留下的陈账，不再挂卡。
    _later_tool_seen: set[str] = set()
    _superseded_ids: set[str] = set()
    for call in reversed(calls):
        if call.status == "awaiting_approval" and call.name in _later_tool_seen:
            _superseded_ids.add(call.id)
        _later_tool_seen.add(call.name)
    _unresolved_awaiting = [
        call
        for call in calls
        if call.status == "awaiting_approval" and call.id not in _superseded_ids
    ]
    # 待审批卡只在任务真的在等待时才有意义；任务已结束时挂着一张必然 409 的卡，
    # 用户点什么都是徒劳（benchmark coding-036 实测：任务 succeeded 后证据包仍报「待审批」）。
    # v0.10.1 §3.2：审批卡必须带完整身份（approval_id/tool_call_id/arguments_hash/
    # resource_path）——前端提交审批时原样带回，后端按身份匹配，不能按文件名猜。
    pending_approvals = [
        {
            "tool_call_id": call.id,
            "tool": call.name,
            "arguments": call.arguments,
            "risk_level": call.risk_level,
            "message": call.result_summary,
            **_approval_lifecycle(approval_context.get("expires_at")),
            **_approval_hints(approval_context, call.name, call.arguments),
            **_approval_identity_fields(approval_context, call.name),
        }
        for call in _unresolved_awaiting
        if task.status == "waiting_approval"
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
                    **_approval_identity_fields(payload if isinstance(payload, dict) else {}, str(payload.get("tool") or "")),
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
                              ("sandbox_enforcement", payload.get("sandbox_enforcement")),
                              ("sandbox_mode", payload.get("sandbox_mode")),
                              ("sandbox_bypass", payload.get("sandbox_bypass")),
                              ("sandbox_bypass_reason", payload.get("sandbox_bypass_reason")),
                              ("boundary_kind", payload.get("boundary_kind")),
                              ("fs_broker_latency", payload.get("fs_broker_latency")),
                              ("fs_broker_error", payload.get("fs_broker_error")))
            if k in payload
        }
        for payload in (event.payload for event in events if event.type == "tool.completed")
        if isinstance(payload, dict)
        and ("sandbox_backend" in payload or "boundary_kind" in payload)
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
    # 按模型拆账（任务书 §4.3"每个模型 tokens"）：对比哪个模型更省时按行分摊，
    # 不从 totals 里猜。usage_sources 回答"这些数字是供应商说的还是没说"。
    by_model: dict[str, dict[str, int]] = {}
    for row, _ in usage_rows:
        key = row.model or "(unknown)"
        bucket = by_model.setdefault(
            key, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        )
        bucket["calls"] += 1
        bucket["prompt_tokens"] += int(row.prompt_tokens or 0)
        bucket["completion_tokens"] += int(row.completion_tokens or 0)
        bucket["total_tokens"] += int(row.prompt_tokens or 0) + int(row.completion_tokens or 0)
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
    elif task.status != "waiting_approval" and _unresolved_awaiting:
        # 任务结束了还留着从没获批、也没被后续执行覆盖的等待账：如实说「没批、没跑」，
        # 不再说「待审批」——已经没有可批的卡了，这句话只会误导用户去等一张不存在的卡。
        unverified.append(
            "高风险操作未获批准，未执行："
            + ", ".join(sorted({call.name for call in _unresolved_awaiting}))
        )
    started_payload = next(
        (
            event.payload
            for event in events
            if event.type == "task.started" and isinstance(event.payload, dict)
        ),
        {},
    )
    boundaries = boundary_report(events, started_payload)
    diff_text, diff_truncated = _bounded_diff(changes, DIFF_CHAR_BUDGET)
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
            "total": changes_total,
            "files": [change.path for change in changes],
            "reversible": all(bool(change.backup_path) or change.change_type == "created" for change in changes),
            "diff": diff_text,
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
        "changes_meta": {
            "total": changes_total,
            "returned": len(changes),
            "has_more": changes_total > changes_offset + len(changes),
            "diff_truncated": diff_truncated,
            # 结构化「无需改动」证据（任务书 §3）：成功终态却零文件改动时，前端与
            # 判分器要能直接读到，而不是靠最后一条文本猜"它到底改没改"。
            "zero_change": changes_total == 0,
        },
        "validation": validation,
        "governance": governance_summary,
        "world": world_summary,
        "boundaries": boundaries,
        "guardian_history": _guardian_history(events),
        "usage": {
            "providers": providers,
            "models": models,
            # 每个模型各花多少（任务书 §4.3）；usage_sources 是这批数字的来源集合
            # （provider=供应商回的，unknown=没回——全零不等于免费）。
            "by_model": by_model,
            "usage_sources": sorted(
                {str(getattr(row, "usage_source", "") or "unknown") for row, _ in usage_rows}
            ),
            # 来源得按行计数：一组调用里只要有一条没拿到 usage，集合就会同时出现
            # provider 与 unknown。只凭集合读不出"是整段都没账，还是只丢了一次"，
            # 于是总 token 会被误当成不可信。口径：provider=有真实数字的行数。
            "usage_source_calls": {
                source: sum(
                    1
                    for row, _ in usage_rows
                    if (str(getattr(row, "usage_source", "") or "unknown") == source)
                )
                for source in sorted(
                    {str(getattr(row, "usage_source", "") or "unknown") for row, _ in usage_rows}
                )
            },
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
            # 按发起轮汇总：一轮团队里有几个成员/几条 run，这里合起来只有一个数。
            # 未挂发起轮的老账单独计数，不混进任何一轮。
            "origins": origin_rollup([row for row, _ in usage_rows]),
            "origin_unattributed_calls": sum(
                1 for row, _ in usage_rows if not str(getattr(row, "origin_run_id", "") or "")
            ),
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
            # 每条用量行都记着它是在哪一档沙箱里产生的。这里给去重后的档位集合；
            # 空串是 M3 之前的账，单独计数，不当成 workspace 也不算进集合。
            "sandbox_modes": sorted(
                {str(getattr(row, "sandbox_mode", "") or "") for row, _ in usage_rows} - {""}
            ),
            "sandbox_mode_unattributed_calls": sum(
                1 for row, _ in usage_rows if not str(getattr(row, "sandbox_mode", "") or "")
            ),
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
        "tools_meta": {
            "total": calls_total,
            "returned": len(calls),
            "has_more": calls_total > tools_offset + len(calls),
        },
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
                "run_id": event.run_id,
                # 每个事件自带发起轮：不用再拿 task_id 去 join 账本，也能回答
                # "这条事件属于哪一轮群聊引起的任务树"。
                "origin_run_id": event.origin_run_id,
                "node": event.node,
                "type": event.type,
                "payload": event.payload,
                "created_at": _iso(event.created_at),
            }
            for event in timeline_events
        ],
        "timeline_meta": _timeline_meta(
            timeline_events,
            type_rows,
            limit=limit,
            before_seq=timeline_before_seq,
            earlier=earlier_count,
        ),
    }


def _timeline_meta(
    timeline_events: list[AgentEvent],
    type_rows,
    *,
    limit: int,
    before_seq: int | None,
    earlier: int,
) -> dict[str, Any]:
    """这一页时间线是怎么切出来的。字段全部是数出来的，读侧不需要猜。

    ``next_before_seq`` 为空才表示"到头了"。``earlier`` 是"本页之前还有多少条"
    ——翻页界面要显示的是"还有 380 条更早的"，不是"总共 581 条"。
    """
    oldest = timeline_events[0].seq if timeline_events else None
    more = older_exists(timeline_events, before_seq=before_seq)
    return {
        "total_events": sum(int(count) for _, count in type_rows),
        "loaded": len(timeline_events),
        "limit": limit,
        "before_seq": before_seq,
        # 非空 = 用这个游标再要一次就能继续往前翻。
        "next_before_seq": oldest if more else None,
        "by_type": {str(event_type): int(count) for event_type, count in type_rows},
        "truncated": bool(more),
        "earlier": earlier,
    }


def older_exists(timeline_events: list[AgentEvent], *, before_seq: int | None) -> bool:
    """这一页前面还有没有更早的事件。

    "满页"不等于"还有"：恰好卡在最后一页边界时，满页会让人以为还能往前翻，
    点下去拿到空页。所以判据是**本页最小 seq 是否就是 1**——seq 从 1 起严格递增，
    最小 seq 为 1 就一定到头了。若给了 ``before_seq``，则要看本页最小 seq 是否
    小于它（说明还没翻到头）。
    """
    if not timeline_events:
        return False
    oldest = min(event.seq for event in timeline_events)
    if before_seq is not None and oldest >= int(before_seq):
        return False
    return oldest > 1


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


def _guardian_history(events: list[AgentEvent]) -> dict[str, Any]:
    """审阅侧的账（对标 §4.1 的 `guardian_history`）。

    真源只有两处，这里一个都不新造：`guardian.reviewed` / `guardian.auto_approved` 事件
    （每次独立审查的判定）与 `context.compacted` 里的 `guardian_history`（那次压缩之前，
    审阅侧看得到几条历史、模型侧只剩几条）。逐条 `items` 留在事件里不抄第二遍——与
    `windows` 只搬条数同一个理由：两处副本会写歪。

    `windows_unrecorded` 是给老任务留的：压缩发生过、但那条记录里没有审阅侧轨迹，就数一笔，
    不补一份"看起来记过了"的空条目。`decisions` 为空既可能是"没审过"也可能是"当时没记"，
    所以判定条数一律从事件读，不按档位或审批模式反推。
    """
    decisions: list[dict[str, Any]] = []
    auto: list[dict[str, Any]] = []
    windows: list[dict[str, Any]] = []
    unrecorded = 0
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.type == "guardian.reviewed":
            decisions.append(
                {
                    "seq": event.seq,
                    "run_id": event.run_id,
                    "tool": str(payload.get("tool") or ""),
                    # 一次具体动作的稳定标识：靠它和审批卡、ToolCall 行对上。
                    "approval_key": str(payload.get("approval_key") or ""),
                    "verdict": str(payload.get("verdict") or ""),
                    "reasons": list(payload.get("reasons") or []),
                    "model": str(payload.get("model") or ""),
                    "latency_ms": int(payload.get("latency_ms") or 0),
                    "timed_out": bool(payload.get("timed_out")),
                    "error": str(payload.get("error") or ""),
                }
            )
            if "outcome" in payload:
                # 审完之后走到了哪一步（blocked/auto_approved/asked）。老事件没有这一格：
                # 缺键就是"当时没记"，不补成空串——补了就被读成"记过了、没有去向"。
                decisions[-1]["outcome"] = str(payload["outcome"])
        elif event.type == "guardian.auto_approved":
            auto.append({"seq": event.seq, "approval_key": str(payload.get("approval_key") or "")})
        elif event.type == "context.compacted":
            trail = payload.get("guardian_history")
            if not isinstance(trail, dict):
                unrecorded += 1
                continue
            windows.append(
                {
                    "seq": event.seq,
                    "window_number": int(payload.get("window_number") or 0),
                    # 审阅侧 vs 模型侧：Codex 实测同一窗 96 vs 18，压缩丢的是模型的历史。
                    "items_reviewed": int(trail.get("items_reviewed") or 0),
                    "items_in_model_context": int(trail.get("items_in_model_context") or 0),
                    "reviewed_chars": int(trail.get("reviewed_chars") or 0),
                    "reviews": len(trail.get("reviews") or []),
                    "in_effect": dict(trail.get("in_effect") or {}),
                }
            )
    return {
        "decisions": decisions,
        "auto_approvals": auto,
        "windows": windows,
        "windows_unrecorded": unrecorded,
    }


def _approval_identity_fields(context: dict, tool: str) -> dict[str, Any]:
    """把审批身份（v0.10.1 §3.2）摘到待审批卡上。

    上下文只描述最近一次 approval.required 那张卡：只有卡上的工具和它一致时才带
    身份，其他卡不冒领——多张卡并存时，提交哪张就校验哪张，不能拿 A 的 id 批 B。
    ``tool_call_id`` 刻意**不**在这里覆盖：真实路径的中断卡本来就没有 ToolCall 行
    （卡上的 tool_call_id 就是中断事件里的调用 id），而网关形态的 awaiting 行有自己
    的行 id——两个 id 各有含义，校验侧两个都认，展示侧不偷换。
    老任务的中断事件没有这些键，返回空 dict，行为与从前一致。
    """
    if not context or str(context.get("tool") or "") != tool:
        return {}
    fields: dict[str, Any] = {}
    for key in ("approval_id", "arguments_hash", "resource_path"):
        value = context.get(key)
        if value:
            fields[key] = str(value)
    return fields


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
    if "guardian" in context:
        # 独立审查员当时说了什么，批准的人必须看得见：`None` 是记过的真值（审查关着），
        # 键不存在才是"这条卡是审查进载荷之前的老任务"。缺键不补空字典。
        hints["guardian"] = context.get("guardian")
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


def _bounded_diff(changes: list, budget: int) -> tuple[str, int]:
    """把文件 diff 拼成一段文本，但总字符数封顶到 ``budget``。

    原来是无条件 ``"\\n".join(change.diff ...)``：几百个文件、每个几万字符，拼出来就是
    数 MB 的响应体，长任务直接把后端内存和前端渲染一起拖垮。超预算的文件不再并进
    ``diff`` 字段，返回漏掉了几条，由调用方（``changes_meta.diff_truncated``）如实说出来，
    而不是假装全给了。单条 diff 超过预算时仍让它进（否则整段就空了），只是后面的被截断。
    """
    parts: list[str] = []
    total = 0
    truncated = 0
    for change in changes:
        diff = change.diff or ""
        if parts and total + len(diff) > budget:
            truncated += 1
            continue
        parts.append(diff)
        total += len(diff)
    return "\n".join(parts), truncated
