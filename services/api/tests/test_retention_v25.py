"""留存形状：轮归属与跨窗身份进压缩记录（对标 Codex rollout 里 `guardian_history` 的
`retained_source` / `user_input_order` / `internal_chat_message_metadata_passthrough.turn_id`，
见 docs/REA-WAVE4-CODEX-RUNTIME.md §4.1 与 §7.9）。

实测的 Codex 实物：3 条 `guardian_history`，元素 96–151 个，每条都带
`guardian_metadata.retained_source.{message_id,turn_id,role}`、`revision`、`user_input_order`，
历史消息本身带 `internal_chat_message_metadata_passthrough.turn_id`。我们**借这三个结构语义**，
不抄正文（事件进证据包、进全文索引、给同项目其他人看）：

1. **这段历史是哪一轮写的？** 会话自己写的每条消息带 `run_id`，引用直接读它。
   没有的就留空——把"没记过"写成"第 0 轮"是假账，所以判定里既有"该有必须有"，
   也有"不该有必须没有"（历史里 carried 进来的老问答绝不能冒充本轮）。
2. **这是第几次用户输入？** 只有 `user` 条目有序号，从 0 起，其余是 None 不是 0。
3. **这条内容从第几窗起留下、已跨过几窗？** 身份用内容指纹派生（`ret:<sha12>`），
   同一份内容在任何一窗都认成同一条；同窗内重复只算跨了一窗。

每条判定都能失败，装置自己也自检（先确认改坏会红，再断言现在是对的）。
"""

import hashlib
from types import SimpleNamespace

import pytest

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.context import (
    ContextCompactor,
    history_refs,
    message_ref,
    replacement_record,
)
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.evidence import build_evidence, window_turn_rollup
from blank_api.services.agent.graph import (
    AgentSession,
    _append_tool_response,
    _insert_handoff,
    authored,
    initial_messages,
)
from blank_api.services.instructions import content_sha
from blank_api.services.models.base import ModelError, Usage
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway

TASK_ID = "task-retention-1"
RUN_ID = "task:0f0f0f0f-1111-4222-8888-abcdef000099:attempt:1"
MARKER = "留存判定里不许外传的正文 OMEGA-5511"


async def _seed() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="project-retention", name="留存形状"))
        session.add(Task(id=TASK_ID, project_id="project-retention", title="留存", goal="留存", max_steps=10))
        await session.commit()


@pytest.fixture
async def seeded():
    await _seed()


def _session(tmp_path, adapter, *, run_id: str = RUN_ID) -> AgentSession:
    factory = get_session_factory()
    return AgentSession(
        adapter=adapter,  # type: ignore[arg-type]
        gateway=ToolGateway([]),
        context=ToolContext(root=tmp_path, task_id=TASK_ID, session_factory=factory),
        recorder=EventRecorder(factory, TASK_ID, trace_id="main-thread", run_id=run_id),
        session_factory=factory,
        provider_id="provider-1",
        provider_name="mock",
        goal="留存",
        max_steps=10,
    )


class _CompactAdapter:
    """invoke=摘要那笔（每次都回同样的正文，好让跨窗身份可比）；bind_tools 立刻失败。"""

    def __init__(self, window: int = 400) -> None:
        self.config = SimpleNamespace(
            name="mock", model="test-model", reasoning_effort="", verbosity="",
            context_window=window, supports_tools=True,
        )
        self.last_provider = "mock"
        self.summary = '{"goal":"留存","done":"读了文件","blockers":"无","next":"跑测试","evidence":"pytest 3 passed"}'

    async def invoke(self, _chat):
        return SimpleNamespace(
            text=self.summary,
            usage=Usage(prompt_tokens=800, completion_tokens=40),
            model="test-model",
            response_id="resp_retention_1",
            tool_calls=[],
        )

    async def bind_tools(self, _specs, _chat):
        raise ModelError("upstream_error", "真正这次调用故意失败：压缩记录必须先于它落下")


def _bulk(steps: int = 12, size: int = 3_000) -> list[dict]:
    messages = [
        {"role": "system", "content": "你是开发 Agent"},
        {"role": "user", "content": "任务目标：改 main.py"},
    ]
    for index in range(steps):
        messages.append({"role": "assistant", "content": f"第 {index} 步"})
        messages.append({"role": "tool", "content": "x" * size + f"-{index}", "tool_call_id": f"call_{index}"})
    return messages


async def _events(type_name: str) -> list[dict]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                AgentEvent.__table__.select()
                .where(AgentEvent.type == type_name)
                .order_by(AgentEvent.seq)
            )
        ).fetchall()
    return [row.payload for row in rows]


