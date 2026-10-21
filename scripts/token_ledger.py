#!/usr/bin/env python3
"""Token 台账：把"花了多少"拆成"花在哪、值不值"。

只看总额没有意义——9M token 里可能 7M 出自一个跑废的任务。这个脚本读
``blank.db``（只读，不写库），按任务把 token 归到三类去处：

- ``delivered``  有文件改动且成功：钱换成了产物
- ``advised``    成功但没有改动：钱换成了判断（调研/问答），本该很便宜
- ``wasted``     失败、取消、跑飞：钱换成了废纸

同时给出四个用来判"贵不贵"的口径：

- ``per_call_context``  单次调用平均上下文。这是成本主因：每一步都要把整段
  上下文重发一遍，步数不变而上下文翻倍，账单就翻倍。
- ``cache_hit``         命中缓存的比例。命中的部分单价低，但绝对量仍在。
- ``failed_tool_rate``  工具调用失败率。失败一次要重试，是"多跑几步"的源头。
- ``tokens_per_file``   每个有效改动文件的 token。跨模型、跨任务可比。

用法::

    python scripts/token_ledger.py                     # 默认读本机客户端库
    python scripts/token_ledger.py --db path/to/blank.db
    python scripts/token_ledger.py --compare 2026-10-21T06:00,2026-10-21T08:00
    python scripts/token_ledger.py --json ledger.json

``--compare`` 给两个 ISO 时间点，把"改造前 / 改造后"并排放出来。它比较的是
同一台机器上同一批用户请求，所以不需要造数据，也不需要模型配合。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("APPDATA", "")) / "blank-ai-workbench-desktop" / "blank.db"

# 任务状态归到三个去处。``running`` 单列：它的钱还没结算，算进任何一类都会失真。
DELIVERED_STATUSES = {"succeeded", "completed"}
DEAD_STATUSES = {"failed", "cancelled", "error"}


def connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"找不到数据库：{path}\n用 --db 指定，或先启动一次客户端。")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "select name from sqlite_master where type='table' and name=?", (name,)
    ).fetchone()
    return row is not None


def collect(connection: sqlite3.Connection, since: str | None, until: str | None) -> list[dict]:
    """一个任务一行，把用量、工具调用、改动文件全部聚到任务上。"""
    where, params = [], []
    if since:
        where.append("t.created_at >= ?")
        params.append(since)
    if until:
        where.append("t.created_at < ?")
        params.append(until)
    clause = f"where {' and '.join(where)}" if where else ""
    tasks = connection.execute(
        f"""select t.id, t.title, t.status, t.steps_used, t.created_at, t.failure_reason,
                   (select count(*) from model_usage m where m.task_id = t.id) calls,
                   (select coalesce(sum(m.prompt_tokens),0) from model_usage m where m.task_id = t.id) prompt,
                   (select coalesce(sum(m.completion_tokens),0) from model_usage m where m.task_id = t.id) completion,
                   (select coalesce(sum(m.cached_tokens),0) from model_usage m where m.task_id = t.id) cached,
                   (select coalesce(max(m.prompt_tokens),0) from model_usage m where m.task_id = t.id) peak_context
            from tasks t {clause} order by t.created_at""",
        params,
    ).fetchall()

    records: list[dict] = []
    for row in tasks:
        tools = connection.execute(
            """select name, status from tool_calls where task_id = ?""", (row["id"],)
        ).fetchall()
        files = connection.execute(
            """select path, length(diff) size from file_changes where task_id = ?""", (row["id"],)
        ).fetchall()
        # 同一文件改多次算一个文件：要的是"动了几个文件"，不是"打了几次补丁"。
        touched = {item["path"] for item in files}
        failed_tools = [item for item in tools if item["status"] != "ok"]
        prompt = int(row["prompt"] or 0)
        completion = int(row["completion"] or 0)
        record = {
            "id": row["id"],
            "title": (row["title"] or "").strip()[:40],
            "status": row["status"],
            "created_at": row["created_at"],
            "steps": int(row["steps_used"] or 0),
            "calls": int(row["calls"] or 0),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cached_tokens": int(row["cached"] or 0),
            "tokens": prompt + completion,
            "peak_context": int(row["peak_context"] or 0),
            "tools": len(tools),
            "failed_tools": len(failed_tools),
            "files": len(touched),
            "failure_reason": (row["failure_reason"] or "")[:120],
        }
        record["bucket"] = bucket_of(record)
        records.append(record)
    return records


def bucket_of(record: dict) -> str:
    if record["status"] in DEAD_STATUSES:
        return "wasted"
    if record["status"] not in DELIVERED_STATUSES:
        return "running"
    return "delivered" if record["files"] > 0 else "advised"


def summarize(records: list[dict]) -> dict:
    total = sum(item["tokens"] for item in records)
    by_bucket: dict[str, dict] = {}
    for key in ("delivered", "advised", "wasted", "running"):
        rows = [item for item in records if item["bucket"] == key]
        tokens = sum(item["tokens"] for item in rows)
        by_bucket[key] = {
            "tasks": len(rows),
            "tokens": tokens,
            "share": round(tokens / total, 4) if total else 0.0,
        }
    calls = sum(item["calls"] for item in records)
    prompt = sum(item["prompt_tokens"] for item in records)
    cached = sum(item["cached_tokens"] for item in records)
    tools = sum(item["tools"] for item in records)
    failed_tools = sum(item["failed_tools"] for item in records)
    files = sum(item["files"] for item in records)
    delivered_tokens = by_bucket["delivered"]["tokens"]
    delivered = [item["tokens"] for item in records if item["bucket"] == "delivered"]
    return {
        "tasks": len(records),
        "calls": calls,
        "prompt_tokens": prompt,
        "completion_tokens": sum(item["completion_tokens"] for item in records),
        "total_tokens": total,
        "cache_hit": round(cached / prompt, 4) if prompt else 0.0,
        # 单次调用的平均上下文：成本的地基。步数不变、上下文翻倍，账单就翻倍。
        "per_call_context": round(prompt / calls) if calls else 0,
        "peak_context": max((item["peak_context"] for item in records), default=0),
        "failed_tool_rate": round(failed_tools / tools, 4) if tools else 0.0,
        # 每个工具调用对应几次模型调用。健康的执行大约是 1:1~1.5:1；数字高说明在空转
        # （反复审查、反复重试），每一步都在把整段上下文重发一遍。
        "calls_per_tool": round(calls / tools, 2) if tools else 0.0,
        "tools": tools,
        "files": files,
        "buckets": by_bucket,
        "median_task_tokens": int(statistics.median([item["tokens"] for item in records])) if records else 0,
        "median_delivered_tokens": int(statistics.median(delivered)) if delivered else 0,
        "tokens_per_file": round(delivered_tokens / files) if files else 0,
        # 有效产出占比：钱真正换成产物的比例。这是唯一一个"越低越该改"的总分。
        "effective_share": by_bucket["delivered"]["share"],
    }


def top_offenders(records: list[dict], limit: int = 5) -> list[dict]:
    rows = sorted(records, key=lambda item: item["tokens"], reverse=True)[:limit]
    return [
        {
            "title": item["title"],
            "status": item["status"],
            "calls": item["calls"],
            "tokens": item["tokens"],
            "files": item["files"],
            "failed_tools": item["failed_tools"],
            # 逐个任务给"几次调用才换一次工具动作"：空转是单任务现象，
            # 混在总数里会被正常任务平均掉。
            "per_tool": round(item["calls"] / item["tools"], 1) if item["tools"] else 0.0,
        }
        for item in rows
    ]


def _thousands(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}K"
    return str(value)


def render(summary: dict, records: list[dict]) -> str:
    lines: list[str] = []
    lines.append("== 总账 ==")
    lines.append(
        f"任务 {summary['tasks']} · 模型调用 {summary['calls']} · "
        f"prompt {_thousands(summary['prompt_tokens'])} · 输出 {_thousands(summary['completion_tokens'])} · "
        f"合计 {_thousands(summary['total_tokens'])}"
    )
    lines.append(
        f"单次调用平均上下文 {_thousands(summary['per_call_context'])} token（峰值 {_thousands(summary['peak_context'])}）"
        f" · 缓存命中 {summary['cache_hit']:.1%} · 工具失败率 {summary['failed_tool_rate']:.1%}"
    )
    lines.append(
        f"每次工具调用对应 {summary['calls_per_tool']:.1f} 次模型调用"
        f"（明显大于 2 就是在空转：没做新事，却在反复发上下文）"
    )
    lines.append("")
    lines.append("== 花在哪 ==")
    labels = {
        "delivered": "有改动且成功 —— 换成了产物",
        "advised": "成功但无改动 —— 换成了判断",
        "wasted": "失败/取消/跑飞 —— 全废",
        "running": "进行中 —— 未结算",
    }
    for key in ("delivered", "advised", "wasted", "running"):
        item = summary["buckets"][key]
        if not item["tasks"]:
            continue
        lines.append(
            f"{labels[key]:<28} {_thousands(item['tokens']):>8}  {item['share']:>6.1%}  {item['tasks']} 个任务"
        )
    lines.append("")
    lines.append("== 效率 ==")
    lines.append(f"每任务 token 中位 {_thousands(summary['median_task_tokens'])}")
    lines.append(f"每个有效改动文件 {_thousands(summary['tokens_per_file'])} token")
    lines.append(f"有效产出占比 {summary['effective_share']:.1%}（目标：先把 wasted 压到 10% 以下）")
    lines.append("")
    lines.append("== 最贵的任务 ==")
    for item in top_offenders(records):
        lines.append(
            f"{_thousands(item['tokens']):>8}  {item['calls']:>3} 次调用  "
            f"{item['files']} 个文件  {item['failed_tools']} 次工具失败  "
            f"{item['per_tool']:.1f} 次调用/工具  [{item['status']}] {item['title']}"
        )
    return "\n".join(lines)


def render_compare(before: dict, after: dict) -> str:
    rows = [
        ("任务数", "tasks", 0),
        ("模型调用", "calls", 0),
        ("prompt token", "prompt_tokens", 0),
        ("合计 token", "total_tokens", 0),
        ("单次调用上下文", "per_call_context", 0),
        ("缓存命中", "cache_hit", 2),
        ("工具失败率", "failed_tool_rate", 2),
        ("每次工具/模型调用", "calls_per_tool", 2),
        ("每任务中位 token", "median_task_tokens", 0),
        ("每文件 token", "tokens_per_file", 0),
        ("有效产出占比", "effective_share", 2),
    ]
    lines = ["== 改造前 vs 改造后 ==", f"{'指标':<18}{'前':>12}{'后':>12}{'变化':>12}"]
    for label, key, digits in rows:
        left = before[key]
        right = after[key]
        if key == "calls_per_tool":
            left_s, right_s = f"{left:.2f}", f"{right:.2f}"
            delta = f"{right - left:+.2f}"
        elif digits == 2:
            left_s, right_s = f"{left:.1%}", f"{right:.1%}"
            delta = f"{(right - left) * 100:+.1f}pt"
        else:
            left_s, right_s = _thousands(int(left)), _thousands(int(right))
            delta = f"{(right - left) / left:+.0%}" if left else "—"
        lines.append(f"{label:<18}{left_s:>12}{right_s:>12}{delta:>12}")
    lines.append("")
    lines.append("注：两边比较的是同一台机器上的真实使用记录，不含造出来的数据。")
    return "\n".join(lines)


def split_window(value: str) -> tuple[str | None, str | None]:
    """``A,B`` → (A, B)；只给 A 表示"A 之前 / A 之后"。"""
    parts = [item.strip() for item in value.split(",") if item.strip()]
    if len(parts) != 2:
        raise SystemExit("--compare 需要两个 ISO 时间点，例如 2026-10-21T06:00,2026-10-21T08:00")
    return parts[0], parts[1]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="把 token 花销拆成'花在哪、值不值'")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="blank.db 路径（只读打开）")
    parser.add_argument("--since", help="只要这个时间之后创建的任务（ISO 时间）")
    parser.add_argument("--until", help="只要这个时间之前创建的任务（ISO 时间）")
    parser.add_argument(
        "--compare",
        help="两个 ISO 时间点 A,B：比较 A 之前与 B 之后两段（用来量化改造效果）",
    )
    parser.add_argument("--json", help="把明细写到这个 JSON 文件")
    args = parser.parse_args(argv)

    connection = connect(Path(args.db))
    if not _table_exists(connection, "model_usage"):
        raise SystemExit("这个库里没有 model_usage 表：不是本应用的数据库，或版本过旧。")

    if args.compare:
        left, right = split_window(args.compare)
        before = summarize(collect(connection, None, left))
        after = summarize(collect(connection, right, None))
        print(render_compare(before, after))
        if args.json:
            Path(args.json).write_text(
                json.dumps({"before": before, "after": after}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return 0

    records = collect(connection, args.since, args.until)
    if not records:
        print("这段时间里没有任务记录。")
        return 0
    summary = summarize(records)
    print(render(summary, records))
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {"summary": summary, "tasks": records, "generated_at": datetime.now().isoformat()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
