#!/usr/bin/env python
"""使用者模型：看得见、改得动、撤得掉。

## 为什么必须有这个入口

调研里最一致的一条（`docs/self-learning-devtools.md`、`docs/self-learning-consumer.md`）：
**能让人信任的自学，都是白盒**。没有一家把用户偏好存成不可读的黑盒；
反面案例是 GitLab Duo——关掉记忆但数据保留、删掉的推断 30 天内复活、没有审计日志，
信任就是这么崩的。

所以"agent 学了我什么"必须能一句话回答，而且每条都要能看到**我当时说的原话**。

## 两个模式

    python scripts/persona.py                     # 我学了什么（含原话）
    python scripts/persona.py --lint              # 体检：冲突 / 无原话 / 未确认
    python scripts/persona.py --promote <指纹>     # 这一条确实是我的一贯要求，立刻生效
    python scripts/persona.py --retire <指纹>      # 这条不对，别再用了
    python scripts/persona.py --report            # 我最近被拦了多少次（"像不像我"的度量）

退役不是删除：条目还在文件里，只是不再被注入。这一点比"删掉"重要——
删掉之后连"它曾经学错过什么"都不知道了。

## 关于 `--report` 的口径

`repeat_mistake_rate`（`scripts/repeat_mistakes.py`）衡量的是"少犯错"，
不衡量"像不像他"。像不像他看三个数：

- **驳回率** = 驳回 / (驳回 + 批准)。他在拦我多少次。
- **需要解释的驳回占比** = 带反馈的驳回 / 驳回。**这个数越高，说明我越不懂他**——
  如果我知道他要什么，他点一下"不允许"就够了，不用打字告诉我为什么。
- **一次通过率** = 没进过修复轮就成功的任务 / 有结论的任务。返工多说明交出来的
  东西不是他想要的那个样子。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "services" / "api" / "src"))

from blank_api.services.agent.persona import (  # noqa: E402
    MIN_OCCURRENCES,
    PersonaStore,
    extract_instructions,
    lint,
    preference_from_quote,
    render_lint,
    render_standards,
    standards_of,
)

STATUS_MARK = {"active": "生效", "candidate": "待确认", "conflict": "冲突", "retired": "已退役"}


def show(store: PersonaStore, workspace: str) -> None:
    entries = store.load(workspace)
    if not entries:
        print("还没有学到任何关于你的东西。")
        print("（它会从你说过的话里学：任务描述里的明确要求、驳回时写的理由。")
        print(f"  同一句要在 {MIN_OCCURRENCES} 个任务里出现过才算'一贯'，不注入单次发言。）")
        return

    live = [item for item in entries if item.status != "retired"]
    print(f"关于你，它学到了 {len(live)} 条（库文件：{store.path_for(workspace).name}）\n")
    for item in sorted(live, key=lambda e: (e.status != "active", -e.occurrences)):
        mark = STATUS_MARK.get(item.status, item.status)
        if item.origin == "harvested" and item.status == "candidate":
            mark = "待你确认"
        print(f"[{mark}] [{item.label}] {item.rule}")
        print(f"        出现 {item.occurrences} 次 · 置信 {item.confidence} · 指纹 {item.fingerprint}")
        for quote in item.quotes[-2:]:
            source = "驳回时说的" if quote.get("source") == "denial" else "任务描述里"
            print(f"        你说过（{source}）：{quote.get('text', '')[:100]}")
        if item.origin == "harvested":
            print("        （从任务描述里摘的；重复多也可能只是同一批脚本跑了多遍，确认后才生效）")
        if item.status == "conflict":
            print("        ⚠ 这条与同维度的另一条方向相反，两条都停用了；用 --keep 留一条")
        print()

    retired = [item for item in entries if item.status == "retired"]
    if retired:
        print(f"另有 {len(retired)} 条已退役（不再注入，但保留可查）：")
        for item in retired:
            print(f"  · {item.rule[:70]}")


def keep(store: PersonaStore, workspace: str, fingerprint: str) -> None:
    """从一对冲突里留一条：留下的转 active，其余同维度的冲突条目退役。"""
    entries = store.load(workspace)
    target = next((item for item in entries if item.fingerprint.startswith(fingerprint)), None)
    if target is None:
        print(f"没找到指纹以 {fingerprint} 开头的条目。")
        return
    for item in entries:
        if item is target:
            continue
        if item.status == "conflict" and item.dimension == target.dimension:
            item.status = "retired"
    target.status = "active"
    target.last_confirmed = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.save(workspace, entries)
    print(f"已留：{target.rule}")
    print("同维度的其它冲突条目已退役（不是删除，可查）。")


def main() -> int:
    parser = argparse.ArgumentParser(description="使用者模型：查看 / 确认 / 退役")
    parser.add_argument(
        "--workspace", default=str(REPO), help="工作区（默认仓库根）"
    )
    parser.add_argument(
        "--db-dir",
        default=str(REPO / "services" / "api" / "data" / "experience"),
        help="存放目录（默认 services/api/data/experience）",
    )
    parser.add_argument("--lint", action="store_true", help="体检")
    parser.add_argument(
        "--harvest",
        action="store_true",
        help="扫历史任务描述，摘出候选（**只出候选，不自动生效**）",
    )
    parser.add_argument("--dry-run", action="store_true", help="和 --harvest 一起用：只看不写")
    parser.add_argument(
        "--tell",
        default="",
        help="他当面提的要求，记下来并立刻生效（如 --tell \"做事要严谨\"）",
    )
    parser.add_argument(
        "--global",
        dest="global_scope",
        action="store_true",
        help="和 --tell 一起用：这是跨项目的要求（存全局，每个仓库都生效）",
    )
    parser.add_argument("--promote", default="", help="确认这一条，立刻生效（指纹前缀）")
    parser.add_argument("--keep", default="", help="冲突里留这一条（指纹前缀）")
    parser.add_argument("--retire", default="", help="退役这一条（指纹前缀）")
    parser.add_argument("--retire-all", action="store_true", help="退役全部")
    parser.add_argument(
        "--crystallize",
        action="store_true",
        help="看他的标准会被沉淀成什么 Skill（只预览，不写库）",
    )
    parser.add_argument("--report", action="store_true", help="度量：我有多懂他")
    parser.add_argument(
        "--db", default=str(REPO / "services" / "api" / "data" / "blank_dev.db"), help="事件库"
    )
    args = parser.parse_args()

    store = PersonaStore(args.db_dir)

    if args.harvest:
        return harvest(store, args.workspace, args.db, dry_run=args.dry_run)

    if args.crystallize:
        from blank_api.services.agent.skillsmith import drafts_for

        drafts = drafts_for(store, args.workspace)
        if not drafts:
            print("还没有已生效的做事标准——先说一句「做事要严谨」，再来看。")
            return 0
        print(f"会沉淀成 {len(drafts)} 个 Skill（每次任务收尾自动提交为提案，要采用才生效）：\n")
        for item in drafts:
            print(f"=== {item['name']} ===")
            print(item["skill_md"])
        return 0

    if args.report:
        return report(args.db)

    if args.tell:
        return tell(
            store, args.workspace, args.tell,
            scope="global" if args.global_scope else "project",
        )

    if args.keep:
        keep(store, args.workspace, args.keep)
        return 0

    if args.promote:
        entries = store.load(args.workspace)
        target = next((item for item in entries if item.fingerprint.startswith(args.promote)), None)
        if target is None:
            print(f"没找到指纹以 {args.promote} 开头的条目。")
            return 1
        target.status = "active"
        # 人工确认也是一次"他确认过"，置信度抬到 0.8——但**不给 1.0**：
        # 连他本人都可能是这次这么想，下次不。
        target.confidence = max(float(target.confidence), 0.8)
        # 同时续期：这也是"确认"的一种，否则刚 promote 的条目可能当天就过期。
        target.last_confirmed = datetime.now(timezone.utc).isoformat(timespec="seconds")
        store.save(args.workspace, entries)
        print(f"已生效：{target.rule}")
        return 0

    if args.retire or args.retire_all:
        entries = store.load(args.workspace)
        hit = 0
        for item in entries:
            if item.status == "retired":
                continue
            if args.retire_all or item.fingerprint.startswith(args.retire):
                item.status = "retired"
                hit += 1
                print(f"已退役：{item.rule[:70]}")
        if not hit:
            print("没有符合条件的条目。")
            return 1
        store.save(args.workspace, entries)
        return 0

    if args.lint:
        print(render_lint(lint(store.load(args.workspace))))
        return 0

    show(store, args.workspace)
    return 0


def tell(store: PersonaStore, workspace: str, text: str, *, scope: str = "project") -> int:
    """他当面提的要求：记下来，**一次就生效**。

    为什么这一次不用"出现两次"：那个门槛是给**自动推断**用的——从行为里猜他想要什么，
    猜错代价高，所以要攒证据。他当面说明白话是另一回事，那是最强的证据本身。
    （消费级调研里的同一条：钉钉只认"帮我记住……"这类触发指令，且上限 10 条。）

    代价是：他会看到一条自己刚说过的话立刻生效。这正是想要的——他能马上看见
    "它记住了"，也能马上撤。
    """
    touched = store.record_assertion(workspace, text, scope=scope)
    if not touched:
        print("没从这句话里听出可执行的要求——换一种说法，比如「做数据库改动必须先问我」。")
        return 1
    where = "所有项目" if scope == "global" else "这个项目"
    print(f"记住了 {len(touched)} 条（{where}生效）：")
    for item in touched:
        print(f"  [{item.label}] {item.rule}")
    entries = store.load_all(workspace)
    standards = standards_of(entries)
    if standards:
        print()
        print(render_standards(standards))
        print()
        print("（这几条工序会在每次任务收尾时逐条核对，结果写进 standard.conformance 事件。）")
    return 0


def harvest(store: PersonaStore, workspace: str, db_path: str, *, dry_run: bool = False) -> int:
    """从历史任务描述里摘候选。

    为什么是**显式运行**而不是任务开始时顺手学：任务描述写的是"这一次"，
    不是"他一贯"。我们拿 111 个真实任务试过，自动学的结果是把冒烟脚本
    （"必须用 run_shell 执行 echo smoke-ok"）学成了"一贯要求"，还因为同一段脚本
    跑了 26 遍而晋级。重复不等于一贯——得看重复的是不是同一个人的同一个意图。

    所以这条路只产出候选，要你在这里点一下才生效。低频、要审视，本来就不该自动。
    """
    path = Path(db_path)
    if not path.exists():
        print(f"找不到事件库：{path}")
        return 1
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "select id, goal from tasks where goal is not null and goal != ''"
        ).fetchall()
    finally:
        conn.close()

    before = {item.fingerprint for item in store.load(workspace)}
    staged: dict[str, dict] = {}
    for task_id, goal in rows:
        for clause in extract_instructions(goal, task_specific=True):
            digest = hashlib.sha256(
                "|".join([clause, "project", str(workspace).lower()]).encode("utf-8")
            ).hexdigest()[:16]
            slot = staged.setdefault(digest, {"rule": clause, "tasks": []})
            slot["tasks"].append(task_id[:8])

    if not staged:
        print(f"扫了 {len(rows)} 个任务描述，没有摘出像'一贯要求'的东西。")
        print("（这对本仓库是正常的：历史任务大多是一次性脚本，不是你在提要求。）")
        return 0

    print(f"扫了 {len(rows)} 个任务描述，摘出 {len(staged)} 条候选（**都还没生效**）：\n")
    for digest, slot in sorted(staged.items(), key=lambda kv: -len(kv[1]["tasks"])):
        mark = "（库里已有）" if digest in before else ""
        print(f"  {len(slot['tasks'])} 次  {slot['rule'][:80]} {mark}")
        print(f"         指纹 {digest}  来自 {', '.join(slot['tasks'][:3])}")
    print()
    print("这些是从任务描述里摘的，重复多也可能只是同一批脚本跑了很多遍，")
    print("所以默认**一条都不生效**。确认哪条是你要的，就：")
    print("    python scripts/persona.py --promote <指纹>")
    if dry_run:
        print("\n（--dry-run：没有写入。）")
        return 0

    written = 0
    for task_id, goal in rows:
        for clause in extract_instructions(goal, task_specific=True):
            store.record(
                workspace,
                preference_from_quote(
                    clause,
                    task_id=task_id,
                    source="goal",
                    scope="project",
                    scope_ref=str(workspace),
                    origin="harvested",
                ),
            )
            written += 1
    print(f"\n已写入 {written} 条候选（origin=harvested，重复再多次也不会自动生效）。")
    return 0


def report(db_path: str) -> int:
    """"像不像他"的三个数。驳回侧重在'他拦我'，解释率重在'他得跟我讲道理'。"""
    path = Path(db_path)
    if not path.exists():
        print(f"找不到事件库：{path}")
        return 1
    conn = sqlite3.connect(path)
    try:
        grants = conn.execute(
            "select count(*) from agent_events where type='approval.granted'"
        ).fetchone()[0]
        denied = conn.execute(
            "select count(*) from agent_events where type='approval.denied'"
        ).fetchone()[0]
        explained = 0
        for (raw,) in conn.execute(
            "select payload from agent_events where type='approval.denied'"
        ):
            try:
                if str(json.loads(raw).get("feedback") or "").strip():
                    explained += 1
            except ValueError:
                continue
        total = grants + denied
        denial_rate = round(denied / total, 3) if total else 0.0
        explain_rate = round(explained / denied, 3) if denied else 0.0

        tasks = conn.execute(
            "select count(*) from tasks where status in ('succeeded','failed')"
        ).fetchone()[0]

        # 符合度：他一贯的标准，我们做到了几条。这才是"像不像他"的直接读数。
        conformance_runs = 0
        clause_hits: dict[str, dict] = {}
        for (raw,) in conn.execute(
            "select payload from agent_events where type='standard.conformance'"
        ):
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            conformance_runs += 1
            for item in payload.get("results") or []:
                slot = clause_hits.setdefault(
                    str(item.get("clause") or ""),
                    {"text": str(item.get("text") or "")[:60], "passed": 0, "failed": 0},
                )
                if item.get("status") == "passed":
                    slot["passed"] += 1
                elif item.get("status") == "failed":
                    slot["failed"] += 1
    finally:
        conn.close()

    checked = sum(slot["passed"] + slot["failed"] for slot in clause_hits.values())
    met = sum(slot["passed"] for slot in clause_hits.values())
    fit = round(met / checked, 3) if checked else 0.0

    print("像不像他：")
    print(f"  符合度          {fit:>6}   ({met} 条做到 / {checked} 条核对，{conformance_runs} 个任务)")
    if clause_hits:
        worst = sorted(
            (slot for slot in clause_hits.values() if slot["failed"]),
            key=lambda slot: -slot["failed"],
        )[:3]
        if worst:
            print("  最常没做到的：")
            for slot in worst:
                print(f"      {slot['failed']} 次  {slot['text']}")
    print()
    print(f"  驳回率          {denial_rate:>6}   ({denied} 驳回 / {total} 次审批)")
    print(f"  需要解释的驳回  {explain_rate:>6}   ({explained} 次他打了字 / {denied} 次驳回)")
    print(f"  有结论的任务    {tasks:>6}")
    print()
    print("读法：**符合度是最主的那一个**——它量的是\"这次是不是按他的方式做的\"，")
    print("跟任务成不成功是两件事：一个任务可以成功，但没验证、结论靠猜，那就不算像他。")
    print()
    print("驳回率低说明我给的方向对；**解释率高说明我不懂他**——")
    print("如果我知道他要什么，他点一下\"不允许\"就够了，不用打字讲原因。")
    print("这两个数要一起看：驳回率降到 0 不一定好，可能是他懒得管了。")
    print()
    print("（另一条线是 `scripts/repeat_mistakes.py` 的 repeat_mistake_rate，")
    print("  它量的是\"少犯错\"，不是\"像他\"。两个都要看。）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
