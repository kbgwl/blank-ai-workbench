"""压缩窗口链与替换历史：对标 Codex rollout 里 `compacted` 的完整形状（docs/REA-WAVE4-CODEX-RUNTIME.md §4.1）。

这一批要能回答此前答不出的三个问句，每个判定都能失败：

1. **现在活的是第几段历史？** `window_number` + `window_id/first_window_id/previous_window_id` 组成链。
   实测的三条 Codex 记录是 1→2→3、`previous` 等于上一条的 `window_id`、`first` 始终不变，
   且第一条的 `previous == first`（出生窗就是 first）。把窗口在"每次组装"而不是"每次压缩"时推进，
   或者忘了 `first` 不随窗口变，这两条就红。
2. **这次压缩丢了哪些内容？** 替换后的历史逐条留指纹引用，被替换掉的逐条列出来。
   把正文抄进事件（Codex 本机日志那一手）在我们是泄漏：事件进证据包、进全文索引、给同项目其他人看，
   所以 `items` 里只许有 `sha/chars/kind`，正文一个字节都不许出现——这条专门拿一条带
   主机路径和私钥样文本的历史来验。
3. **做这次压缩的那笔供应商调用是哪一条？** `compaction_response_id` 必须等于同一时刻
   `model.usage`（node=`context_compact`）里那一枚。写死、取上一窗的、或者复制一份用量进事件
   （两处会写歪的账）都会红。
"""

import hashlib
from types import SimpleNamespace

import pytest

from blank_api.db import get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.context import (
    ContextCompactor,
    history_refs,
    message_ref,
    replacement_record,
)
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import AgentSession
from blank_api.services.instructions import content_sha
from blank_api.services.models.base import ModelError, Usage
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway

TASK_ID = "task-window-1"
MARKER = "压缩也不许外传的正文标记 OMEGA-4477"
HOST_PATH = r"C:\Users\linprivate\.ssh\id_ed25519"


def _ref(content: str, index: int = 0, **kw) -> dict:
    return message_ref({"role": "user", "content": content}, index, **kw)


def _adapter(window: int = 400) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            name="mock", model="test-model", reasoning_effort="", verbosity="", context_window=window, supports_tools=True
        ),
        last_provider="mock",
    )


async def _seed() -> None:
    from blank_api.db import Base, get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="project-window", name="窗口链"))
        session.add(Task(id=TASK_ID, project_id="project-window", title="压缩", goal="压缩", max_steps=10))
        await session.commit()


@pytest.fixture
async def seeded():
    await _seed()


def _session(tmp_path, adapter: SimpleNamespace) -> AgentSession:
    factory = get_session_factory()
    return AgentSession(
        adapter=adapter,  # type: ignore[arg-type]
        gateway=ToolGateway([]),
        context=ToolContext(root=tmp_path, task_id=TASK_ID, session_factory=factory),
        recorder=EventRecorder(factory, TASK_ID),
        session_factory=factory,
        provider_id="provider-1",
        provider_name="mock",
        goal="压缩",
        max_steps=10,
    )


async def _events(type_name: str) -> list[dict]:
    """按 seq 有序取回载荷：seq 是确定性的，主键不是。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                AgentEvent.__table__.select()
                .where(AgentEvent.type == type_name)
                .order_by(AgentEvent.seq)
            )
        ).fetchall()
    return [row.payload for row in rows]


def _bulk(steps: int = 12, size: int = 3_000) -> list[dict]:
    messages = [
        {"role": "system", "content": "你是开发 Agent"},
        {"role": "user", "content": "任务目标：改 main.py"},
    ]
    for index in range(steps):
        messages.append({"role": "assistant", "content": f"第 {index} 步"})
        messages.append({"role": "tool", "content": "x" * size + f"-{index}", "tool_call_id": f"call_{index}"})
    return messages


async def _events_with_node(type_name: str) -> list[tuple[str, dict]]:
    """`node` 是事件表上的列，不在载荷里：按 node 找账必须把列一起取回来。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                AgentEvent.__table__.select()
                .where(AgentEvent.type == type_name)
                .order_by(AgentEvent.seq)
            )
        ).fetchall()
    return [(str(row.node), row.payload) for row in rows]


# --------------------------------------------------------------------------- #
# 1. 逐条引用：指认得出来，正文一个字都不抄
# --------------------------------------------------------------------------- #
def test_message_ref_carries_fingerprint_not_the_text() -> None:
    text = f"读 {HOST_PATH} 失败：{MARKER}"
    ref = _ref(text)
    assert MARKER not in str(ref) and "linprivate" not in str(ref)
    assert ref["chars"] == len(text)
    # 与 #41 同一个口径：sha256 前 40 位，独立复算必须对得上
    assert ref["sha"] == hashlib.sha256(text.encode("utf-8")).hexdigest()[:40] == content_sha(text)


