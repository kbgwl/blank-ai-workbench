"""点验判据：事件流进、判定出，全部是纯函数。

分成两组：`judge_*` 只吃已经落盘的事件（可离线自检，见
`services/api/tests/test_realmodel_device_v120.py`），驱动脚本
`async_hooks_v119.py` / `prompt_submit_v123.py` 负责真跑与取数。判据名字是契约的一部分——
装置自检按名字打红集合，改名就等于把一条判据悄悄删掉。
"""

from __future__ import annotations

import json
from pathlib import Path

from lib import AGENT_ID_RE, SHA12_RE, WAKE_WRAPPER, Recorder, arg_of, by_tool, of_type

# ---- #119 异步钩子 + 退出码 2 唤醒 ------------------------------------------


def judge_wakeup(rec: Recorder, events: list[dict]) -> None:
    async_done = [e for e in of_type(events, "hook.completed") if e["payload"].get("async") is True]
    rec.check(
        "119 异步钩子完成事件",
        len(async_done) >= 1,
        f"count={len(async_done)} "
        f"post_tool_use={sum(1 for e in async_done if e['payload'].get('event') == 'post_tool_use')}",
    )
    wake = of_type(events, "hook.wakeup_delivered")
    rec.check("119 唤醒投递事件", len(wake) >= 1,
              f"count={len(wake)} payloads={[e['payload'] for e in wake][:2]}")

    # 注入事件按脱敏设计只带 chars 与 sources，不带正文（正文里可能有敏感输出）。
    # 所以这里判的是"注入块的长度容得下钩子原文 + 包装语"，正文逐字对不对
    # 由编码格在线上钉（它读的是 additional_context_chars 与原文长度的相等）。
    injected = [e["payload"] for e in of_type(events, "hook.context_injected")]
    ctx_chars = max(
        [int(r.get("additional_context_chars") or 0)
         for e in async_done for r in e["payload"].get("results", [])
         if r.get("exit_code") == 2] or [0]
    )
    wrapper = len(WAKE_WRAPPER)
    floor = ctx_chars + wrapper
    rec.check(
        "119 唤醒作为系统消息进了下一次请求",
        bool(injected)
        and floor > wrapper
        and any(
            p.get("chars", 0) >= floor
            and any(s.get("event") == "post_tool_use" for s in p.get("sources", []))
            for p in injected
        ),
        f"injected={[(p.get('chars'), p.get('sources')) for p in injected]} "
        f"ctx_chars={ctx_chars} floor={floor}",
    )
    rf = by_tool(events, "tool.completed", "read_file")
    rec.check("119 触发源 read_file 已执行", len(rf) >= 1, f"count={len(rf)}")


# ---- #118 worktree_create / worktree_remove 钩子 ----------------------------


def _hook_paths(modes: list[tuple]) -> list[str]:
    """本轮事件里由钩子建出的副本路径——清理通知只能对着这些路径算数。"""
    return [p for m, p in modes if m == "hook" and isinstance(p, str)]


def _removal_rows(hook_log_lines: list[str]) -> list[dict]:
    """审计文件每行是一整个 JSON（路径里的反斜杠已由 json 转义还原）。"""
    rows = []
    for line in hook_log_lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("event") == "worktree_remove":
            rows.append(row)
    return rows


def judge_worktree_hook(rec: Recorder, events: list[dict], hook_log_lines: list[str]) -> None:
    wt_hook = [e for e in of_type(events, "hook.completed")
               if e["payload"].get("event") == "worktree_create"]
    rec.check("118 worktree_create 钩子被调用", len(wt_hook) >= 1, f"count={len(wt_hook)}")
    created = of_type(events, "worktree.created")
    modes = [(e["payload"].get("mode"), e["payload"].get("path")) for e in created]
    rec.check("118 副本由钩子提供（mode=hook）", any(m == "hook" for m, _ in modes), f"modes={modes}")
    rec.check("118 钩子路径被真实使用",
              any(isinstance(p, str) and "hookcopy-" in p for _, p in modes), f"paths={modes}")

    # 清理通知分两格判。事件流那一格天然按轮隔离，审计文件那一格是带外证据：
    # 它按行累积，只判「文件里出现过 worktree_remove」会被上一轮的同名行蒙过，
    # 所以必须钉到本轮建出的那个具体副本路径上。
    remove_hook = [e for e in of_type(events, "hook.completed")
                   if e["payload"].get("event") == "worktree_remove"]
    rec.check("118 worktree_remove 钩子收到清理通知",
              len(remove_hook) >= 1
              and all(any(int(r.get("exit_code") or 0) == 0 for r in e["payload"].get("results", []))
                      for e in remove_hook),
              f"count={len(remove_hook)} "
              f"exit={[r.get('exit_code') for e in remove_hook for r in e['payload'].get('results', [])]}")
    mine = {Path(p) for p in _hook_paths(modes)}
    removals = _removal_rows(hook_log_lines)
    rec.check("118 清理通知里带着本轮副本的路径",
              bool(mine) and any(Path(str(r.get("path") or "")) in mine for r in removals),
              f"本轮副本={sorted(str(p) for p in mine)} "
              f"审计清理={[(r.get('path'), r.get('mode')) for r in removals][-3:]} "
              f"log_lines={len(hook_log_lines)}")


