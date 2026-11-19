#!/usr/bin/env python3
"""经验库体检：过载、矛盾、低质、卡住。

## 为什么要有这个

自学习最大的风险不是"学得少"，是**学了错的东西然后照着做**。实证：两条互相矛盾的
记忆同时被检索到时，遵从率 63–72%，而遵从之后任务成功率塌到 17–31%（2607.10608）。
等于说"有两条矛盾记忆"比"一条都没有"更坏。

而 2610.04083 又证明"关掉记忆不是防御"——把记忆工具拿掉，74% 的会话会改用文件系统
接着写。所以只能靠**看得见**：把问题摆出来让人判。

这个脚本只报告，不删任何东西。

用法：
    python scripts/lint_experience.py --workspace E:/projects/kbgai/kbgai
    python scripts/lint_experience.py --workspace ... --db-dir services/api/data/experience
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "services" / "api" / "src"))


def main() -> None:
    parser = argparse.ArgumentParser(description="经验库体检")
    parser.add_argument("--workspace", default=str(REPO), help="哪个工作区的经验库")
    parser.add_argument(
        "--db-dir",
        default=str(REPO / "services" / "api" / "data" / "experience"),
        help="经验库目录（默认 services/api/data/experience）",
    )
    args = parser.parse_args()

    from blank_api.services.agent.experience import ExperienceStore, lint, render_lint

    store = ExperienceStore(args.db_dir)
    path = store.path_for(args.workspace)
    entries = store.load(args.workspace)
    if not entries:
        print(f"（{path.name} 里还没有任何经验）")
        return

    active = sum(1 for item in entries if item.status == "active")
    candidate = sum(1 for item in entries if item.status == "candidate")
    retired = sum(1 for item in entries if item.status == "retired")
    print(f"库：{path}")
    print(f"条目 {len(entries)} 条（active {active} / candidate {candidate} / retired {retired}）")
    print()
    print(render_lint(lint(entries)))


if __name__ == "__main__":
    main()
