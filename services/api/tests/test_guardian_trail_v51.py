"""审阅轨迹成为一等记录（对标 §4.1 的 `guardian_history`）。

Codex 一条 `compacted` 里两侧条数是**不对称**的（实测 96/151/133 vs `replacement_history`
的 18/36/39，审阅侧 228,102 字节起）：压缩丢的是模型的历史，审阅人看的那一份一条不丢。
我们借这个结构、不借抄正文，因此每一条判定都瞄着"能不能失败"：

1. 审阅侧必须**覆盖替换前的全量历史**，且被折掉的每一条都能按 `retained_id` 对上；
   只列丢掉的那几条（= #42 已有的 `dropped`）就是没做这件事。
2. 正文一个字节都不许进这条记录——拿一条带主机路径和私钥样文本的历史来验。
3. 判定清单必须与 `guardian.reviewed` 事件**逐 seq 相等**：两处各写一遍就会写歪。
4. 证据包只从事件重算，不许凭空补：老任务的压缩没有轨迹就数进 `windows_unrecorded`。
5. `in_effect` 与末次 `turn.context` 同源：那次审阅当时在哪个窗口、哪套审批模式下发生，
   两处念的必须是一个值。
"""

import json
from types import SimpleNamespace

import pytest

from blank_api.db import get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.context import guardian_trail, history_refs, replacement_record
from blank_api.services.agent.evidence import _approval_hints, _guardian_history
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import AgentSession, guardian_review_of, guardian_reviews_for
from blank_api.services.approvals import approval_key
from blank_api.services.models.base import ModelError, Usage
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway

from tests.test_agent_v04 import (  # noqa: F401  (复用阶段3 的夹具与端到端入口)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)

TASK_ID = "task-trail-1"
MARKER = "审阅轨迹也不许外传的正文标记 SIGMA-8813"
HOST_PATH = r"C:\Users\linprivate\.ssh\id_ed25519"


def _bulk(steps: int = 12, size: int = 3_000) -> list[dict]:
    messages = [
        {"role": "system", "content": "你是开发 Agent"},
        {"role": "user", "content": "任务目标：改 main.py"},
    ]
    for index in range(steps):
        messages.append({"role": "assistant", "content": f"第 {index} 步"})
        messages.append({"role": "tool", "content": "x" * size + f"-{index}", "tool_call_id": f"call_{index}"})
    return messages


async def _seed() -> None:
    from blank_api.db import Base, get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="project-trail", name="审阅轨迹"))
        session.add(Task(id=TASK_ID, project_id="project-trail", title="审阅", goal="审阅", max_steps=10))
        await session.commit()


@pytest.fixture
async def seeded():
    await _seed()


def _session(tmp_path, adapter, run_id: str = "run-trail-1") -> AgentSession:
    factory = get_session_factory()
    return AgentSession(
        adapter=adapter,  # type: ignore[arg-type]
        gateway=ToolGateway([]),
        context=ToolContext(root=tmp_path, task_id=TASK_ID, session_factory=factory),
        recorder=EventRecorder(factory, TASK_ID, run_id=run_id),
        session_factory=factory,
        provider_id="provider-1",
        provider_name="mock",
        goal="审阅",
        max_steps=10,
    )


async def _add_review(session: AgentSession, **fields: object) -> int:
    """往事件流里写一条真判定（真源只有一个：审过的东西只进事件，不进会话内存）。"""
    payload = {
        "tool": "run_shell",
        "approval_key": "k1",
        "arguments": {"command": "rm -rf build"},
        "verdict": "deny",
        "reasons": ["未经确认的递归删除"],
        "model": "fake-guard",
        "latency_ms": 12,
        "outcome": "blocked",
    }
    payload.update(fields)
    return await session.recorder.add("guardian.reviewed", "call_model", payload)


