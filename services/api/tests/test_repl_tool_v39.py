"""R 批第 2 项：REPL 工具对齐 Claude。

判据分四层：

- 契约：`code` 必填、`timeout` 默认 30000 / 上限 600000；参数越界给明确中文回执；
- 边界：脚本跑在密封 Node 上下文，内层工具调用回宿主走同一网关（`inner=True`）；
  会话/编排类工具不注入，场景排除的工具也不注入；需要审批的内层调用直接拒绝；
- 会话：变量跨调用保留、顶层 await、最后一条表达式即返回值、stdout/stderr 分开、
  同步死循环由 vm 超时打断且会话保留、异步挂起由宿主截止时间杀掉并清空状态；
- 接线：场景/占位/注册表/子代理的登记一致；任务收尾能摘掉会话。
"""

from __future__ import annotations

import asyncio

import pytest

from blank_api.services.agent import repl as repl_module
from blank_api.services.agent import workflow as wf
from blank_api.services.agent.placeholders import WRITE_TOOLS as PLACEHOLDER_WRITE_TOOLS
from blank_api.services.agent.scenarios import (
    DAILY,
    DEV,
    DOCS,
    REVERSE,
    WRITE_TOOLS as SCENARIO_WRITE_TOOLS,
    excluded_tools,
)
from blank_api.services.agent.stall import structured_missing_tool
from blank_api.services.permissions import MODE_DEFAULT, PermissionPolicy
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome
from blank_api.services.tools.gateway import ToolGateway
from blank_api.services.tools.registry import ALIASES, DEFERRED_TOOLS
from blank_api.services.tools.repl import (
    INNER_TOOL_DENY,
    NODE_GAP,
    _render,
    _repl,
    repl_tools,
)

TOOL = {tool.name: tool for tool in build_default_tools()}["repl"]

_node = wf.find_node()
requires_node = pytest.mark.skipif(not _node, reason="本机没有 Node.js，REPL 集成用例跳过")


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def add(self, event: str, phase: str, payload: dict) -> None:
        self.events.append((event, phase, payload))


class FakeGateway:
    """只记录内层调用；返回值形状与真实网关 `execute` 一致。"""

    def __init__(self, names: list[str], excluded: tuple[str, ...] = ()) -> None:
        self.tools = {name: object() for name in names}
        self.excluded = frozenset(excluded)
        self.calls: list[tuple[str, dict, bool]] = []

    async def execute(self, name, arguments, context, node="", inner=False):
        self.calls.append((name, arguments, inner))
        return {"ok": True, "status": "ok", "output": f"ran {name}", "data": {"name": name}}


class FakeSession:
    def __init__(self, *, message=None, error=None) -> None:
        self.message = message or {}
        self.error = error
        self.tool_calls = 2
        self.calls: list[dict] = []
        self.closed = False

    async def evaluate(self, code, timeout_ms, runner, *, root, tools):
        self.calls.append(
            {"code": code, "timeout_ms": timeout_ms, "root": root, "tools": list(tools)}
        )
        if self.error is not None:
            raise self.error
        return dict(self.message)

    async def close(self):
        self.closed = True


def _context(tmp_path, gateway=None) -> ToolContext:
    return ToolContext(
        root=tmp_path,
        task_id="task-repl",
        session_factory=None,
        recorder=_Recorder(),
        tool_gateway=gateway,
    )


@pytest.fixture(autouse=True)
def _stub_node(monkeypatch):
    """工具层用例不依赖本机 Node；缺口的用例自己覆盖它。"""
    monkeypatch.setattr(wf, "find_node", lambda: "node")


def _install(monkeypatch, session) -> None:
    async def fake_get_session(task_id, node):
        return session

    monkeypatch.setattr(repl_module, "get_session", fake_get_session)


# --------------------------------------------------------------------------- #
# 契约
# --------------------------------------------------------------------------- #
def test_schema_matches_claude_contract():
    schema = TOOL.input_schema
    assert schema["required"] == ["code"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"code", "description", "timeout"}
    assert schema["properties"]["timeout"]["maximum"] == repl_module.REPL_MAX_TIMEOUT_MS == 600_000
    assert TOOL.risk_level == "low"
    # 工具自身的超时必须大于脚本上限：网关外层再加 5s，不能先于脚本期限杀掉等待。
    assert TOOL.timeout_seconds > repl_module.REPL_MAX_TIMEOUT_MS / 1000


@pytest.mark.asyncio
async def test_missing_code_rejected(tmp_path):
    outcome = await _repl(_context(tmp_path), {})
    assert outcome.ok is False and outcome.status == "error"
    assert "code" in outcome.message