# ---- #116 后台子代理句柄 ----------------------------------------------------


def judge_subagent(rec: Recorder, events: list[dict], evidence_text: str) -> None:
    spawn = by_tool(events, "tool.completed", "spawn_agent")
    rec.check("116 spawn_agent 真的被调用", len(spawn) >= 1, f"count={len(spawn)}")
    handle_ids = {m for e in spawn
                  for m in AGENT_ID_RE.findall(str(e["payload"].get("summary") or ""))}
    rec.check("116 立即返回了 agent_id 句柄", len(handle_ids) >= 1, f"ids={sorted(handle_ids)}")
    requested = [e["payload"] for e in by_tool(events, "tool.requested", "task_output")]
    asked_ids = {arg_of(p, "agent_id") for p in requested}
    rec.check("116 模型用同一个句柄调 task_output", bool(asked_ids & handle_ids),
              f"handle={sorted(handle_ids)} asked={sorted(asked_ids)}")
    tout = by_tool(events, "tool.completed", "task_output")
    rec.check("116 task_output 取回结论",
              len(tout) >= 1 and any(e["payload"].get("ok") for e in tout),
              f"count={len(tout)} ok={[e['payload'].get('ok') for e in tout]} "
              f"summary={[(e['payload'].get('summary') or '')[:180] for e in tout]}")
    rec.check("116 子代理读到了钩子副本独有的文件（证明工作目录来自钩子）",
              any("FROM-HOOK" in json.dumps(e["payload"], ensure_ascii=False)
                  or "worktree_create hook" in json.dumps(e["payload"], ensure_ascii=False)
                  for e in tout),
              f"summaries={[(e['payload'].get('summary') or '')[:200] for e in tout]}")
    durations = [e["payload"].get("duration_ms") for e in spawn]
    rec.check("116 后台派出没有阻塞调用点（spawn 往返 < 15 秒）",
              bool(spawn) and all(isinstance(d, int) for d in durations)
              and max(durations) < 15000,
              f"durations={durations}")
    rec.check("116 子代理用量计入证据包", "subagent" in evidence_text,
              f"evidence has subagent={'subagent' in evidence_text}")


# ---- #117 每轮回退点 --------------------------------------------------------


def judge_rewind(rec: Recorder, points: list[dict], rewind: dict,
                 turns_after: list[dict], task1: str, events: list[dict]) -> None:
    rec.check("117 回退点含两轮", len(points) == 2,
              f"points={[(p['turn'], p['status'], p['restorable'], p['skipped_reason']) for p in points]}")
    rec.check("117 第 1 轮回退点明确代码是否可回",
              any(p["turn"] == 1 and isinstance(p["restorable"], bool)
                  and (p["restorable"] or p["skipped_reason"]) for p in points),
              f"point1={[p for p in points if p['turn'] == 1]}")
    rec.check("117 回退响应分开汇报对话与代码",
              "restored" in rewind and "skipped_reason" in rewind, f"keys={list(rewind)}")
    rec.check("117 对话截断到第 1 轮",
              len(turns_after) == 1 and turns_after[0]["id"] == task1,
              f"turns={[t['id'] for t in turns_after]}")
    rew = [e for e in events if e["type"] == "thread.rewound"]
    rec.check("117 thread.rewound 事件留痕", len(rew) >= 1,
              f"count={len(rew)} {json.dumps(rewind, ensure_ascii=False)[:200]}")