async def _events(type_name: str) -> list[tuple[int, dict]]:
    """按 seq 有序取回 (seq, 载荷)：seq 是确定性的，主键不是。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                AgentEvent.__table__.select()
                .where(AgentEvent.type == type_name)
                .order_by(AgentEvent.seq)
            )
        ).fetchall()
    return [(int(row.seq), row.payload) for row in rows]


class _CompactAdapter:
    """invoke=摘要那笔；bind_tools=真正那次调用（故意失败，压缩记录必须在它之前落下）。"""

    def __init__(self, window: int = 400) -> None:
        self.config = SimpleNamespace(
            name="mock", model="test-model", reasoning_effort="", verbosity="",
            context_window=window, supports_tools=True,
        )
        self.last_provider = "mock"
        self.calls = 0

    async def invoke(self, _chat):
        self.calls += 1
        return SimpleNamespace(
            text='{"goal":"改 main.py","done":"读了文件","blockers":"无","next":"跑测试","evidence":"pytest 3 passed"}',
            usage=Usage(prompt_tokens=800, completion_tokens=40),
            model="test-model",
            response_id=f"resp_compact_{self.calls}",
            tool_calls=[],
        )

    async def bind_tools(self, _specs, _chat):
        raise ModelError("upstream_error", "真正这次调用故意失败：压缩记录必须先于它落下")


# --------------------------------------------------------------------------- #
# 1. 轨迹本身：覆盖全量、对得上身份、不抄正文
# --------------------------------------------------------------------------- #
def test_trail_covers_the_whole_history_the_reviewer_had_seen() -> None:
    before = _bulk(steps=4, size=40)
    after = before[-4:]
    trail = guardian_trail(before, after)
    assert trail["items_reviewed"] == len(before)
    assert trail["items_in_model_context"] == len(after)
    record = replacement_record(before, after)
    # 被折掉的每一条都必须在审阅侧存在，且逐字段等于 replacement 里那一条引用。
    by_id = {item["retained_id"]: item for item in trail["items"]}
    for dropped in record["dropped"]:
        assert dropped["retained_id"] in by_id, "被折掉的条目在审阅轨迹里找不到"
        assert {k: dropped[k] for k in ("index", "role", "kind", "chars", "sha", "run")} == {
            k: by_id[dropped["retained_id"]][k] for k in ("index", "role", "kind", "chars", "sha", "run")
        }


def test_trail_items_are_references_not_bodies() -> None:
    before = [
        {"role": "system", "content": "你是开发 Agent"},
        {"role": "user", "content": f"读 {HOST_PATH} 失败：{MARKER}"},
    ]
    trail = guardian_trail(before, before[:1])
    assert MARKER not in json.dumps(trail, ensure_ascii=False)
    assert "linprivate" not in json.dumps(trail, ensure_ascii=False)
    assert {tuple(sorted(item)) for item in trail["items"]} == {
        tuple(sorted(("index", "role", "kind", "chars", "sha", "run", "user_order", "retained_id")))
    }, "审阅引用多出了未声明的字段，或缺了约定的字段"
    # 与模型侧同一枚身份不是另发的 uuid，而是同一份内容派生的同一个值
    assert trail["items"][1]["retained_id"] == f"ret:{trail['items'][1]['sha'][:12]}"


def test_reviewed_chars_is_the_exact_sum_not_a_positive_number() -> None:
    before = _bulk(steps=3, size=120)
    trail = guardian_trail(before, before[-2:])
    assert trail["reviewed_chars"] == sum(int(item["chars"]) for item in trail["items"])
    assert trail["reviewed_chars"] == sum(len(str(item["content"])) for item in before)


def test_trail_carries_the_reviews_and_the_modes_in_effect() -> None:
    reviews = [{"seq": 7, "tool": "run_shell", "verdict": "deny", "run": "run-a"}]
    trail = guardian_trail(
        _bulk(steps=2, size=30), _bulk(steps=2, size=30)[-1:],
        reviews=reviews, in_effect={"window_tokens": 400, "approval_mode": "default", "guardian_mode": "veto"},
    )
    assert trail["reviews"] == reviews
    assert trail["in_effect"] == {"window_tokens": 400, "approval_mode": "default", "guardian_mode": "veto"}
    # 记下的是当时的清单：之后又来一次审查，不该改写已经落下的那一份
    reviews.append({"seq": 9, "tool": "write_file", "verdict": "allow", "run": "run-a"})
    assert len(trail["reviews"]) == 1


# --------------------------------------------------------------------------- #
# 2. 接线：真走 call_model，压缩事件里带轨迹，且与判定清单同源
# --------------------------------------------------------------------------- #
async def test_compacted_event_carries_the_review_trail(seeded, tmp_path) -> None:
    session = _session(tmp_path, _CompactAdapter())
    review_seq = await _add_review(session)
    with pytest.raises(ModelError):
        await session.call_model(_bulk(), "call_model", step=3)

    rows = [payload for _, payload in await _events("context.compacted")]
    assert len(rows) == 1
    payload = rows[0]
    trail = payload["guardian_history"]
    assert {tuple(sorted(trail))} == {
        tuple(sorted(("items", "items_reviewed", "items_in_model_context", "reviewed_chars", "reviews", "in_effect")))
    }
    # 审阅侧看得到的全量，必然不少于模型侧剩下的（Codex 实测 96 vs 18 的那个不对称）
    assert trail["items_reviewed"] == payload["replacement"]["before_items"]
    assert trail["items_in_model_context"] == payload["replacement"]["after_items"]
    assert trail["items_reviewed"] > trail["items_in_model_context"]
    assert trail["reviewed_chars"] == sum(int(item["chars"]) for item in trail["items"])
    # 判定清单是事件的投影：seq 与审批指纹都来自那条事件，不是第二本账
    assert trail["reviews"] == [
        {
            "seq": review_seq,
            "run": "run-trail-1",
            "tool": "run_shell",
            "approval_key": "k1",
            "verdict": "deny",
            "reasons": ["未经确认的递归删除"],
            "model": "fake-guard",
            "latency_ms": 12,
            "outcome": "blocked",
        }
    ]
    assert "rm -rf build" not in json.dumps(trail, ensure_ascii=False), "判定条目把命令原文抄进了轨迹"
    assert MARKER not in json.dumps(payload, ensure_ascii=False)


async def test_trail_in_effect_matches_the_governance_snapshot(seeded, tmp_path) -> None:
    session = _session(tmp_path, _CompactAdapter(window=1_200))
    session.permissions.mode = "default"
    session.guardian = SimpleNamespace(mode="veto", enabled=False)
    with pytest.raises(ModelError):
        await session.call_model(_bulk(), "call_model", step=2)
    trail = [payload for _, payload in await _events("context.compacted")][0]["guardian_history"]
    snapshot = [payload for _, payload in await _events("turn.context")][-1]
    # 「那次审阅当时在哪个窗口、哪套模式下做的」——两处必须念同一个值
    assert trail["in_effect"]["window_tokens"] == 1_200
    assert trail["in_effect"]["approval_mode"] == snapshot["permission_mode"]
    assert trail["in_effect"]["guardian_mode"] == snapshot["guardian_mode"] == "veto"


async def test_no_compaction_means_no_review_trail(seeded, tmp_path) -> None:
    """没压缩就没有审阅轨迹的记录点：不许凭空补一条"第 1 窗、0 条判定"。"""
    adapter = _CompactAdapter(window=200_000)
    session = _session(tmp_path, adapter)
    with pytest.raises(ModelError):
        await session.call_model([{"role": "user", "content": "短任务"}], "call_model", step=1)
    assert await _events("context.compacted") == []
    # 这一步确实跑过了（度量记录落了），不是测试没触发到路径
    assert await _events("context.measured")


# --------------------------------------------------------------------------- #
# 3. 证据包：只从事件重算，没记过的数进 unrecorded
# --------------------------------------------------------------------------- #
async def test_evidence_guardian_history_is_the_events_recomputed(seeded, tmp_path) -> None:
    session = _session(tmp_path, _CompactAdapter())
    review_seq = await _add_review(
        session, approval_key=approval_key("run_shell", {"command": "rm -rf build"}),
        verdict="deny", reasons=["不可逆"], latency_ms=9, timed_out=False, error="",
        prompt_tokens=42, completion_tokens=7,
    )
    with pytest.raises(ModelError):
        await session.call_model(_bulk(), "call_model", step=3)
    events = []
    async with get_session_factory()() as db:
        rows = (await db.execute(AgentEvent.__table__.select().order_by(AgentEvent.seq))).fetchall()
        events = [SimpleNamespace(seq=int(row.seq), type=str(row.type), run_id=str(row.run_id), payload=row.payload)
                  for row in rows]
    packed = _guardian_history(events)
    compacted = await _events("context.compacted")
    trail = compacted[0][1]["guardian_history"]
    assert packed["windows"] == [
        {
            "seq": compacted[0][0],
            "window_number": 1,
            "items_reviewed": trail["items_reviewed"],
            "items_in_model_context": trail["items_in_model_context"],
            "reviewed_chars": trail["reviewed_chars"],
            "reviews": len(trail["reviews"]),
            "in_effect": trail["in_effect"],
        }
    ]
    assert packed["windows_unrecorded"] == 0
    # 独立重算：字节数与条数必须等于**喂进去的那份历史**，不是等于轨迹自己抄自己
    assert packed["windows"][0]["reviewed_chars"] == sum(len(str(item["content"])) for item in _bulk())
    assert packed["windows"][0]["items_reviewed"] == len(_bulk())
    assert [item["approval_key"] for item in packed["decisions"]] == [
        approval_key("run_shell", {"command": "rm -rf build"})
    ]
    assert packed["decisions"][0]["verdict"] == "deny"
    assert packed["decisions"][0]["outcome"] == "blocked"
    assert packed["decisions"][0]["run_id"] == "run-trail-1"
    # 判定只有事件这一个真源：证据包的条数、轨迹里的清单、decisions 念的是同一条 seq
    assert packed["decisions"][0]["seq"] == review_seq
    assert [item["seq"] for item in trail["reviews"]] == [review_seq]
    assert packed["windows"][0]["reviews"] == 1


async def test_an_old_compaction_is_counted_not_filled_in() -> None:
    """#79 之前的压缩没有审阅侧轨迹：数进 `windows_unrecorded`，不补一份"看起来记过了"的空条目。"""
    fake = [SimpleNamespace(seq=5, type="context.compacted", run_id="", payload={"window_number": 1, "replacement": {}})]
    packed = _guardian_history(fake)
    assert packed["windows"] == []
    assert packed["windows_unrecorded"] == 1


