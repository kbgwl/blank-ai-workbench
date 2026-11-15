"""#320 子代理工具循环跑 PreToolUse / PostToolUse / PostToolUseFailure / PostToolBatch。

上游语义（`grep -boF` 现场复核，偏移即字节）：

* base hook input 带 ``agent_id``：*"Subagent identifier. Present only when the
  hook fires from within a subagent (e.g., a tool called by an AgentTool
  worker). Absent for the main thread"*（24336471），并带 ``agent_type``；
* ``PreToolUse`` 的载荷是 ``tool_name`` / ``tool_input`` / ``tool_use_id``
  （24337589）；``PostToolUse`` 另带 ``tool_response`` 与 ``duration_ms``
  （24337830 / 24337928）；``PostToolUseFailure`` 带 ``error`` / ``is_interrupt``
  / ``duration_ms``（24338072 / 24338196）；``PostToolBatch`` 一次带上
  ``tool_calls[{tool_name, tool_input, tool_use_id, tool_response}]``（24338432）。

本机在此之前只在父会话的工具管线里跑这四类事件；子代理循环直接调
``gateway.execute``，定义里写的 PreToolUse 守卫在子代理内静默不生效（#316 的
文档化偏离 c）。本批把它接上，并与父会话同一套处置口径：

* deny 不执行，回执写成"钩子拒绝执行"；ask 在子代理里没有人可确认，按拒绝
  fail-closed（父会话会把 ask 变成审批中断，这是两者的唯一处置差异）；
* ``updatedInput`` 必须过工具 schema，且改写后的审批判定不能比原来更宽；
* ``additionalContext`` 作为一条 user 消息注入下一次模型调用；
* 被钩子拦下的调用不跑 ``post_tool_use``（它压根没执行）；执行失败额外跑
  ``post_tool_use_failure``；一批调用跑完发一次 ``post_tool_batch``；
* 没配钩子时事件与消息零变化。

每条判据都钉在「拿掉实现就红」的位置上。
"""

from __future__ import annotations

import json
from pathlib import Path

from blank_api.services.models.base import ChatMessage, ModelResponse, Usage
from blank_api.services.subagent import SubAgentRequest
from blank_api.services.tools.base import ToolDefinition, fail, ok

from tests.test_agent_defs_v44 import _agent, _write
from tests.test_agent_hooks_v316 import (  # noqa: F401  (_Rec/_run_child 复用)
    _Rec,
    _ToolCallAdapter,
    _run_child,
    _runner,
    _spec,
)
from tests.test_agent_v04 import (  # noqa: F401  (env 等夹具)
    API,
    create_provider,
    create_project,
    create_task,
    env,
)
from tests.test_hook_events_v52 import _lines, _log_hook


def _call(name: str, arguments: dict) -> dict:
    return {
        "id": f"call-{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _responses(*calls: dict) -> list[ModelResponse]:
    """第一步按参数调用工具，第二步给结论。"""
    return [
        ModelResponse(
            text="",
            usage=Usage(10, 5),
            model="hooks-v320",
            tool_calls=list(calls),
        ),
        ModelResponse(text="子代理结论", usage=Usage(5, 2), model="hooks-v320"),
    ]


def _script(*calls: dict) -> _ToolCallAdapter:
    return _ToolCallAdapter(_responses(*calls))


_EXECUTED: list[dict] = []


def _reset() -> None:
    _EXECUTED.clear()


async def _record(context, arguments):  # pragma: no cover - 只证明跑没跑
    _EXECUTED.append(dict(arguments))
    return ok("pong")


def _probe(
    *,
    name: str = "probe",
    schema: dict | None = None,
    risk_for=None,
    handler=None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="探针工具",
        input_schema=schema
        or {"type": "object", "properties": {}, "required": []},
        risk_level="low",
        handler=handler or _record,
        risk_for=risk_for,
    )


def _agent_hook(event: str, command: str):
    return {event: (_spec(command),)}


# --------------------------------------------------------------------------- #
# 1. pre_tool_use：载荷、拒绝、ask fail-closed、updatedInput
# --------------------------------------------------------------------------- #

async def test_definition_pre_tool_hook_runs_in_child_loop(tmp_path: Path):
    _reset()
    command, log = _log_hook(
        tmp_path,
        "pre",
        body="print(json.dumps({'decision': 'allow', 'reason': 'probe ok'}))\n",
    )
    recorder = _Rec()

    result = await _run_child(
        tmp_path,
        hooks=_runner({}),
        agent_hooks=_agent_hook("pre_tool_use", command),
        adapter=_script(_call("probe", {"path": "a.txt"})),
        tools=[_probe(schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})],
        recorder=recorder,
    )
    assert result.ok, result.error
    assert _EXECUTED == [{"path": "a.txt"}]

    rows = _lines(log)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["hook_event_name"] == "pre_tool_use"
    assert row["tool"] == "probe"
    assert row["arguments"] == {"path": "a.txt"}
    # 上游 base hook input 里区分"子代理里的调用"靠这两个字段（24336471）。
    assert row["agent_id"] == "agent-v316"
    assert row["agent_type"] == "fast"
    assert row["session_id"] == "task-v316"
    assert row["tool_use_id"] == "call-probe"
    assert row["transcript_path"].endswith("subagent-transcript.jsonl")

    completed = [payload for event, _node, payload in recorder.events if event == "hook.completed"]
    assert [item["event"] for item in completed] == ["pre_tool_use"]