# ---- #119 编码格：唤醒正文逐字 ----------------------------------------------


def judge_encoding(rec: Recorder, events: list[dict], hook_text: str) -> None:
    woke = [r for e in of_type(events, "hook.completed") if e["payload"].get("async")
            for r in e["payload"].get("results", []) if r.get("exit_code") == 2]
    rec.check("线上确有退出码 2 的异步钩子", len(woke) >= 1, f"count={len(woke)}")
    chars = [int(r.get("additional_context_chars") or 0) for r in woke]
    rec.check("唤醒正文长度=钩子原文长度（不许静默丢字）",
              bool(chars) and max(chars) == len(hook_text),
              f"chars={chars} expected={len(hook_text)} 修复前实测=39")
    injected = [e["payload"] for e in of_type(events, "hook.context_injected")]
    floor = len(hook_text) + len(WAKE_WRAPPER)
    rec.check("注入块带上了来源与完整正文",
              any(p.get("chars", 0) >= floor
                  and any(s.get("event") == "post_tool_use" for s in p.get("sources", []))
                  for p in injected),
              f"injected={[(p.get('chars'), p.get('sources')) for p in injected]} floor={floor}")


# ---- #118/#119 对照格：不配钩子时主判据必须红 --------------------------------


CONTROL_RED_NAME = "对照·mode 不该是 hook（必须为红）"


def judge_control(rec: Recorder, events: list[dict]) -> bool:
    """返回「对照格该红的那条是否真红了」。"""
    created = of_type(events, "worktree.created")
    modes = [(e["payload"].get("mode"), e["payload"].get("path")) for e in created]
    wt_hook = [e for e in of_type(events, "hook.completed")
               if e["payload"].get("event") == "worktree_create"]
    async_done = [e for e in of_type(events, "hook.completed") if e["payload"].get("async") is True]
    wake = of_type(events, "hook.wakeup_delivered")

    rec.check("对照·副本仍然创建（兜底路径生效）", len(created) >= 1, f"modes={modes}")
    # 这一条在对照格里**必须为红**：它才是「主判据测的是钩子路径」的证据。
    # 它若在这里也绿，主判据就没测到任何东西，整次点验作废。
    rec.check(CONTROL_RED_NAME, any(m == "hook" for m, _ in modes), f"modes={modes}")
    rec.check("对照·没有 worktree_create 钩子事件（绿=符合）", len(wt_hook) == 0,
              f"count={len(wt_hook)}")
    rec.check("对照·没有异步钩子完成事件（绿=符合）", len(async_done) == 0,
              f"count={len(async_done)}")
    rec.check("对照·没有唤醒事件（绿=符合）", len(wake) == 0, f"count={len(wake)}")
    return CONTROL_RED_NAME in rec.fails


# ---- #123 提交钩子的三道决定（补上下文 / 抑制原文 / 拦下提交） -----------------
# 这一组吃三份实物：事件流、任务行（`GET /tasks/{id}`）、夹具规格
# （`fixtures/prompt-submit-v123.json`）。锚与正文只有夹具那一份，判据不许自己抄。


def _prompt_reads(events: list[dict]) -> dict[str, list[dict]]:
    return {
        name: of_type(events, f"hook.{name}")
        for name in ("prompt_blocked", "prompt_injected", "prompt_substituted")
    }