@pytest.mark.asyncio
async def test_code_too_long_rejected(tmp_path, monkeypatch):
    session = FakeSession(message={"stdout": "", "stderr": ""})
    _install(monkeypatch, session)
    outcome = await _repl(
        _context(tmp_path, FakeGateway(["read_file"])),
        {"code": "x" * (repl_module.MAX_CODE_CHARS + 1)},
    )
    assert outcome.status == "error" and "太长" in outcome.message
    assert session.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, -1, 600_001, 1.5, True, "30000"])
async def test_timeout_bounds(tmp_path, value):
    outcome = await _repl(_context(tmp_path, FakeGateway(["read_file"])), {"code": "1", "timeout": value})
    assert outcome.ok is False and outcome.status == "error"
    assert "timeout" in outcome.message


@pytest.mark.asyncio
async def test_timeout_default_is_passed_through(tmp_path, monkeypatch):
    session = FakeSession(message={"stdout": "", "stderr": "", "result": 1})
    _install(monkeypatch, session)
    outcome = await _repl(_context(tmp_path, FakeGateway(["read_file"])), {"code": "1"})
    assert outcome.ok is True
    assert session.calls[0]["timeout_ms"] == repl_module.REPL_DEFAULT_TIMEOUT_MS == 30_000


@pytest.mark.asyncio
async def test_timeout_is_capped_at_max(tmp_path, monkeypatch):
    session = FakeSession(message={"stdout": "", "stderr": "", "result": 1})
    _install(monkeypatch, session)
    outcome = await _repl(
        _context(tmp_path, FakeGateway(["read_file"])),
        {"code": "1", "timeout": repl_module.REPL_MAX_TIMEOUT_MS},
    )
    assert outcome.ok is True
    assert session.calls[0]["timeout_ms"] == repl_module.REPL_MAX_TIMEOUT_MS


# --------------------------------------------------------------------------- #
# 边界：内层工具
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_without_gateway_refuses(tmp_path):
    outcome = await _repl(_context(tmp_path, None), {"code": "1"})
    assert outcome.ok is False and outcome.status == "error"
    assert "主代理" in outcome.message


@pytest.mark.asyncio
async def test_node_missing_skips_with_structured_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(wf, "find_node", lambda: "")
    outcome = await _repl(_context(tmp_path, FakeGateway(["read_file"])), {"code": "1"})
    assert outcome.status == "skipped"
    assert outcome.data["missing_command"] == "node"
    assert outcome.message == NODE_GAP
    # 结构化缺口要能被空转断路器认成「环境缺 node」，而不是一条普通失败。
    assert structured_missing_tool(outcome.as_dict()) == "node"


@pytest.mark.asyncio
async def test_only_allowed_tools_are_injected(tmp_path, monkeypatch):
    session = FakeSession(message={"stdout": "", "stderr": "", "result": None})
    _install(monkeypatch, session)
    gateway = FakeGateway(
        ["read_file", "run_shell", "skill", "spawn_agent", "repl", "git"],
        excluded=("git",),
    )
    await _repl(_context(tmp_path, gateway), {"code": "1"})
    assert session.calls[0]["tools"] == ["read_file", "run_shell"]
    for name in ("skill", "spawn_agent", "repl", "git"):
        assert name not in session.calls[0]["tools"]


@pytest.mark.asyncio
async def test_inner_denylist_is_not_routed(tmp_path, monkeypatch):
    session = FakeSession(message={"stdout": "", "stderr": "", "result": None})
    _install(monkeypatch, session)
    gateway = FakeGateway(["read_file", "skill"])
    ctx = _context(tmp_path, gateway)

    captured = {}

    async def capture(code, timeout_ms, runner, *, root, tools):
        captured["runner"] = runner
        return {"stdout": "", "stderr": "", "result": None}

    session.evaluate = capture  # type: ignore[method-assign]
    await _repl(ctx, {"code": "1"})

    result = await captured["runner"]("skill", {"name": "x"})
    assert result["ok"] is False and result["status"] == "denied"
    assert gateway.calls == []  # 连网关都不该进：它不是"被拒"，是"不该发生"