async def test_decisions_are_read_not_inferred_from_the_mode() -> None:
    """审批模式说审查开着，不代表记过判定：没有 `guardian.reviewed` 事件就是空清单。"""
    packed = _guardian_history([SimpleNamespace(seq=1, type="turn.context", run_id="r",
                                                payload={"guardian_mode": "veto"})])
    assert packed["decisions"] == [] and packed["auto_approvals"] == []


# --------------------------------------------------------------------------- #
# 4. 审批卡：批准的人要看得见独立审查说过什么；没记过的不补
# --------------------------------------------------------------------------- #
def test_pending_card_shows_the_verdict_the_reviewer_gave() -> None:
    verdict = {"verdict": "abstain", "reasons": ["模型没给出结论"], "model": "fake-guard", "latency_ms": 30}
    hints = _approval_hints({"tool": "run_shell", "guardian": verdict}, "run_shell", {})
    assert hints["guardian"] == verdict


def test_a_card_from_before_the_review_was_recorded_gets_no_invented_key() -> None:
    """关着审查是"记过的真值"（`None`），老任务是"没记"（键不存在）——两种状态不许混成一种。"""
    assert _approval_hints({"tool": "run_shell", "guardian": None}, "run_shell", {})["guardian"] is None
    hints = _approval_hints({"tool": "run_shell"}, "run_shell", {})
    assert "guardian" not in hints


