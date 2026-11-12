"""T 批：fork 子代理（对齐 Claude 的 Agent(fork)）。

Claude Code 的 fork 形态不是"另一个只读子代理"，而是**当前会话的分身**：它继承
主会话到派发这一刻为止的完整对话、同一段系统提示、同一个模型与同一套工具权限，
只是换一个新的上下文窗口接着干。这里钉六件事：

1. **对话继承**：父会话的系统提示与历史原样进入分身的第一次模型调用，分身的
   任务作为最后一条用户消息追加；不会退回到"从零开始的受限子代理"提示。
2. **快照是派发那一刻**：父会话里还没拿到回执的那次工具交换必须剪掉——否则
   消息序列会出现 assistant(tool_calls) 直接跟 user 的非法形状。
3. **工具面 = 父会话工具表减去派发类**：fork 能看到父会话此刻的工具（含写工具），
   但不能嵌套再派子代理，也不能拿 REPL/LSP/workflow。
4. **权限是共享不是新授**：分身复用父会话的 PermissionPolicy 对象，审批卡在
   子代理里弹不出来，所以需要审批的动作照旧被拒绝。
5. **只能前台**：默认同步等待；显式传 run_in_background=true 是参数错，直接拒绝。
6. **拿不到父对话就失败**：没有快照时明确报错，不伪装成普通子代理。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from blank_api.services.agents_defs import BUILTIN_FORK, discover_agents
from blank_api.services.agent.graph import AgentSession, _fork_history, execute_action_node
from blank_api.services.models.base import AdapterConfig, ModelAdapter, ModelResponse, Usage
from blank_api.services.permissions import MODE_ACCEPT_EDITS, PermissionPolicy
from blank_api.services.subagent import (
    FORK_MARKER,
    SubAgentRequest,
    SubAgentResult,
    SubAgentRunner,
    _fork_tools,
)
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome
from blank_api.services.tools.delegate import (
    _parse_tasks,
    _spawn_agent,
    delegate_tools,
)
from blank_api.services.tools.gateway import ToolGateway


class RecordingAdapter(ModelAdapter):
    """记录每次模型调用看到的工具名与消息；按脚本返回，脚本用完后给一段结论。"""

    def __init__(self, scripted: list[ModelResponse] | None = None) -> None:
        super().__init__(
            AdapterConfig(
                name="fork-v30",
                kind="openai",
                base_url="http://localhost",
                api_key="",
                model="fork-v30",
                supports_tools=True,
            )
        )
        self.scripted = list(scripted or [])
        self.calls: list[dict] = []

    async def invoke(self, messages, **kwargs) -> ModelResponse:  # pragma: no cover
        return ModelResponse(text="未用", usage=Usage(1, 1), model="fork-v30")

    async def stream(self, messages, **kwargs):  # pragma: no cover
        yield ""

    async def bind_tools(self, specs, messages, **kwargs) -> ModelResponse:
        self.calls.append(
            {
                "tools": [spec.name for spec in specs],
                "messages": [
                    {
                        "role": str(getattr(item, "role", "")),
                        "content": str(getattr(item, "content", "")),
                    }
                    for item in messages
                ],
            }
        )
        if self.scripted:
            return self.scripted.pop(0)
        return ModelResponse(text="分身结论", usage=Usage(5, 2), model="fork-v30")


class FakeRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.run_id = ""
        self.origin_run_id = ""

    async def add(self, event: str, node: str, payload: dict) -> None:
        self.events.append((event, payload))

    def types(self) -> list[str]:
        return [event for event, _ in self.events]


class FakeRunner:
    """只回答"派给谁、前台还是后台"，不真跑子代理。"""

    def __init__(self) -> None:
        self.requests: list[SubAgentRequest] = []
        self.background_requests: list[SubAgentRequest] = []

    async def __call__(self, request: SubAgentRequest, *, agent_id: str = "") -> SubAgentResult:
        self.requests.append(request)
        return SubAgentResult(
            ok=True,
            summary="分身结论",
            agent_id=agent_id or "agent-x",
            agent_path="root/sub-1",
        )

    async def spawn_background(self, request: SubAgentRequest):
        self.background_requests.append(request)
        return "agent-bg", None


class _EmptyTotals:
    def one(self):
        return (0, 0, 0, 0, 0, 0, 0)


class _FakeSession:
    """工具落库与用量聚合只要一个空会话；这些用例不验台账内容。"""

    def add(self, _item) -> None:
        pass

    async def execute(self, _statement):
        return _EmptyTotals()

    async def commit(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _tool(name: str, handler=None) -> ToolDefinition:
    async def default(context, arguments):
        return ToolOutcome(ok=True, status="ok", output=f"{name}-ok")

    return ToolDefinition(
        name=name,
        description=name,
        input_schema={"type": "object", "properties": {}},
        risk_level="low",
        handler=handler or default,
    )


def _session(
    root: Path,
    adapter: ModelAdapter,
    *,
    tools: list[ToolDefinition] | None = None,
    snapshot: list[dict] | None = None,
    permissions: PermissionPolicy | None = None,
    scenario: str = "dev",
    excluded: tuple[str, ...] = (),
    write_root: Path | None = None,
):
    return SimpleNamespace(
        adapter=adapter,
        recorder=FakeRecorder(),
        trace_id="thread-fork",
        root_thread_id="thread-fork",
        depth=0,
        agent_path="root",
        sandbox_tier="workspace",
        session_factory=_FakeSession,
        provider_id="",
        provider_name="",
        compactor=None,
        permissions=permissions if permissions is not None else PermissionPolicy(),
        gateway=ToolGateway(
            tools if tools is not None else [_tool("read_file")],
            defer=False,
            scenario=scenario,
            excluded=excluded,
            write_root=write_root,
        ),
        conversation_snapshot=list(snapshot or []),
        context=SimpleNamespace(
            root=Path(root),
            task_id="task-fork",
            skills=None,
            allow_background_shell=False,
        ),
    )


def _spawn_tool():
    return {item.name: item for item in delegate_tools()}["spawn_agent"]


# --------------------------------------------------------------------------- #
# 1. 内置类型与对话继承
# --------------------------------------------------------------------------- #


def test_fork_is_a_builtin_agent_type(tmp_path: Path) -> None:
    catalog = discover_agents(tmp_path)

    assert BUILTIN_FORK in catalog
    # 默认类型仍然排第一：fork 是工具形态的选择，不是通用探索的默认值。
    assert catalog.names()[:2] == ["general-purpose", "fork"]


async def test_fork_inherits_parent_conversation_and_system_prompt(tmp_path: Path) -> None:
    adapter = RecordingAdapter()
    snapshot = [
        {"role": "system", "content": "父会话系统提示：先读再改"},
        {"role": "user", "content": "修一下登录 bug"},
        {"role": "assistant", "content": "我看过 auth.py，问题在 token 过期判断"},
    ]
    session = _session(tmp_path, adapter, snapshot=snapshot)
    runner = SubAgentRunner().bind(session)

    result = await runner(SubAgentRequest(goal="接着把修复写完", fork=True))

    assert result.ok and len(adapter.calls) == 1
    messages = adapter.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    # 父系统提示原样保留，fork 约束接在同一条 system 末尾。
    assert "父会话系统提示：先读再改" in messages[0]["content"]
    assert FORK_MARKER in messages[0]["content"]
    # 没有退回"你是一个受限的子代理"那套从零开始的提示。
    assert "你是一个受限的子代理" not in messages[0]["content"]
    contents = [item["content"] for item in messages]
    assert "修一下登录 bug" in contents
    assert "我看过 auth.py，问题在 token 过期判断" in contents
    assert messages[-1] == {"role": "user", "content": "接着把修复写完"}

    started = [payload for event, payload in session.recorder.events if event == "subagent.started"]
    assert started and started[0]["fork"] is True


async def test_fork_without_parent_conversation_fails_closed(tmp_path: Path) -> None:
    adapter = RecordingAdapter()
    session = _session(tmp_path, adapter, snapshot=[])
    runner = SubAgentRunner().bind(session)

    result = await runner(SubAgentRequest(goal="接管", fork=True))

    assert result.ok is False
    assert "拿不到父会话对话" in result.error
    assert adapter.calls == []
    assert "subagent.started" not in session.recorder.types()


def test_fork_snapshot_drops_the_unanswered_tool_exchange() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "先查一下", "tool_calls": [{"id": "call-1"}]},
    ]

    assert _fork_history(messages) == messages[:2]
    # 完整对话原样保留：只有"最后一条 assistant 带 tool_calls"才是半截交换。
    complete = messages[:2]
    assert _fork_history(complete) == complete


class _NoHooks:
    async def run(self, event: str, tool: str = "", payload: dict | None = None):
        return []


async def test_action_node_snapshots_before_running_the_tools(tmp_path: Path) -> None:
    """图里必须在执行这一批工具之前刷新快照，而不是等回执写回之后。"""

    async def fake_ask(state, prompt, node, *, messages=None):
        return {
            "text": "完成",
            "tool_calls": [],
            "usage": {},
            "messages": list(messages or []) + [{"role": "assistant", "content": "完成"}],
        }

    session = AgentSession(
        adapter=None,  # type: ignore[arg-type]
        gateway=ToolGateway([_tool("read_file")], defer=False),
        context=ToolContext(
            root=Path(tmp_path),
            task_id="task-fork",
            session_factory=_FakeSession,
            approval_policy=lambda *_: False,
            permissions=PermissionPolicy.build(MODE_ACCEPT_EDITS),
        ),
        recorder=FakeRecorder(),
        session_factory=_FakeSession,
        provider_id="",
        provider_name="",
        goal="fork 快照",
        trace_id="thread-fork",
        max_steps=3,
    )
    session.hooks = _NoHooks()  # type: ignore[assignment]
    session.ask = fake_ask  # type: ignore[method-assign]
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "先查一下", "tool_calls": [{"id": "call-1"}]},
    ]

    await execute_action_node(
        {"messages": messages, "steps": 0, "pending": [], "usage": {}, "tool_results": []},
        session,
    )

    assert session.conversation_snapshot == messages[:2]


# --------------------------------------------------------------------------- #
# 2. 工具面与权限
# --------------------------------------------------------------------------- #


async def test_fork_gets_parent_tools_minus_nested_spawning(tmp_path: Path) -> None:
    adapter = RecordingAdapter()
    tools = [
        _tool(name)
        for name in ("read_file", "edit_file", "spawn_agent", "spawn_agents", "repl", "lsp", "workflow")
    ]
    session = _session(
        tmp_path,
        adapter,
        tools=tools,
        snapshot=[{"role": "system", "content": "父"}, {"role": "user", "content": "起点"}],
    )
    runner = SubAgentRunner().bind(session)

    result = await runner(SubAgentRequest(goal="继续", fork=True))

    assert result.ok
    assert set(adapter.calls[0]["tools"]) == {"read_file", "edit_file"}


async def test_fork_shares_the_parent_permission_policy(tmp_path: Path) -> None:
    seen: dict = {}
    policy = PermissionPolicy.build(MODE_ACCEPT_EDITS)

    async def probe(context, arguments):
        seen["same_policy"] = context.permissions is policy
        seen["mode"] = context.permissions.mode
        return ToolOutcome(ok=True, status="ok", output="probe-ok")

    adapter = RecordingAdapter(
        scripted=[
            ModelResponse(
                text="",
                usage=Usage(1, 1),
                model="fork-v30",
                tool_calls=[
                    {"id": "call-1", "type": "function", "function": {"name": "probe", "arguments": "{}"}}
                ],
            )
        ]
    )
    session = _session(
        tmp_path,
        adapter,
        tools=[_tool("probe", probe)],
        snapshot=[{"role": "system", "content": "父"}, {"role": "user", "content": "起点"}],
        permissions=policy,
    )
    runner = SubAgentRunner().bind(session)

    result = await runner(SubAgentRequest(goal="继续", fork=True))

    assert result.ok, result.error
    assert seen == {"same_policy": True, "mode": MODE_ACCEPT_EDITS}


async def test_fork_continuation_keeps_parent_tools(tmp_path: Path) -> None:
    adapter = RecordingAdapter()
    tools = [_tool(name) for name in ("read_file", "edit_file", "spawn_agent")]
    session = _session(
        tmp_path,
        adapter,
        tools=tools,
        snapshot=[{"role": "system", "content": "父"}, {"role": "user", "content": "起点"}],
    )
    runner = SubAgentRunner().bind(session)

    first = await runner(SubAgentRequest(goal="先做一半", fork=True))
    second = await runner.send_message(first.agent_id, "接着做")

    assert first.ok and second.ok
    # 续聊不能悄悄退回只读基线。
    assert set(adapter.calls[1]["tools"]) == {"read_file", "edit_file"}
    # fork 约束只追加一次：续聊取回的系统提示已经带着它。
    assert adapter.calls[1]["messages"][0]["content"].count(FORK_MARKER) == 1


# --------------------------------------------------------------------------- #
# 2b. 场景边界不能因为分身而消失
# --------------------------------------------------------------------------- #


def test_fork_tools_drop_scenario_excluded_tools(tmp_path: Path) -> None:
    """场景禁用的工具不进分身的工具表——重算工具表不能把父会话关掉的能力打开。"""

    session = SimpleNamespace(
        gateway=ToolGateway(
            [_tool(name) for name in ("read_file", "run_shell", "run_tests")],
            defer=False,
            excluded=("run_shell", "run_tests"),
        )
    )

    assert [tool.name for tool in _fork_tools(session)] == ["read_file"]


async def test_fork_keeps_the_scenario_file_type_gate(tmp_path: Path) -> None:
    """父会话在文档场景时，分身也不能改 .py——它必须沿用父网关的场景。"""

    written: list[dict] = []

    async def write(context, arguments):
        written.append(arguments)
        return ToolOutcome(ok=True, status="ok", output="written")

    adapter = RecordingAdapter(
        scripted=[
            ModelResponse(
                text="",
                usage=Usage(1, 1),
                model="fork-v30",
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "write_file", "arguments": '{"path": "main.py"}'},
                    }
                ],
            )
        ]
    )
    session = _session(
        tmp_path,
        adapter,
        tools=[_tool("read_file"), _tool("write_file", write)],
        snapshot=[{"role": "system", "content": "父"}, {"role": "user", "content": "起点"}],
        scenario="docs",
    )

    result = await SubAgentRunner().bind(session)(SubAgentRequest(goal="继续", fork=True))

    assert result.ok, result.error
    assert written == []
    completed = [payload for event, payload in session.recorder.events if event == "tool.completed"]
    assert completed and completed[0]["status"] == "denied"
    assert "文档" in completed[0]["summary"]


async def test_fork_keeps_the_scenario_write_root(tmp_path: Path) -> None:
    """逆向场景的写根也要带过去：分身写派生文件时落独立分析区，不碰被分析的仓库。"""

    seen: dict = {}

    async def write(context, arguments):
        seen["root"] = context.root
        return ToolOutcome(ok=True, status="ok", output="written")

    analysis = tmp_path / "analysis"
    analysis.mkdir()
    adapter = RecordingAdapter(
        scripted=[
            ModelResponse(
                text="",
                usage=Usage(1, 1),
                model="fork-v30",
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "write_file", "arguments": '{"path": "notes.md"}'},
                    }
                ],
            )
        ]
    )
    session = _session(
        tmp_path,
        adapter,
        tools=[_tool("read_file"), _tool("write_file", write)],
        snapshot=[{"role": "system", "content": "父"}, {"role": "user", "content": "起点"}],
        scenario="docs",
        write_root=analysis,
    )

    result = await SubAgentRunner().bind(session)(SubAgentRequest(goal="继续", fork=True))

    assert result.ok, result.error
    assert seen["root"] == analysis


# --------------------------------------------------------------------------- #
# 3. 前台与派发入口
# --------------------------------------------------------------------------- #


async def test_fork_defaults_to_foreground(tmp_path: Path) -> None:
    runner = FakeRunner()
    context = SimpleNamespace(
        root=Path(tmp_path), task_id="task-fork", subagent_runner=runner, agents=None
    )

    outcome = await _spawn_agent(context, {"goal": "接着做", "subagent_type": "fork"})

    assert outcome.ok
    assert [request.fork for request in runner.requests] == [True]
    assert runner.background_requests == []


async def test_fork_rejects_explicit_background(tmp_path: Path) -> None:
    runner = FakeRunner()
    context = SimpleNamespace(
        root=Path(tmp_path), task_id="task-fork", subagent_runner=runner, agents=None
    )

    outcome = await _spawn_agent(
        context,
        {"goal": "接着做", "subagent_type": "fork", "run_in_background": True},
    )

    assert outcome.ok is False
    assert "只能前台运行" in (outcome.message or "")
    assert runner.requests == [] and runner.background_requests == []


async def test_general_purpose_still_defaults_to_background(tmp_path: Path) -> None:
    runner = FakeRunner()
    context = SimpleNamespace(
        root=Path(tmp_path), task_id="task-fork", subagent_runner=runner, agents=None
    )

    outcome = await _spawn_agent(context, {"goal": "查一下"})

    assert outcome.ok
    assert [request.fork for request in runner.background_requests] == [False]
    assert runner.requests == []


def test_spawn_agents_rejects_fork_tasks() -> None:
    requests, error = _parse_tasks(
        [{"goal": "并行做", "subagent_type": "fork"}], SimpleNamespace(agents=None)
    )

    assert requests == []
    assert "只能通过 spawn_agent" in error