@pytest.mark.asyncio
async def test_inner_call_goes_through_gateway_with_inner_flag(tmp_path, monkeypatch):
    session = FakeSession(message={"stdout": "", "stderr": "", "result": None})
    _install(monkeypatch, session)
    gateway = FakeGateway(["read_file"])
    ctx = _context(tmp_path, gateway)

    captured = {}

    async def capture(code, timeout_ms, runner, *, root, tools):
        captured["runner"] = runner
        return {"stdout": "", "stderr": "", "result": None}

    session.evaluate = capture  # type: ignore[method-assign]
    await _repl(ctx, {"code": "1"})

    result = await captured["runner"]("read_file", {"path": "a.md"})
    assert result["ok"] is True
    assert gateway.calls == [("read_file", {"path": "a.md"}, True)]


# --------------------------------------------------------------------------- #
# 回执渲染
# --------------------------------------------------------------------------- #
def test_render_puts_each_channel_in_its_own_section():
    text, data = _render(
        {
            "stdout": "out-line",
            "stderr": "err-line",
            "error": "boom",
            "result": {"ok": 1},
            "code": "1",
            "tool_calls": 3,
        }
    )
    assert text.index("[result]") < text.index("[stdout]") < text.index("[stderr]") < text.index("[error]")
    assert data["stdout"] == "out-line" and data["stderr"] == "err-line"
    assert data["result"] == {"ok": 1} and data["tool_calls"] == 3


def test_render_empty_result_is_still_explicit():
    text, data = _render({"stdout": "", "stderr": ""})
    assert "执行完成" in text
    assert "result" not in data


def test_render_truncates_streams_and_error():
    text, data = _render(
        {
            "stdout": "o" * (repl_module.MAX_STREAM_CHARS + 10),
            "stderr": "e" * (repl_module.MAX_STREAM_CHARS + 10),
            "error": "x" * (repl_module.MAX_ERROR_CHARS + 10),
        }
    )
    assert len(data["stdout"]) == repl_module.MAX_STREAM_CHARS
    assert len(data["stderr"]) == repl_module.MAX_STREAM_CHARS
    assert len(data["error"]) == repl_module.MAX_ERROR_CHARS
    assert "截断" not in text  # truncated 标记只认显式位，不靠长度猜


@pytest.mark.asyncio
async def test_success_records_event(tmp_path, monkeypatch):
    session = FakeSession(
        message={"stdout": "hi", "stderr": "", "result": {"value": 3}, "truncated": True}
    )
    _install(monkeypatch, session)
    ctx = _context(tmp_path, FakeGateway(["read_file"]))
    outcome = await _repl(ctx, {"code": "1", "description": "看看文件"})

    assert outcome.ok is True
    assert outcome.data["result"] == {"value": 3}
    assert outcome.data["truncated"] is True
    assert outcome.data["tool_calls"] == 2
    events = [item for item in ctx.recorder.events if item[0] == "repl.executed"]
    assert len(events) == 1
    payload = events[0][2]
    assert payload["ok"] is True and payload["tool_calls"] == 2
    assert payload["code_chars"] == 1 and payload["description"] == "看看文件"


@pytest.mark.asyncio
async def test_host_timeout_clears_session(tmp_path, monkeypatch):
    session = FakeSession(error=repl_module.ReplTimeout("REPL 调用超时"))
    _install(monkeypatch, session)
    outcome = await _repl(_context(tmp_path, FakeGateway(["read_file"])), {"code": "1"})
    assert outcome.ok is False and outcome.status == "timeout"
    assert outcome.data["session_restarted"] is True
    assert "已清空" in outcome.message


@pytest.mark.asyncio
async def test_process_crash_reports_restart(tmp_path, monkeypatch):
    session = FakeSession(error=repl_module.ReplError("REPL 进程意外退出"))
    _install(monkeypatch, session)
    outcome = await _repl(_context(tmp_path, FakeGateway(["read_file"])), {"code": "1"})
    assert outcome.ok is False and outcome.status == "error"
    assert outcome.data["session_restarted"] is True


# --------------------------------------------------------------------------- #
# 网关：REPL 里弹不出审批卡
# --------------------------------------------------------------------------- #
def _danger_tool() -> ToolDefinition:
    async def handler(context, arguments):
        return ToolOutcome(ok=True, output="should not run")

    return ToolDefinition(
        name="dangerous",
        description="测试用高风险工具",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        risk_level="high",
        handler=handler,
        timeout_seconds=5.0,
    )


def _gateway() -> tuple[ToolGateway, ToolContext]:
    from pathlib import Path

    gateway = ToolGateway([_danger_tool()], defer=False)
    context = ToolContext(
        root=Path("."),
        task_id="task-gateway",
        session_factory=None,
        permissions=PermissionPolicy.build(MODE_DEFAULT),
    )
    return gateway, context


