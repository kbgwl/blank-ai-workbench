"""进化评价：这次改动到底是不是真的变好了。

## 为什么和 `optimize.py` 不重复

`harness/optimize.py` 是**离线对照实验**：同一批 fixture、同一个模型，跑候选 harness
树 vs 未改动的基线，用 `score.py` 的五项分选赢家。它回答"这棵树在固定题上更强吗"。

这一层回答另一个问题：**真实任务跑下来，横跨一段时间，整体是变好还是变坏。**
现实里没有 fixture，也没有"同一个任务跑两遍"的条件；能用的只有事件账本。

## 三条纪律（都吃过亏）

1. **没有基线就不算改进。** 只报"现在是 0.83"没有意义——0.83 是好是坏取决于之前是多少。
2. **样本不够就不下结论。** 今晚已经栽过：把时间切成两段，机制后 0.5 高于机制前 0.071，
   差一点被读成"机制没用"——实际是分母只有 4 个签名，任何一个新样本都能把比率翻倍。
3. **方向因指标而异。** 成功率涨是好事，驳回率涨是坏事。把"涨了"一律当好事是最常见的误判。
"""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# 一侧少于这么多条已结束任务时，任何差值都可能是噪声。
MIN_SAMPLE = 20

# 相对变化小于这个比例时，算"没实质变化"。
MATERIAL_CHANGE = 0.05

# 指标 → 越大越好（True）还是越小越好（False）。
DIRECTION = {
    "success_rate": True,
    "acceptance_rate": True,
    "standard_fit": True,
    "median_seconds": False,
    "tokens_per_task": False,
    "steps_per_task": False,
    "approvals_per_task": False,
    "denial_rate": False,
    "explained_denial_rate": False,
}

METRIC_LABELS = {
    "success_rate": "成功率",
    "acceptance_rate": "验收覆盖",
    "standard_fit": "符合度",
    "median_seconds": "耗时中位数",
    "tokens_per_task": "每任务 token",
    "steps_per_task": "每任务步数",
    "approvals_per_task": "每任务审批",
    "denial_rate": "驳回率",
    "explained_denial_rate": "需要解释的驳回",
}


@dataclass
class Snapshot:
    label: str
    at: str
    window: dict[str, str]
    sample: int
    metrics: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _seconds(start: str, end: str) -> float | None:
    try:
        a = datetime.fromisoformat(str(start))
        b = datetime.fromisoformat(str(end))
    except (TypeError, ValueError):
        return None
    return max(0.0, (b - a).total_seconds())


