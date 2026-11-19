#!/usr/bin/env python3
"""同一个坑摔了几次：`repeat_mistake_rate` 基线。

## 为什么是这个名字

自学习做完之后最容易被问的一句话是"它到底有用吗"。benchmark 分数回答不了——
我们的题不是公开集，跑一遍成本也高。能回答的是**同一个失败有没有反复出现**：
如果 harness 真的从踩坑里学到了东西，第二次碰到同一个形状时该有变化。

指标：
    repeat_mistake_rate = 出现在 ≥2 个不同任务里的签名数 / 签名总数

它不衡量"任务成功率"，只衡量"教训有没有沉淀"。低不一定好（可能只是任务都很简单），
但同一批题前后两次对比是有意义的。

## 口径（别改着玩）

- 只算 `tool.completed` 且 `ok=false`
- **`denied` 不算 mistake**：那是审批/策略拒绝，是系统在正常工作，不是 agent 犯错
- 签名 = 工具名 + 状态 + 归一化后的错误摘要（去掉数字、路径、引号内容，
  因为"任务 A 的 12 行"和"任务 B 的 30 行"是同一个错误）
- 按 task_id 去重：一个任务里同一个签名错十次，只算这个任务碰到过一次

用法：
    python scripts/repeat_mistakes.py --db services/api/data/blank_dev.db
    python scripts/repeat_mistakes.py --db ... --since 2026-11-19   # 只算某天之后
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict

# 归一化时抹掉的碎片：数字、行号、具体路径、引号里的内容。
_DIGITS = re.compile(r"\d+")
_PATHS = re.compile(r"[A-Za-z]:[\\/][^\s\"']+|(?:\.{0,2}/)[^\s\"']+")
_QUOTED = re.compile(r"[\"'`][^\"'`]{0,120}[\"'`]")
_WS = re.compile(r"\s+")


# 少于这么多唯一签名时，比率的置信区间宽到没有意义。
MIN_COMPARABLE_SIGNATURES = 20


def signature(payload: dict) -> str:
    tool = str(payload.get("tool") or payload.get("tool_name") or "?").strip()
    status = str(payload.get("status") or "").strip() or "failed"
    text = str(payload.get("summary") or payload.get("message") or payload.get("error") or "")
    text = _QUOTED.sub("<x>", text)
    text = _PATHS.sub("<path>", text)
    text = _DIGITS.sub("<n>", text)
    text = _WS.sub(" ", text).strip().lower()
    return f"{tool}|{status}|{text[:80]}"


def analyse(db_path: str, since: str = "") -> dict:
    conn = sqlite3.connect(db_path)
    query = "select task_id, payload, created_at from agent_events where type='tool.completed'"
    params: tuple = ()
    if since:
        query += " and created_at >= ?"
        params = (since,)
    per_task: dict[str, set[str]] = defaultdict(set)
    total_failures = 0
    skipped_denied = 0
    for task_id, raw, _created in conn.execute(query, params):
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if payload.get("ok") is not False:
            continue
        if str(payload.get("status") or "") == "denied":
            skipped_denied += 1
            continue
        total_failures += 1
        per_task[str(task_id)].add(signature(payload))
    conn.close()

    counter: dict[str, set[str]] = defaultdict(set)
    for task_id, signatures in per_task.items():
        for item in signatures:
            counter[item].add(task_id)

    repeated = {sig: tasks for sig, tasks in counter.items() if len(tasks) >= 2}
    return {
        "db": db_path,
        "since": since or "(全部)",
        "failures": total_failures,
        "denied_excluded": skipped_denied,
        "signatures": len(counter),
        "repeated_signatures": len(repeated),
        "repeat_mistake_rate": round(len(repeated) / len(counter), 3) if counter else 0.0,
        "top": [
            {"signature": sig, "tasks": len(tasks), "sample": sorted(tasks)[0][:8]}
            for sig, tasks in sorted(repeated.items(), key=lambda kv: len(kv[1]), reverse=True)[:8]
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="算 repeat_mistake_rate")
    parser.add_argument("--db", required=True)
    parser.add_argument("--since", default="", help="只算 created_at >= 这个值（ISO 字符串）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args()
    result = analyse(args.db, args.since)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return
    print(f"库：{result['db']}  区间：{result['since']}")
    print(f"失败调用 {result['failures']} 次（另有 {result['denied_excluded']} 次 denied 不计）")
    print(f"唯一签名 {result['signatures']} 个，其中 {result['repeated_signatures']} 个跨任务重复")
    print(f"repeat_mistake_rate = {result['repeat_mistake_rate']}")
    # 小样本警告不是客套话：实测把时间切成两段做前后对比时，机制后只剩 6 次失败、
    # 4 个签名，算出来 0.5 比机制前的 0.071 还高——那不是"机制没用"，
    # 是分母小到任何一个新签名都能把比率翻倍。**签名数不到 20 就别做对比结论。**
    if result["signatures"] < MIN_COMPARABLE_SIGNATURES:
        print(
            f"⚠ 只有 {result['signatures']} 个签名（不到 {MIN_COMPARABLE_SIGNATURES}），"
            "这个比率波动很大，不要拿它做前后对比结论。"
        )
    if result["top"]:
        print("\n重复最多的几个：")
        for item in result["top"]:
            print(f"  {item['tasks']} 个任务 | {item['signature'][:100]}")


if __name__ == "__main__":
    main()