async def test_pre_tool_deny_blocks_execution(tmp_path: Path):
    _reset()
    command, pre_log = _log_hook(
        tmp_path,
        "deny",
        body="print(json.dumps({'decision': 'deny', 'reason': '这个文件不许读'}))\n",
    )
    post_command, post_log = _log_hook(tmp_path, "post-after-deny")
    recorder = _Rec()

    result = await _run_child(
        tmp_path,
        hooks=_runner({}),
        agent_hooks={"pre_tool_use": (_spec(command),), "post_tool_use": (_spec(post_command),)},
        adapter=_script(_call("probe", {"path": "secret.txt"})),
        tools=[_probe()],
        recorder=recorder,
    )
    assert result.ok, result.error
    # 拒绝 = 这一次不执行；handler 一次都没被调。
    assert _EXECUTED == []
    assert len(_lines(pre_log)) == 1
    # 没执行的调用不跑 post_tool_use。
    assert not post_log.exists()
    # 模型看到的是钩子拒绝的回执，不是工具成功。
    tool_messages = [item for item in result.conversation if item.get("role") == "tool"]
    assert tool_messages, result.conversation
    assert "钩子拒绝执行" in tool_messages[-1]["content"]
    payload = json.loads(tool_messages[-1]["content"])
    assert payload["status"] == "denied" and payload["ok"] is False
    assert recorder.named("tool.completed")[-1]["status"] == "denied"


async def test_pre_tool_ask_fails_closed_in_subagent(tmp_path: Path):
    _reset()
    command, _log = _log_hook(
        tmp_path,
        "ask",
        body="print(json.dumps({'decision': 'ask', 'reason': '要人点头'}))\n",
    )

    result = await _run_child(
        tmp_path,
        hooks=_runner({}),
        agent_hooks=_agent_hook("pre_tool_use", command),
        adapter=_script(_call("probe", {})),
        tools=[_probe()],
    )
    assert result.ok, result.error
    assert _EXECUTED == []
    tool_messages = [item for item in result.conversation if item.get("role") == "tool"]
    assert tool_messages
    assert "人工确认" in tool_messages[-1]["content"]
    assert json.loads(tool_messages[-1]["content"])["status"] == "denied"


async def test_updated_input_is_applied_and_carried_to_post_hook(tmp_path: Path):
    _reset()
    pre_command, pre_log = _log_hook(
        tmp_path,
        "rewrite",
        body="print(json.dumps({'updatedInput': {'path': 'b.txt'}}))\n",
    )
    post_command, post_log = _log_hook(tmp_path, "post-rewritten")
    recorder = _Rec()

    result = await _run_child(
        tmp_path,
        hooks=_runner({}),
        agent_hooks={"pre_tool_use": (_spec(pre_command),), "post_tool_use": (_spec(post_command),)},
        adapter=_script(_call("probe", {"path": "a.txt"})),
        tools=[_probe(schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})],
        recorder=recorder,
    )
    assert result.ok, result.error
    # 钩子改写后的参数既进执行，也进 post 钩子：两边看到的不能是两份参数。
    assert _EXECUTED == [{"path": "b.txt"}]
    assert _lines(pre_log)[0]["arguments"] == {"path": "a.txt"}
    assert _lines(post_log)[0]["arguments"] == {"path": "b.txt"}
    updated = [payload for event, _node, payload in recorder.events if event == "hook.input_updated"]
    assert updated and updated[0]["arguments"] == {"path": "b.txt"}


