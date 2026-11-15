"""#317 子代理定义 frontmatter 的 ``memory``：作用域、目录与持久记忆。

上游语义（本批现场取证，偏移见 CHANGELOG）：

* ``memory`` 只认 ``user`` / ``project`` / ``local``；插件 agent 同样接受它
  （上游 ``Skn`` 的 ``_kn``），非法值只告警、不拒绝整份定义；
* 目录映射：``project`` → ``<项目根>/.claude/agent-memory/<name>``，
  ``local`` → ``<项目根>/.claude/agent-memory-local/<name>``，
  ``user`` → ``<config home>/agent-memory/<name>``；名字先去非法字符
  （``[^a-zA-Z0-9\\-_]`` → ``-``，空串 → ``unknown``）；
* 派发时先建目录，再把 ``# Persistent Agent Memory`` 段挂进子代理系统提示：
  三条 scope 指导语逐字保留，``MEMORY.md`` 有内容就内联、为空就写空态文案；
* 只有显式声明 ``memory`` 的子代理才有这段提示与记忆写入工具；未声明时
  一个目录都不建、提示词与工具表都不变。

本地偏离（写进 README）：不自动补 Read/Write/Edit 工具——子代理工具池不因
定义扩大；改为提供一个只写本作用域 ``MEMORY.md`` 的 ``memory_write`` 工具。
远程记忆目录（``CLAUDE_CODE_REMOTE_MEMORY_DIR``）与 primed entrypoint 未移植。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blank_api.services.agent_memory import (
    MAX_AGENT_MEMORY_CHARS,
    agent_memory_dir,
    append_memory,
    agent_memory_tools,
    render_memory_section,
    sanitize_agent_name,
)
from blank_api.services.agents_defs import load_definition
from blank_api.services.models.base import (
    AdapterConfig,
    ModelAdapter,
    ModelResponse,
    Usage,
)
from blank_api.services.subagent import SubAgentRequest, SubAgentRunner, run_subagent

from blank_api.services.tools.delegate import _parse_tasks

from tests.test_agent_defs_v44 import _agent, _context, _discover, _write
from tests.test_agent_hooks_v316 import _Rec
from tests.test_agent_model_v315 import _session
from tests.test_claude_fork_v30 import FakeRunner, _FakeSession

PROJECT_GUIDELINE = (
    "- Since this memory is project-scope and shared with your team via version "
    "control, tailor your memories to this project"
)
USER_GUIDELINE = (
    "- Since this memory is user-scope, keep learnings general since they apply "
    "across all projects"
)
LOCAL_GUIDELINE = (
    "- Since this memory is local-scope (not checked into version control), "
    "tailor your memories to this project and machine"
)


def _definition(tmp_path: Path, name: str = "memory-keeper", memory: str = "project"):
    path = _write(
        tmp_path / "agents" / f"{name}.md",
        _agent(name=name, extra=f"memory: {memory}\n"),
    )
    definition, problems = load_definition(path)
    assert not problems, problems
    assert definition is not None
    return definition


class _MemoryAdapter(ModelAdapter):
    """脚本化模型：先调一个工具，再给结论；同时记下它看到的工具名。"""

    def __init__(self, script: list[ModelResponse]) -> None:
        super().__init__(
            AdapterConfig(
                name="memory-v317",
                kind="openai_compatible",
                base_url="",
                api_key="",
                model="memory-v317",
                supports_tools=True,
            )
        )
        self.script = script
        self.calls = 0
        self.tool_names: list[list[str]] = []
        self.system_texts: list[str] = []

    async def invoke(self, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def stream(self, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def bind_tools(self, specs, messages, **kwargs):
        self.tool_names.append([getattr(spec, "name", "") for spec in specs])
        system = ""
        for item in messages:
            role = item.get("role") if isinstance(item, dict) else getattr(item, "role", "")
            if str(role) == "system":
                system = (
                    str(item.get("content") or "")
                    if isinstance(item, dict)
                    else str(getattr(item, "content", "") or "")
                )
                break
        self.system_texts.append(system)
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return item


def _tool_call(name: str, arguments: dict) -> ModelResponse:
    return ModelResponse(
        text="",
        usage=Usage(10, 5),
        model="memory-v317",
        tool_calls=[
            {
                "id": f"call-{name}",
                "type": "function",
                "function": {"name": name, "arguments": __import__("json").dumps(arguments)},
            }
        ],
    )


async def _run_child(
    root: Path,
    *,
    memory_scope: str = "",
    agent_name: str = "memory-keeper",
    adapter=None,
    recorder: _Rec | None = None,
    isolate: bool = False,
    disallowed_tools: tuple[str, ...] = (),
):
    return await run_subagent(
        adapter=adapter or _MemoryAdapter([ModelResponse(text="子代理结论", usage=Usage(2, 1), model="memory-v317")]),
        tools=[],
        root=root,
        task_id="task-v317",
        session_factory=_FakeSession,
        recorder=recorder or _Rec(),
        request=SubAgentRequest(
            goal="核对 README",
            max_steps=3,
            agent_name=agent_name,
            memory_scope=memory_scope,
            isolate=isolate,
            disallowed_tools=disallowed_tools,
        ),
        agent_path=f"root/{agent_name}",
        depth=1,
        parent_thread_id="root-thread",
        agent_id="agent-v317",
        start_hooks=False,
    )


# --------------------------------------------------------------------------- #
# 1. 解析：合法值进定义，非法值告警丢弃，插件 agent 同样接受
# --------------------------------------------------------------------------- #

def test_memory_scope_parsed_and_visible_in_audit(tmp_path: Path):
    definition = _definition(tmp_path, memory="project")
    assert definition.memory == "project"
    assert "memory" not in definition.ignored
    assert definition.as_dict()["memory"] == "project"


@pytest.mark.parametrize("scope", ["user", "project", "local"])
def test_all_three_scopes_accepted(tmp_path: Path, scope: str):
    definition = _definition(tmp_path, name=f"keeper-{scope}", memory=scope)
    assert definition.memory == scope
    assert "memory" not in definition.ignored


def test_invalid_scope_warns_and_drops_without_rejecting(tmp_path: Path):
    path = _write(
        tmp_path / "agents" / "bad-scope.md",
        _agent(name="bad-scope", extra="memory: global\n"),
    )
    definition, warnings = load_definition(path)
    assert definition is not None
    assert len(warnings) == 1 and "memory" in warnings[0], warnings
    assert definition.memory == ""
    assert "memory" in definition.ignored
    assert "memory" not in definition.as_dict()


def test_non_string_scope_warns_and_drops(tmp_path: Path):
    path = _write(
        tmp_path / "agents" / "bool-scope.md",
        _agent(name="bool-scope", extra="memory: true\n"),
    )
    definition, warnings = load_definition(path)
    assert definition is not None
    assert len(warnings) == 1 and "memory" in warnings[0], warnings
    assert definition.memory == ""
    assert "memory" in definition.ignored


def test_plugin_agent_accepts_memory_scope(tmp_path: Path):
    path = _write(
        tmp_path / "plugin-agents" / "plug.md",
        _agent(name="plug", extra="memory: local\n"),
    )
    definition, problems = load_definition(path, source="plugin:demo")
    assert not problems, problems
    assert definition is not None
    assert definition.memory == "local"
    assert "memory" not in definition.ignored


def test_plugin_agent_invalid_scope_warns(tmp_path: Path):
    path = _write(
        tmp_path / "plugin-agents" / "plug-bad.md",
        _agent(name="plug-bad", extra="memory: machine\n"),
    )
    definition, warnings = load_definition(path, source="plugin:demo")
    assert definition is not None
    assert len(warnings) == 1 and "memory" in warnings[0], warnings
    assert definition.memory == ""
    assert "memory" in definition.ignored


# --------------------------------------------------------------------------- #
# 2. 目录映射与名字消毒
# --------------------------------------------------------------------------- #

def test_sanitize_agent_name_matches_upstream():
    assert sanitize_agent_name("code review:sec/ops") == "code-review-sec-ops"
    assert sanitize_agent_name("a_b-c") == "a_b-c"
    assert sanitize_agent_name("中文名") == "---"
    assert sanitize_agent_name("") == "unknown"


def test_scope_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "project"
    root.mkdir()
    user_root = tmp_path / "user-blank"
    monkeypatch.setenv("BLANK_USER_AGENT_MEMORY_DIR", str(user_root / "agent-memory"))
    assert agent_memory_dir("project", "keeper", root) == (
        root / ".blank" / "agent-memory" / "keeper"
    )
    assert agent_memory_dir("local", "keeper", root) == (
        root / ".blank" / "agent-memory-local" / "keeper"
    )
    assert agent_memory_dir("user", "keeper", root) == (
        user_root / "agent-memory" / "keeper"
    )


def test_scope_directories_sanitize_name(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    assert agent_memory_dir("project", "code review:sec", root) == (
        root / ".blank" / "agent-memory" / "code-review-sec"
    )


# --------------------------------------------------------------------------- #
# 3. 提示词段：三条 scope 指导语逐字、MEMORY.md 内联与空态
# --------------------------------------------------------------------------- #

def test_render_section_scope_guidelines_are_verbatim(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    expected = {
        "project": PROJECT_GUIDELINE,
        "user": USER_GUIDELINE,
        "local": LOCAL_GUIDELINE,
    }
    for scope, guideline in expected.items():
        directory = agent_memory_dir(scope, "keeper", root)
        text = render_memory_section(scope=scope, agent_name="keeper", directory=directory)
        assert "# Persistent Agent Memory" in text
        assert str(directory) in text
        assert guideline in text
        assert "## MEMORY.md" in text
        assert "## Types of memory" in text
        assert "## When to access memories" in text


def test_render_section_empty_state(tmp_path: Path):
    directory = tmp_path / "mem"
    directory.mkdir()
    text = render_memory_section(scope="project", agent_name="keeper", directory=directory)
    assert (
        "Your MEMORY.md is currently empty. When you save new memories, "
        "they will appear here."
    ) in text


def test_render_section_inlines_existing_memory(tmp_path: Path):
    directory = tmp_path / "mem"
    directory.mkdir()
    (directory / "MEMORY.md").write_text(
        "- [project] 发布窗口在周五下午\n", encoding="utf-8"
    )
    text = render_memory_section(scope="project", agent_name="keeper", directory=directory)
    assert "- [project] 发布窗口在周五下午" in text
    assert "currently empty" not in text


def test_render_section_caps_entrypoint(tmp_path: Path):
    directory = tmp_path / "mem"
    directory.mkdir()
    old = "- [project] OLDEST-ENTRY\n"
    middle = "".join(
        f"- [project] 填充{index} " + "长" * 350 + "\n" for index in range(40)
    )
    new = "- [project] NEWEST-ENTRY\n"
    (directory / "MEMORY.md").write_text(old + middle + new, encoding="utf-8")
    text = render_memory_section(scope="project", agent_name="keeper", directory=directory)
    assert "（较早的记忆已截断）" in text
    assert "NEWEST-ENTRY" in text
    assert "OLDEST-ENTRY" not in text
    assert len(text) < MAX_AGENT_MEMORY_CHARS + 4_000


def test_append_creates_missing_directory(tmp_path: Path):
    target = tmp_path / "a" / "b"
    result = append_memory(target, memory_type="user", content="偏好：先给结论")
    assert result.added == 1
    assert (target / "MEMORY.md").is_file()


def test_append_trims_to_cap(tmp_path: Path):
    directory = tmp_path / "mem"
    directory.mkdir()
    for index in range(40):
        append_memory(
            directory, memory_type="project", content=f"第{index}条 " + "细" * 400
        )
    body = (directory / "MEMORY.md").read_text(encoding="utf-8")
    assert "（较早的记忆已截断）" in body
    assert "第39条" in body
    assert "第0条" not in body
    assert len(body) <= MAX_AGENT_MEMORY_CHARS + 200


# --------------------------------------------------------------------------- #
# 4. 派发接线：建目录、注入、给工具、写得进去、下一轮读得到
# --------------------------------------------------------------------------- #

async def test_dispatch_creates_dir_and_injects_section(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    adapter = _MemoryAdapter(
        [ModelResponse(text="子代理结论", usage=Usage(2, 1), model="memory-v317")]
    )
    recorder = _Rec()
    result = await _run_child(root, memory_scope="project", adapter=adapter, recorder=recorder)
    assert result.ok, result.error
    directory = root / ".blank" / "agent-memory" / "memory-keeper"
    assert directory.is_dir()
    system = adapter.system_texts[0]
    assert "# Persistent Agent Memory" in system
    assert str(directory) in system
    assert PROJECT_GUIDELINE in system
    assert "## MEMORY.md" in system
    assert "currently empty" in system
    assert "memory_write" in adapter.tool_names[0]
    events = recorder.named("subagent.memory_enabled")
    assert events, [name for name, _node, _payload in recorder.events]
    assert events[0]["scope"] == "project"
    assert events[0]["chars"] > 0
    assert events[0]["path"].startswith("<workspace:")
    assert str(root) not in events[0]["path"]


def adapter_stub_text(adapter: _MemoryAdapter) -> str:
    """取第一次模型调用看到的系统提示（测试辅助，不参与产品逻辑）。"""
    return adapter.system_texts[0]


async def test_dispatch_inlines_existing_memory_md(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    directory = root / ".blank" / "agent-memory" / "memory-keeper"
    directory.mkdir(parents=True)
    (directory / "MEMORY.md").write_text(
        "- [project] 这个仓库的发布分支叫 release/next\n", encoding="utf-8"
    )
    adapter = _MemoryAdapter(
        [ModelResponse(text="子代理结论", usage=Usage(2, 1), model="memory-v317")]
    )
    result = await _run_child(root, memory_scope="project", adapter=adapter)
    assert result.ok, result.error
    system = adapter_stub_text(adapter)
    assert "这个仓库的发布分支叫 release/next" in system
    assert "currently empty" not in system


async def test_memory_write_tool_persists_and_next_dispatch_reads_it(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    first = _MemoryAdapter(
        [
            _tool_call(
                "memory_write",
                {"type": "project", "content": "回滚脚本在 scripts/rollback.ps1"},
            ),
            ModelResponse(text="子代理结论", usage=Usage(2, 1), model="memory-v317"),
        ]
    )
    result = await _run_child(root, memory_scope="project", adapter=first)
    assert result.ok, result.error
    entrypoint = root / ".blank" / "agent-memory" / "memory-keeper" / "MEMORY.md"
    assert entrypoint.is_file()
    body = entrypoint.read_text(encoding="utf-8")
    assert "- [project] 回滚脚本在 scripts/rollback.ps1" in body

    second = _MemoryAdapter(
        [ModelResponse(text="子代理结论", usage=Usage(2, 1), model="memory-v317")]
    )
    result = await _run_child(root, memory_scope="project", adapter=second)
    assert result.ok, result.error
    assert "回滚脚本在 scripts/rollback.ps1" in adapter_stub_text(second)


async def test_memory_write_dedupes_exact_repeat(tmp_path: Path):
    directory = tmp_path / "mem"
    directory.mkdir()
    first = append_memory(directory, memory_type="project", content="周五冻结合并")
    second = append_memory(directory, memory_type="project", content="  周五冻结合并  ")
    assert first.added == 1
    assert second.added == 0
    body = (directory / "MEMORY.md").read_text(encoding="utf-8")
    assert body.count("周五冻结合并") == 1


async def test_memory_write_tool_rejects_bad_type(tmp_path: Path):
    directory = tmp_path / "mem"
    directory.mkdir()
    tools = {item.name: item for item in agent_memory_tools(directory)}
    outcome = await tools["memory_write"].handler(None, {"type": "global", "content": "x"})
    assert not outcome.ok
    assert (directory / "MEMORY.md").exists() is False


async def test_no_memory_declaration_has_no_side_effects(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    adapter = _MemoryAdapter(
        [ModelResponse(text="子代理结论", usage=Usage(2, 1), model="memory-v317")]
    )
    result = await _run_child(root, memory_scope="", adapter=adapter)
    assert result.ok, result.error
    assert not (root / ".blank" / "agent-memory").exists()
    assert "# Persistent Agent Memory" not in adapter_stub_text(adapter)
    assert "memory_write" not in adapter.tool_names[0]


async def test_user_scope_dispatch_uses_override_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "project"
    root.mkdir()
    user_root = tmp_path / "user-blank" / "agent-memory"
    monkeypatch.setenv("BLANK_USER_AGENT_MEMORY_DIR", str(user_root))
    adapter = _MemoryAdapter(
        [ModelResponse(text="子代理结论", usage=Usage(2, 1), model="memory-v317")]
    )
    result = await _run_child(root, memory_scope="user", adapter=adapter)
    assert result.ok, result.error
    directory = user_root / "memory-keeper"
    assert directory.is_dir()
    assert str(directory) in adapter_stub_text(adapter)
    assert USER_GUIDELINE in adapter_stub_text(adapter)
    assert not (root / ".blank" / "agent-memory").exists()


async def test_disallowed_memory_write_keeps_section_but_not_tool(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    adapter = _MemoryAdapter(
        [ModelResponse(text="子代理结论", usage=Usage(2, 1), model="memory-v317")]
    )
    result = await _run_child(
        root,
        memory_scope="project",
        adapter=adapter,
        disallowed_tools=("memory_write",),
    )
    assert result.ok, result.error
    assert "# Persistent Agent Memory" in adapter.system_texts[0]
    assert "memory_write" not in adapter.tool_names[0]


async def test_fork_ignores_memory_scope(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    adapter = _MemoryAdapter(
        [ModelResponse(text="子代理结论", usage=Usage(2, 1), model="memory-v317")]
    )
    snapshot = [
        {"role": "system", "content": "父会话系统提示"},
        {"role": "user", "content": "修一下"},
    ]
    runner = SubAgentRunner().bind(_session(root, adapter, snapshot=snapshot))
    result = await runner(SubAgentRequest(goal="修一下", fork=True, memory_scope="project"))
    assert result.ok, result.error
    assert not (root / ".blank" / "agent-memory").exists()
    assert "# Persistent Agent Memory" not in adapter.system_texts[0]
    assert "memory_write" not in adapter.tool_names[0]


async def test_send_message_keeps_memory_scope(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    adapter = _MemoryAdapter(
        [
            ModelResponse(text="子代理结论", usage=Usage(2, 1), model="memory-v317"),
            ModelResponse(text="接着答", usage=Usage(2, 1), model="memory-v317"),
        ]
    )
    runner = SubAgentRunner().bind(_session(root, adapter))
    first = await runner(
        SubAgentRequest(goal="看看", agent_name="memory-keeper", memory_scope="project")
    )
    assert first.ok, first.error
    second = await runner.send_message(first.agent_id, "接着说")
    assert second.ok, second.error
    assert "# Persistent Agent Memory" in adapter.system_texts[1]
    assert "memory_write" in adapter.tool_names[1]


def test_parse_tasks_carries_memory_scope(tmp_path: Path):
    _write(
        tmp_path / ".blank" / "agents" / "mapper.md",
        _agent(name="mapper", extra="memory: local\n"),
    )
    catalog = _discover(tmp_path)
    requests, error = _parse_tasks(
        [{"goal": "看看", "subagent_type": "mapper"}],
        _context(tmp_path, FakeRunner(), catalog),
    )
    assert not error
    assert requests[0].memory_scope == "local"