def test_ref_kinds_separate_summary_from_handoff_from_tools() -> None:
    summary, notes = "【摘要】目标已完成", "交接笔记：已改 3 个文件"
    items = [
        {"role": "system", "content": summary},
        {"role": "system", "content": notes},
        {"role": "assistant", "content": "跑测试", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "content": "3 passed", "tool_call_id": "c1"},
        {"role": "user", "content": "继续"},
    ]
    kinds = [
        item["kind"]
        for item in history_refs(items, summary_text=summary, handoff_text=notes)
    ]
    assert kinds == ["summary", "handoff", "tool_call", "tool_result", "user"]


def test_replacement_delta_is_about_content_not_message_identity() -> None:
    """同一条被裁剪过 = 旧的一条丢了、新的一条进来了。这不是缺陷，是事实。"""
    before = [{"role": "tool", "content": "长输出" * 40, "tool_call_id": "c1"}, {"role": "user", "content": "保留我"}]
    after = [{"role": "system", "content": "【摘要】"}, {"role": "tool", "content": "长输出" * 3, "tool_call_id": "c1"}, {"role": "user", "content": "保留我"}]
    record = replacement_record(before, after, summary_text="【摘要】")
    assert record["retained"] == 1  # 只有"保留我"内容原样还在
    assert [item["role"] for item in record["dropped"]] == ["tool"]
    # 新增的两条：摘要，以及那条被裁剪过的工具结果（内容变了就是新的一版，这正是"按内容比"的含义）
    assert {item["kind"] for item in record["added"]} == {"summary", "tool_result"}
    assert record["before_items"] == 2 and record["after_items"] == 3
    assert MARKER not in str(record)


# --------------------------------------------------------------------------- #
# 2. 窗口链：形状照实测的三条 Codex 记录
# --------------------------------------------------------------------------- #
def test_window_chain_starts_at_the_birth_window() -> None:
    compactor = ContextCompactor()
    assert compactor.window == {
        "window_number": 0,
        "window_id": compactor.window_id,
        "first_window_id": compactor.window_id,
    }


def test_window_chain_advances_only_when_a_window_is_replaced() -> None:
    compactor = ContextCompactor()
    birth = compactor.window_id
    first = compactor.open_window()
    assert first["window_number"] == 1
    # 实测 Codex 的 window 1：previous 与 first 同为出生窗那个 id
    assert first["previous_window_id"] == birth == first["first_window_id"]
    assert first["window_id"] != birth
    second = compactor.open_window()
    assert (second["window_number"], second["previous_window_id"]) == (2, first["window_id"])
    assert second["first_window_id"] == birth, "first 是链头，不随窗口滑动"


async def test_no_compaction_means_no_new_window(seeded, tmp_path) -> None:
    """没压缩就不许推进窗口链：否则"第几窗"变成一个凭调用次数编出来的数。"""
    adapter = _CompactAdapter()
    session = _session(tmp_path, adapter)
    birth = session.compactor.window_id
    with pytest.raises(ModelError):
        await session.call_model([{"role": "user", "content": "短任务"}], "call_model", step=1)
    assert session.compactor.window_number == 0
    assert session.compactor.window_id == birth, "窗口 id 被换掉了，但什么都没压缩"
    assert await _events("context.compacted") == []
    # 度量记录照常落，说明这一步真的跑过了（不是测试没触发到路径）
    assert await _events("context.measured")


# --------------------------------------------------------------------------- #
# 3. 接线：真走 call_model，压缩事件里带链、带引用、带那一笔调用的 id
# --------------------------------------------------------------------------- #
class _CompactAdapter:
    """invoke=摘要那笔；bind_tools=真正那次调用（故意失败，压缩记录必须在它之前落下）。"""

    def __init__(self, window: int = 400) -> None:
        self.config = SimpleNamespace(
            name="mock", model="test-model", reasoning_effort="", verbosity="", context_window=window, supports_tools=True
        )
        self.last_provider = "mock"
        self.invoke_ids: list[str] = []
        self.calls = 0

    async def invoke(self, _chat):
        self.calls += 1
        self.invoke_ids.append(f"resp_compact_{self.calls}")
        return SimpleNamespace(
            text='{"goal":"改 main.py","done":"读了文件","blockers":"无","next":"跑测试","evidence":"pytest 3 passed"}',
            usage=Usage(prompt_tokens=800, completion_tokens=40),
            model="test-model",
            response_id=self.invoke_ids[-1],
            tool_calls=[],
        )

    async def bind_tools(self, _specs, _chat):
        raise ModelError("upstream_error", "真正这次调用故意失败：压缩记录必须先于它落下")