async def test_updated_input_must_pass_schema(tmp_path: Path):
    _reset()
    command, _log = _log_hook(
        tmp_path,
        "bad-schema",
        body="print(json.dumps({'updatedInput': {'other': 1}}))\n",
    )
    recorder = _Rec()

    result = await _run_child(
        tmp_path,
        hooks=_runner({}),
        agent_hooks=_agent_hook("pre_tool_use", command),
        adapter=_script(_call("probe", {"path": "a.txt"})),
        tools=[_probe(schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})],
        recorder=recorder,
    )
    assert result.ok, result.error
    # 未过 schema 的改写被丢弃，执行仍是原参数。
    assert _EXECUTED == [{"path": "a.txt"}]
    rejected = [payload for event, _node, payload in recorder.events if event == "hook.input_rejected"]
    assert rejected and "schema" in rejected[0]["reason"]


async def test_updated_input_cannot_raise_risk(tmp_path: Path):
    _reset()
    command, _log = _log_hook(
        tmp_path,
        "loosen",
        body="print(json.dumps({'updatedInput': {'danger': True}}))\n",
    )
    recorder = _Rec()

    result = await _run_child(
        tmp_path,
        hooks=_runner({}),
        agent_hooks=_agent_hook("pre_tool_use", command),
        adapter=_script(_call("probe", {"danger": False})),
        tools=[
            _probe(
                schema={"type": "object", "properties": {"danger": {"type": "boolean"}}, "required": []},
                risk_for=lambda arguments: "high" if arguments.get("danger") else "low",
            )
        ],
        recorder=recorder,
    )
    assert result.ok, result.error
    # 改写把风险从 low 抬到 high → 拒绝改写，执行仍是原参数。
    assert _EXECUTED == [{"danger": False}]
    rejected = [payload for event, _node, payload in recorder.events if event == "hook.input_rejected"]
    assert rejected and "风险" in rejected[0]["reason"]


async def test_additional_context_is_injected_for_next_call(tmp_path: Path):
    _reset()
    command, _log = _log_hook(
        tmp_path,
        "context",
        body="print(json.dumps({'additionalContext': '外部检查：先读 README'}))\n",
    )

    class _Recording(_ToolCallAdapter):
        def __init__(self, script):
            super().__init__(script)
            self.seen: list[list[ChatMessage]] = []

        async def bind_tools(self, specs, messages, **kwargs):
            self.seen.append(list(messages))
            return await super().bind_tools(specs, messages, **kwargs)

    adapter = _Recording(_responses(_call("probe", {})))
    recorder = _Rec()

    result = await _run_child(
        tmp_path,
        hooks=_runner({}),
        agent_hooks=_agent_hook("pre_tool_use", command),
        adapter=adapter,
        tools=[_probe()],
        recorder=recorder,
    )
    assert result.ok, result.error
    assert len(adapter.seen) >= 2
    second = "\n".join(item.content for item in adapter.seen[1])
    assert "外部检查：先读 README" in second
    assert "只作参考" in second
    injected = [payload for event, _node, payload in recorder.events if event == "hook.context_injected"]
    assert injected and injected[0]["chars"] > 0


# --------------------------------------------------------------------------- #
# 2. post_tool_use / post_tool_use_failure / post_tool_batch
# --------------------------------------------------------------------------- #

async def test_post_tool_hook_sees_result_and_failure_hook_sees_error(tmp_path: Path):
    _reset()
    post_command, post_log = _log_hook(tmp_path, "post-ok")
    fail_command, fail_log = _log_hook(tmp_path, "post-fail")

    async def _boom(_context, _arguments):
        return fail("error", "磁盘满了")

    result = await _run_child(
        tmp_path,
        hooks=_runner({}),
        agent_hooks={"post_tool_use": (_spec(post_command),), "post_tool_use_failure": (_spec(fail_command),)},
        adapter=_script(_call("probe", {})),
        tools=[_probe(handler=_boom)],
    )
    assert result.ok, result.error
    # 失败时两个事件都跑：旧的 post_tool_use 兼容保留，失败事件额外一次。
    post_rows = _lines(post_log)
    assert len(post_rows) == 1
    assert post_rows[0]["hook_event_name"] == "post_tool_use"
    assert post_rows[0]["ok"] is False
    assert post_rows[0]["status"] == "error"
    assert post_rows[0]["agent_id"] == "agent-v316"
    fail_rows = _lines(fail_log)
    assert len(fail_rows) == 1
    assert fail_rows[0]["hook_event_name"] == "post_tool_use_failure"
    assert "磁盘满了" in fail_rows[0]["error"]


