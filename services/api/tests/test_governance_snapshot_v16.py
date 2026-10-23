"""步级治理快照 `turn.context`：对标 Codex rollout 里的 `turn_context`。

要证明四件事，缺一件这个事件就是自嗨：

1. 第一步必发，且带齐真正在管这一步的策略（模型/审批模式/沙箱数值/特性开关生效值）；
2. 策略变了必须再发一条 —— 而且新值得是真的新值（把快照在开场冻结一次，这条就红）；
3. 沙箱数值来自真实 policy，不是抄的常量（改环境变量能反映出来）；
4. 主机路径绝不能进事件（这条会同时喂时间线、证据包和全文索引）。
"""

from types import SimpleNamespace

import pytest

from blank_api.db import get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent import governance
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import AgentSession
from blank_api.services.agent.evidence import build_evidence
from blank_api.services.permissions import MODE_DEFAULT, MODE_PLAN
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway

TASK_ID = "task-governance-1"


def _adapter(model: str = "test-model", effort: str = "", verbosity: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            model=model,
            reasoning_effort=effort,
            verbosity=verbosity,
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
        session.add(Project(id="project-gov", name="治理快照"))
        session.add(Task(id=TASK_ID, project_id="project-gov", title="治理", goal="治理", max_steps=10))
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
        goal="治理",
        max_steps=10,
    )


async def _snapshots() -> list[dict]:
    async with get_session_factory()() as session:
        rows = (
            (await session.execute(AgentEvent.__table__.select().where(AgentEvent.type == "turn.context")))
        ).fetchall()
    return [row.payload for row in rows]


async def test_first_step_records_and_identical_steps_do_not_repeat(seeded, tmp_path):
    session = _session(tmp_path)
    await session.emit_governance("call_model", 0, _adapter(), "")
    await session.emit_governance("call_model", 1, _adapter(), "")
    rows = await _snapshots()
    assert len(rows) == 1, "策略没变就重复发，时间线会被同样的快照灌满"
    assert rows[0]["step"] == 0


async def test_snapshot_carries_the_policies_actually_in_force(seeded, tmp_path):
    session = _session(tmp_path)
    payload = await session.emit_governance("call_model", 0, _adapter("deepseek-chat", "medium"), "")
    assert payload["model"] == "deepseek-chat"
    assert payload["reasoning_effort"] == "medium"
    assert payload["permission_mode"] == session.permission_mode == MODE_DEFAULT
    assert payload["provider"] == "mock"
    assert payload["agent_path"] == "root"
    # 沙箱数值必须是真值：默认无网、根目录只读、有 cpu/内存/PID 上限
    sandbox = payload["sandbox"]
    assert sandbox["network"] is False
    assert sandbox["read_only_root"] is True
    assert sandbox["cpus"] and sandbox["memory"] and sandbox["pids"]
    # 特性开关要带来源，否则回答不了「我明明开了为什么没生效」
    assert set(payload["features"]) == set(governance.FEATURES)
    assert all({"on", "source"} <= item.keys() for item in payload["features"].values())


async def test_mode_change_emits_a_fresh_snapshot(seeded, tmp_path):
    session = _session(tmp_path)
    await session.emit_governance("call_model", 0, _adapter(), "")
    session.permissions.mode = MODE_PLAN
    await session.emit_governance("call_model", 1, _adapter(), "")
    rows = await _snapshots()
    assert [row["permission_mode"] for row in rows] == [MODE_DEFAULT, MODE_PLAN], (
        "只发了一条 = 快照被开场冻结，中途切模式在事件流里查不到"
    )
    assert rows[1]["permissions"]["mode"] == MODE_PLAN


