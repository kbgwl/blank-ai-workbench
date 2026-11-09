"""边界的来源证明：规则版本、探针版本，以及「变弱了」必须单独成事件。

三件事各自能失败才算数：

1. 规则指纹只跟**生效规则**走：加注释/空行不变，改一条规则或换顺序必须变。
   只记路径和条数，回答不了「这一步被哪一版规则管着」。
2. 探针版本进快照与指纹：证据包里要能区分「机器变了」和「量它的尺子变了」。
3. 前后两条边界快照变弱时，写一条 `boundary.downgraded`；升级不告警，
   同样的弱边界也不重复告警。证据包按条数与原因汇总这段历史。
"""

from types import SimpleNamespace
import time

import pytest

from blank_api.db import get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent import governance
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.evidence import build_evidence
from blank_api.services.agent.graph import AgentSession
from blank_api.services.permission_rules import empty_ruleset, parse_rules
from blank_api.services.sandbox import boundary
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway

TASK_ID = "task-attestation-1"


# --------------------------------------------------------------------------- 规则指纹


def test_rule_sha_tracks_effective_rules_not_comments_or_blank_lines() -> None:
    commented = parse_rules("# 注释不改变策略\n\nallow run_shell(git status)\n")
    plain = parse_rules("allow run_shell(git status)\n")
    assert commented.sha and commented.sha == plain.sha


def test_rule_sha_changes_when_a_rule_or_its_order_changes() -> None:
    allow = parse_rules("allow run_shell(git status)\n")
    deny = parse_rules("deny run_shell(git status)\n")
    assert allow.sha != deny.sha, "动作变了还是同一个指纹，等于规则版本号撒谎"
    first_wins = parse_rules("allow read_file(src/**)\ndeny read_file(src/**)\n")
    reordered = parse_rules("deny read_file(src/**)\nallow read_file(src/**)\n")
    assert first_wins.sha != reordered.sha, "顺序决定第一条命中，换顺序就是换了版本"


def test_rule_sha_distinguishes_no_file_from_an_empty_one() -> None:
    assert empty_ruleset().sha == "", "没加载过规则文件是空串，不是空文件的指纹"
    empty_file = parse_rules("")
    assert empty_file.sha and empty_file.sha != empty_ruleset().sha
    broken = parse_rules("allow run_shell(git status\n")
    assert broken.error and broken.sha == "", "坏文件整份停用，不许留下一个像生效过的指纹"


def test_rule_sha_lands_in_the_snapshot_summary() -> None:
    ruleset = parse_rules("allow run_shell(git status)\n")
    summary = ruleset.summary()
    assert summary["sha"] == ruleset.sha


# --------------------------------------------------------------------------- 探针版本


def test_probe_version_is_reported_and_changes_the_governance_fingerprint(monkeypatch) -> None:
    stamp = time.time()
    monkeypatch.setattr(
        boundary, "_CACHE",
        (stamp, boundary.Capability(probe_version=7, measured_at=stamp)),
    )
    block = governance.sandbox_boundary("")
    assert block["boundary_probe_version"] == 7
    with_older_probe = {"sandbox": {**block, "boundary_probe_version": 6}}
    assert governance.digest({"sandbox": block}) != governance.digest(with_older_probe), (
        "换了量尺却算出同一个指纹，证据包里就分不清是哪一边变了"
    )


# --------------------------------------------------------------------------- 降级判定


def _block(**overrides) -> dict:
    block = {
        "backend": "restricted",
        "boundary_strength": "os_enforced",
        "boundary_apparatus_ok": True,
        "boundary_probe_version": 1,
        "tier_available": True,
        "denied": "",
        "filesystem_enforced": True,
        "network": False,
        "enforced": {
            "memory": True, "pids": True, "tree_kill": True,
            "cpu": False, "disk": False, "network": False, "filesystem": True,
        },
    }
    block.update(overrides)
    return block


def _kinds(report: dict) -> list[str]:
    return [item["kind"] for item in report["reasons"]]


def test_identical_boundaries_are_not_a_downgrade() -> None:
    assert governance.boundary_downgrade(_block(), _block()) == {}


def test_strength_drop_is_reported_with_both_ends() -> None:
    report = governance.boundary_downgrade(
        _block(), _block(backend="local", boundary_strength="job_only")
    )
    assert _kinds(report) == ["strength"]
    assert (report["reasons"][0]["from"], report["reasons"][0]["to"]) == ("os_enforced", "job_only")
    assert report["from"]["boundary_strength"] == "os_enforced"
    assert report["to"]["boundary_strength"] == "job_only"


def test_apparatus_turning_untrusted_is_a_downgrade_on_its_own() -> None:
    report = governance.boundary_downgrade(
        _block(), _block(boundary_apparatus_ok=False)
    )
    assert _kinds(report) == ["apparatus_untrusted"]


