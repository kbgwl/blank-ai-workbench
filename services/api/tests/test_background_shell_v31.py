# -*- coding: utf-8 -*-
"""Q1：后台 shell（run_shell(run_in_background=true)）对齐 Claude 的 Bash 工具。

钉住六件事：

1. **非阻塞**：传 run_in_background 立即返回 shell_id，命令在后台真的跑（日志里有输出）；
2. **可取输出**：task_output 等到结束并带回输出与退出码；还在跑时如实说"仍不完整"；
3. **可终止**：task_stop 终止进程并把状态记成 cancelled，不谎报"已结束"；
4. **流式**：宿主执行路径上，命令还没结束时就能看到已经产生的输出；
5. **完成通知**：结束由系统在下一次模型调用前注入，每个句柄只投递一次；
6. **收尾不留孤儿**：任务结束时仍在跑的后台命令被取消、日志被删除。

每条都对应一处会退回去的实现；改坏它，这一格会红在具体断言上。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from blank_api.db import get_session_factory
from blank_api.services import shell_tasks
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import SYSTEM_PROMPT, AgentSession, render_shell_notifications
from blank_api.services.agent.runtime import AgentRuntime
from blank_api.services.models.base import AdapterConfig, ModelAdapter, ModelResponse, Usage
from blank_api.services.safety import SafetyPolicy
from blank_api.services.subagent import SubAgentRequest, SubAgentRunner
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.delegate import _task_output, _task_stop, delegate_tools
from blank_api.services.tools.gateway import ToolGateway
from blank_api.services.tools.shell import _run_shell, shell_tools

from tests.test_agent_v04 import (  # noqa: F401  (env 是夹具，故意导入)
    create_project,
    create_task,
    env,
)
from tests.test_background_subagent_v116 import SlowAdapter, _session


PY = sys.executable


def _context(tmp_path: Path, task_id: str, **extra) -> ToolContext:
    return ToolContext(
        root=tmp_path,
        task_id=task_id,
        session_factory=None,
        safety=SafetyPolicy(shell_enabled=True, network_enabled=True, confirm_all_commands=False),
        sandbox_tier="off",
        **extra,
    )


def _command(body: str) -> str:
    # 用解释器而不是 echo/sleep：Windows cmd 与 POSIX sh 都能跑，且能控制输出与耗时。
    return f'"{PY}" -c "{body}"'


async def _wait_status(task_id: str, shell_id: str, status: str, limit: float = 10.0) -> dict:
    deadline = asyncio.get_running_loop().time() + limit
    snapshot: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        snapshot = await shell_tasks.output(task_id, shell_id, block=False)
        if snapshot.get("status") == status:
            return snapshot
        await asyncio.sleep(0.05)
    return snapshot


# --------------------------------------------------------------------------- #
# 1. 工具面与非阻塞
# --------------------------------------------------------------------------- #
def test_run_shell_exposes_background_parameters() -> None:
    tool = next(tool for tool in shell_tools() if tool.name == "run_shell")
    props = tool.input_schema["properties"]
    assert "run_in_background" in props and props["run_in_background"]["type"] == "boolean"
    assert "description" in props
    assert "run_in_background" in tool.description and "task_output" in tool.description


async def test_background_shell_returns_a_handle_then_completes(tmp_path: Path) -> None:
    task_id = "bg-complete"
    try:
        context = _context(tmp_path, task_id)
        outcome = await _run_shell(
            context,
            {
                "command": _command("import time;print('bg-ok');time.sleep(0.2)"),
                "description": "后台打印",
                "run_in_background": True,
            },
        )
        assert outcome.ok, outcome.message
        shell_id = outcome.data["shell_id"]
        assert shell_id.startswith("shell-"), shell_id
        assert outcome.data["status"] == "running"
        # 没传 timeout_seconds 的后台命令按 600 秒上限跑；30 秒的前台默认值会把构建直接判死。
        assert outcome.data["timeout_seconds"] == 600

        snapshot = await shell_tasks.output(task_id, shell_id, block=True, timeout=20)
        assert snapshot["status"] == "completed", snapshot
        assert snapshot["exit_code"] == 0
        assert "bg-ok" in snapshot["text"], snapshot["text"]
        # 后台命令真的起过进程：日志文件里留着输出，而不是工具凭空编了句 completed。
        record = shell_tasks.find(task_id, shell_id)
        assert record is not None and record.log_path.stat().st_size > 0
    finally:
        await shell_tasks.aclose(task_id)


async def test_background_shell_streams_output_before_it_finishes(tmp_path: Path) -> None:
    task_id = "bg-stream"
    try:
        context = _context(tmp_path, task_id)
        outcome = await _run_shell(
            context,
            {
                "command": _command("import time;print('early',flush=True);time.sleep(2)"),
                "run_in_background": True,
            },
        )
        shell_id = outcome.data["shell_id"]
        seen_while_running = False
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            snapshot = await shell_tasks.output(task_id, shell_id, block=False)
            if "early" in snapshot.get("text", "") and snapshot["status"] == "running":
                seen_while_running = True
                break
            if snapshot["status"] != "running":
                break
            await asyncio.sleep(0.05)
        assert seen_while_running, "命令结束前没有看到已经产生的输出：后台执行退回了整段缓冲"
    finally:
        await shell_tasks.aclose(task_id)


async def test_run_in_background_false_still_waits_for_the_result(tmp_path: Path) -> None:
    outcome = await _run_shell(
        _context(tmp_path, "bg-front"),
        {"command": _command("print('front')")},
    )
    assert outcome.ok and "front" in outcome.output, outcome.message
    assert "shell_id" not in outcome.data


# --------------------------------------------------------------------------- #
# 2. task_output / task_stop 路由到 shell 句柄
# --------------------------------------------------------------------------- #
async def test_task_output_reads_a_shell_handle(tmp_path: Path) -> None:
    task_id = "bg-output"
    try:
        context = _context(tmp_path, task_id)
        started = await _run_shell(
            context, {"command": _command("print('read-me')"), "run_in_background": True}
        )
        shell_id = started.data["shell_id"]
        out = await _task_output(context, {"task_id": shell_id, "block": True, "timeout": 20})
        assert out.ok and "read-me" in out.output, out.output
        assert out.data["status"] == "completed"
        assert out.data["exit_code"] == 0
        # 旧参数名继续接受，行为一致。
        legacy = await _task_output(context, {"shell_id": shell_id, "block": False})
        assert legacy.ok and legacy.data["status"] == "completed"
    finally:
        await shell_tasks.aclose(task_id)


async def test_task_stop_terminates_a_running_shell(tmp_path: Path) -> None:
    task_id = "bg-stop"
    try:
        context = _context(tmp_path, task_id)
        started = await _run_shell(
            context,
            {
                "command": _command(
                    "import time;print('started',flush=True);time.sleep(1.5);"
                    "open('survived.txt','w').write('x')"
                ),
                "run_in_background": True,
            },
        )
        shell_id = started.data["shell_id"]
        # 先等它真的起进程并打出第一行：否则测到的可能是「协程还没跑到起进程」。
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            if "started" in (await shell_tasks.output(task_id, shell_id, block=False))["text"]:
                break
            await asyncio.sleep(0.05)
        stopped = await _task_stop(context, {"task_id": shell_id})
        assert stopped.ok and "已取消" in stopped.output, stopped.output
        assert stopped.data["status"] == "cancelled"
        # 不是只把状态写成 cancelled：后台协程必须真的结束，进程才跟着被杀。
        record = shell_tasks.find(task_id, shell_id)
        assert record is not None and record.task is not None and record.task.done()
        after = await _task_output(context, {"task_id": shell_id, "block": False})
        assert after.ok is False and after.data["status"] == "cancelled", after
        assert "已取消" in after.message
        # 取消的必须是进程，不是只取消协程：命令后半段（写文件）不能再发生。
        await asyncio.sleep(2.0)
        assert not (tmp_path / "survived.txt").exists(), "取消的只是协程，子进程还在跑"
    finally:
        await shell_tasks.aclose(task_id)


async def test_unknown_shell_handle_is_reported_honestly(tmp_path: Path) -> None:
    out = await _task_output(_context(tmp_path, "bg-unknown"), {"task_id": "shell-deadbeef"})
    assert out.ok is False
    assert "没有找到后台命令" in out.message


async def test_task_output_accepts_task_id_for_agent_handles(tmp_path: Path) -> None:
    """新参数名 task_id 也必须能取子代理结论；老名字继续能用。"""

    class RunnerStub:
        def __init__(self) -> None:
            self.seen: list[tuple[str, bool]] = []

        async def output(self, agent_id: str, *, block: bool, timeout: float) -> dict:
            self.seen.append((agent_id, block))
            return {
                "ok": True,
                "status": "completed",
                "agent_id": agent_id,
                "text": f"结论：{agent_id}",
                "summary": "结论",
            }

    runner = RunnerStub()
    context = _context(tmp_path, "bg-agent", subagent_runner=runner)
    out = await _task_output(context, {"task_id": "sub-abc", "block": False})
    assert out.ok and out.output == "结论：sub-abc", out.output
    assert runner.seen == [("sub-abc", False)]


# --------------------------------------------------------------------------- #
# 3. 失败 / 超时 / 通知 / 收尾
# --------------------------------------------------------------------------- #
async def test_failed_command_reports_exit_code_and_stderr(tmp_path: Path) -> None:
    task_id = "bg-fail"
    try:
        context = _context(tmp_path, task_id)
        started = await _run_shell(
            context,
            {
                "command": _command("import sys;print('boom',file=sys.stderr);sys.exit(3)"),
                "run_in_background": True,
            },
        )
        snapshot = await shell_tasks.output(
            task_id, started.data["shell_id"], block=True, timeout=20
        )
        assert snapshot["status"] == "failed", snapshot
        assert snapshot["exit_code"] == 3
        assert "boom" in snapshot["text"]
        out = await _task_output(context, {"task_id": started.data["shell_id"]})
        assert out.ok is False and "退出码 3" in out.message
    finally:
        await shell_tasks.aclose(task_id)


async def test_timeout_is_reported_as_timeout_not_failure(tmp_path: Path) -> None:
    task_id = "bg-timeout"
    try:
        context = _context(tmp_path, task_id)
        started = await _run_shell(
            context,
            {
                "command": _command("import time;time.sleep(20)"),
                "timeout_seconds": 0.6,
                "run_in_background": True,
            },
        )
        snapshot = await shell_tasks.output(
            task_id, started.data["shell_id"], block=True, timeout=20
        )
        assert snapshot["status"] == "timeout", snapshot
        assert snapshot["timed_out"] is True
        assert "超时" in snapshot["message"]
    finally:
        await shell_tasks.aclose(task_id)


async def test_completion_notification_is_drained_once(tmp_path: Path) -> None:
    task_id = "bg-notify"
    try:
        context = _context(tmp_path, task_id)
        started = await _run_shell(
            context, {"command": _command("print('notify-me')"), "run_in_background": True}
        )
        await shell_tasks.output(task_id, started.data["shell_id"], block=True, timeout=20)
        notes = shell_tasks.drain_notifications(task_id)
        assert len(notes) == 1, notes
        assert notes[0]["shell_id"] == started.data["shell_id"]
        assert notes[0]["status"] == "completed" and notes[0]["exit_code"] == 0
        assert "notify-me" in notes[0]["tail"]
        assert shell_tasks.drain_notifications(task_id) == [], "同一个句柄被通知了第二次"
    finally:
        await shell_tasks.aclose(task_id)


def test_notification_renderer_names_handle_status_and_tail() -> None:
    block = render_shell_notifications(
        [
            {
                "shell_id": "shell-abcd1234",
                "command": "npm run build",
                "description": "构建前端",
                "status": "failed",
                "exit_code": 2,
                "timed_out": False,
                "tail": "error TS1234",
            }
        ]
    )
    assert "shell-abcd1234" in block and "exit=2" in block
    assert "构建前端" in block and "error TS1234" in block
    assert "系统事件" in block and "task_output" in block


class _NoteAdapter(ModelAdapter):
    """记录每次模型调用看到的完整消息列表。"""

    def __init__(self) -> None:
        super().__init__(
            AdapterConfig(
                name="note-bg",
                kind="openai",
                base_url="http://localhost",
                api_key="",
                model="note-bg",
                supports_tools=False,
            )
        )
        self.payloads: list[list] = []

    async def _answer(self, messages):
        self.payloads.append(list(messages))
        return ModelResponse(text="好", usage=Usage(1, 1), model="note-bg")

    async def invoke(self, messages, **kwargs):  # noqa: ANN001
        return await self._answer(messages)

    async def bind_tools(self, specs, messages, **kwargs):  # noqa: ANN001
        return await self._answer(messages)

    async def stream(self, messages, **kwargs):  # noqa: ANN001
        self.payloads.append(list(messages))
        yield "好"


async def test_shell_completion_is_injected_as_a_system_note_once(env, workspace: Path):  # noqa: F811
    project_id = await create_project(env, workspace)
    task_id = await create_task(env, project_id, ["后台命令跑完"])
    adapter = _NoteAdapter()

    async def fake_runner(sink):
        sink("stdout", b"injected-ok\n")
        return {"exit_code": 0, "stdout": "injected-ok\n", "stderr": "", "timed_out": False}

    record = shell_tasks.start(
        task_id=task_id,
        command="build",
        description="后台构建",
        timeout_seconds=30,
        runner=fake_runner,
    )
    for _ in range(50):
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
        provider_id="bg-provider",
        provider_name="note-bg",
        goal="后台命令跑完",
    )
    state = {"messages": [{"role": "user", "content": "继续"}]}
    try:
        await session.ask(state, "", "execute_action")
        last = adapter.payloads[-1][-1]
        assert last.role == "system", last
        assert record.shell_id in last.content
        assert "injected-ok" in last.content
        assert "系统事件" in last.content

        await session.ask(state, "", "execute_action")
        assert adapter.payloads[-1][-1].role != "system", "同一份通知被注入了第二次"
    finally:
        await shell_tasks.aclose(task_id)


async def test_aclose_cancels_running_shells_and_removes_logs(tmp_path: Path) -> None:
    task_id = "bg-aclose"
    context = _context(tmp_path, task_id)
    started = await _run_shell(
        context,
        {
            "command": _command("import time;print('log-line',flush=True);time.sleep(30)"),
            "run_in_background": True,
        },
    )
    shell_id = started.data["shell_id"]
    record = shell_tasks.find(task_id, shell_id)
    assert record is not None
    log_path = record.log_path
    # 先等日志真的落盘：一条不产生输出的命令根本不会建文件，那样「已删除」是空判据。
    deadline = asyncio.get_running_loop().time() + 10
    while asyncio.get_running_loop().time() < deadline and not (
        log_path.exists() and log_path.stat().st_size > 0
    ):
        await asyncio.sleep(0.05)
    assert log_path.exists() and log_path.stat().st_size > 0, "日志从未落盘，收尾判据无从成立"

    cancelled = await shell_tasks.aclose(task_id)
    assert cancelled == [shell_id]
    assert not log_path.exists(), "收尾后日志还留在临时目录里"
    assert shell_tasks.find(task_id, shell_id) is None
    missing = await shell_tasks.output(task_id, shell_id, block=False)
    assert missing["status"] == "not_found"


def test_subagent_context_cannot_start_background_shell(tmp_path: Path) -> None:
    context = _context(tmp_path, "bg-sub", allow_background_shell=False)
    outcome = asyncio.run(
        _run_shell(context, {"command": _command("print(1)"), "run_in_background": True})
    )
    assert outcome.ok is False and outcome.status == "denied"
    assert "子代理" in outcome.message


def test_system_prompt_teaches_background_shell() -> None:
    assert "run_in_background" in SYSTEM_PROMPT
    assert "task_output" in SYSTEM_PROMPT and "task_stop" in SYSTEM_PROMPT
    assert "不要为此 sleep" in SYSTEM_PROMPT


def test_delegate_schemas_accept_the_generic_task_id() -> None:
    tools = {tool.name: tool for tool in delegate_tools()}
    for name in ("task_output", "task_stop"):
        props = tools[name].input_schema["properties"]
        assert "task_id" in props and "agent_id" in props and "shell_id" in props
        assert "required" not in tools[name].input_schema, "旧名调用会被 schema 直接拒掉"


async def test_real_subagent_context_denies_background_shell(env, workspace: Path):  # noqa: F811
    """上面的用例只能证明入口会看这个开关；这里让子代理真的走到那个入口。

    子代理的 ToolContext 由 subagent.py 自己构造，判据必须穿过那条真实装配线，
    否则「子代理里关掉后台命令」只是一句写在别处的话。
    """

    class _BackgroundShellAdapter(SlowAdapter):
        def __init__(self) -> None:
            super().__init__(0.0)
            self.calls = 0

        async def bind_tools(self, specs, messages, **kwargs):  # noqa: ANN001
            assert "run_shell" in {getattr(item, "name", "") for item in specs}
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    text="",
                    usage=Usage(1, 1),
                    model="bg-sub",
                    tool_calls=[
                        {
                            "id": "call-bg",
                            "type": "function",
                            "function": {
                                "name": "run_shell",
                                "arguments": json.dumps(
                                    {"command": _command("print(1)"), "run_in_background": True}
                                ),
                            },
                        }
                    ],
                )
            return ModelResponse(text="看到了拒绝", usage=Usage(1, 1), model="bg-sub")

    project_id = await create_project(env, workspace)
    task_id = await create_task(env, project_id, ["子代理不许起后台命令"])
    session = _session(workspace, _BackgroundShellAdapter(), task_id=task_id)
    session.session_factory = get_session_factory()
    runner = SubAgentRunner().bind(session)
    try:
        result = await runner(SubAgentRequest(goal="启动一条后台命令", isolate=True, max_steps=2))
        assert result.ok, result.error
        completed = [
            payload
            for event, payload in session.recorder.events
            if event == "tool.completed" and payload.get("tool") == "run_shell"
        ]
        assert completed, [event for event, _ in session.recorder.events]
        assert completed[0]["status"] == "denied", completed[0]
        assert "子代理里不能启动后台命令" in completed[0]["summary"], completed[0]["summary"]
        assert shell_tasks.drain_notifications(task_id) == [], "被拒绝的命令不该留下句柄"
    finally:
        await runner.aclose(task_id)


# --------------------------------------------------------------------------- #
# 4. 任务收尾：runtime 真的会来收后台命令
# --------------------------------------------------------------------------- #
def _finish_scope(factory, task_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        store=factory,
        task_id=task_id,
        recorder=EventRecorder(factory, task_id),
        session=SimpleNamespace(router=None),
        subagents=None,
    )


async def test_finishing_the_task_cancels_leftover_background_shells(env, workspace: Path):  # noqa: F811
    project_id = await create_project(env, workspace)
    task_id = await create_task(env, project_id, ["收尾时还有后台命令"])
    factory = get_session_factory()
    started = await _run_shell(
        _context(workspace, task_id), {"command": _command("import time;time.sleep(30)"), "run_in_background": True}
    )
    shell_id = started.data["shell_id"]
    record = shell_tasks.find(task_id, shell_id)
    assert record is not None
    log_path = record.log_path

    await AgentRuntime(factory)._finish(
        _finish_scope(factory, task_id), "succeeded", summary="完成", reason=""
    )

    assert shell_tasks.find(task_id, shell_id) is None, "任务结束后句柄还留着"
    assert not log_path.exists(), "任务结束后日志还占着盘"
    assert (await shell_tasks.output(task_id, shell_id, block=False))["status"] == "not_found"


async def test_waiting_approval_keeps_background_shell_alive(env, workspace: Path):  # noqa: F811
    """等审批不是终点：恢复后还要用同一个句柄取输出，不能在这里把它杀掉。"""
    project_id = await create_project(env, workspace)
    task_id = await create_task(env, project_id, ["审批中仍有后台命令"])
    factory = get_session_factory()
    started = await _run_shell(
        _context(workspace, task_id), {"command": _command("import time;time.sleep(30)"), "run_in_background": True}
    )
    shell_id = started.data["shell_id"]
    try:
        await AgentRuntime(factory)._finish(
            _finish_scope(factory, task_id), "waiting_approval", summary="等待批准", reason=""
        )
        assert shell_tasks.find(task_id, shell_id) is not None
        assert (await shell_tasks.output(task_id, shell_id, block=False))["status"] == "running"
    finally:
        await shell_tasks.aclose(task_id)