# --------------------------------------------------------------------------- #
# 5. 审批门：判定只进事件，轨迹从事件读——跨运行也不许丢
# --------------------------------------------------------------------------- #
def _high_risk_tool():
    async def handler(_context, _arguments):
        raise AssertionError("deny 路径不该真的执行工具")

    return SimpleNamespace(name="run_shell", description="", input_schema={}, risk_level="high",
                           handler=handler, timeout_seconds=30.0, risk_for=None,
                           requires_approval=True,
                           resolve_risk=lambda _arguments: "high", spec=lambda: None)


def _gate_session(tmp_path, guardian_verdict: str):
    from blank_api.services.guardian import Guardian
    from blank_api.services.tools.gateway import ToolGateway

    from tests.test_guardian_v12 import FakeAdapter

    session = _session(tmp_path, _CompactAdapter())
    session.gateway = ToolGateway([_high_risk_tool()])
    session.guardian = Guardian(FakeAdapter(guardian_verdict), mode="veto")
    return session


async def test_the_gate_records_the_review_with_the_same_seq_as_the_event(seeded, tmp_path) -> None:
    """轨迹里的判定是事件的**投影**，不是第二本账：seq、审批指纹、去向必须逐一对齐。"""
    from blank_api.services.agent.graph import _approval_gate

    arguments = {"command": "rm -rf build"}
    session = _gate_session(
        tmp_path, '{"verdict": "deny", "reasons": ["未经确认的递归删除"]}'
    )
    denied, blocked = await _approval_gate(
        session, "call_model", [{"id": "call-1", "name": "run_shell", "args": arguments}]
    )
    assert denied == set() and blocked == {"run_shell": "未经确认的递归删除"}
    events = await _events("guardian.reviewed")
    assert len(events) == 1, "审查判定必须进事件流（真源只有一个）"
    seq, payload = events[0]
    reviews = await guardian_reviews_for(session)
    assert reviews == [
        {
            "seq": seq,
            "run": "run-trail-1",
            "tool": "run_shell",
            "approval_key": approval_key("run_shell", arguments),
            "verdict": "deny",
            "reasons": ["未经确认的递归删除"],
            "model": payload["model"],
            "latency_ms": int(payload["latency_ms"]),
            # 审完之后走到了哪一步：deny 是不弹卡直接拒。去向写在事件里，轨迹才带得动。
            "outcome": "blocked",
        }
    ]
    assert "rm -rf build" not in json.dumps(reviews, ensure_ascii=False)