def test_lost_backend_field_and_tier_are_each_reported() -> None:
    report = governance.boundary_downgrade(
        _block(),
        _block(
            backend="none", boundary_strength="none", tier_available=False,
            enforced={**_block()["enforced"], "filesystem": False},
        ),
    )
    assert {"strength", "backend_lost", "enforced_lost", "tier_unavailable"} <= set(_kinds(report))
    lost = next(item for item in report["reasons"] if item["kind"] == "enforced_lost")
    assert lost["fields"] == ["filesystem"]


def test_an_upgrade_is_not_an_alert() -> None:
    report = governance.boundary_downgrade(
        _block(backend="local", boundary_strength="job_only", tier_available=False,
               enforced={**_block()["enforced"], "filesystem": False}),
        _block(),
    )
    assert report == {}, "装好了 Docker 是好事，不该写成降级"


def test_a_missing_side_is_unknown_not_a_change() -> None:
    assert governance.boundary_downgrade({}, _block()) == {}
    assert governance.boundary_downgrade(_block(), {}) == {}


# --------------------------------------------------------------------------- 端到端


def _adapter(model: str = "test-model") -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            model=model,
            reasoning_effort="",
            verbosity="",
            context_window=8000,
            supports_tools=True,
        )
    )


async def _seed() -> None:
    from blank_api.db import Base, get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="project-attest", name="来源证明"))
        session.add(Task(id=TASK_ID, project_id="project-attest", title="来源", goal="来源", max_steps=10))
        await session.commit()


@pytest.fixture
async def seeded():
    await _seed()


def _session(tmp_path) -> AgentSession:
    factory = get_session_factory()
    return AgentSession(
        adapter=_adapter(),  # type: ignore[arg-type]
        gateway=ToolGateway([]),
        context=ToolContext(root=tmp_path, task_id=TASK_ID, session_factory=factory),
        recorder=EventRecorder(factory, TASK_ID),
        session_factory=factory,
        provider_id="provider-1",
        provider_name="mock",
        goal="来源",
        max_steps=10,
    )


async def _events(kind: str) -> list[tuple[AgentEvent, dict]]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                AgentEvent.__table__.select()
                .where(AgentEvent.type == kind)
                .order_by(AgentEvent.seq)
            )
        ).fetchall()
    return [(row, row.payload) for row in rows]


def _scripted_boundary(monkeypatch, blocks: list[dict]) -> dict:
    """按调用次数返回预设的边界快照；最后一次之后一直重复它。"""
    calls = {"n": 0}

    def fake(*args, **kwargs):
        item = blocks[min(calls["n"], len(blocks) - 1)]
        calls["n"] += 1
        return dict(item)

    monkeypatch.setattr(governance, "sandbox_boundary", fake)
    return calls


WEAKER = {
    "backend": "none",
    "boundary_strength": "none",
    "tier_available": False,
    "filesystem_enforced": False,
    "enforced": {**_block()["enforced"], "filesystem": False},
}


async def test_a_weaker_boundary_is_announced_once(seeded, tmp_path, monkeypatch) -> None:
    calls = _scripted_boundary(monkeypatch, [_block(), _block(**WEAKER)])
    session = _session(tmp_path)
    await session.emit_governance("call_model", 0, _adapter(), "")
    assert await _events("boundary.downgraded") == [], "第一条快照没有参照物，不许报警"

    await session.emit_governance("execute_action", 1, _adapter(), "")
    events = await _events("boundary.downgraded")
    assert len(events) == 1
    payload = events[0][1]
    assert payload["step"] == 1
    kinds = [item["kind"] for item in payload["reasons"]]
    assert {"strength", "backend_lost", "enforced_lost", "tier_unavailable"} <= set(kinds), kinds

    await session.emit_governance("execute_action", 2, _adapter(), "")
    assert len(await _events("boundary.downgraded")) == 1, "同样的弱边界反复告警等于噪声"
    assert calls["n"] == 3


async def test_evidence_summarises_downgrades_and_carries_the_rule_version(seeded, tmp_path, monkeypatch) -> None:
    _scripted_boundary(monkeypatch, [_block(), _block(**WEAKER)])
    session = _session(tmp_path)
    session.permissions.ruleset = parse_rules("allow run_shell(git status)\n")
    expected_sha = session.permissions.ruleset.sha
    await session.emit_governance("call_model", 0, _adapter(), "")
    await session.emit_governance("execute_action", 1, _adapter(), "")

    summary = (await build_evidence(get_session_factory(), TASK_ID))["governance"]
    assert summary["rules_sha"] == expected_sha
    downgrades = summary["boundary_downgrades"]
    assert downgrades["count"] == 1
    assert downgrades["events"][0]["step"] == 1
    assert "strength" in downgrades["events"][0]["reasons"]