def judge_submit_suppress(rec: Recorder, events: list[dict], task: dict, spec: dict) -> None:
    goal = str(task.get("goal") or "")
    summary = str(task.get("result_summary") or "")
    subs = of_type(events, "hook.prompt_substituted")
    payload = subs[0]["payload"] if subs else {}
    marker = spec["suppress_marker"]
    side_door = spec["side_door_file"]
    curated = json.dumps([e["payload"] for e in of_type(events, "context.curated")], ensure_ascii=False)
    rec.check("123 抑制格留下 prompt_substituted 且 suppressed=true",
              len(subs) >= 1 and payload.get("suppressed") is True,
              f"count={len(subs)} payload={json.dumps(payload, ensure_ascii=False)[:200]}")
    rec.check("123 抑制事件的原文读数对得上账（长度 + 12 位指纹）",
              payload.get("original_chars") == len(goal)
              and bool(SHA12_RE.match(str(payload.get("original_sha") or ""))),
              f"original_chars={payload.get('original_chars')} goal_len={len(goal)} "
              f"sha={payload.get('original_sha')!r}")
    rec.check("123 替代正文长度逐字等于夹具文本",
              payload.get("replacement_chars") == len(spec["suppress_replacement"]),
              f"replacement_chars={payload.get('replacement_chars')} "
              f"expected={len(spec['suppress_replacement'])}")
    rec.check("123 事件里不落原文正文（只留长度与指纹）",
              marker not in json.dumps(payload, ensure_ascii=False),
              f"payload={json.dumps(payload, ensure_ascii=False)[:200]}")
    rec.check("123 账面 goal 仍是用户写的那份（抑制的是上下文，不是账）",
              marker in goal, f"goal={goal[:120]}")
    rec.check("123 模型读的是替代正文",
              spec["suppress_token"] in summary, f"summary={summary[:200]}")
    rec.check("123 模型读数里不含原文口令",
              marker not in summary, f"summary={summary[:200]}")
    # 侧门：抑制时上下文管家拿不到原文关键词，工作区里那枚同名文件不该被带进 system。
    rec.check("123 上下文管家的候选没被原文关键词带出来",
              side_door not in curated and side_door not in summary,
              f"curated={curated[:220]} summary={summary[:120]}")


def judge_submit_blocked(rec: Recorder, events: list[dict], task: dict, spec: dict) -> None:
    goal = str(task.get("goal") or "")
    marker = spec["block_marker"]
    blocked = of_type(events, "hook.prompt_blocked")
    payload = blocked[0]["payload"] if blocked else {}
    usage = of_type(events, "model.usage")
    started = of_type(events, "task.started")
    rec.check("123 拦下格留下 prompt_blocked 且带着阻断原因",
              len(blocked) >= 1 and payload.get("reason") == spec["block_reason"],
              f"count={len(blocked)} reason={payload.get('reason')!r}")
    rec.check("123 拦下事件只登记长度与指纹，不落原文",
              payload.get("original_chars") == len(goal)
              and bool(SHA12_RE.match(str(payload.get("original_sha") or "")))
              and marker not in json.dumps(payload, ensure_ascii=False),
              f"payload={json.dumps(payload, ensure_ascii=False)[:200]}")
    rec.check("123 拦下时一次模型调用都没发起（无用量、无 task.started、零步）",
              not usage and not started and task.get("steps_used") == 0,
              f"usage={len(usage)} started={len(started)} steps_used={task.get('steps_used')}")
    rec.check("123 任务以失败收场且原因指向钩子",
              task.get("status") == "failed" and "提交被钩子拦下" in str(task.get("failure_reason") or ""),
              f"status={task.get('status')} reason={str(task.get('failure_reason'))[:160]}")
    rec.check("123 拦下不改账（goal 仍是用户写的那份）", marker in goal, f"goal={goal[:120]}")


def judge_submit_injected(rec: Recorder, events: list[dict], task: dict, spec: dict) -> None:
    summary = str(task.get("result_summary") or "")
    inj = of_type(events, "hook.prompt_injected")
    payload = inj[0]["payload"] if inj else {}
    rec.check("123 补上下文格留下 prompt_injected 且 original_kept=true",
              len(inj) >= 1 and payload.get("original_kept") is True,
              f"count={len(inj)} payload={json.dumps(payload, ensure_ascii=False)[:200]}")
    rec.check("123 补的正文长度逐字等于夹具文本",
              payload.get("chars") == len(spec["inject_text"]),
              f"chars={payload.get('chars')} expected={len(spec['inject_text'])}")
    rec.check("123 没要求抑制时不许冒出抑制事件",
              not of_type(events, "hook.prompt_substituted"),
              f"types={sorted({e['type'] for e in events if e['type'].startswith('hook.prompt')})}")
    rec.check("123 补充正文不进事件（只带长度与来源）",
              spec["inject_text"] not in json.dumps(payload, ensure_ascii=False),
              f"payload={json.dumps(payload, ensure_ascii=False)[:200]}")
    rec.check("123 模型既读得到原文口令、也读得到钩子补充",
              spec["inject_marker"] in summary and spec["inject_token"] in summary,
              f"summary={summary[:220]}")


