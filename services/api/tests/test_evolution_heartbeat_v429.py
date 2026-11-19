"""进化评价 + 心跳：一个量"变好没"，一个查"有没有事"。

两个模块的共同纪律：**数字必须能站住**。样本不够就说不结论，同类问题合并成一条报告。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from blank_api.services.harness.evolution import (  # noqa: E402
    MIN_SAMPLE,
    Snapshot,
    compare,
    metrics,
)
from blank_api.services.heartbeat import analyze  # noqa: E402


def _task(status="succeeded", seconds=30, minutes_ago=0, **extra):
    now = datetime.now(timezone.utc)
    start = now - timedelta(seconds=seconds + minutes_ago * 60)
    return {
        "id": extra.pop("id", "t1"),
        "status": status,
        "created_at": start.replace(tzinfo=None).isoformat(sep=" ", timespec="seconds"),
        "updated_at": (now - timedelta(minutes=minutes_ago))
        .replace(tzinfo=None)
        .isoformat(sep=" ", timespec="seconds"),
        "steps_used": extra.pop("steps", 3),
        "acceptance_results": extra.pop("acceptance", "[]"),
        "title": extra.pop("title", "任务"),
        "failure_reason": extra.pop("failure_reason", ""),
        **extra,
    }


# ── 指标 ──────────────────────────────────────────────────────────────────
def test_metrics_read_what_they_promise():
    tasks = [_task(), _task(status="failed"), _task(), _task()]
    events = [
        {"type": "task.completed", "payload": {"usage": {"prompt_tokens": 100, "completion_tokens": 20}}},
        {"type": "approval.required", "payload": {}},
        {"type": "approval.granted", "payload": {}},
        {"type": "approval.denied", "payload": {"feedback": "别这样"}},
    ]

    values, sample = metrics(tasks, events)

    assert sample == 4
    assert values["success_rate"] == 0.75
    assert values["steps_per_task"] == 3
    assert values["tokens_per_task"] == 30.0
    assert values["denial_rate"] == 0.5
    assert values["explained_denial_rate"] == 1.0


def test_payload_must_be_decoded_before_counting():
    """库里 payload 是 JSON 文本。不解码的话 usage 会静默变成"没有这一项"，
    而不是报错——这是今晚栽过的同一种形状。"""
    from blank_api.services.harness.evolution import _load

    assert _load('{"usage": {"prompt_tokens": 5}}')["usage"]["prompt_tokens"] == 5
    assert _load("不是 json") == {}


# ── 对照 ──────────────────────────────────────────────────────────────────
def _snap(metrics_map, sample):
    return Snapshot(label="x", at="", window={}, sample=sample, metrics=metrics_map)


def test_small_samples_never_produce_a_verdict():
    report = compare(
        _snap({"success_rate": 0.5}, MIN_SAMPLE - 1),
        _snap({"success_rate": 0.9}, MIN_SAMPLE - 1),
    )

    assert report["verdict"] == "inconclusive"
    assert "不下结论" in __import__("blank_api.services.harness.evolution", fromlist=["render"]).render(report)


def test_direction_depends_on_the_metric():
    """成功率涨是好，驳回率涨是坏。把"涨了"一律当好事是最常见的误判。"""
    good = compare(
        _snap({"success_rate": 0.7}, MIN_SAMPLE),
        _snap({"success_rate": 0.9}, MIN_SAMPLE),
    )
    bad = compare(
        _snap({"denial_rate": 0.2}, MIN_SAMPLE),
        _snap({"denial_rate": 0.4}, MIN_SAMPLE),
    )

    assert good["verdict"] == "improved"
    assert bad["verdict"] == "regressed"


def test_any_regression_blocks_the_evolution_claim():
    """目标文档 9.4：没有可验证改善就不能宣称进化成功。有一项变坏就不算。"""
    report = compare(
        _snap({"success_rate": 0.7, "denial_rate": 0.1}, MIN_SAMPLE),
        _snap({"success_rate": 0.9, "denial_rate": 0.3}, MIN_SAMPLE),
    )

    assert report["verdict"] == "regressed"


def test_no_material_change_is_not_an_improvement():
    report = compare(
        _snap({"success_rate": 0.700}, MIN_SAMPLE),
        _snap({"success_rate": 0.702}, MIN_SAMPLE),
    )

    assert report["verdict"] == "flat"


# ── 心跳 ──────────────────────────────────────────────────────────────────
def test_same_kind_of_stuck_task_is_one_line_not_nine():
    """报告是给人看的，不是日志。一屏里九条一样的句子等于没有报告。"""
    tasks = [_task(status="waiting_approval", minutes_ago=200, id=f"t{i}") for i in range(9)]

    findings = analyze(tasks, [], now=datetime.now(timezone.utc))

    approval = [item for item in findings if item.kind == "stuck_approval"]
    assert len(approval) == 1
    assert "9 个" in approval[0].title


def test_a_repeated_failure_is_flagged_as_a_common_defect():
    tasks = [
        _task(status="failed", failure_reason="模型返回 400：上下文超限", id=f"t{i}")
        for i in range(3)
    ]

    findings = analyze(tasks, [], now=datetime.now(timezone.utc))

    clusters = [item for item in findings if item.kind == "failure_cluster"]
    assert clusters and clusters[0].severity == "high"
    assert "共同" in clusters[0].suggest or "共性" in clusters[0].suggest


def test_a_quiet_system_reports_nothing():
    assert analyze([_task()], [], now=datetime.now(timezone.utc)) == []


def test_heartbeat_only_reports():
    """自动处置要等验证与回滚成熟；现在它只出结论，不返回任何动作。"""
    tasks = [_task(status="running", minutes_ago=600)]

    findings = analyze(tasks, [], now=datetime.now(timezone.utc))

    payload = findings[0].as_dict()
    assert set(payload) == {"kind", "severity", "title", "detail", "evidence", "suggest"}
