#!/usr/bin/env python
"""进化评价：改之前存一份，改完再存一份，然后比。

    python scripts/evolution.py --snapshot --label 加心跳前
    python scripts/evolution.py --compare data/evolution/<文件>.json
    python scripts/evolution.py --list

纪律：一侧不足 20 条已结束任务就明确说"样本不足，不下结论"——不给一个看着漂亮的假数。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "services" / "api" / "src"))

from blank_api.services.harness.evolution import (  # noqa: E402
    MIN_SAMPLE,
    SnapshotStore,
    compare,
    render,
    snapshot,
)

DEFAULT_DB = REPO / "services" / "api" / "data" / "blank_dev.db"
DEFAULT_DIR = REPO / "services" / "api" / "data" / "evolution"


def main() -> int:
    parser = argparse.ArgumentParser(description="进化评价：快照与对照")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="快照存放目录")
    parser.add_argument("--snapshot", action="store_true", help="存一份快照")
    parser.add_argument("--label", default="", help="这次快照的说明（改了什么）")
    parser.add_argument("--since", default="", help="起始时间 YYYY-MM-DD HH:MM:SS")
    parser.add_argument("--until", default="", help="截止时间")
    parser.add_argument("--compare", default="", help="拿这份旧快照和当前比")
    parser.add_argument("--list", action="store_true", help="列出已有快照")
    args = parser.parse_args()

    store = SnapshotStore(args.dir)

    if args.list:
        files = store.list()
        if not files:
            print("还没有快照。先跑 --snapshot --label <这次改动>。")
            return 0
        for path in files:
            item = store.load(path)
            print(f"{item.at}  {item.label}  样本 {item.sample}  {path.name}")
        return 0

    if args.compare:
        before = store.load(args.compare)
        after = snapshot(args.db, label="当前", since=args.since, until=args.until)
        report = compare(before, after, min_sample=MIN_SAMPLE)
        print(f"基线：{before.label}（{before.at}，样本 {before.sample}）")
        print()
        print(render(report))
        return 0

    if args.snapshot:
        snap = snapshot(args.db, label=args.label or "未命名", since=args.since, until=args.until)
        path = store.save(snap)
        print(f"已存快照：{path}")
        print(f"样本 {snap.sample} 条")
        for key, value in snap.metrics.items():
            print(f"  {key:24} {value}")
        if snap.sample < MIN_SAMPLE:
            print(f"\n⚠ 只有 {snap.sample} 条（门槛 {MIN_SAMPLE}），以后拿它做对比会不准。")
        return 0

    print(render(compare(
        snapshot(args.db, label="全部", until="1970-01-01 00:00:00"),
        snapshot(args.db, label="当前"),
    )))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