async def test_feature_flag_change_is_visible_with_its_source(seeded, tmp_path, monkeypatch):
    session = _session(tmp_path)
    await session.emit_governance("call_model", 0, _adapter(), "")
    before = (await _snapshots())[-1]["features"]["model_routing"]
    monkeypatch.setenv("BLANK_FEATURE_MODEL_ROUTING", "1")
    await session.emit_governance("call_model", 1, _adapter(), "")
    after = (await _snapshots())[-1]["features"]["model_routing"]
    assert before["on"] is False
    assert after["on"] is True and after["source"] == "env"
    assert len(await _snapshots()) == 2


async def test_sandbox_numbers_come_from_the_live_policy(seeded, tmp_path, monkeypatch):
    monkeypatch.setenv("BLANK_SANDBOX_CPUS", "2.5")
    monkeypatch.setenv("BLANK_SANDBOX_MEMORY", "1g")
    monkeypatch.setenv("BLANK_SANDBOX_NETWORK", "true")
    boundary = governance.sandbox_boundary("<workspace:x>")
    assert boundary["cpus"] == 2.5
    assert boundary["memory"] == "1g"
    assert boundary["network"] is True
    assert boundary["writable_root"] == "<workspace:x>"


async def test_unavailable_sandbox_is_reported_as_such(seeded, tmp_path, monkeypatch):
    """本机没 Docker、又没显式开宿主直连时，命令会被直接拒。快照要写「根本没跑」，
    不能写一个看起来像在跑的档位。"""
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "true")
    monkeypatch.setenv("BLANK_SANDBOX_ALLOW_LOCAL", "false")
    monkeypatch.setattr("blank_api.services.sandbox.runner.shutil_which", lambda *_: None)
    boundary = governance.sandbox_boundary()
    assert boundary["backend"] == "none"
    assert boundary["isolation"] == "host"
    assert "ALLOW_LOCAL" in boundary["denied"]


async def test_host_paths_never_reach_the_event(seeded, tmp_path):
    session = _session(tmp_path)
    payload = await session.emit_governance("call_model", 0, _adapter(), "")
    blob = str(payload)
    assert str(tmp_path) not in blob
    assert ":\\" not in blob, "主机绝对路径泄进事件：它还会进时间线、证据包和全文索引"
    assert payload["workspace"].startswith("<workspace:")


async def test_snapshot_lands_before_the_model_is_called(seeded, tmp_path):
    """调用必然会失败时，恰恰最需要知道「当时是谁、按什么策略在跑」——所以快照在前面。

    这里把压缩那一步（快照之后的第一件事）打成异常：如果快照排在它后面，就一条都不会有。
    """
    session = _session(tmp_path)

    async def explode(*_a, **_k):
        raise RuntimeError("压缩这一步先炸")

    session.compactor.build = explode  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await session.call_model([{"role": "user", "content": "跑一下"}], "call_model", step=3)
    rows = await _snapshots()
    assert rows and rows[0]["model"] == "test-model"
    assert rows[0]["step"] == 3


async def test_evidence_exposes_the_governance_trail(seeded, tmp_path):
    session = _session(tmp_path)
    await session.emit_governance("call_model", 0, _adapter("a-model"), "")
    session.permissions.mode = MODE_PLAN
    await session.emit_governance("execute_action", 1, _adapter("b-model", effort="high"), "heavy")
    evidence = await build_evidence(get_session_factory(), TASK_ID)
    block = evidence["governance"]
    assert block["snapshots"] == 2
    assert block["first"]["sandbox"]["network"] is False
    assert len(block["changes"]) == 1
    change = block["changes"][0]
    assert (change["step"], change["node"], change["model"], change["tier"]) == (1, "execute_action", "b-model", "heavy")
    assert change["permission_mode"] == MODE_PLAN
    assert isinstance(change["seq"], int) and change["seq"] > 0
    assert block["models"] == ["a-model", "b-model"]
    assert block["modes"] == [MODE_DEFAULT, MODE_PLAN]


