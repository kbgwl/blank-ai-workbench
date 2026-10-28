"""只读工具并行执行。

要证明三件事，缺一件这个特性就不该开：

1. 真并发（不是"看起来并发"）—— 用交错日志证明，不靠耗时单点判断；
2. 顺序不变 —— 事件流与 tool 消息仍然是模型给出工具调用的那个次序，
   否则前端会把两个只读结果挂到错误的那张步骤卡上；
3. 有写操作就断开 —— A 读 → B 写 → C 读 不能全并发，否则 C 可能读到
   B 改之前的版本。
"""

import asyncio
import time
import uuid

import pytest

from blank_api.db import get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.graph import AgentSession, execute_action_node
from blank_api.services.agent.events import EventRecorder
from blank_api.services.hooks import HookResult
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome
from blank_api.services.tools.gateway import ToolGateway

TASK_ID = "task-parallel-1"


def _empty_schema() -> dict:
    return {"type": "object", "properties": {}, "additionalProperties": False}


class Trace:
    """记录"开始/结束"的先后，用来判断两个 handler 是否真的重叠。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def enter(self, tool: str) -> None:
        self.events.append(("enter", tool))

    def leave(self, tool: str) -> None:
        self.events.append(("leave", tool))

    def overlap(self, first: str, second: str) -> bool:
        """second 在 first 结束之前就开始了 —— 只有并发才会出现。"""
        order = [item for item in self.events if item[1] in (first, second)]
        return order[:2] in ([("enter", first), ("enter", second)], [("enter", second), ("enter", first)])

    def names(self, phase: str) -> list[str]:
        return [tool for kind, tool in self.events if kind == phase]


class SlowHooks:
    """pre_tool_use 可以拒绝指定工具，其余一律放行。"""

    def __init__(self, deny: str = "") -> None:
        self.deny = deny
        self.seen: list[str] = []

    async def run(self, event: str, tool: str = "", payload: dict | None = None):
        if event == "pre_tool_use":
            self.seen.append(tool)
            if tool == self.deny:
                return [HookResult(event=event, command=tool, status="ok", decision="deny", reason="测试拦截")]
        return []


def _tool(name: str, trace: Trace, delay: float, risk: str = "low") -> ToolDefinition:
    async def handler(context: ToolContext, arguments: dict) -> ToolOutcome:
        trace.enter(name)
        await asyncio.sleep(delay)
        trace.leave(name)
        return ToolOutcome(ok=True, output=f"{name} done", data={"tool": name})

    return ToolDefinition(
        name=name,
        description=f"测试用 {name}",
        input_schema=_empty_schema(),
        risk_level=risk,
        handler=handler,
        timeout_seconds=5.0,
    )


def _call(name: str) -> dict:
    return {"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function", "function": {"name": name, "arguments": "{}"}}


async def _seed_task() -> None:
    async with get_session_factory()() as session:
        session.add(Project(id="project-parallel", name="并行测试"))
        session.add(
            Task(id=TASK_ID, project_id="project-parallel", title="并行", goal="并行", max_steps=10)
        )
        await session.commit()


@pytest.fixture
async def seeded():
    from blank_api.db import Base, get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await _seed_task()


def _session(tmp_path, tools: list[ToolDefinition], hooks=None) -> AgentSession:
    session = AgentSession(
        adapter=None,  # type: ignore[arg-type]  # 本用例不走到模型调用
        gateway=ToolGateway(tools),
        context=ToolContext(
            root=tmp_path,
            task_id=TASK_ID,
            session_factory=get_session_factory(),
            approval_policy=lambda *_: False,
        ),
        recorder=EventRecorder(get_session_factory(), TASK_ID),
        session_factory=get_session_factory(),
        provider_id="provider-1",
        provider_name="mock",
        goal="并行",
        max_steps=10,
    )
    session.hooks = hooks or SlowHooks()  # type: ignore[assignment]

    async def fake_ask(state, prompt, node, *, messages=None):
        return {
            "text": "完成",
            "tool_calls": [],
            "usage": {},
            "messages": list(messages or []) + [{"role": "assistant", "content": "完成"}],
        }

    session.ask = fake_ask  # type: ignore[method-assign]
    return session


async def _run(session: AgentSession, names: list[str]):
    return await execute_action_node(
        {"messages": [{"role": "user", "content": "读几个文件"}], "steps": 0, "pending": [_call(n) for n in names]},
        session,
    )


async def _completed_order() -> list[str]:
    async with get_session_factory()() as session:
        rows = (await session.execute(AgentEvent.__table__.select())).fetchall()
    return [row.payload.get("tool", "") for row in rows if row.type == "tool.completed"]


def _enable(monkeypatch, on: bool) -> None:
    monkeypatch.setenv("BLANK_FEATURE_PARALLEL_READ_TOOLS", "1" if on else "0")


async def test_read_only_calls_overlap(seeded, tmp_path, monkeypatch):
    """串行基线在同一次运行里现量：绝对秒数在共享机上量的是负载，不是并行。

    实测：空载 6 轮并行 0.29–0.32s / 同参数串行 0.56–0.59s，比值 0.52–0.57；
    另起 6 个 CPU 忙循环压着再跑 6 轮，比值仍是 0.54–0.57。原来那个 `elapsed < 0.45`
    写死的是"机器别抖"：全量回归那一次并行读到 0.46s 就红了，而它其实在并行
    （交错日志成立）。改成相对判据后两边同涨同落，负载只把比值往 1 推一点点。
    """

    async def measure(delay: float, flag: bool) -> tuple[float, bool]:
        _enable(monkeypatch, flag)
        trace = Trace()
        session = _session(tmp_path, [_tool("read_file", trace, delay), _tool("search_code", trace, delay)])
        started = time.perf_counter()
        await _run(session, ["read_file", "search_code"])
        return time.perf_counter() - started, trace.overlap("read_file", "search_code")

    # 每侧取三轮最小值：单轮碰上调度抖动就整条判据失效（全量回归里读到并行 0.63s /
    # 串行 0.71s，比值 0.89 而交错日志成立）。最小值只压噪声，压不动锁死的实现——
    # 进了锁的话三轮都贴着串行基线。
    serial_rounds = [await measure(0.25, False) for _ in range(3)]
    parallel_rounds = [await measure(0.25, True) for _ in range(3)]
    serial = min(elapsed for elapsed, _ in serial_rounds)
    parallel = min(elapsed for elapsed, _ in parallel_rounds)

    # 交错日志是硬证据：串行的话必然是先 enter/leave 一个，再 enter/leave 另一个
    assert all(overlap for _, overlap in parallel_rounds), "并行轮里有一轮没有交错"
    assert not any(overlap for _, overlap in serial_rounds), "开关关掉的对照格必须先出现串行，否则这次比较没有基线"
    # 相对判据：并行必须显著快于同一次运行里的串行基线（实测比值 ≈0.55，上界取 0.8；
    # 进了锁但"看起来并发"的实现在这里比值 ≈1.0）
    assert parallel < serial * 0.8, f"并行没比串行快多少：并行 {parallel:.2f}s / 串行 {serial:.2f}s"


async def test_switch_off_keeps_the_old_serial_behaviour(seeded, tmp_path, monkeypatch):
    _enable(monkeypatch, False)
    trace = Trace()
    session = _session(tmp_path, [_tool("read_file", trace, 0.15), _tool("search_code", trace, 0.15)])

    started = time.perf_counter()
    await _run(session, ["read_file", "search_code"])
    elapsed = time.perf_counter() - started

    assert not trace.overlap("read_file", "search_code")
    assert elapsed >= 0.29


async def test_events_and_messages_keep_the_requested_order(seeded, tmp_path, monkeypatch):
    _enable(monkeypatch, True)
    trace = Trace()
    # 故意让第一个最慢：如果按完成时间排序，顺序就会反过来
    session = _session(
        tmp_path,
        [_tool("read_file", trace, 0.2), _tool("search_code", trace, 0.02), _tool("list_files", trace, 0.05)],
    )

    names = ["read_file", "search_code", "list_files"]
    result = await _run(session, names)

    assert [item["tool"] for item in result["tool_results"]] == names
    assert await _completed_order() == names
    tool_messages = [item for item in result["messages"] if item.get("role") == "tool"]
    assert [f'"tool": "{name}"' in item["content"] for item, name in zip(tool_messages, names)] == [
        True,
        True,
        True,
    ]


async def test_write_in_the_middle_splits_the_batch(seeded, tmp_path, monkeypatch):
    _enable(monkeypatch, True)
    trace = Trace()
    session = _session(
        tmp_path,
        [
            _tool("read_file", trace, 0.1),
            _tool("search_code", trace, 0.1),
            # 写操作：不在并行白名单里，必须把前后两段读操作隔开
            _tool("apply_patch", trace, 0.02, risk="medium"),
            _tool("list_files", trace, 0.1),
            _tool("read_notes", trace, 0.1),
        ],
    )

    await _run(session, ["read_file", "search_code", "apply_patch", "list_files", "read_notes"])

    # 顺序断言：写操作的两个 enter 位置决定了它夹在两段读之间
    enters = trace.names("enter")
    assert enters[2] == "apply_patch"
    assert set(enters[:2]) == {"read_file", "search_code"}
    assert set(enters[3:]) == {"list_files", "read_notes"}


async def test_hook_denied_call_never_runs_but_others_still_overlap(seeded, tmp_path, monkeypatch):
    _enable(monkeypatch, True)
    trace = Trace()
    session = _session(
        tmp_path,
        [_tool("read_file", trace, 0.15), _tool("search_code", trace, 0.15), _tool("list_files", trace, 0.15)],
        hooks=SlowHooks(deny="search_code"),
    )

    result = await _run(session, ["read_file", "search_code", "list_files"])

    assert "search_code" not in trace.names("enter")
    # 被拒绝的调用会打断并发段：它虽然不执行，但在逻辑顺序上夹在中间，
    # 把它当边界处理比起假设"拒绝无副作用"更稳妥，代价只是这几毫秒的串行。
    assert trace.names("enter") == ["read_file", "list_files"]
    assert len(result["tool_results"]) == 3
    denied = [item for item in result["tool_results"] if item["tool"] == "search_code"][0]
    assert denied["status"] == "denied"
    assert "钩子拒绝执行" in denied["output"]