async def test_post_tool_batch_receives_the_whole_step(tmp_path: Path):
    _reset()
    command, log = _log_hook(tmp_path, "batch")

    result = await _run_child(
        tmp_path,
        hooks=_runner({}),
        agent_hooks=_agent_hook("post_tool_batch", command),
        adapter=_script(_call("probe", {}), _call("second", {})),
        tools=[_probe(), _probe(name="second")],
    )
    assert result.ok, result.error
    rows = _lines(log)
    assert len(rows) == 1
    calls = rows[0]["tool_calls"]
    assert [item["tool_name"] for item in calls] == ["probe", "second"]
    assert [item["ok"] for item in calls] == [True, True]
    assert [item["mode"] for item in calls] == ["execute", "execute"]


# --------------------------------------------------------------------------- #
# 3. 作用域与零改动
# --------------------------------------------------------------------------- #

async def test_parent_settings_hooks_and_definition_hooks_both_run(tmp_path: Path):
    _reset()
    parent_command, parent_log = _log_hook(tmp_path, "parent-pre")
    definition_command, definition_log = _log_hook(tmp_path, "definition-pre")
    parent = _runner({"PreToolUse": [{"command": parent_command, "timeout": 20}]})

    result = await _run_child(
        tmp_path,
        hooks=parent,
        agent_hooks=_agent_hook("pre_tool_use", definition_command),
        adapter=_script(_call("probe", {})),
        tools=[_probe()],
    )
    assert result.ok, result.error
    assert len(_lines(parent_log)) == 1
    assert len(_lines(definition_log)) == 1
    # 定义钩子只属于这个子代理，没有泄漏进父 runner。
    assert [item.command for item in parent.hooks["pre_tool_use"]] == [parent_command]


async def test_no_hooks_means_zero_tool_hook_events(tmp_path: Path):
    _reset()
    recorder = _Rec()
    result = await _run_child(
        tmp_path,
        hooks=_runner({}),
        agent_hooks={},
        adapter=_script(_call("probe", {})),
        tools=[_probe()],
        recorder=recorder,
    )
    assert result.ok, result.error
    assert _EXECUTED == [{}]
    assert not [event for event, _node, _payload in recorder.events if event.startswith("hook.")]


# --------------------------------------------------------------------------- #
# 4. 端到端：运行时派发后定义里的 PreToolUse 守卫真的在子代理里执行
# --------------------------------------------------------------------------- #

async def test_runtime_definition_pre_tool_hook_guards_child(env, workspace: Path, tmp_path: Path):
    _reset()
    command, log = _log_hook(
        tmp_path,
        "e2e-pre",
        body="print(json.dumps({'decision': 'deny', 'reason': '只读演示'}))\n",
    )
    _write(
        workspace / ".blank" / "agents" / "fast.md",
        _agent(
            name="fast",
            description="快速子代理",
            extra=(
                "hooks:\n"
                "  PreToolUse:\n"
                "    - hooks:\n"
                "        - type: command\n"
                f"          command: '{command}'\n"
            ),
        ),
    )
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])
    env.mock.reset(
        [
            {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-spawn",
                        "type": "function",
                        "function": {
                            "name": "spawn_agent",
                            "arguments": json.dumps(
                                {
                                    "goal": "看一眼目录",
                                    "subagent_type": "fast",
                                    "run_in_background": False,
                                }
                            ),
                        },
                    }
                ],
            },
            {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-read",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "main.py"}),
                        },
                    }
                ],
            },
            {"content": "子代理结论"},
            {"content": "完成"},
        ]
    )

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text
    rows = _lines(log)
    assert [row["hook_event_name"] for row in rows] == ["pre_tool_use"]
    assert rows[0]["tool"] == "read_file"
    assert rows[0]["agent_type"] == "fast"
