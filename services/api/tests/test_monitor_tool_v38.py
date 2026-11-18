# -*- coding: utf-8 -*-
"""R1：monitor 工具对齐 Claude 的 Monitor。

钉住这些事（每一条都对应一处会退回去的实现）：

1. **契约**：description / command / timeout_ms 必填；timeout_ms 上限 1800000，
   0 表示不设监控期限；句柄带 mon- 前缀；
2. **逐行事件**：一条输出行就是一个事件，stdout/stderr 分得清，跨 chunk 的
   半行会拼回去；命令结束时最后一条事件是「监控已结束」；
3. **边跑边投**：命令还没结束就能取到已经产生的事件（而不是等进程退出才一次性给）；
4. **投递有上限**：一次模型调用最多一屏，积压的行留在队列里下一轮继续；
   队列满了只丢最旧的行，并把丢弃条数如实上报；
5. **系统通知**：事件在下一次模型调用前注入，且只注入一次；文案明确「不是用户发言」；
6. **可终止/收尾**：task_stop 能终止监控；任务收尾取消仍在跑的监控并删日志；
7. **执行边界不被绕过**：命令过 safety、scenarios 与 shell allowlist，子代理里直接拒绝；
8. **task_output 认 mon- 句柄**：返回监控日志与状态。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from blank_api.db import get_session_factory
from blank_api.services import shell_tasks
from blank_api.services.agent import scenarios
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import AgentSession, render_monitor_notifications
from blank_api.services.models.base import AdapterConfig, ModelAdapter, ModelResponse, Usage
from blank_api.services.permissions import (
    ALLOW,
    ARGUMENT_SCOPED_TOOLS,
    ASK,
    PermissionPolicy,
)
from blank_api.services.safety import SafetyPolicy
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.base import ToolContext, ToolDefinition
from blank_api.services.tools.delegate import _task_output, _task_stop
from blank_api.services.tools.gateway import ToolGateway
from blank_api.services.tools.monitor import (
    MONITOR_DEFAULT_TIMEOUT_MS,
    MONITOR_MAX_TIMEOUT_MS,
    _monitor,
    monitor_tools,
)
from blank_api.services.tools.registry import DEFERRED_TOOLS
from blank_api.services.tools.shell import assess_risk

from tests.test_agent_v04 import (  # noqa: F401  (env 是夹具，故意导入)
    create_project,
    create_task,
    env,
)


PY = sys.executable


def _context(tmp_path: Path, task_id: str, **extra) -> ToolContext:
    options = {
        "root": tmp_path,
        "task_id": task_id,
        "session_factory": None,
        "safety": SafetyPolicy(
            shell_enabled=True,
            network_enabled=True,
            confirm_all_commands=False,
            timeout_seconds=120.0,
        ),
        "sandbox_tier": "off",
    }
    options.update(extra)
    return ToolContext(**options)


def _command(body: str) -> str:
    return f'"{PY}" -c "{body}"'


async def _wait_done(task_id: str, monitor_id: str, limit: float = 20.0) -> dict:
    return await shell_tasks.output(task_id, monitor_id, block=True, timeout=limit)


def _collect(task_id: str, monitor_id: str) -> lambda: None:  # type: ignore[valid-type]
    return lambda: [
        note
        for note in shell_tasks.drain_monitor_events(task_id)
        if note.get("monitor_id") == monitor_id
    ]


async def _drain_until_ended(task_id: str, monitor_id: str, limit: float = 20.0) -> dict:
    deadline = asyncio.get_running_loop().time() + limit
    events: list[dict] = []
    dropped = 0
    ended = False
    status = ""
    while asyncio.get_running_loop().time() < deadline:
        batches = _collect(task_id, monitor_id)()
        for note in batches:
            events.extend(note.get("events") or [])
            dropped += int(note.get("dropped") or 0)
            status = str(note.get("status") or status)
            ended = ended or bool(note.get("ended"))
        if ended:
            break
        await asyncio.sleep(0.05)
    return {"events": events, "dropped": dropped, "ended": ended, "status": status}


def _monitor_tool() -> ToolDefinition:
    return next(tool for tool in monitor_tools() if tool.name == "monitor")


# --------------------------------------------------------------------------- #
# 1. 契约与注册
# --------------------------------------------------------------------------- #
def test_monitor_schema_matches_claude_shape() -> None:
    tool = _monitor_tool()
    schema = tool.input_schema
    assert schema["required"] == ["description", "command", "timeout_ms"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["timeout_ms"]["maximum"] == MONITOR_MAX_TIMEOUT_MS
    assert schema["properties"]["timeout_ms"]["minimum"] == 0
    assert "task_output" in tool.description and "不要轮询" in tool.description
    assert "WebSocket" in tool.description
    assert tool.risk_for is not None


def test_monitor_is_registered_as_deferred_and_argument_scoped() -> None:
    names = {tool.name for tool in build_default_tools()}
    assert "monitor" in names
    assert "monitor" in DEFERRED_TOOLS
    assert "monitor" in ARGUMENT_SCOPED_TOOLS


def test_scenarios_without_shell_also_exclude_monitor() -> None:
    assert "monitor" in scenarios.excluded_tools(scenarios.DOCS)
    assert "monitor" in scenarios.excluded_tools(scenarios.REVERSE)
    # daily 放开为 Codex 全能 agent 后也能跑命令，monitor 不再排除。
    assert "monitor" not in scenarios.excluded_tools(scenarios.DAILY)
    assert "monitor" not in scenarios.excluded_tools(scenarios.DEV)


# --------------------------------------------------------------------------- #
# 2. 参数校验
# --------------------------------------------------------------------------- #
async def test_timeout_ms_is_required_and_bounded(tmp_path: Path) -> None:
    context = _context(tmp_path, "mon-validate")
    base = {"description": "盯日志", "command": _command("print(1)")}
    try:
        missing = await _monitor(context, dict(base))
        assert missing.ok is False and "timeout_ms" in missing.message

        negative = await _monitor(context, {**base, "timeout_ms": -1})
        assert negative.ok is False and "不能为负数" in negative.message

        too_large = await _monitor(context, {**base, "timeout_ms": MONITOR_MAX_TIMEOUT_MS + 1})
        assert too_large.ok is False and "1800000" in too_large.message

        fractional = await _monitor(context, {**base, "timeout_ms": 1000.5})
        assert fractional.ok is False and "整数" in fractional.message

        no_command = await _monitor(context, {"description": "盯", "timeout_ms": 1000})
        assert no_command.ok is False and "command" in no_command.message

        no_description = await _monitor(
            context, {"command": _command("print(1)"), "timeout_ms": 1000}
        )
        assert no_description.ok is False and "description" in no_description.message
    finally:
        await shell_tasks.aclose("mon-validate")


async def test_persistent_timeout_is_reported_with_its_real_ceiling(tmp_path: Path) -> None:
    task_id = "mon-persistent"
    context = _context(
        tmp_path,
        task_id,
        safety=SafetyPolicy(
            shell_enabled=True,
            network_enabled=True,
            confirm_all_commands=False,
            timeout_seconds=5.0,
        ),
    )
    try:
        outcome = await _monitor(
            context,
            {
                "description": "常驻观察",
                "command": _command("print('ok')"),
                "timeout_ms": 0,
            },
        )
        assert outcome.ok, outcome.message
        # 0 是"不设监控期限"，但命令执行仍被安全超时卡住：不能把安全边界说成无限。
        assert outcome.data["persistent"] is True
        assert outcome.data["timeout_seconds"] == 5.0
        assert any("安全超时" in note for note in outcome.data["notes"])
    finally:
        await shell_tasks.aclose(task_id)


async def test_requested_timeout_is_capped_by_safety_and_says_so(tmp_path: Path) -> None:
    task_id = "mon-cap"
    context = _context(
        tmp_path,
        task_id,
        safety=SafetyPolicy(
            shell_enabled=True,
            network_enabled=True,
            confirm_all_commands=False,
            timeout_seconds=2.0,
        ),
    )
    try:
        outcome = await _monitor(
            context,
            {
                "description": "长监控",
                "command": _command("print('x')"),
                "timeout_ms": 600_000,
            },
        )
        assert outcome.ok, outcome.message
        assert outcome.data["timeout_seconds"] == 2.0
        assert any("安全超时" in note for note in outcome.data["notes"])
    finally:
        await shell_tasks.aclose(task_id)


# --------------------------------------------------------------------------- #
# 3. 真实进程：逐行事件、stderr、结束事件
# --------------------------------------------------------------------------- #
async def test_real_lines_become_events_and_end_is_last(tmp_path: Path) -> None:
    task_id = "mon-lines"
    try:
        outcome = await _monitor(
            _context(tmp_path, task_id),
            {
                "description": "打印两行",
                "command": _command(
                    "import sys;print('one');print('two');print('boom',file=sys.stderr)"
                ),
                "timeout_ms": 30_000,
            },
        )
        assert outcome.ok, outcome.message
        monitor_id = outcome.data["monitor_id"]
        assert monitor_id.startswith("mon-")
        assert outcome.data["kind"] == "monitor"

        snapshot = await _wait_done(task_id, monitor_id)
        assert snapshot["status"] == "completed", snapshot
        collected = await _drain_until_ended(task_id, monitor_id)
        assert collected["ended"] is True
        assert collected["status"] == "completed"
        texts = [(event["tag"], event["text"]) for event in collected["events"]]
        assert ("stdout", "one") in texts
        assert ("stdout", "two") in texts
        assert ("stderr", "boom") in texts
        assert texts.index(("stdout", "one")) < texts.index(("stdout", "two"))

        out = await _task_output(
            _context(tmp_path, task_id), {"task_id": monitor_id}
        )
        assert out.ok is True, out.message
        assert "one" in out.output and "two" in out.output
        assert out.data["kind"] == "monitor"
        assert shell_tasks.drain_notifications(task_id) == [], "监控的结束被两条通道各报了一次"
        assert shell_tasks.drain_monitor_events(task_id) == [], "同一个监控被通知了第二次"
    finally:
        await shell_tasks.aclose(task_id)


async def test_events_are_available_before_the_process_exits(tmp_path: Path) -> None:
    task_id = "mon-stream"
    try:
        outcome = await _monitor(
            _context(tmp_path, task_id),
            {
                "description": "边跑边输出",
                "command": _command(
                    "import time,sys;print('first',flush=True);time.sleep(1.2);print('second',flush=True)"
                ),
                "timeout_ms": 30_000,
            },
        )
        monitor_id = outcome.data["monitor_id"]
        deadline = asyncio.get_running_loop().time() + 8
        seen: list[str] = []
        while asyncio.get_running_loop().time() < deadline:
            for note in _collect(task_id, monitor_id)():
                seen.extend(event["text"] for event in note.get("events") or [])
            if "first" in seen:
                record = shell_tasks.find(task_id, monitor_id)
                assert record is not None and record.status == "running", "事件是进程结束后才给的"
                break
            await asyncio.sleep(0.05)
        assert "first" in seen, f"没在命令结束前拿到事件：{seen}"
    finally:
        await shell_tasks.aclose(task_id)


async def test_partial_chunks_are_joined_into_one_line(tmp_path: Path) -> None:
    task_id = "mon-chunk"
    try:
        async def runner(sink):
            sink("stdout", b"abc")
            sink("stdout", b"def\n")
            return {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}

        record = shell_tasks.start(
            task_id=task_id,
            command="split",
            description="跨块的一行",
            timeout_seconds=5,
            runner=runner,
            kind="monitor",
        )
        await _wait_done(task_id, record.shell_id)
        collected = await _drain_until_ended(task_id, record.shell_id)
        assert [event["text"] for event in collected["events"]] == ["abcdef"]
        assert collected["ended"] is True
    finally:
        await shell_tasks.aclose(task_id)


async def test_blank_lines_are_not_delivered_and_utf8_survives_chunk_splits(
    tmp_path: Path,
) -> None:
    """空行不占注入；多字节字符被切在两个 chunk 里也不能变成替换符。"""
    task_id = "mon-utf8"
    try:
        async def runner(sink):
            # "中" = e4 b8 ad：故意从第一个字节后断开。
            sink("stdout", b"a\n\n\xe4\xb8")
            sink("stdout", b"\xad\n\nb\n")
            return {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}

        record = shell_tasks.start(
            task_id=task_id,
            command="utf8",
            description="空行与多字节",
            timeout_seconds=5,
            runner=runner,
            kind="monitor",
        )
        await _wait_done(task_id, record.shell_id)
        collected = await _drain_until_ended(task_id, record.shell_id)
        assert [event["text"] for event in collected["events"]] == ["a", "中", "b"]
    finally:
        await shell_tasks.aclose(task_id)


# --------------------------------------------------------------------------- #
# 4. 投递上限与积压
# --------------------------------------------------------------------------- #
async def test_drain_is_capped_and_reports_pending(tmp_path: Path) -> None:
    task_id = "mon-cap-drain"
    try:
        async def runner(sink):
            for index in range(60):
                sink("stdout", f"line-{index}\n".encode())
            return {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}

        record = shell_tasks.start(
            task_id=task_id,
            command="many",
            description="很多行",
            timeout_seconds=5,
            runner=runner,
            kind="monitor",
        )
        await _wait_done(task_id, record.shell_id)
        first = _collect(task_id, record.shell_id)()
        assert len(first) == 1, first
        assert len(first[0]["events"]) == shell_tasks.MONITOR_EVENTS_PER_DRAIN
        assert first[0]["pending"] == 60 - shell_tasks.MONITOR_EVENTS_PER_DRAIN
        assert first[0]["ended"] is False, "还在投积压时不该先说监控结束"

        collected = await _drain_until_ended(task_id, record.shell_id)
        # 第一屏已经在上面的断言里取走了 25 行，剩下 35 行在后续各轮投完。
        assert len(first[0]["events"]) + len(collected["events"]) == 60
        assert collected["ended"] is True
        assert first[0]["events"][0]["text"] == "line-0"
        assert collected["events"][-1]["text"] == "line-59"
    finally:
        await shell_tasks.aclose(task_id)


async def test_queue_overflow_drops_oldest_and_reports_the_count(tmp_path: Path) -> None:
    task_id = "mon-overflow"
    try:
        async def runner(sink):
            for index in range(shell_tasks.MONITOR_MAX_PENDING_EVENTS + 50):
                sink("stdout", f"burst-{index}\n".encode())
            return {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}

        record = shell_tasks.start(
            task_id=task_id,
            command="burst",
            description="爆量输出",
            timeout_seconds=5,
            runner=runner,
            kind="monitor",
        )
        await _wait_done(task_id, record.shell_id)
        collected = await _drain_until_ended(task_id, record.shell_id)
        assert collected["dropped"] == 50
        assert len(collected["events"]) == shell_tasks.MONITOR_MAX_PENDING_EVENTS
        # 丢的是最旧的：第一条事件已经是第 50 行，队列没有无限长大。
        assert collected["events"][0]["text"] == "burst-50"
    finally:
        await shell_tasks.aclose(task_id)


# --------------------------------------------------------------------------- #
# 5. 系统通知注入
# --------------------------------------------------------------------------- #
def test_notification_renderer_names_handle_events_and_end() -> None:
    block = render_monitor_notifications(
        [
            {
                "monitor_id": "mon-abcd1234",
                "description": "盯构建",
                "status": "completed",
                "events": [
                    {"tag": "stdout", "text": "compiled"},
                    {"tag": "stderr", "text": "warn"},
                ],
                "dropped": 3,
                "pending": 2,
                "ended": True,
                "message": "监控的命令已结束（退出码 0），监控随之结束",
            }
        ]
    )
    assert "mon-abcd1234" in block and "盯构建" in block
    assert "compiled" in block and "[stderr] warn" in block
    assert "另有 3 行" in block and "还有 2 行" in block
    assert "监控已结束" in block
    assert "不是用户发言" in block and "不要轮询" in block


class _NoteAdapter(ModelAdapter):
    def __init__(self) -> None:
        super().__init__(
            AdapterConfig(
                name="note-mon",
                kind="openai",
                base_url="http://localhost",
                api_key="",
                model="note-mon",
                supports_tools=False,
            )
        )
        self.payloads: list[list] = []

    async def _answer(self, messages):
        self.payloads.append(list(messages))
        return ModelResponse(text="好", usage=Usage(1, 1), model="note-mon")

    async def invoke(self, messages, **kwargs):  # noqa: ANN001
        return await self._answer(messages)

    async def bind_tools(self, specs, messages, **kwargs):  # noqa: ANN001
        return await self._answer(messages)

    async def stream(self, messages, **kwargs):  # noqa: ANN001
        self.payloads.append(list(messages))
        yield "好"


async def test_monitor_events_are_injected_as_a_system_note_once(env, workspace: Path):  # noqa: F811
    project_id = await create_project(env, workspace)
    task_id = await create_task(env, project_id, ["监控输出"])
    adapter = _NoteAdapter()

    async def fake_runner(sink):
        sink("stdout", b"logged-line\n")
        return {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}

    record = shell_tasks.start(
        task_id=task_id,
        command="watch",
        description="盯日志",
        timeout_seconds=30,
        runner=fake_runner,
        kind="monitor",
    )
    for _ in range(100):
        if record.task is not None and record.task.done():
            break
        await asyncio.sleep(0.02)
    assert record.status == "completed", record.status

    context = ToolContext(
        root=workspace,
        task_id=task_id,
        session_factory=get_session_factory(),
    )
    session = AgentSession(
        adapter=adapter,
        gateway=ToolGateway([]),
        context=context,
        recorder=EventRecorder(get_session_factory(), task_id),
        session_factory=get_session_factory(),
        provider_id="mon-provider",
        provider_name="note-mon",
        goal="监控输出",
    )
    state = {"messages": [{"role": "user", "content": "继续"}]}
    try:
        await session.ask(state, "", "execute_action")
        last = adapter.payloads[-1][-1]
        assert last.role == "system", last
        assert record.shell_id in last.content
        assert "logged-line" in last.content
        assert "监控已结束" in last.content

        await session.ask(state, "", "execute_action")
        assert adapter.payloads[-1][-1].role != "system", "同一批事件被注入了第二次"
    finally:
        await shell_tasks.aclose(task_id)


# --------------------------------------------------------------------------- #
# 6. 终止与收尾
# --------------------------------------------------------------------------- #
async def test_task_stop_cancels_a_monitor(tmp_path: Path) -> None:
    task_id = "mon-stop"
    try:
        context = _context(tmp_path, task_id)
        outcome = await _monitor(
            context,
            {
                "description": "长跑",
                "command": _command("import time;print('up',flush=True);time.sleep(30)"),
                "timeout_ms": 60_000,
            },
        )
        monitor_id = outcome.data["monitor_id"]
        deadline = asyncio.get_running_loop().time() + 8
        while asyncio.get_running_loop().time() < deadline:
            record = shell_tasks.find(task_id, monitor_id)
            if record is not None and record.log_path.exists() and record.log_path.stat().st_size:
                break
            await asyncio.sleep(0.05)

        stopped = await _task_stop(context, {"task_id": monitor_id})
        assert stopped.ok is True, stopped.message
        assert "已终止监控" in stopped.output
        snapshot = await shell_tasks.output(task_id, monitor_id, block=False)
        assert snapshot["status"] == "cancelled", snapshot

        collected = await _drain_until_ended(task_id, monitor_id)
        assert collected["ended"] is True
        assert collected["status"] == "cancelled"
    finally:
        await shell_tasks.aclose(task_id)


async def test_aclose_cancels_monitors_and_removes_logs(tmp_path: Path) -> None:
    task_id = "mon-aclose"
    context = _context(tmp_path, task_id)
    outcome = await _monitor(
        context,
        {
            "description": "收尾",
            "command": _command("import time;print('log-line',flush=True);time.sleep(30)"),
            "timeout_ms": 60_000,
        },
    )
    monitor_id = outcome.data["monitor_id"]
    record = shell_tasks.find(task_id, monitor_id)
    assert record is not None
    log_path = record.log_path
    deadline = asyncio.get_running_loop().time() + 10
    while asyncio.get_running_loop().time() < deadline and not (
        log_path.exists() and log_path.stat().st_size > 0
    ):
        await asyncio.sleep(0.05)
    assert log_path.exists() and log_path.stat().st_size > 0, "日志从未落盘，收尾判据无从成立"

    cancelled = await shell_tasks.aclose(task_id)
    assert cancelled == [monitor_id]
    assert not log_path.exists(), "收尾后日志还留在临时目录里"
    assert shell_tasks.find(task_id, monitor_id) is None
    assert shell_tasks.drain_monitor_events(task_id) == []


# --------------------------------------------------------------------------- #
# 7. 执行边界
# --------------------------------------------------------------------------- #
async def test_agent_cannot_start_a_monitor(tmp_path: Path) -> None:
    task_id = "mon-subagent"
    context = _context(tmp_path, task_id, allow_background_shell=False)
    try:
        outcome = await _monitor(
            context,
            {
                "description": "子代理里",
                "command": _command("print(1)"),
                "timeout_ms": 1000,
            },
        )
        assert outcome.ok is False and outcome.status == "denied"
        assert "子代理" in outcome.message
        assert shell_tasks.drain_monitor_events(task_id) == [], "被拒绝的调用留下了句柄"
    finally:
        await shell_tasks.aclose(task_id)


async def test_safety_gate_blocks_monitor_before_a_process_starts(tmp_path: Path) -> None:
    task_id = "mon-safety"
    context = _context(
        tmp_path,
        task_id,
        safety=SafetyPolicy(shell_enabled=False, confirm_all_commands=False),
    )
    try:
        outcome = await _monitor(
            context,
            {
                "description": "没开命令执行",
                "command": _command("print(1)"),
                "timeout_ms": 1000,
            },
        )
        assert outcome.ok is False and outcome.status == "denied"
        assert outcome.data["policy"] == "safety"
        assert shell_tasks.find(task_id, "mon-") is None
        assert shell_tasks.drain_monitor_events(task_id) == []
    finally:
        await shell_tasks.aclose(task_id)


def test_shell_allowlist_also_gates_monitor(monkeypatch) -> None:
    """allowlist 只认 run_shell 时，换到 monitor 就能跑同一条命令——那是旁路。"""
    monkeypatch.setenv("BLANK_SHELL_POLICY", "allowlist")

    async def handler(context, arguments):  # pragma: no cover - 只用于构造定义
        raise AssertionError("handler should not be called")

    tool = ToolDefinition(
        name="monitor",
        description="d",
        input_schema={"type": "object", "required": ["description", "command", "timeout_ms"]},
        risk_level="medium",
        risk_for=lambda arguments: assess_risk(str(arguments.get("command", ""))),
        handler=handler,
    )
    decision = PermissionPolicy().decide(
        tool,
        {"description": "盯", "command": "echo hi", "timeout_ms": 1000},
        "medium",
    )
    assert decision.outcome == ASK, decision
    assert decision.rule == "policy:shell_allowlist"


def test_monitor_does_not_get_a_tool_scope_grant() -> None:
    """带 command 的工具不能记成「这个会话以后都免问 monitor」——那等于整台 shell。"""

    async def handler(context, arguments):  # pragma: no cover - 只用于构造定义
        raise AssertionError("handler should not be called")

    tool = ToolDefinition(
        name="monitor",
        description="d",
        input_schema={"type": "object", "required": ["description", "command", "timeout_ms"]},
        risk_level="medium",
        risk_for=lambda arguments: assess_risk(str(arguments.get("command", ""))),
        handler=handler,
    )
    policy = PermissionPolicy()
    arguments = {"description": "盯", "command": "echo hi", "timeout_ms": 1000}
    decision = policy.decide(tool, arguments, "medium")
    assert decision.outcome == ALLOW, decision
    # tool_grant_too_wide 认 command：硬传 scope=tool 也不入账，下一条命令照样问。
    policy.remember(tool.name, arguments, "tool")
    assert policy.approved_tools == set()