def judge_submit_plain(rec: Recorder, events: list[dict], task: dict, spec: dict) -> None:
    summary = str(task.get("result_summary") or "")
    completed = [e for e in of_type(events, "hook.completed")
                 if e["payload"].get("event") == "user_prompt_submit"]
    rec.check("123 无关提交上钩子仍然跑过并留审计", len(completed) >= 1,
              f"count={len(completed)}")
    rec.check("123 无关提交不产生任何改写事件",
              not any(name in e["type"] for e in events for name in
                      ("prompt_blocked", "prompt_injected", "prompt_substituted")),
              f"types={sorted({e['type'] for e in events if e['type'].startswith('hook.')})}")
    rec.check("123 无关提交的原文照送（模型读得到口令）",
              spec["plain_marker"] in summary, f"summary={summary[:200]}")


# 对照格（不配钩子的那台实例）必须红的三条：它们才是「主判据测的是钩子路径」的证据。
# 这一组名字是实测登记的，任何一条在对照格上绿了，主格的读数就不能算数（rc 2）。
CONTROL_123_RED = (
    "123 抑制格留下 prompt_substituted 且 suppressed=true",
    "123 模型读数里不含原文口令",
    "123 上下文管家的候选没被原文关键词带出来",
)


def judge_submit_control(rec: Recorder, events: list[dict], task: dict, spec: dict) -> bool:
    """同一套抑制判据打在没钩子的实例上，返回「该红的三条是否真红」。"""
    judge_submit_suppress(rec, events, task, spec)
    return all(name in rec.fails for name in CONTROL_123_RED)

# 团队入口的对照格（不配钩子的那台）必须红的三条：没钩子时这三件事必然不成立。
# 「账面 goal 仍是原文」在两边都成立（账本来就不受钩子影响），所以它不在这一组里。
CONTROL_123_TEAM_RED = (
    "123 团队入口：每条成员任务都留下 suppressed=true 的抑制事件",
    "123 团队入口：机器人发言里出现替代正文的口令",
    "123 团队入口：机器人发言里没有原文口令",
)


def judge_team_submit(rec: Recorder, tasks: list[dict], messages: list[dict], spec: dict) -> None:
    """团队入口的提交钩子：成员任务的 goal 直接来自用户发言（services/team.py）。

    `tasks` 每项是 `{"id", "task", "events"}`；`messages` 是这一轮的群消息。
    成员任务按 `team.turn.started` 事件认（群轮只给被点到的成员发任务），
    账面 goal 拿**用户发言原文**比——不能用「goal 含原文锚」来挑任务：
    那样「账面被改写」的任务会直接从样本里消失，第三条判据就成了恒真。
    只判「钩子真的作用在成员任务上」：每条成员任务都留下抑制事件、账面 goal 不动、
    机器人发言里出现替代正文的口令且没有原文口令（没钩子时这三条必然不成立）。
    """
    marker, token = spec["suppress_marker"], spec["suppress_token"]
    member = [row for row in tasks
              if of_type(row.get("events") or [], "team.turn.started")]
    user_texts = [str(item.get("content") or "") for item in messages
                  if item.get("role") == "user"]
    expected_goal = user_texts[-1] if user_texts else ""
    substitutions = {str(row.get("id")): of_type(row.get("events") or [],
                                                 "hook.prompt_substituted")
                     for row in member}
    misses = [
        task_id for task_id, events in substitutions.items()
        if not (events and events[0]["payload"].get("suppressed") is True)
    ]
    stray_goals = [
        str(row.get("id")) for row in member
        if str((row.get("task") or {}).get("goal") or "") != expected_goal
    ]
    rec.check("123 团队入口：至少一条成员任务进了群轮", bool(member),
              f"tasks={len(tasks)} member={len(member)}")
    rec.check("123 团队入口：每条成员任务都留下 suppressed=true 的抑制事件",
              bool(member) and not misses, f"member={len(member)} misses={misses[:4]}")
    rec.check("123 团队入口：成员任务的账面 goal 逐字等于用户发言（抑制的是上下文，不是账）",
              bool(member) and bool(expected_goal) and not stray_goals,
              f"member={len(member)} strays={stray_goals[:4]}")
    bot_texts = [str(item.get("content") or "") for item in messages if item.get("role") == "bot"]
    rec.check("123 团队入口：机器人发言里出现替代正文的口令",
              any(token in text for text in bot_texts), f"bot_msgs={len(bot_texts)}")
    rec.check("123 团队入口：机器人发言里没有原文口令",
              all(marker not in text for text in bot_texts),
              f"leaks={[text[:60] for text in bot_texts if marker in text][:1]}")