# --------------------------------------------------------------------------- #
# 1. 出生轮：该有必须有，不该有必须没有
# --------------------------------------------------------------------------- #
def test_ref_reads_the_birth_run_and_leaves_the_unknown_empty() -> None:
    stamped = {"role": "assistant", "content": "跑测试", "run_id": RUN_ID}
    assert message_ref(stamped, 0)["run"] == RUN_ID
    # 没记过出身的条目：留空串。读成"第 0 轮"或"主线程"都是编出来的。
    assert message_ref({"role": "user", "content": "继续"}, 0)["run"] == ""


def test_authored_never_overwrites_the_birth_run() -> None:
    """消息被下一轮复用时，出生那一轮才是事实。"""
    assert authored({"role": "user", "content": "旧问题"}, "RUN-B")["run_id"] == "RUN-B"
    assert authored({"role": "user", "content": "旧问题", "run_id": "RUN-A"}, "RUN-B")["run_id"] == "RUN-A"


def test_handoff_note_is_attributed_to_the_run_that_wrote_it() -> None:
    messages = [{"role": "system", "content": "系统"}, {"role": "user", "content": "目标"}]
    out = _insert_handoff(messages, "交接笔记：已改 3 个文件", run_id=RUN_ID)
    note = next(item for item in out if item.get("content") == "交接笔记：已改 3 个文件")
    assert note["run_id"] == RUN_ID, "笔记是这一轮写的，却没记上出身——下一窗就无法把它归回某轮"
    # 未指定 run 时不冒充：留空，前端读成"没说"
    without = _insert_handoff(messages, "笔记", run_id="")
    assert next(item for item in without if item.get("content") == "笔记")["run_id"] == ""


def test_initial_prompt_carries_the_run_but_carried_history_does_not() -> None:
    """本会话自己写的每条都记出身；从别的任务带进来的历史绝不能冒充本轮。"""
    first = initial_messages(
        "留存", [], history=[{"role": "user", "content": "上一趟的问题"}, {"role": "assistant", "content": "上一趟的答"}],
        run_id=RUN_ID,
    )
    assert first[0]["run_id"] == RUN_ID, "系统提示是本轮拼出来的"
    assert first[-1]["run_id"] == RUN_ID, "当前这一问是本轮写的"
    assert [item.get("run_id", "") for item in first[1:3]] == ["", ""], (
        "带进来的历史属于别轮，补上本轮 id 就是假账"
    )
    # 没有会话可问出身时留空，不编一个
    tool_row = _append_tool_response(
        [], {"id": "call_1", "name": "read_file"}, {"path": "a.txt"}, session=None, step=1
    )[-1]
    assert tool_row["run_id"] == ""


async def test_tool_result_written_by_a_run_is_attributed_to_it(seeded, tmp_path) -> None:
    session = _session(tmp_path, _CompactAdapter())
    row = _append_tool_response(
        [], {"id": "call_1", "name": "read_file"}, {"path": "a.txt"}, session=session, step=1
    )[-1]
    assert row["run_id"] == RUN_ID == session.run_id


# --------------------------------------------------------------------------- #
# 2. 第几次用户输入
# --------------------------------------------------------------------------- #
def test_user_order_counts_only_user_messages() -> None:
    items = [
        {"role": "system", "content": "系统"},
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "答"},
        {"role": "tool", "content": "结果", "tool_call_id": "c1"},
        {"role": "user", "content": "第二问"},
    ]
    refs = history_refs(items)
    assert [item["user_order"] for item in refs] == [None, 0, None, None, 1]
    # 非 user 是 None，不是 0：读成 0 会印出"第 0 次用户输入"这种没发生过的事
    assert all("user_order" in item for item in refs), "缺键与值为 0 必须能分开"


# --------------------------------------------------------------------------- #
# 3. 留存身份：内容寻址 + 跨窗计数
# --------------------------------------------------------------------------- #
def test_retained_id_is_content_addressed_and_stable_across_wins() -> None:
    kept = {"role": "user", "content": "保留我"}
    ledger: dict[str, dict] = {}
    first = replacement_record([kept], [kept], retention=ledger, window_number=1)
    second = replacement_record([kept], [kept], retention=ledger, window_number=2)
    expected = f"ret:{content_sha('保留我')[:12]}"
    assert first["items"][0]["retained_id"] == second["items"][0]["retained_id"] == expected
    assert (first["items"][0]["retained_since"], first["items"][0]["retained_windows"]) == (1, 1)
    assert (second["items"][0]["retained_since"], second["items"][0]["retained_windows"]) == (1, 2)
    assert ledger[content_sha("保留我")]["first_window"] == 1, "first_window 是历史事实，不随窗口滑动"