@pytest.mark.asyncio
async def test_gateway_ask_outer_still_awaits_approval():
    gateway, context = _gateway()
    captured = {}

    async def finish(name, arguments, outcome, ctx, node, risk, started, decision=None):
        captured["outcome"] = outcome
        return outcome.as_dict()

    gateway._finish = finish  # type: ignore[method-assign]
    payload = await gateway.execute("dangerous", {}, context)
    outcome = captured["outcome"]
    assert outcome.status == "awaiting_approval"
    assert outcome.data["requires_approval"] is True
    assert payload["ok"] is False


@pytest.mark.asyncio
async def test_gateway_ask_inside_repl_is_denied():
    gateway, context = _gateway()
    captured = {}

    async def finish(name, arguments, outcome, ctx, node, risk, started, decision=None):
        captured["outcome"] = outcome
        return outcome.as_dict()

    gateway._finish = finish  # type: ignore[method-assign]
    await gateway.execute("dangerous", {}, context, inner=True)
    outcome = captured["outcome"]
    assert outcome.status == "denied"
    assert "弹不出审批卡" in outcome.message
    assert outcome.data["approval_in_repl"] is False
    # 不生成审批卡：内层调用宁可失败也不能让一次未批的动作混进执行。
    assert outcome.data["requires_approval"] is True


# --------------------------------------------------------------------------- #
# 接线：场景 / 占位 / 注册表 / 子代理
# --------------------------------------------------------------------------- #
def test_scenario_registration_is_consistent():
    assert "repl" in SCENARIO_WRITE_TOOLS
    # daily 放开为 Codex 全能 agent：repl 入口可用。
    assert "repl" not in excluded_tools(DAILY)
    # 文档/逆向不禁：内层 run_shell 已被排除，留下的读/算正是这两个场景需要的
    assert "repl" not in excluded_tools(DOCS)
    assert "repl" not in excluded_tools(REVERSE)
    assert "repl" not in excluded_tools(DEV)


def test_placeholder_scan_covers_repl_code():
    assert "repl" in PLACEHOLDER_WRITE_TOOLS


def test_registry_keeps_repl_resident_and_searchable():
    assert "repl" not in DEFERRED_TOOLS
    assert "repl" in ALIASES
    assert "javascript" in ALIASES["repl"] and "脚本" in ALIASES["repl"]


def test_inner_denylist_blocks_session_tools():
    for name in ("repl", "ask_user", "spawn_agent", "workflow", "skill", "tool_search"):
        assert name in INNER_TOOL_DENY
    # 普通能力不该被误伤
    for name in ("read_file", "write_file", "edit_file", "run_shell", "search_code"):
        assert name not in INNER_TOOL_DENY


def test_subagent_tools_drop_repl_and_skillless_skill():
    from types import SimpleNamespace

    from blank_api.services.subagent import _child_tools

    tools = [SimpleNamespace(name=name) for name in ("read_file", "repl", "skill")]
    assert [t.name for t in _child_tools(tools, skill_catalog=True)] == ["read_file", "skill"]
    # 没有 Skill 目录时连入口都不摆；普通工具一个不少
    assert [t.name for t in _child_tools(tools, skill_catalog=False)] == ["read_file"]


@pytest.mark.asyncio
async def test_runtime_finish_closes_repl_session(monkeypatch):
    from types import SimpleNamespace

    from blank_api.services.agent.runtime import AgentRuntime

    closed: list[tuple[str, str]] = []

    async def fake_aclose(task_id, *, reason=""):
        closed.append((task_id, reason))
        return True

    monkeypatch.setattr(repl_module, "aclose", fake_aclose)

    class Store:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, model, key):
            return None

    scope = SimpleNamespace(
        task_id="task-repl-finish",
        store=lambda: Store(),
        subagents=None,
        # 真实 scope 总有 session；这里不带 lsp，走「没有语言服务器管理器」那一支。
        session=SimpleNamespace(context=SimpleNamespace()),
    )
    runtime = AgentRuntime(None)
    # 等待审批不是终点：恢复后还要接着用同一个会话
    await runtime._finish(scope, "waiting_approval", summary="", reason="")
    assert closed == []
    await runtime._finish(scope, "succeeded", summary="", reason="")
    assert closed == [("task-repl-finish", "task_succeeded")]


# --------------------------------------------------------------------------- #
# 真实 Node 集成
# --------------------------------------------------------------------------- #
async def _real_session(task_id: str):
    session = await repl_module.get_session(task_id, _node)
    return session