async def test_compacted_event_carries_the_window_and_the_compaction_response_id(seeded, tmp_path) -> None:
    adapter = _CompactAdapter()
    session = _session(tmp_path, adapter)
    before = session.compactor.window_id
    with pytest.raises(ModelError):
        await session.call_model(_bulk(), "call_model", step=3)

    rows = await _events("context.compacted")
    assert len(rows) == 1, f"压缩事件应恰好一条，实得 {len(rows)}"
    payload = rows[0]
    assert payload["window_number"] == 1
    assert payload["first_window_id"] == before and payload["previous_window_id"] == before
    assert payload["window_id"] != before

    record = payload["replacement"]
    assert record["items"], "替换后的历史必须逐条指认"
    assert record["items"][0]["kind"] in {"summary", "user", "system"}
    # 引用里只许有这些字段（#44 加了出生轮 run、第几次用户输入 user_order 与留存身份三段）。
    assert {tuple(sorted(item)) for item in record["items"]} == {
        tuple(
            sorted(
                (
                    "index",
                    "role",
                    "kind",
                    "chars",
                    "sha",
                    "run",
                    "user_order",
                    "retained_id",
                    "retained_since",
                    "retained_windows",
                )
            )
        )
    }, "压缩引用多出了未声明的字段，或缺了约定的字段"

    usage_rows = [payload for node, payload in await _events_with_node("model.usage") if node == "context_compact"]
    assert usage_rows, "摘要那笔调用必须自己入账"
    assert payload["compaction_response_id"] == usage_rows[-1]["call"]["response_id"] == "resp_compact_1"
    # 只留指针，不复制账本：压缩事件里不许再出现一份 token 明细
    assert "token_usage" not in payload and "usage" not in payload


async def test_compacted_event_never_carries_the_folded_text(seeded, tmp_path) -> None:
    adapter = _CompactAdapter()
    session = _session(tmp_path, adapter)
    messages = _bulk()
    messages[1] = {"role": "user", "content": f"任务目标：读 {HOST_PATH}，{MARKER}"}
    messages[3] = {"role": "tool", "content": f"{MARKER} 在工具输出里" + "y" * 2000, "tool_call_id": "call_0"}
    with pytest.raises(ModelError):
        await session.call_model(messages, "call_model", step=4)

    blob = str(await _events("context.compacted"))
    assert MARKER not in blob, "被折叠掉的内容以正文形式回到了压缩事件里"
    assert "linprivate" not in blob
    # 但"丢了哪些"仍然数得出来：这是这条记录存在的全部意义
    record = (await _events("context.compacted"))[0]["replacement"]
    assert record["dropped"], "丢了内容却报告没丢，等于把账做平"


async def test_second_window_points_at_the_first_one(seeded, tmp_path) -> None:
    adapter = _CompactAdapter()
    session = _session(tmp_path, adapter)
    for step in (1, 2):
        with pytest.raises(ModelError):
            await session.call_model(_bulk(steps=14 + step * 6), "call_model", step=step)
    rows = await _events("context.compacted")
    assert len(rows) == 2, f"两次压缩应有两条记录，实得 {len(rows)}"
    first, second = rows
    assert second["window_number"] == 2
    assert second["previous_window_id"] == first["window_id"]
    assert second["first_window_id"] == first["first_window_id"]
    assert second["compaction_response_id"] == "resp_compact_2" != first["compaction_response_id"]


async def test_evidence_pack_exposes_the_window_chain(seeded, tmp_path) -> None:
    from blank_api.services.agent.evidence import build_evidence

    adapter = _CompactAdapter()
    session = _session(tmp_path, adapter)
    messages = _bulk()
    # 留一条与更早某条完全相同的工具输出在尾部：轻量裁剪会把它的正文换成占位文本，
    # 于是"丢了哪些"比"折进摘要的"多一条。两个口径必须能被分开，否则证据包里
    # 从 folded 倒推的写法照样绿。
    messages[-1]["content"] = messages[-3]["content"]
    with pytest.raises(ModelError):
        await session.call_model(messages, "call_model", step=3)
    evidence = await build_evidence(get_session_factory(), TASK_ID)
    block = evidence["context"]
    assert block["windows"][-1]["window_number"] == 1
    assert MARKER not in str(block)

    rows = await _events("context.compacted")
    dropped_from_events = sum(len((row.get("replacement") or {}).get("dropped") or []) for row in rows)
    folded_from_events = sum(int(row.get("folded") or 0) for row in rows)
    retained_from_events = sum(int((row.get("replacement") or {}).get("retained") or 0) for row in rows)
    # 装置自检：这两个数必须真的不一样，否则"从 folded 倒推"的改坏实现照样绿。
    assert dropped_from_events != folded_from_events, (
        f"测试装置失效：丢弃条数({dropped_from_events})与折入条数({folded_from_events})相同，"
        "换成任何倒推实现都抓不住"
    )
    # 证据包只搬数得出来的那部分：条数与窗口身份，逐条引用留在事件里。
    assert block["dropped"] == dropped_from_events
    assert block["windows"][-1]["dropped"] == dropped_from_events
    assert block["windows"][-1]["retained"] == retained_from_events
    assert block["windows"][-1]["items"] == len(rows[-1]["replacement"]["items"])
    assert set(block["windows"][-1]) == {
        "seq",
        "window_number",
        "window_id",
        "previous_window_id",
        "compaction_response_id",
        "retained",
        "dropped",
        "items",
        "turns",
        "turns_unattributed",
    }
    assert block["windows"][-1]["compaction_response_id"] == rows[-1]["compaction_response_id"]
    # 台账不能把"丢了"和"折进摘要"记成同一个口径
    assert block["boundary"] == folded_from_events != block["dropped"]