def test_same_content_twice_in_one_window_counts_as_one_hop() -> None:
    """装置自检：复读的工具输出在同一窗里只算跨了一窗，否则计数会虚高。"""
    dup = {"role": "tool", "content": "同样的输出", "tool_call_id": "c1"}
    ledger: dict[str, dict] = {}
    record = replacement_record([dup], [dup, dup], retention=ledger, window_number=3)
    assert [item["retained_windows"] for item in record["items"]] == [1, 1]
    assert record["items"][0]["retained_since"] == 3
    # 丢了的那条也带着自己跨过的窗数——"丢了什么"要能回答"它留了几窗才丢"
    gone = replacement_record([dup], [], retention=ledger, window_number=4)
    assert [(item["retained_since"], item["retained_windows"]) for item in gone["dropped"]] == [(3, 1)]


def test_no_retention_ledger_means_no_invented_numbers() -> None:
    """调用方没给台账和窗号时，只给内容身份，不编"从第 1 窗留下"。"""
    record = replacement_record([], [{"role": "user", "content": "x"}])
    assert record["items"][0]["retained_id"].startswith("ret:")
    assert "retained_since" not in record["items"][0]


def test_retained_id_does_not_copy_the_text() -> None:
    text = f"读 C:\\Users\\linprivate\\.ssh\\id_ed25519 失败：{MARKER}"
    record = replacement_record([], [{"role": "tool", "content": text, "tool_call_id": "c1"}])
    assert MARKER not in str(record) and "linprivate" not in str(record)
    assert record["items"][0]["retained_id"] == f"ret:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}"


# --------------------------------------------------------------------------- #
# 4. 接线：真走两次压缩，身份与窗链一起对上
# --------------------------------------------------------------------------- #
async def test_two_windows_keep_one_identity_per_content(seeded, tmp_path) -> None:
    adapter = _CompactAdapter()
    session = _session(tmp_path, adapter)
    # 第二次给更长的历史：折叠边界必须真的往前推，否则压根不会压第二次（装置自检在下面）
    for steps in (12, 24):
        with pytest.raises(ModelError):
            await session.call_model(_bulk(steps), "call_model", step=3)
    rows = await _events("context.compacted")
    assert len(rows) == 2 and [row["window_number"] for row in rows] == [1, 2], (
        f"装置失效：只压了 {len(rows)} 次，跨窗身份无从验证"
    )

    first, second = (row["replacement"]["items"] for row in rows)
    summaries = [item for item in first if item["kind"] == "summary"]
    assert len(summaries) == 1, "装置失效：第一窗没有摘要条目，跨窗身份无从验证"
    # 摘要是本轮（会话自己）写的 → 出身必须记上
    assert summaries[0]["run"] == RUN_ID
    # 摘要正文每次一样 → 同一份内容在第二窗仍认成同一条，且第一次出现在第 1 窗
    kept = next(item for item in second if item["kind"] == "summary")
    assert kept["retained_id"] == summaries[0]["retained_id"]
    assert (kept["retained_since"], kept["retained_windows"]) == (1, 2)
    # 没被会话写过的那些（测试造的假历史）不得凭空得到本轮 id
    assert {item["run"] for item in first if item["kind"] not in {"summary", "handoff"}} == {""}
    assert MARKER not in str(rows)


# --------------------------------------------------------------------------- #
# 5. 证据包：窗口与轮第一次共享一把键
# --------------------------------------------------------------------------- #
def test_window_turn_rollup_counts_without_inventing_a_zero_turn() -> None:
    items = [
        {"run": "RUN-A", "retained_since": 1, "retained_windows": 2},
        {"run": "RUN-A", "retained_since": 3, "retained_windows": 1},
        {"run": "", "retained_since": 1, "retained_windows": 1},
    ]
    turns = window_turn_rollup(items)
    assert [row["run_id"] for row in turns] == ["RUN-A"], "没记出身的不许并成第 0 轮"
    assert turns[0]["items"] == 2
    assert turns[0]["first_window"] == 1, "取这轮里最老的那条，不是最后一条"
    assert turns[0]["max_windows_held"] == 2
    assert window_turn_rollup([]) == [] and window_turn_rollup(None) == []


async def test_evidence_pack_links_windows_to_runs(seeded, tmp_path) -> None:
    adapter = _CompactAdapter()
    session = _session(tmp_path, adapter)
    with pytest.raises(ModelError):
        await session.call_model(_bulk(), "call_model", step=3)
    block = (await build_evidence(get_session_factory(), TASK_ID))["context"]
    window = block["windows"][-1]
    assert [row["run_id"] for row in window["turns"]] == [RUN_ID]
    assert window["turns"][0]["items"] == 1, "只有摘要这一条是本轮写的"
    assert window["turns_unattributed"] == window["items"] - sum(
        row["items"] for row in window["turns"]
    ), "归属不重不漏：每条引用要么认得轮，要么计入未归属"
    assert window["turns_unattributed"] > 0, "装置失效：全是假历史，未归属应为正"
