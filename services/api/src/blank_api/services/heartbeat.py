"""心跳巡检：系统自己醒来看一眼，然后出报告。

## 只报告，不动手，也不往外发

目标文档把 Heartbeat 定位成"定期检查：是否正常、是否阻塞、目标是否有进展"。
这里严格照这一条做：**发现问题只写进报告，不自动改任何东西**。

**它不会主动发东西给你。** 不推送、不发消息、不发邮件、不建定时汇报——
报告只存在于本机，你看的时候才有；不看就等于没发生。这是用户 2026-11-20 明确要求的：
"不要自主发东西"。想放宽只能由人显式打开，不能默认长出来。

理由不是保守，是顺序：自动处置必须建立在"可验证 + 可回滚"之上（文档 9.3/9.4），
而进化评价现在才刚有骨架、回滚也只覆盖技能。在验证还不成熟时自动动手，
等于把"系统跑偏"变成"系统自己动手跑偏"。

## 检查项是确定性的

全部基于任务表与事件账本，不调模型。心跳要便宜、要能高频跑——
它跑得越勤，"系统自己发现问题"才越可能发生。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

# 默认阈值。任务停留超过这些时长就值得看一眼——不是"错了"，是"该有人知道"。
STUCK_RUNNING_MINUTES = 30
STUCK_APPROVAL_MINUTES = 20
FAILURE_CLUSTER_COUNT = 3
COST_EVENT_COUNT = 2
WINDOW_HOURS = 24

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass
class Finding:
    kind: str
    severity: str
    title: str
    detail: str
    evidence: list[str]
    suggest: str

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "evidence": self.evidence[:10],
            "suggest": self.suggest,
        }


def _parse(text: Any) -> datetime | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _load(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw or "{}"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def analyze(
    tasks: Iterable[dict],
    events: Iterable[dict],
    *,
    now: datetime | None = None,
    stuck_running_minutes: int = STUCK_RUNNING_MINUTES,
    stuck_approval_minutes: int = STUCK_APPROVAL_MINUTES,
    window_hours: int = WINDOW_HOURS,
) -> list[Finding]:
    """纯函数：给数据，给结论。方便拿历史账本回放。"""
    moment = now or datetime.now(timezone.utc)
    rows = [item for item in tasks if isinstance(item, dict)]
    logs = [item for item in events if isinstance(item, dict)]
    window_start = moment - timedelta(hours=window_hours)
    findings: list[Finding] = []

    def in_window(text: Any) -> bool:
        stamp = _parse(text)
        return stamp is not None and stamp >= window_start

    # ① 跑着不动的任务（同类合并成一条：一屏里十条一样的句子等于没有报告）
    _group(
        findings,
        rows,
        status="running",
        minutes=stuck_running_minutes,
        moment=moment,
        kind="stuck_running",
        severity="high",
        noun="任务",
        suggest="看一眼是模型卡住、工具挂住，还是本来就该转后台；别直接判失败",
    )

    # ② 等你审批等太久的
    _group(
        findings,
        rows,
        status="waiting_approval",
        minutes=stuck_approval_minutes,
        moment=moment,
        kind="stuck_approval",
        severity="medium",
        noun="审批",
        suggest="是不是没人看？或者这条审批本身设计得太频繁——检查是不是该给前缀授权",
    )

    # ③ 同一类失败反复出现
    reasons: dict[str, list[str]] = {}
    for item in rows:
        reason = str(item.get("failure_reason") or "").strip()
        if str(item.get("status") or "") != "failed" or not reason:
            continue
        if not in_window(item.get("updated_at")):
            continue
        reasons.setdefault(reason[:60], []).append(str(item.get("id") or ""))
    for reason, ids in reasons.items():
        if len(ids) >= FAILURE_CLUSTER_COUNT:
            findings.append(
                Finding(
                    kind="failure_cluster",
                    severity="high",
                    title=f"同一原因失败了 {len(ids)} 次",
                    detail=reason,
                    evidence=ids,
                    suggest="这不是单次事故，是共性缺陷；该沉淀成经验条目或修机制，别再逐个重试",
                )
            )

    # ④ 交付普遍不符合标准
    checked = passed = failed = 0
    for log in logs:
        if str(log.get("type") or "") != "standard.conformance":
            continue
        payload = log.get("payload") if isinstance(log.get("payload"), dict) else {}
        checked += 1
        passed += int(payload.get("passed") or 0)
        failed += int(payload.get("failed") or 0)
    if passed + failed and failed / (passed + failed) >= 0.5:
        findings.append(
            Finding(
                kind="standard_drift",
                severity="medium",
                title="交付普遍没达到他的标准",
                detail=f"最近 {checked} 个任务里，核对 {passed + failed} 条，没做到 {failed} 条",
                evidence=[],
                suggest="要么是我们对他标准的理解偏了（去改展开条款），要么是提示没起作用（考虑开启强制）",
            )
        )

    # ⑤ 金额闸频繁触发
    budget_hits = [log for log in logs if str(log.get("type") or "") == "budget.exceeded"]
    if len(budget_hits) >= COST_EVENT_COUNT:
        findings.append(
            Finding(
                kind="cost_pressure",
                severity="medium",
                title=f"金额闸最近触发 {len(budget_hits)} 次",
                detail="任务被预算截断，可能正在做无用的长上下文",
                evidence=[],
                suggest="看是不是单步重发整段上下文；或者上限本身定得太低",
            )
        )

    findings.sort(key=lambda item: SEVERITY_ORDER.get(item.severity, 9))
    return findings


def _group(
    findings: list[Finding],
    rows: list[dict],
    *,
    status: str,
    minutes: int,
    moment: datetime,
    kind: str,
    severity: str,
    noun: str,
    suggest: str,
) -> None:
    """把同一种滞留合成一条。报告是给人看的，不是日志。"""
    hits: list[tuple[float, str, str]] = []
    for item in rows:
        if str(item.get("status") or "") != status:
            continue
        stamp = _parse(item.get("updated_at"))
        if stamp is None:
            continue
        idle = (moment - stamp).total_seconds() / 60
        if idle >= minutes:
            hits.append((idle, str(item.get("id") or ""), str(item.get("title") or item.get("goal") or "")))
    if not hits:
        return
    hits.sort(reverse=True)
    worst = int(hits[0][0])
    if len(hits) == 1:
        title = f"有 1 个{noun}停留了 {worst} 分钟"
        detail = hits[0][2][:80]
    else:
        title = f"有 {len(hits)} 个{noun}停着不动，最久 {worst} 分钟"
        detail = "；".join(item[2][:30] for item in hits[:3] if item[2])
    findings.append(
        Finding(
            kind=kind,
            severity=severity,
            title=title,
            detail=detail,
            evidence=[item[1] for item in hits[:5]],
            suggest=suggest,
        )
    )


def scan(db_path: str | Path, **kwargs: Any) -> list[Finding]:
    path = Path(db_path)
    if not path.exists():
        return []
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        tasks = [
            dict(row)
            for row in conn.execute(
                "select id, title, goal, status, created_at, updated_at, steps_used, "
                "failure_reason, acceptance_results from tasks"
            )
        ]
        events = [
            {"type": row["type"], "payload": _load(row["payload"])}
            for row in conn.execute(
                "select type, payload from agent_events "
                "where type in ('standard.conformance','budget.exceeded')"
            )
        ]
    finally:
        conn.close()
    return analyze(tasks, events, **kwargs)


def render(findings: list[Finding], *, window_hours: int = WINDOW_HOURS) -> str:
    if not findings:
        return f"心跳正常：最近 {window_hours} 小时没有需要你看的事。"
    lines = [f"心跳发现 {len(findings)} 件（最近 {window_hours} 小时）：", ""]
    for item in findings:
        mark = {"high": "!!", "medium": "!", "low": "·"}.get(item.severity, "·")
        lines.append(f"{mark} {item.title}")
        if item.detail:
            lines.append(f"   {item.detail}")
        if item.evidence:
            lines.append(f"   涉及：{', '.join(item.evidence[:3])}")
        lines.append(f"   建议：{item.suggest}")
        lines.append("")
    lines.append("（心跳只报告，不动手。自动处置要等验证与回滚成熟。）")
    return "\n".join(lines)
