"""#318 子代理继承 AGENTS.md 指令；``omitClaudeMd`` 可关闭。

上游语义（本批现场取证，偏移见 CHANGELOG）：

* agent schema 的 ``omitClaudeMd``：子代理默认会带上 user / project / local 的
  CLAUDE.md 指令；写 true 时只剩托管策略文件（``6478360``）；
* 文件解析只认字面 true（布尔或字符串 ``"true"``），其余值静默按未声明处理
  （``20542883``）；
* 派发点：``e.omitClaudeMd && !Se?.userContext ? wDs(...) : ...``（``15186251``），
  ``wDs`` 把 ``claudeMd`` 摘掉、只把托管策略内容放回去（``15205840``）；
* 内置 Explore / Plan 都写死 ``omitClaudeMd:!0``（``9239069`` / ``11209824``）。

本地偏离（写进 README）：

* 指令文件是本地那套 ``AGENTS.md`` / ``.blank/AGENTS.md`` / harness / 外部追加文件，
  而不是 CLAUDE.md；本机没有托管策略级指令文件，所以 ``omitClaudeMd`` 生效时
  一个指令层都不注入；
* fork 子代理继承父会话系统提示，父提示里本来就带着指令层，不重复注入。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blank_api.services.agents_defs import load_definition
from blank_api.services.models.base import ModelResponse, Usage
from blank_api.services.subagent import (
    SubAgentRequest,
    SubAgentRunner,
    run_subagent,
)
from blank_api.services.tools.delegate import _parse_tasks

from tests.test_agent_defs_v44 import _agent, _context, _discover, _write
from tests.test_agent_hooks_v316 import _Rec
from tests.test_agent_memory_v317 import _MemoryAdapter
from tests.test_agent_model_v315 import _session
from tests.test_claude_fork_v30 import FakeRunner, _FakeSession

PROJECT_MARKER = "项目约定标记-v318"
USER_MARKER = "操作者偏好标记-v318"
DEFINITION_MARKER = "定义正文标记-v318"


def _definition(tmp_path: Path, name: str = "doc-writer", omit: str = ""):
    extra = f"omitClaudeMd: {omit}\n" if omit else ""
    path = _write(
        tmp_path / "agents" / f"{name}.md",
        _agent(name=name, extra=extra),
    )
    definition, problems = load_definition(path)
    assert not problems, problems
    assert definition is not None
    return definition


def _reply() -> ModelResponse:
    return ModelResponse(text="子代理结论", usage=Usage(2, 1), model="instructions-v318")


async def _run_child(
    root: Path,
    *,
    omit: bool = False,
    instructions: str = "",
    adapter=None,
    recorder: _Rec | None = None,
    agent_name: str = "doc-writer",
):
    return await run_subagent(
        adapter=adapter or _MemoryAdapter([_reply()]),
        tools=[],
        root=root,
        task_id="task-v318",
        session_factory=_FakeSession,
        recorder=recorder or _Rec(),
        request=SubAgentRequest(
            goal="写一段文档",
            max_steps=3,
            agent_name=agent_name,
            instructions=instructions,
            omit_claude_md=omit,
        ),
        agent_path=f"root/{agent_name}",
        depth=1,
        parent_thread_id="root-thread",
        agent_id="agent-v318",
        start_hooks=False,
    )


# --------------------------------------------------------------------------- #
# 1. 解析：只认字面 true，其余静默按未声明
# --------------------------------------------------------------------------- #

def test_omit_parsed_as_bool_true(tmp_path: Path):
    definition = _definition(tmp_path, omit="true")
    assert definition.omit_claude_md is True
    assert "omitClaudeMd" not in definition.ignored
    assert definition.as_dict()["omitClaudeMd"] is True


def test_omit_parsed_as_string_true(tmp_path: Path):
    definition = _definition(tmp_path, omit='"true"')
    assert definition.omit_claude_md is True


@pytest.mark.parametrize("raw", ['"false"', "false", '"yes"', '"1"'])
def test_omit_non_true_is_silently_false(tmp_path: Path, raw: str):
    definition = _definition(tmp_path, name="silent", omit=raw)
    assert definition.omit_claude_md is False
    assert "omitClaudeMd" not in definition.ignored
    assert "omitClaudeMd" not in definition.as_dict()


def test_omit_missing_is_false(tmp_path: Path):
    definition = _definition(tmp_path, name="missing")
    assert definition.omit_claude_md is False


def test_plugin_agent_accepts_omit(tmp_path: Path):
    path = _write(
        tmp_path / "plugin-agents" / "plug.md",
        _agent(name="plug", extra="omitClaudeMd: true\n"),
    )
    definition, problems = load_definition(path, source="plugin:demo")
    assert not problems, problems
    assert definition is not None
    assert definition.omit_claude_md is True
    assert "omitClaudeMd" not in definition.ignored


# --------------------------------------------------------------------------- #
# 2. 派发：默认继承项目/用户 AGENTS.md，omit 时一个都不注入
# --------------------------------------------------------------------------- #

async def test_dispatch_inherits_project_agents_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "project"
    root.mkdir()
    (root / "AGENTS.md").write_text(f"# 约定\n\n{PROJECT_MARKER}\n", encoding="utf-8")
    monkeypatch.setenv("BLANK_USER_INSTRUCTIONS_DIR", str(tmp_path / "no-user"))
    adapter = _MemoryAdapter([_reply()])
    recorder = _Rec()
    result = await _run_child(root, adapter=adapter, recorder=recorder)
    assert result.ok, result.error
    system = adapter.system_texts[0]
    assert PROJECT_MARKER in system
    assert "## 本项目约定（AGENTS.md）" in system
    assert "任何一层都不能放宽安全规则" in system
    assert "你是一个受限的子代理" in system

    events = recorder.named("subagent.instructions")
    assert events, [name for name, _node, _payload in recorder.events]
    assert events[0]["omitted"] is False
    layers = events[0]["layers"]
    assert [item["source"] for item in layers] == ["project"]
    assert layers[0]["path"].startswith("<workspace:")
    assert layers[0]["chars"] == len(f"# 约定\n\n{PROJECT_MARKER}")
    assert str(root) not in json.dumps(events[0], ensure_ascii=False)


async def test_dispatch_inherits_user_agents_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "project"
    root.mkdir()
    user_dir = tmp_path / "user-blank"
    user_dir.mkdir()
    (user_dir / "AGENTS.md").write_text(USER_MARKER + "\n", encoding="utf-8")
    monkeypatch.setenv("BLANK_USER_INSTRUCTIONS_DIR", str(user_dir))
    adapter = _MemoryAdapter([_reply()])
    recorder = _Rec()
    result = await _run_child(root, adapter=adapter, recorder=recorder)
    assert result.ok, result.error
    assert USER_MARKER in adapter.system_texts[0]
    events = recorder.named("subagent.instructions")
    assert [item["source"] for item in events[0]["layers"]] == ["user"]


async def test_omit_true_skips_agents_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "project"
    root.mkdir()
    (root / "AGENTS.md").write_text(PROJECT_MARKER + "\n", encoding="utf-8")
    monkeypatch.setenv("BLANK_USER_INSTRUCTIONS_DIR", str(tmp_path / "no-user"))
    adapter = _MemoryAdapter([_reply()])
    recorder = _Rec()
    result = await _run_child(root, omit=True, adapter=adapter, recorder=recorder)
    assert result.ok, result.error
    system = adapter.system_texts[0]
    assert PROJECT_MARKER not in system
    assert "## 本项目约定（AGENTS.md）" not in system
    assert "你是一个受限的子代理" in system

    events = recorder.named("subagent.instructions")
    assert events, [name for name, _node, _payload in recorder.events]
    assert events[0]["omitted"] is True
    assert events[0]["layers"] == []


async def test_omit_keeps_definition_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "project"
    root.mkdir()
    (root / "AGENTS.md").write_text(PROJECT_MARKER + "\n", encoding="utf-8")
    monkeypatch.setenv("BLANK_USER_INSTRUCTIONS_DIR", str(tmp_path / "no-user"))
    adapter = _MemoryAdapter([_reply()])
    result = await _run_child(
        root, omit=True, adapter=adapter,
        instructions=DEFINITION_MARKER,
    )
    assert result.ok, result.error
    system = adapter.system_texts[0]
    assert DEFINITION_MARKER in system
    assert PROJECT_MARKER not in system


async def test_no_agents_md_no_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("BLANK_USER_INSTRUCTIONS_DIR", str(tmp_path / "no-user"))
    adapter = _MemoryAdapter([_reply()])
    recorder = _Rec()
    result = await _run_child(root, adapter=adapter, recorder=recorder)
    assert result.ok, result.error
    assert recorder.named("subagent.instructions") == []


async def test_fork_does_not_inject_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "project"
    root.mkdir()
    (root / "AGENTS.md").write_text(PROJECT_MARKER + "\n", encoding="utf-8")
    monkeypatch.setenv("BLANK_USER_INSTRUCTIONS_DIR", str(tmp_path / "no-user"))
    adapter = _MemoryAdapter([_reply()])
    session = _session(
        root,
        adapter,
        snapshot=[
            {"role": "system", "content": "父会话系统提示"},
            {"role": "user", "content": "写一段文档"},
        ],
    )
    runner = SubAgentRunner().bind(session)
    result = await runner(SubAgentRequest(goal="写一段文档", fork=True))
    assert result.ok, result.error
    assert PROJECT_MARKER not in adapter.system_texts[0]
    assert "subagent.instructions" not in session.recorder.types()


# --------------------------------------------------------------------------- #
# 3. 携带：批量派发与续聊都保留 omit 标志
# --------------------------------------------------------------------------- #

def test_parse_tasks_carries_omit(tmp_path: Path):
    _write(
        tmp_path / ".blank" / "agents" / "mapper.md",
        _agent(name="mapper", extra="omitClaudeMd: true\n"),
    )
    catalog = _discover(tmp_path)
    requests, error = _parse_tasks(
        [{"goal": "看看", "subagent_type": "mapper"}],
        _context(tmp_path, FakeRunner(), catalog),
    )
    assert not error
    assert requests[0].omit_claude_md is True


async def test_send_message_keeps_omit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "project"
    root.mkdir()
    (root / "AGENTS.md").write_text(PROJECT_MARKER + "\n", encoding="utf-8")
    monkeypatch.setenv("BLANK_USER_INSTRUCTIONS_DIR", str(tmp_path / "no-user"))
    adapter = _MemoryAdapter([_reply(), _reply()])
    session = _session(root, adapter)
    runner = SubAgentRunner().bind(session)
    first = await runner(
        SubAgentRequest(goal="先写", agent_name="doc-writer", omit_claude_md=True)
    )
    assert first.ok, first.error
    second = await runner.send_message(first.agent_id, "接着写")
    assert second.ok, second.error
    assert PROJECT_MARKER not in adapter.system_texts[1]
    # 系统提示续聊时沿用第一轮那条，光看提示词分辨不出来；第二轮若把 omit 丢了，
    # ``run_subagent`` 会照常加载指令层并落一条 omitted=False 的事件——审计就会
    # 声称这个子代理继承了它第一轮明确关掉的项目约定。
    events = [
        payload
        for name, payload in session.recorder.events
        if name == "subagent.instructions"
    ]
    assert len(events) == 2, events
    assert events[1]["omitted"] is True
    assert events[1]["layers"] == []