def _usage_tokens(payload: dict) -> int:
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    total = 0
    for key in ("prompt_tokens", "completion_tokens"):
        try:
            total += int(usage.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return total


def metrics(tasks: Iterable[dict], events: Iterable[dict]) -> tuple[dict[str, float], int]:
    """从任务行与事件行算指标。纯函数，方便拿真实数据回放。

    `tasks` 每项：``{status, created_at, updated_at, steps_used, acceptance_results}``
    `events` 每项：``{type, payload}``
    """
    rows = [item for item in tasks if isinstance(item, dict)]
    logs = [item for item in events if isinstance(item, dict)]
    finished = [
        item for item in rows if str(item.get("status") or "") in ("succeeded", "failed", "cancelled")
    ]
    out: dict[str, float] = {}
    if not finished:
        return out, 0

    ok = [item for item in finished if str(item.get("status") or "") == "succeeded"]
    out["success_rate"] = round(len(ok) / len(finished), 3)

    durations = [
        value
        for value in (_seconds(item.get("created_at"), item.get("updated_at")) for item in finished)
        if value is not None
    ]
    if durations:
        out["median_seconds"] = round(statistics.median(durations), 1)

    steps = [int(item.get("steps_used") or 0) for item in finished]
    if steps:
        out["steps_per_task"] = round(sum(steps) / len(steps), 2)

    # 验收覆盖：有验收结论的任务里，覆盖了几条
    covered = total = 0
    for item in finished:
        try:
            results = json.loads(str(item.get("acceptance_results") or "[]"))
        except ValueError:
            continue
        if not isinstance(results, list) or not results:
            continue
        total += len(results)
        covered += sum(1 for entry in results if isinstance(entry, dict) and entry.get("covered"))
    if total:
        out["acceptance_rate"] = round(covered / total, 3)

    tokens = 0
    counts = {"approval.required": 0, "approval.granted": 0, "approval.denied": 0}
    explained = 0
    for log in logs:
        kind = str(log.get("type") or "")
        payload = log.get("payload") if isinstance(log.get("payload"), dict) else {}
        if kind == "task.completed":
            tokens += _usage_tokens(payload)
        elif kind in counts:
            counts[kind] += 1
            if kind == "approval.denied" and str(payload.get("feedback") or "").strip():
                explained += 1
    if tokens:
        out["tokens_per_task"] = round(tokens / len(finished), 1)

    approvals = counts["approval.required"]
    if approvals:
        out["approvals_per_task"] = round(approvals / len(finished), 2)
    handled = counts["approval.granted"] + counts["approval.denied"]
    if handled:
        out["denial_rate"] = round(counts["approval.denied"] / handled, 3)
    if counts["approval.denied"]:
        out["explained_denial_rate"] = round(explained / counts["approval.denied"], 3)

    passed = failed = 0
    for log in logs:
        if str(log.get("type") or "") != "standard.conformance":
            continue
        payload = log.get("payload") if isinstance(log.get("payload"), dict) else {}
        passed += int(payload.get("passed") or 0)
        failed += int(payload.get("failed") or 0)
    if passed + failed:
        out["standard_fit"] = round(passed / (passed + failed), 3)

    return out, len(finished)


def compare(
    before: Snapshot, after: Snapshot, *, min_sample: int = MIN_SAMPLE
) -> dict[str, Any]:
    """两次快照的对照。**样本不够就明确说不够，不给结论。**"""
    verdict = "unknown"
    if before.sample < min_sample or after.sample < min_sample:
        verdict = "inconclusive"
    rows: list[dict] = []
    improved: list[str] = []
    regressed: list[str] = []
    for key, label in METRIC_LABELS.items():
        old = before.metrics.get(key)
        new = after.metrics.get(key)
        if old is None or new is None:
            continue
        delta = round(new - old, 4)
        relative = round(delta / old, 3) if old else None
        better = DIRECTION.get(key, True)
        if relative is None or abs(relative) < MATERIAL_CHANGE:
            judgement = "持平"
        elif (delta > 0) == better:
            judgement = "变好"
            improved.append(key)
        else:
            judgement = "变坏"
            regressed.append(key)
        rows.append(
            {
                "metric": key,
                "label": label,
                "before": old,
                "after": new,
                "delta": delta,
                "relative": relative,
                "judgement": judgement,
            }
        )
    if verdict != "inconclusive":
        # 只要有一项明确变坏就不算"进化成功"——这是目标文档 9.4 的原话：
        # 没有可验证改善就不能宣称进化成功。
        verdict = "regressed" if regressed else ("improved" if improved else "flat")
    return {
        "verdict": verdict,
        "rows": rows,
        "improved": improved,
        "regressed": regressed,
        "sample": {"before": before.sample, "after": after.sample},
        "min_sample": min_sample,
    }


def render(report: dict) -> str:
    head = {
        "improved": "有可验证的改善",
        "regressed": "有指标变坏（不能宣称进化成功）",
        "flat": "无可验证改善（不算进化）",
        "inconclusive": "样本不足，不下结论",
    }.get(report["verdict"], report["verdict"])
    lines = [f"结论：{head}", ""]
    sample = report["sample"]
    lines.append(f"样本：改前 {sample['before']} 条 / 改后 {sample['after']} 条（门槛 {report['min_sample']}）")
    if report["verdict"] == "inconclusive":
        lines.append("")
        lines.append("（一侧不足门槛时，任何差值都可能是噪声。今晚已经踩过一次：")
        lines.append("  分母只有 4 个签名时，一个新样本就能把比率翻倍。）")
        return "\n".join(lines)
    lines.append("")
    lines.append(f"{'指标':14}{'改前':>10}{'改后':>10}{'相对':>9}  判定")
    for row in report["rows"]:
        relative = "—" if row["relative"] is None else f"{row['relative']:+.0%}"
        lines.append(
            f"{row['label']:14}{row['before']:>10}{row['after']:>10}{relative:>9}  {row['judgement']}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 落盘：一次改动前后各存一份，才能对照
# --------------------------------------------------------------------------- #

class SnapshotStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or "data/evolution")

    def save(self, snapshot: Snapshot) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in snapshot.label)[:40]
        path = self.root / f"{snapshot.at.replace(':', '')}-{slug or 'snap'}.json"
        path.write_text(json.dumps(snapshot.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def list(self) -> list[Path]:
        try:
            return sorted(self.root.glob("*.json"))
        except OSError:
            return []

    def load(self, path: str | Path) -> Snapshot:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return Snapshot(
            label=str(data.get("label") or ""),
            at=str(data.get("at") or ""),
            window=dict(data.get("window") or {}),
            sample=int(data.get("sample") or 0),
            metrics=dict(data.get("metrics") or {}),
        )


def _load(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw or "{}"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def collect(
    db_path: str | Path, *, since: str = "", until: str = ""
) -> tuple[list[dict], list[dict], dict[str, str]]:
    """从事件账本取一段数据。返回 (任务行, 事件行, 实际窗口)。

    `since` / `until` 是 ``YYYY-MM-DD HH:MM:SS``（与库里 created_at 同格式）。
    不给就用全量——第一次建基线时通常就是全量。
    """
    import sqlite3

    path = Path(db_path)
    if not path.exists():
        return [], [], {"since": since, "until": until}
    clause = ""
    params: list[Any] = []
    if since:
        clause += " and created_at >= ?"
        params.append(since)
    if until:
        clause += " and created_at <= ?"
        params.append(until)
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        tasks = [
            dict(row)
            for row in conn.execute(
                "select id, status, created_at, updated_at, steps_used, acceptance_results "
                f"from tasks where 1=1{clause}",
                params,
            )
        ]
        events = [
            # payload 在库里是 JSON 文本。不解码的话，usage / feedback 全部读不到——
            # 指标会静默变成"没有这一项"，而不是报错。
            {"type": row["type"], "payload": _load(row["payload"])}
            for row in conn.execute(
                "select type, payload from agent_events where 1=1" + clause, params
            )
        ]
    finally:
        conn.close()
    return tasks, events, {"since": since, "until": until}


def snapshot(
    db_path: str | Path, *, label: str, since: str = "", until: str = ""
) -> Snapshot:
    tasks, events, window = collect(db_path, since=since, until=until)
    values, sample = metrics(tasks, events)
    return Snapshot(label=label, at=now_iso(), window=window, sample=sample, metrics=values)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
