#!/usr/bin/env python
"""心跳：系统自己醒来查一遍，然后出报告。只报告，不动手。

    python scripts/heartbeat.py
    python scripts/heartbeat.py --json
    python scripts/heartbeat.py --stuck-minutes 10 --window-hours 6

为什么不动手：自动处置必须建立在"可验证 + 可回滚"之上，而进化评价刚有骨架、
回滚只覆盖技能。现在让它自动改，等于把"跑偏"升级成"自己动手跑偏"。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "services" / "api" / "src"))

from blank_api.services.heartbeat import (  # noqa: E402
    STUCK_APPROVAL_MINUTES,
    STUCK_RUNNING_MINUTES,
    WINDOW_HOURS,
    render,
    scan,
)

DEFAULT_DB = REPO / "services" / "api" / "data" / "blank_dev.db"


def main() -> int:
    parser = argparse.ArgumentParser(description="心跳巡检（只报告）")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--json", action="store_true", help="输出 JSON，便于接定时任务")
    parser.add_argument("--stuck-minutes", type=int, default=STUCK_RUNNING_MINUTES)
    parser.add_argument("--approval-minutes", type=int, default=STUCK_APPROVAL_MINUTES)
    parser.add_argument("--window-hours", type=int, default=WINDOW_HOURS)
    args = parser.parse_args()

    findings = scan(
        args.db,
        stuck_running_minutes=args.stuck_minutes,
        stuck_approval_minutes=args.approval_minutes,
        window_hours=args.window_hours,
    )
    if args.json:
        print(json.dumps([item.as_dict() for item in findings], ensure_ascii=False, indent=2))
        return 0
    print(render(findings, window_hours=args.window_hours))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