async def test_evidence_on_an_old_task_does_not_invent_history(seeded, tmp_path):
    """这个特性上线前的任务一条快照都没有：证据包必须是 0 条，而不是编一个默认值。"""
    evidence = await build_evidence(get_session_factory(), TASK_ID)
    assert evidence["governance"] == {"snapshots": 0, "first": None, "changes": [], "models": [], "modes": []}


async def test_probe_cache_age_never_counts_as_a_policy_change(seeded, tmp_path, monkeypatch):
    """探测结果的「距今多少秒」变了不是策略变了 —— 去重不许被它打散。

    这条是 M1b 实测踩出来的：边界强度进快照时顺手把 age_seconds 也带了进去，digest 于是
    每隔几步就变一次，同一套策略被连着写成好几条 turn.context，面板上显示"策略变了"。
    上面那条按天粒度的用例抓不到它——它只在两步刚好跨过 0.1 秒刻度时才红。
    """
    from dataclasses import replace
    import time as _time

    from blank_api.services.sandbox import boundary

    session = _session(tmp_path)
    await session.emit_governance("call_model", 0, _adapter(), "")
    stamp, cap = boundary._CACHE
    assert cap is not None, "快照既然读过探测结果，缓存就该在那儿"
    # 把"测于何时"往过去挪 37.5 秒：年龄随之变化，策略一个字段都没动。
    monkeypatch.setattr(boundary, "_CACHE", (stamp + 37.5, replace(cap, measured_at=cap.measured_at - 37.5)))
    assert abs((boundary.measure().as_dict()["age_seconds"] or 0) - (cap.as_dict()["age_seconds"] or 0)) > 30
    await session.emit_governance("execute_action", 2, _adapter(), "")
    assert len(await _snapshots()) == 1, "只有年龄变了却写了两条快照，等于每步重贴一遍同样的策略"


async def test_the_wire_contract_is_what_the_panel_reads(seeded, tmp_path):
    """字段名就是跨语言契约：前端 `web/src/components/tasks/api.ts` 的 normalizeGovernance
    按这些键名取值，改名字不会让前端报错，只会让它安静地显示「暂无」。所以钉在这里。"""
    session = _session(tmp_path)
    await session.emit_governance("call_model", 0, _adapter(), "")
    block = (await build_evidence(get_session_factory(), TASK_ID))["governance"]
    assert set(block) == {"snapshots", "first", "changes", "models", "modes"}
    first = block["first"]
    assert {"model", "tier", "permission_mode", "sandbox", "features"} <= set(first)
    assert {"backend", "enabled", "isolation", "network", "read_only_root", "denied"} <= set(first["sandbox"])
    assert {"cpus", "memory", "pids", "tmpfs", "timeout_seconds"} <= set(first["sandbox"])
    # M1b 起面板还要读"档位从哪来、哪几项真被强制"：改名不会让前端报错，只会让它安静地
    # 把未强制的限额显示成生效值，所以逐键钉住。
    assert {"boundary_strength", "boundary_strength_label", "enforcement_source", "enforced"} <= set(first["sandbox"])
    assert isinstance(first["sandbox"]["enforced"], dict) and first["sandbox"]["enforced"]
    assert all({"on", "source"} <= set(item) for item in first["features"].values())


async def test_current_date_is_audited_without_breaking_dedupe(seeded, tmp_path):
    """快照要能回答「这一步当时是哪一天」，但只到天的粒度。

    放进秒级时间戳，digest 每步都不同，去重永不命中 —— 这个事件立刻退化成
    "每步重贴一遍同样的策略"，也就失去了它唯一的意义。
    """
    from datetime import datetime

    session = _session(tmp_path)
    payload = await session.emit_governance("call_model", 0, _adapter(), "")
    assert payload["current_date"] == datetime.now().strftime("%Y-%m-%d")
    assert payload["timezone"]
    await session.emit_governance("call_model", 1, _adapter(), "")
    await session.emit_governance("execute_action", 2, _adapter(), "")
    assert len(await _snapshots()) == 1