@pytest.mark.asyncio
@requires_node
async def test_real_node_persistence_await_and_result(tmp_path):
    task_id = "repl-v39-persist"
    session = await _real_session(task_id)
    runner_calls: list[tuple[str, dict]] = []

    async def runner(name, arguments):
        runner_calls.append((name, arguments))
        return {"ok": True, "status": "ok", "output": "body", "data": {"path": arguments.get("path")}}

    try:
        first = await session.evaluate("const base = 40; base", 5000, runner, root=tmp_path, tools=["read_file"])
        assert first.get("result") == 40

        second = await session.evaluate("base + 2", 5000, runner, root=tmp_path, tools=["read_file"])
        assert second.get("result") == 42

        tla = await session.evaluate(
            "const text = await read_file({path: 'a.md'}); text.output",
            5000,
            runner,
            root=tmp_path,
            tools=["read_file"],
        )
        assert tla.get("result") == "body"
        assert runner_calls == [("read_file", {"path": "a.md"})]

        # TLA 分支里声明的变量也要活到下一次调用
        later = await session.evaluate("text.output", 5000, runner, root=tmp_path, tools=["read_file"])
        assert later.get("result") == "body"

        stream = await session.evaluate(
            "console.log('out'); console.error('err'); 'done'", 5000, runner, root=tmp_path, tools=[]
        )
        assert stream.get("stdout") == "out" and stream.get("stderr") == "err"
        assert stream.get("result") == "done"
    finally:
        await repl_module.aclose(task_id)


@pytest.mark.asyncio
@requires_node
async def test_real_node_context_is_sealed(tmp_path):
    task_id = "repl-v39-sealed"
    session = await _real_session(task_id)

    async def runner(name, arguments):
        return {"ok": True}

    try:
        message = await session.evaluate(
            "[typeof require, typeof process, typeof module]", 5000, runner, root=tmp_path, tools=[]
        )
        assert message.get("result") == ["undefined", "undefined", "undefined"]
        # 语法错误是脚本自己的错：会话保留，下一次调用照常
        bad = await session.evaluate("this is not js", 5000, runner, root=tmp_path, tools=[])
        assert "error" in bad
        alive = await session.evaluate("1+1", 5000, runner, root=tmp_path, tools=[])
        assert alive.get("result") == 2
    finally:
        await repl_module.aclose(task_id)


@pytest.mark.asyncio
@requires_node
async def test_real_node_sync_loop_times_out_but_keeps_session(tmp_path):
    task_id = "repl-v39-vm-timeout"
    session = await _real_session(task_id)

    async def runner(name, arguments):
        return {"ok": True}

    try:
        message = await session.evaluate("while(true){}", 300, runner, root=tmp_path, tools=[])
        assert message.get("timed_out") is True
        assert "error" in message
        alive = await session.evaluate("2+2", 5000, runner, root=tmp_path, tools=[])
        assert alive.get("result") == 4
    finally:
        await repl_module.aclose(task_id)


@pytest.mark.asyncio
@requires_node
async def test_real_node_async_hang_kills_and_restarts_session(tmp_path):
    task_id = "repl-v39-host-timeout"
    session = await _real_session(task_id)

    async def runner(name, arguments):
        return {"ok": True}

    try:
        with pytest.raises(repl_module.ReplTimeout):
            await session.evaluate("await new Promise(() => {})", 200, runner, root=tmp_path, tools=[])
        # 超时必须真的杀掉卡住的那个进程：只是不再等它的话，进程会一直陪跑。
        assert session.process is None
        alive = await session.evaluate("3+3", 5000, runner, root=tmp_path, tools=[])
        assert alive.get("result") == 6
    finally:
        await repl_module.aclose(task_id)


@pytest.mark.asyncio
@requires_node
async def test_real_node_tool_error_reaches_script(tmp_path):
    task_id = "repl-v39-tool-error"
    session = await _real_session(task_id)

    async def runner(name, arguments):
        return {"ok": False, "status": "denied", "message": "场景不允许"}

    try:
        message = await session.evaluate(
            "const r = await read_file({path: 'a'}); r.error",
            5000,
            runner,
            root=tmp_path,
            tools=["read_file"],
        )
        assert message.get("result") == "场景不允许"
    finally:
        await repl_module.aclose(task_id)


def test_sessions_are_tracked_and_closable():
    async def scenario():
        session = await repl_module.get_session("repl-v39-track", "node")
        assert repl_module.session_count() >= 1
        assert await repl_module.aclose("repl-v39-track") is True
        assert await repl_module.aclose("repl-v39-track") is False
        assert not session.process

    asyncio.run(scenario())
