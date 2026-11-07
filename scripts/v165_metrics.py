"""v165 §16 Super Agent 新增质量门槛的只读计算器。

用法::

    python scripts/v165_metrics.py --db path/to/blank.db [--json]

只读 sqlite，不连服务、不改库。指标全部从「落库事实」算，不看 Agent 自述：
goal 关联看 goal_spec.goal_id，审批状态看 approval.granted 事件，
自动修复看 repair.* 事件，交付泄漏看最终 result_summary 的措辞。

口径说明：分母为 0 的指标返回 ``None``（不是 100%），避免「没跑任何任务」
被算成满分。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

# 交付正文里出现这些措辞 = 把过程写进了结论（用户明确反对）。
_PROCESS_MARKS = (
    "我做了", "我先", "我首先", "接下来我", "然后我", "我已经完成以下",
    "下面是我的步骤", "我先读取", "我调用了",
)


def _table(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "select count(*) from sqlite_master where type='table' and name=?", (name,)
    ).fetchone()[0] > 0


def _rows(con: sqlite3.Connection, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    return list(con.execute(sql, args).fetchall())


def _ratio(hit: int, total: int) -> float | None:
    return round(hit / total, 4) if total else None


def _events(con: sqlite3.Connection) -> dict[str, set[str]]:
    """type -> 出现过这种事件的 task_id 集合。

    另外派生一个 ``task.resumed.crash``：只有 payload 里带 ``crash_recovery=true``
    的恢复才算「崩溃/重启后接回来」。同一个 ``task.resumed`` 事件也会在审批恢复时
    发出（payload 里是 ``decision``），两者混在一起会把「点了一次批准」算成关窗恢复。
    """
    out: dict[str, set[str]] = {}
    for row in _rows(con, "select type, task_id, payload from agent_events"):
        kind = str(row["type"])
        task_id = str(row["task_id"])
        out.setdefault(kind, set()).add(task_id)
        if kind != "task.resumed":
            continue
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (ValueError, TypeError):
            payload = {}
        if isinstance(payload, dict) and payload.get("crash_recovery"):
            out.setdefault("task.resumed.crash", set()).add(task_id)
    return out


def compute(db_path: str | Path) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        events = _events(con) if _table(con, "agent_events") else {}
        tasks = (
            _rows(
                con,
                "select id, thread_id, status, goal_spec, failure_reason, result_summary from tasks",
            )
            if _table(con, "tasks")
            else []
        )
        goals = (
            {str(row["id"]) for row in _rows(con, "select id from goals")}
            if _table(con, "goals")
            else set()
        )

        def spec_of(row: sqlite3.Row) -> dict:
            raw = row["goal_spec"]
            if not raw:
                return {}
            try:
                value = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except (ValueError, TypeError):
                return {}
            return value if isinstance(value, dict) else {}

        threaded = [row for row in tasks if str(row["thread_id"] or "").strip()]
        # 分母只取 v0.10.0 之后由 Chat Supervisor 创建的会话任务：Phase 1 起
        # 每条新消息都会写 goal_spec.supervisor。把 v0.9.1 及更早的历史轮次也算进来，
        # 关联率会被永远压在个位数，指标就失去意义了。
        supervised = [row for row in threaded if "supervisor" in spec_of(row)]
        linked = [
            row
            for row in supervised
            if str(spec_of(row).get("goal_id") or "").strip()
            and str(spec_of(row).get("goal_id")) in goals
        ]
        orphans = [row for row in supervised if row not in linked]

        granted = events.get("approval.granted", set())
        mismatch = [
            row
            for row in tasks
            if str(row["id"]) in granted
            and "未获批准" in str(row["failure_reason"] or "")
        ]

        repair_started = events.get("repair.started", set())
        repair_evidence = events.get("repair.completed", set()) | events.get("verification.verdict", set())
        repaired = [tid for tid in repair_started if tid in repair_evidence]

        # 「关窗 / 重启后恢复」有两条真实路径，必须都进分母：
        #   ① 进程被杀后 watchdog 另起一次 attempt（agent_runs.attempts > 1）；
        #   ② 进程重启后由 `resume_after_crash` 把还在 running 的任务接回来
        #      （事件 task.resumed，payload 里带 crash_recovery=true）。
        # 注意不能用「存在 task.resumed」当判据：审批恢复也会发同一个事件
        # （payload 里是 decision），那样会把每次点「仅本次允许」都算成一次关窗恢复。
        status_by_task = {str(row["id"]): str(row["status"]) for row in tasks}
        resumed_ids: set[str] = {
            str(tid) for tid in events.get("task.resumed.crash", set())
        }
        if _table(con, "agent_runs"):
            for row in _rows(con, "select task_id, status, attempts from agent_runs"):
                if int(row["attempts"] or 0) > 1:
                    resumed_ids.add(str(row["task_id"]))
        # 与另外三项口径对齐：有 v0.10.0 任务时只看它们。把 v0.9.1 及更早的历史续跑
        # 算进来，这个比率反映的是「历史数据里有多少续跑成功」，而不是这一版的能力。
        scoped = {str(row["id"]) for row in supervised} if supervised else None
        if scoped:
            resumed_ids &= scoped
        recovered_ids = {tid for tid in resumed_ids if status_by_task.get(tid) == "succeeded"}
        resumed = sorted(resumed_ids)
        recovered = sorted(recovered_ids)

        leak = [
            row
            for row in tasks
            if str(row["status"]) == "succeeded"
            and any(mark in str(row["result_summary"] or "") for mark in _PROCESS_MARKS)
        ]
        succeeded = [row for row in tasks if str(row["status"]) == "succeeded"]

        return {
            "conversationGoalLinkRate": _ratio(len(linked), len(supervised)),
            "orphanTaskRate": _ratio(len(orphans), len(supervised)),
            "approvalStateMismatch": len(mismatch),
            "resumeAfterCloseRate": _ratio(len(recovered), len(resumed)),
            "automaticRepairEvidenceRate": _ratio(len(repaired), len(repair_started)),
            "currentTurnAnswerLeak": len(leak),
            "_counts": {
                "tasks": len(tasks),
                "threaded_tasks": len(threaded),
                "v165_supervised_tasks": len(supervised),
                "goals": len(goals),
                "linked": len(linked),
                "orphans": len(orphans),
                "repair_started": len(repair_started),
                "repaired_with_evidence": len(repaired),
                "resumed_runs": len(resumed),
                "recovered_runs": len(recovered),
                "succeeded_tasks": len(succeeded),
            },
        }
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="计算 v165 Super Agent 新增质量门槛")
    parser.add_argument("--db", required=True, help="blank.db 路径（只读）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args()
    report = compute(args.db)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    for key, value in report.items():
        if key.startswith("_"):
            continue
        shown = "n/a（无样本）" if value is None else value
        print(f"{key} = {shown}")
    print("counts =", json.dumps(report["_counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
