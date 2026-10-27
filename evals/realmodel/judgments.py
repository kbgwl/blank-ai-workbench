"""点验判据：事件流进、判定出，全部是纯函数。

分成两组：`judge_*` 只吃已经落盘的事件（可离线自检，见
`services/api/tests/test_realmodel_device_v120.py`），驱动脚本
`async_hooks_v119.py` 负责真跑与取数。判据名字是契约的一部分——
装置自检按名字打红集合，改名就等于把一条判据悄悄删掉。
"""

from __future__ import annotations

import json
from pathlib import Path

from lib import AGENT_ID_RE, WAKE_WRAPPER, Recorder, arg_of, by_tool, of_type

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