async def test_a_review_from_an_earlier_run_is_still_in_the_trail(seeded, tmp_path) -> None:
    """驳回/暂停之后再跑一次，会话是新建的——上一轮的审查必须还进得了这条轨迹。

    真链路实测到的形状：4 次审查发生在前，6 次压缩发生在后，而轨迹里 `reviews` 全是空的
    （副本长在 `AgentSession` 内存上）。这一条就是把那个形状钉成可失败的判定。
    """
    from blank_api.services.agent.graph import _approval_gate

    first = _gate_session(tmp_path, '{"verdict": "deny", "reasons": ["先拒一次"]}')
    await _approval_gate(first, "call_model", [{"id": "call-1", "name": "run_shell",
                                               "args": {"command": "rm -rf build"}}])
    seq, _ = (await _events("guardian.reviewed"))[0]

    # 换一个 run、换一个新建的会话——真跑里就是"再点一次运行"
    resumed = _session(tmp_path, _CompactAdapter(), run_id="run-trail-2")
    with pytest.raises(ModelError):
        await resumed.call_model(_bulk(), "call_model", step=4)
    trail = [payload for _, payload in await _events("context.compacted")][0]["guardian_history"]
    assert [item["seq"] for item in trail["reviews"]] == [seq]
    assert trail["reviews"][0]["run"] == "run-trail-1", "判定条目念的必须是它出生时那一轮，不是当前这轮"
    assert trail["reviews"][0]["outcome"] == "blocked"


def test_a_review_recorded_before_the_outcome_was_kept_has_no_invented_outcome() -> None:
    """老事件没有 `outcome`：轨迹里就不写这个键。缺键＝当时没记，补成空串＝说它没去向。"""
    old = guardian_review_of(7, "run-old", {"tool": "run_shell", "verdict": "abstain", "reasons": []})
    assert "outcome" not in old
    assert old["seq"] == 7 and old["run"] == "run-old" and old["approval_key"] == ""


# --------------------------------------------------------------------------- #
# 6. 端到端：真运行时里判定既进事件、也进证据包，并且对得上同一次动作
# --------------------------------------------------------------------------- #
async def test_a_real_reviewed_decision_is_joinable_in_evidence(env, workspace, monkeypatch):  # noqa: F811
    monkeypatch.setenv("BLANK_GUARDIAN", "veto")
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["清理构建产物"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 清理构建目录"},
            {"content": "删除构建目录", "tool_calls": [call("run_shell", {"command": "rm -rf build"})]},
            {"content": '{"verdict": "deny", "reasons": ["未经确认的递归删除"]}'},
            {"content": "已完成核查"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    reviewed = [event for event in events if event["type"] == "guardian.reviewed"]
    assert len(reviewed) == 1
    key = approval_key("run_shell", {"command": "rm -rf build"})
    # 事件、审批指纹与证据包三处认的是同一次动作
    assert reviewed[0]["payload"]["approval_key"] == key
    packed = body["evidence"]["guardian_history"]
    assert [item["seq"] for item in packed["decisions"]] == [reviewed[0]["seq"]]
    assert [item["approval_key"] for item in packed["decisions"]] == [key]
    assert packed["decisions"][0]["verdict"] == "deny"
    assert packed["decisions"][0]["outcome"] == "blocked"
    # 这一单没发生压缩：轨迹窗为空，也不许出现"数过但没记"的笔
    assert packed["windows"] == [] and packed["windows_unrecorded"] == 0
