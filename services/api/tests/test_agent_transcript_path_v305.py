"""#305 批：agent_transcript_path 补齐（对齐上游 SubagentStop / StopFailure 载荷）。

上游语义：``SubagentStop`` 与 ``StopFailure`` 钩子的 stdin 载荷带
``agent_transcript_path``，指向一份 JSONL 格式的对话记录文件，钩子可以读取
它来检查整段对话。本机此前没有 transcript 落盘机制——钩子拿到这个路径也没东西
可读。本批在两个触发点把当前对话消息写成 JSONL 快照，并在载荷里带上路径。

覆盖三件事，每件都钉在"能失败"的判据上：

* **SubagentStop**：``stop_gate`` 在触发钩子前把 ``messages`` 写成
  ``.blank/context/subagent-transcript.jsonl``，载荷带 ``agent_transcript_path``；
  文件内容逐行 ``{"role", "content"}`` JSON，与对话消息一一对应。
* **StopFailure**：``_fire_stop_failure`` 在读取检查点消息时把 ``messages`` 写成
  ``.blank/context/agent-transcript.jsonl``，载荷带 ``agent_transcript_path``；
  文件内容同样逐行 JSON。
* **端到端**：真子进程钩子在两个事件上都拿到路径、能读到文件内容；没配钩子时
  不写文件（StopFailure 的 try 块外无钩子仍惰性）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from blank_api.services.hooks import (
    EVENT_ALIASES,
    HOOK_EVENTS,
    write_transcript,
)

from tests.test_agent_v04 import (  # noqa: F401  (复用阶段3 的夹具)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)
from tests.test_claude_send_message_v30 import ScriptedAdapter
from tests.test_hook_events_v52 import _hooks_env, _lines, _log_hook


# --------------------------------------------------------------------------- #
# 1. write_transcript 单元
# --------------------------------------------------------------------------- #
def test_write_transcript_writes_jsonl(tmp_path: Path):
    """每条消息一行 JSON，带 role 与 content。"""
    messages = [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "读取 main.py"},
        {"role": "assistant", "content": "读完了"},
    ]
    path = tmp_path / "out.jsonl"
    got = write_transcript(messages, path)

    assert got == str(path)
    assert path.exists()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["role"] for row in rows] == ["system", "user", "assistant"]
    assert [row["content"] for row in rows] == ["系统提示", "读取 main.py", "读完了"]


def test_write_transcript_coerces_non_string_content(tmp_path: Path):
    """content 不是字符串时 str() 兜底，不抛异常。"""
    messages = [
        {"role": "assistant", "content": ["工具返回", {"code": 1}]},
        {"role": "user", "content": None},
    ]
    path = tmp_path / "out.jsonl"
    write_transcript(messages, path)

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["content"] == str(["工具返回", {"code": 1}])
    assert rows[1]["content"] == ""


def test_write_transcript_empty_messages(tmp_path: Path):
    """空消息列表写出空文件（不是不写）。"""
    path = tmp_path / "out.jsonl"
    write_transcript([], path)
    assert path.exists()
    assert path.read_text(encoding="utf-8") == ""


def test_write_transcript_creates_parent_dirs(tmp_path: Path):
    """.blank/context/ 不存在时自动创建。"""
    path = tmp_path / ".blank" / "context" / "deep" / "out.jsonl"
    write_transcript([{"role": "user", "content": "x"}], path)
    assert path.exists()


# --------------------------------------------------------------------------- #
# 2. 事件面：两个事件都已注册
# --------------------------------------------------------------------------- #
def test_subagent_stop_and_stop_failure_registered():
    assert "subagent_stop" in HOOK_EVENTS
    assert "stop_failure" in HOOK_EVENTS
    assert EVENT_ALIASES["SubagentStop"] == "subagent_stop"
    assert EVENT_ALIASES["StopFailure"] == "stop_failure"


# --------------------------------------------------------------------------- #
# 3. SubagentStop 端到端：载荷带 agent_transcript_path，文件可读
# --------------------------------------------------------------------------- #
def _hook_runner(specs: dict[str, list[dict]]):
    from blank_api.services.hooks import HookRunner

    runner = HookRunner.from_env(json.dumps(specs), settings=None)
    assert not runner.warnings, runner.warnings
    return runner


async def _run_child_with_stop_hook(tmp_path: Path, hooks, adapter: ScriptedAdapter):
    from blank_api.services.subagent import (
        SubAgentRequest,
        run_subagent,
    )

    class _Rec:
        def __init__(self) -> None:
            self.events: list = []
            self.run_id = ""
            self.origin_run_id = ""

        async def add(self, event: str, node: str, payload: dict) -> None:
            self.events.append((event, node, dict(payload)))

    return await run_subagent(
        adapter=adapter,
        tools=[],
        root=tmp_path,
        task_id="task-v305",
        session_factory=None,
        recorder=_Rec(),
        request=SubAgentRequest(goal="读取 README 并复述", max_steps=4),
        agent_path="root/v305",
        depth=1,
        parent_thread_id="root-thread",
        agent_id="agent-v305",
        hooks=hooks,
    )


async def test_subagent_stop_payload_carries_agent_transcript_path(tmp_path: Path):
    """SubagentStop 钩子 stdin 带 agent_transcript_path，文件内容是 JSONL。"""
    command, log = _log_hook(tmp_path, "st-path")
    hooks = _hook_runner({"subagent_stop": [{"command": command, "timeout": 20}]})
    adapter = ScriptedAdapter(["读完了"])

    result = await _run_child_with_stop_hook(tmp_path, hooks, adapter)
    assert result.ok, result

    rows = _lines(log)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["hook_event_name"] == "subagent_stop"
    assert row["agent_id"] == "agent-v305"
    transcript_path = row["agent_transcript_path"]
    assert transcript_path, "SubagentStop 载荷必须带 agent_transcript_path"

    transcript = Path(transcript_path)
    assert transcript.exists(), f"transcript 文件不存在：{transcript_path}"
    assert transcript.parent.name == "context"
    assert transcript.parent.parent.name == ".blank"
    # 文件在 run_root 下：tmp_path 就是 root。
    assert str(transcript).startswith(str(tmp_path))

    lines = transcript.read_text(encoding="utf-8").splitlines()
    assert lines, "transcript 不能为空"
    msgs = [json.loads(line) for line in lines]
    roles = [m["role"] for m in msgs]
    assert "system" in roles
    assert "user" in roles
    # goal 原文必须出现在 transcript 里。
    contents = [m["content"] for m in msgs]
    assert any("读取 README 并复述" in c for c in contents)
    # assistant 的回复也在。
    assert any("读完了" in c for c in contents)


async def test_subagent_stop_without_hooks_writes_no_transcript(tmp_path: Path):
    """没配钩子时 stop_gate 直接返回，不写 transcript。"""
    adapter = ScriptedAdapter(["读完了"])
    await _run_child_with_stop_hook(tmp_path, hooks=None, adapter=adapter)
    transcript = tmp_path / ".blank" / "context" / "subagent-transcript.jsonl"
    assert not transcript.exists(), "没配钩子时不该写 transcript"


# --------------------------------------------------------------------------- #
# 4. StopFailure 端到端：载荷带 agent_transcript_path，文件可读
# --------------------------------------------------------------------------- #
async def test_stop_failure_payload_carries_agent_transcript_path(
    env, workspace: Path, tmp_path: Path, monkeypatch  # noqa: F811
):
    """StopFailure 钩子 stdin 带 agent_transcript_path，文件内容是 JSONL。"""
    command, log = _log_hook(tmp_path, "sf-path")
    _hooks_env(monkeypatch, stop_failure=command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])
    env.mock.reset([{"content": "执行计划：\n1. 读取 main.py"}])
    env.mock.fail_status = 401

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 502, resp.text

    rows = _lines(log)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["hook_event_name"] == "stop_failure"
    assert row["error"] == "authentication_failed"
    transcript_path = row["agent_transcript_path"]
    assert transcript_path, "StopFailure 载荷必须带 agent_transcript_path"

    transcript = Path(transcript_path)
    assert transcript.exists(), f"transcript 文件不存在：{transcript_path}"
    assert transcript.parent.name == "context"
    assert transcript.parent.parent.name == ".blank"

    lines = transcript.read_text(encoding="utf-8").splitlines()
    assert lines, "transcript 不能为空"
    msgs = [json.loads(line) for line in lines]
    roles = [m["role"] for m in msgs]
    assert "user" in roles
    contents = [m["content"] for m in msgs]
    assert any("读取 main.py" in c for c in contents)


async def test_stop_failure_transcript_reflects_checkpoint_messages(
    env, workspace: Path, tmp_path: Path, monkeypatch  # noqa: F811
):
    """模型先回一句、再挂：transcript 里要有模型回复，不只是任务目标。"""
    command, log = _log_hook(tmp_path, "sf-msgs")
    _hooks_env(monkeypatch, stop_failure=command)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])
    env.mock.reset([{"content": "先看一步", "tool_calls": [call("read_file", {"path": "main.py"})]}])
    env.mock.fail_status = 401
    env.mock.fail_after = 1

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 502, resp.text

    rows = _lines(log)
    assert len(rows) == 1, rows
    transcript_path = rows[0]["agent_transcript_path"]
    assert transcript_path

    transcript = Path(transcript_path)
    msgs = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines()]
    contents = [m["content"] for m in msgs]
    assert any("先看一步" in c for c in contents), "transcript 缺模型回复"
    assert any("读取 main.py" in c for c in contents)


async def test_stop_failure_without_hooks_still_fails(env, workspace: Path):
    """没配钩子：StopFailure 路径惰性，任务照常失败，无事件。"""
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读取 main.py"])
    env.mock.reset([{"content": "执行计划：\n1. 读取 main.py"}])
    env.mock.fail_status = 401

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 502, resp.text

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    assert not [item for item in events if item["type"].startswith("hook.")], events

    task = (await env.client.get(f"{API}/tasks/{task_id}")).json()
    assert task["status"] == "failed"
    assert "401" in task["failure_reason"]
