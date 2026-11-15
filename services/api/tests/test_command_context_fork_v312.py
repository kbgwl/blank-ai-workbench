"""#312：``context: fork`` 是子代理派发的闸门（命令侧 + Skill 侧）。

上游语义（官方文档 + ``vQt``）：``context`` 只认 ``fork``；只有声明了
``context: fork`` 的命令/Skill 才把正文派给子代理，``agent`` 只是"fork 时用
哪个子代理类型"，``background`` 只是"fork 时前台等还是后台跑"。

#311 把闸门写成了 ``agent or background``（声明任一就派发）。这是语义缺口：
没写 ``context: fork``、只写了 ``agent: reviewer`` 的命令会被误派；写了
``context: fork``、没写 ``agent`` 的命令反而不派。本批把闸门改成
``context == "fork"``，并让 ``agent`` / ``background`` 变成 fork 下的子选项。

覆盖三段：
1. **声明携带**：``context`` 字段、frontmatter 解析（只认 fork）、``as_entry``、
   ``expand_user_input`` 的 info；
2. **不支持清单**：命令侧与 Skill 侧的 ``context`` 都不再是"不支持"字段；
3. **运行时闸门**：fork + agent → 前台派发；fork + background → 后台派发；
   fork 无 agent → 默认 general-purpose；agent/background 无 fork → 不派发。

钉的不是「字段存在」——每条判据指向一个会走形的具体行为：解析丢字段、
只认大小写、目录不携带、info 不带、无 fork 误派、有 fork 不派。
改前这些用例要么 AttributeError/KeyError、要么断言失败。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.db import get_session_factory
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.agents_defs import AgentCatalog, AgentDefinition
from blank_api.services.commands_defs import (
    COMMAND_UNSUPPORTED_FIELDS,
    CommandDefinition,
    discover_commands,
    expand_user_input,
)
from blank_api.services.models.registry import build_adapter
from blank_api.services.skills_defs import (
    SKILL_UNSUPPORTED_FIELDS,
    SkillDefinition,
    _definition_from_dir,
)
from blank_api.services.subagent import SubAgentResult
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.skills import SkillCatalog, skill_tools

from tests.test_agent_v04 import (  # noqa: F401
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)
from tests.test_background_subagent_v116 import FakeRecorder


# --------------------------------------------------------------------------- #
# 测试装置
# --------------------------------------------------------------------------- #


class _FakeRunner:
    """记录收到的请求，返回预设结果。前台 ``__call__`` + 后台 ``spawn_background``。"""

    instances: list = []

    def __init__(self):
        self.calls: list = []
        self.foreground_result = SubAgentResult(ok=True, summary="子代理完成", steps=2)
        self.background_agent_id = "bg-agent-1"
        self.background_error: str = ""
        _FakeRunner.instances.append(self)

    def bind(self, session):
        pass

    def drain_notifications(self):
        return []

    def list_agents(self):
        return []

    async def __call__(self, request, *, agent_id: str = "") -> SubAgentResult:
        self.calls.append(("foreground", request))
        return self.foreground_result

    async def spawn_background(self, request):
        self.calls.append(("background", request))
        if self.background_error:
            return "", SubAgentResult(ok=False, error=self.background_error)
        return self.background_agent_id, None

    async def aclose(self, task_id: str = "", *, reason: str = "") -> int:
        return 0


def _reviewer_catalog() -> AgentCatalog:
    """含 reviewer 与默认 general-purpose 的 AgentCatalog，供 _resolve_agent 解析。"""
    return AgentCatalog(
        entries={
            "general-purpose": AgentDefinition(
                name="general-purpose",
                description="默认子代理",
                instructions="你是通用子代理。",
            ),
            "reviewer": AgentDefinition(
                name="reviewer",
                description="评审子代理",
                instructions="你是评审员。",
            ),
        }
    )


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_command(root: Path, relative: str, text: str) -> Path:
    return _write(root / ".blank" / "commands" / relative, text)


def _write_agent(root: Path, relative: str, text: str) -> Path:
    return _write(root / ".blank" / "agents" / relative, text)


def _write_skill(root: Path, name: str, text: str) -> Path:
    skill_dir = root / ".blank" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    _write(skill_dir / "SKILL.md", text)
    return skill_dir


def _agent_md(body: str = "你是评审员。") -> str:
    return f"---\nname: reviewer\ndescription: 评审子代理\n---\n{body}"


def _discover(root: Path, user_dir: Path | None = None):
    env_map = {"BLANK_USER_COMMANDS_DIR": str(user_dir or (root.parent / "no-user-commands"))}
    return discover_commands(root, env=env_map)


def _expand(root: Path, text: str, **kwargs):
    env_map = {"BLANK_USER_COMMANDS_DIR": str(root.parent / "no-user-commands")}
    return expand_user_input(text, root=root, env=env_map, **kwargs)


def _command_md(body: str = "按命令正文执行。", extra: str = "") -> str:
    return f"---\ndescription: 命令说明\n{extra}---\n{body}"


async def _provider_row(provider_id: str):
    from blank_api.models.model_provider import ModelProvider

    async with get_session_factory()() as session:
        return await session.get(ModelProvider, provider_id)


def _make_runtime_context(catalog: AgentCatalog | None = None) -> SimpleNamespace:
    """构造一个带 agents 目录的 ToolContext，供 _resolve_agent 使用。"""
    return SimpleNamespace(
        agents=catalog,
        root=Path("."),
        task_id="task-v312",
        recorder=FakeRecorder(),
    )


def _tool_context(
    catalog: SkillCatalog,
    *,
    runner: _FakeRunner | None = None,
    agents: AgentCatalog | None = None,
) -> ToolContext:
    return ToolContext(
        root=Path("."),
        task_id="task-v312",
        session_factory=None,
        skills=catalog,
        subagent_runner=runner,
        agents=agents,
        turn_state={},
    )


def _skill_tool():
    return {tool.name: tool for tool in skill_tools()}["skill"]


# --------------------------------------------------------------------------- #
# 1. CommandDefinition 携带 context
# --------------------------------------------------------------------------- #


def test_command_definition_default_context_empty() -> None:
    """默认值：context 空串——不声明就是不派发。"""
    definition = CommandDefinition(name="x", description="d", instructions="body")
    assert definition.context == ""


def test_command_definition_carries_context() -> None:
    """显式设置 context=fork 时原样携带。"""
    definition = CommandDefinition(
        name="x", description="d", instructions="body", context="fork"
    )
    assert definition.context == "fork"


# --------------------------------------------------------------------------- #
# 2. frontmatter 解析：只认 fork
# --------------------------------------------------------------------------- #


def test_parse_context_fork(tmp_path: Path) -> None:
    """frontmatter 里的 context: fork 被解析进 CommandDefinition。"""
    _write_command(
        tmp_path,
        "review.md",
        _command_md("评审。", extra="context: fork\nagent: reviewer\n"),
    )
    definition = _discover(tmp_path).entries["review"]
    assert definition.context == "fork"


def test_parse_context_other_value_warns_and_ignores(tmp_path: Path) -> None:
    """context 只认 fork：写别的值告警丢弃，不猜值。"""
    _write_command(
        tmp_path,
        "bad.md",
        _command_md("执行。", extra="context: remote\n"),
    )
    definition = _discover(tmp_path).entries["bad"]
    assert definition.context == ""
    assert any("context" in w and "fork" in w for w in definition.warnings)


def test_parse_context_null_leaves_empty(tmp_path: Path) -> None:
    """context: null 等同不声明——留空、不告警。"""
    _write_command(
        tmp_path,
        "null-ctx.md",
        _command_md("执行。", extra="context:\n"),
    )
    definition = _discover(tmp_path).entries["null-ctx"]
    assert definition.context == ""
    assert not any("context" in w for w in definition.warnings)


def test_parse_context_non_string_warns_and_ignores(tmp_path: Path) -> None:
    """context 写成非字符串非 null：告警丢弃，不猜值。"""
    _write_command(
        tmp_path,
        "num-ctx.md",
        _command_md("执行。", extra="context: 123\n"),
    )
    definition = _discover(tmp_path).entries["num-ctx"]
    assert definition.context == ""
    assert any("context" in w for w in definition.warnings)


def test_parse_context_case_sensitive(tmp_path: Path) -> None:
    """context: Fork（大写）不被接受——上游只认小写 fork。"""
    _write_command(
        tmp_path,
        "upper.md",
        _command_md("执行。", extra="context: Fork\n"),
    )
    definition = _discover(tmp_path).entries["upper"]
    assert definition.context == ""
    assert any("context" in w for w in definition.warnings)


def test_parse_context_whitespace_stripped(tmp_path: Path) -> None:
    """context: "  fork  " 首尾空白被去掉。"""
    _write_command(
        tmp_path,
        "padded.md",
        _command_md("执行。", extra='context: "  fork  "\n'),
    )
    definition = _discover(tmp_path).entries["padded"]
    assert definition.context == "fork"


# --------------------------------------------------------------------------- #
# 3. as_entry / expand info 携带 context
# --------------------------------------------------------------------------- #


def test_as_entry_carries_context() -> None:
    """as_entry 的返回 dict 里带 context——目录消费方看得见。"""
    definition = CommandDefinition(
        name="x", description="d", instructions="body", context="fork"
    )
    entry = definition.as_entry()
    assert entry["context"] == "fork"


def test_as_entry_default_context() -> None:
    """没声明时 as_entry 里 context 空串。"""
    definition = CommandDefinition(name="x", description="d", instructions="body")
    entry = definition.as_entry()
    assert entry["context"] == ""


def test_expand_carries_context(tmp_path: Path) -> None:
    """展开 /name 时，info 里带 context——运行时从 expansion 读闸门。"""
    _write_command(
        tmp_path,
        "review.md",
        _command_md("评审。", extra="context: fork\nagent: reviewer\n"),
    )
    _, info = _expand(tmp_path, "/review")
    assert info is not None
    assert info["context"] == "fork"


def test_expand_without_context_carries_empty(tmp_path: Path) -> None:
    """没声明 context 时，info 里带空串。"""
    _write_command(tmp_path, "plain.md", _command_md("执行。"))
    _, info = _expand(tmp_path, "/plain")
    assert info is not None
    assert info["context"] == ""


# --------------------------------------------------------------------------- #
# 4. 不支持清单排除 context（命令侧 + Skill 侧）
# --------------------------------------------------------------------------- #


def test_command_unsupported_fields_excludes_context() -> None:
    """命令侧已支持 context——它不再出现在"不支持"清单里。"""
    assert "context" not in COMMAND_UNSUPPORTED_FIELDS


def test_skill_unsupported_fields_excludes_context() -> None:
    """Skill 侧已支持 context——它不再出现在"不支持"清单里。"""
    assert "context" not in SKILL_UNSUPPORTED_FIELDS


def test_command_ignored_does_not_flag_context(tmp_path: Path) -> None:
    """声明了 context: fork 的命令，ignored 里不含 context。"""
    _write_command(
        tmp_path,
        "ok.md",
        _command_md("执行。", extra="context: fork\n"),
    )
    definition = _discover(tmp_path).entries["ok"]
    assert "context" not in definition.ignored


# --------------------------------------------------------------------------- #
# 5. 运行时闸门：fork + agent → 前台派发
# --------------------------------------------------------------------------- #


async def test_runtime_dispatches_foreground_when_fork_and_agent(
    env, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """context: fork + agent: reviewer（无 background）→ 前台派发 reviewer 子代理。"""
    monkeypatch.setenv("BLANK_USER_COMMANDS_DIR", str(tmp_path / "empty-user-commands"))
    _write_agent(workspace, "reviewer.md", _agent_md())
    _write_command(
        workspace,
        "review.md",
        _command_md("评审这份代码。", extra="context: fork\nagent: reviewer\n"),
    )
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["评审完成"])
    expanded_goal, command_info = expand_user_input(
        "/review",
        root=workspace,
        session_id=task_id,
        env={"BLANK_USER_COMMANDS_DIR": str(tmp_path / "empty-user-commands")},
    )
    assert command_info is not None
    assert command_info["context"] == "fork"
    env.mock.reset([])
    _FakeRunner.instances.clear()
    monkeypatch.setattr(
        "blank_api.services.agent.runtime.SubAgentRunner", _FakeRunner
    )

    evidence = await AgentRuntime(get_session_factory()).run(
        RunRequest(
            task_id=task_id,
            provider_id=provider_id,
            provider_name="mock-vendor",
            adapter=build_adapter(await _provider_row(provider_id), env.mock.factory),
            root=workspace,
            goal=expanded_goal,
            acceptance_criteria=["评审完成"],
            max_steps=3,
            expansion=command_info,
        )
    )

    assert evidence["task"]["status"] == "succeeded", evidence["task"]
    runner = _FakeRunner.instances[-1]
    assert len(runner.calls) == 1
    kind, request = runner.calls[0]
    assert kind == "foreground"
    assert request.goal == expanded_goal
    assert request.agent_name == "reviewer"
    assert env.mock.requests == []


# --------------------------------------------------------------------------- #
# 6. 运行时闸门：fork + background → 后台派发
# --------------------------------------------------------------------------- #


async def test_runtime_dispatches_background_when_fork_and_background(
    env, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """context: fork + background: true → 后台派发子代理。"""
    monkeypatch.setenv("BLANK_USER_COMMANDS_DIR", str(tmp_path / "empty-user-commands"))
    _write_command(
        workspace,
        "bg.md",
        _command_md("后台执行。", extra="context: fork\nbackground: true\n"),
    )
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["后台完成"])
    expanded_goal, command_info = expand_user_input(
        "/bg",
        root=workspace,
        session_id=task_id,
        env={"BLANK_USER_COMMANDS_DIR": str(tmp_path / "empty-user-commands")},
    )
    assert command_info is not None
    env.mock.reset([])
    _FakeRunner.instances.clear()
    monkeypatch.setattr(
        "blank_api.services.agent.runtime.SubAgentRunner", _FakeRunner
    )

    evidence = await AgentRuntime(get_session_factory()).run(
        RunRequest(
            task_id=task_id,
            provider_id=provider_id,
            provider_name="mock-vendor",
            adapter=build_adapter(await _provider_row(provider_id), env.mock.factory),
            root=workspace,
            goal=expanded_goal,
            acceptance_criteria=["后台完成"],
            max_steps=3,
            expansion=command_info,
        )
    )

    assert evidence["task"]["status"] == "succeeded", evidence["task"]
    runner = _FakeRunner.instances[-1]
    assert len(runner.calls) == 1
    kind, request = runner.calls[0]
    assert kind == "background"
    assert request.goal == expanded_goal
    assert env.mock.requests == []


# --------------------------------------------------------------------------- #
# 7. 运行时闸门：fork 无 agent → 默认 general-purpose 前台派发
# --------------------------------------------------------------------------- #


async def test_runtime_fork_without_agent_uses_default(
    env, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """context: fork 但没声明 agent → 前台派发默认 general-purpose 子代理。"""
    monkeypatch.setenv("BLANK_USER_COMMANDS_DIR", str(tmp_path / "empty-user-commands"))
    _write_command(
        workspace,
        "fork-only.md",
        _command_md("执行。", extra="context: fork\n"),
    )
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["执行完成"])
    expanded_goal, command_info = expand_user_input(
        "/fork-only",
        root=workspace,
        session_id=task_id,
        env={"BLANK_USER_COMMANDS_DIR": str(tmp_path / "empty-user-commands")},
    )
    assert command_info is not None
    env.mock.reset([])
    _FakeRunner.instances.clear()
    monkeypatch.setattr(
        "blank_api.services.agent.runtime.SubAgentRunner", _FakeRunner
    )

    evidence = await AgentRuntime(get_session_factory()).run(
        RunRequest(
            task_id=task_id,
            provider_id=provider_id,
            provider_name="mock-vendor",
            adapter=build_adapter(await _provider_row(provider_id), env.mock.factory),
            root=workspace,
            goal=expanded_goal,
            acceptance_criteria=["执行完成"],
            max_steps=3,
            expansion=command_info,
        )
    )

    assert evidence["task"]["status"] == "succeeded", evidence["task"]
    runner = _FakeRunner.instances[-1]
    assert len(runner.calls) == 1
    kind, request = runner.calls[0]
    assert kind == "foreground"
    assert request.agent_name == "general-purpose"
    assert env.mock.requests == []


# --------------------------------------------------------------------------- #
# 8. 运行时闸门：agent 无 fork → 不派发，走常规路径
# --------------------------------------------------------------------------- #


async def test_runtime_no_dispatch_with_agent_but_no_fork(
    env, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """agent: reviewer 但没写 context: fork → 不派发，走主代理循环。

    #311 的闸门是 ``agent or background``——只写 agent 就会误派。本批修复：没有
    context: fork 时，agent 字段只是"惰性声明"，不触发派发。
    """
    monkeypatch.setenv("BLANK_USER_COMMANDS_DIR", str(tmp_path / "empty-user-commands"))
    _write_command(
        workspace,
        "lazy-agent.md",
        _command_md("执行。", extra="agent: reviewer\n"),
    )
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["执行完成"])
    expanded_goal, command_info = expand_user_input(
        "/lazy-agent",
        root=workspace,
        session_id=task_id,
        env={"BLANK_USER_COMMANDS_DIR": str(tmp_path / "empty-user-commands")},
    )
    assert command_info is not None
    assert command_info["context"] == ""
    env.mock.reset([{"content": "执行完成"}])
    _FakeRunner.instances.clear()
    monkeypatch.setattr(
        "blank_api.services.agent.runtime.SubAgentRunner", _FakeRunner
    )

    evidence = await AgentRuntime(get_session_factory()).run(
        RunRequest(
            task_id=task_id,
            provider_id=provider_id,
            provider_name="mock-vendor",
            adapter=build_adapter(await _provider_row(provider_id), env.mock.factory),
            root=workspace,
            goal=expanded_goal,
            acceptance_criteria=["执行完成"],
            max_steps=3,
            expansion=command_info,
        )
    )

    assert evidence["task"]["status"] == "succeeded", evidence["task"]
    runner = _FakeRunner.instances[-1]
    assert runner.calls == []
    assert len(env.mock.requests) >= 1


# --------------------------------------------------------------------------- #
# 9. 运行时闸门：background 无 fork → 不派发，走常规路径
# --------------------------------------------------------------------------- #


async def test_runtime_no_dispatch_with_background_but_no_fork(
    env, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """background: true 但没写 context: fork → 不派发，走主代理循环。

    上游 ``vQt`` 的 ``background`` 只在 fork 分支里有意义；没有 fork 时
    ``background: true`` 等同于普通声明，不触发派发。
    """
    monkeypatch.setenv("BLANK_USER_COMMANDS_DIR", str(tmp_path / "empty-user-commands"))
    _write_command(
        workspace,
        "lazy-bg.md",
        _command_md("执行。", extra="background: true\n"),
    )
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["执行完成"])
    expanded_goal, command_info = expand_user_input(
        "/lazy-bg",
        root=workspace,
        session_id=task_id,
        env={"BLANK_USER_COMMANDS_DIR": str(tmp_path / "empty-user-commands")},
    )
    assert command_info is not None
    assert command_info["context"] == ""
    env.mock.reset([{"content": "执行完成"}])
    _FakeRunner.instances.clear()
    monkeypatch.setattr(
        "blank_api.services.agent.runtime.SubAgentRunner", _FakeRunner
    )

    evidence = await AgentRuntime(get_session_factory()).run(
        RunRequest(
            task_id=task_id,
            provider_id=provider_id,
            provider_name="mock-vendor",
            adapter=build_adapter(await _provider_row(provider_id), env.mock.factory),
            root=workspace,
            goal=expanded_goal,
            acceptance_criteria=["执行完成"],
            max_steps=3,
            expansion=command_info,
        )
    )

    assert evidence["task"]["status"] == "succeeded", evidence["task"]
    runner = _FakeRunner.instances[-1]
    assert runner.calls == []
    assert len(env.mock.requests) >= 1


# --------------------------------------------------------------------------- #
# 10. 运行时闸门：无任何声明 → 不派发（不变）
# --------------------------------------------------------------------------- #


async def test_runtime_no_dispatch_without_any_dispatch_field(
    env, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """context/agent/background 都没写 → 不派发，走常规 graph.ainvoke。"""
    monkeypatch.setenv("BLANK_USER_COMMANDS_DIR", str(tmp_path / "empty-user-commands"))
    _write_command(workspace, "plain.md", _command_md("执行。"))
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["执行完成"])
    expanded_goal, command_info = expand_user_input(
        "/plain",
        root=workspace,
        session_id=task_id,
        env={"BLANK_USER_COMMANDS_DIR": str(tmp_path / "empty-user-commands")},
    )
    assert command_info is not None
    env.mock.reset([{"content": "执行完成"}])
    _FakeRunner.instances.clear()
    monkeypatch.setattr(
        "blank_api.services.agent.runtime.SubAgentRunner", _FakeRunner
    )

    evidence = await AgentRuntime(get_session_factory()).run(
        RunRequest(
            task_id=task_id,
            provider_id=provider_id,
            provider_name="mock-vendor",
            adapter=build_adapter(await _provider_row(provider_id), env.mock.factory),
            root=workspace,
            goal=expanded_goal,
            acceptance_criteria=["执行完成"],
            max_steps=3,
            expansion=command_info,
        )
    )

    assert evidence["task"]["status"] == "succeeded", evidence["task"]
    runner = _FakeRunner.instances[-1]
    assert runner.calls == []
    assert len(env.mock.requests) >= 1


# --------------------------------------------------------------------------- #
# 11. Skill 侧：解析与携带
# --------------------------------------------------------------------------- #


def test_skill_definition_default_context_empty() -> None:
    """SkillDefinition 默认 context 空串。"""
    definition = SkillDefinition(name="x", description="d", instructions="body")
    assert definition.context == ""


def test_skill_definition_carries_context() -> None:
    """显式设置 context=fork 时原样携带。"""
    definition = SkillDefinition(
        name="x", description="d", instructions="body", context="fork"
    )
    assert definition.context == "fork"


def test_skill_parse_context_fork(tmp_path: Path) -> None:
    """Skill frontmatter 的 context: fork 被解析进 SkillDefinition。"""
    skill_dir = tmp_path / "review-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: review-skill\ndescription: 评审\ncontext: fork\nagent: reviewer\n---\n评审代码。\n",
        encoding="utf-8",
    )
    definition, errors, warnings = _definition_from_dir(skill_dir, "project")
    assert definition is not None
    assert definition.context == "fork"
    assert not errors


def test_skill_parse_context_other_value_warns(tmp_path: Path) -> None:
    """Skill frontmatter 的 context 只认 fork，其他值告警丢弃。"""
    skill_dir = tmp_path / "bad-ctx"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: bad-ctx\ndescription: d\ncontext: remote\n---\nbody\n",
        encoding="utf-8",
    )
    definition, _, warnings = _definition_from_dir(skill_dir, "project")
    assert definition is not None
    assert definition.context == ""
    assert any("context" in w and "fork" in w for w in warnings)


def test_skill_as_entry_carries_context() -> None:
    """Skill 的 as_entry 里带 context 键。"""
    definition = SkillDefinition(
        name="x", description="d", instructions="body", context="fork"
    )
    entry = definition.as_entry()
    assert entry["context"] == "fork"


def test_skill_as_entry_default_context() -> None:
    """没声明 context 时 as_entry 里是空串。"""
    definition = SkillDefinition(name="x", description="d", instructions="body")
    entry = definition.as_entry()
    assert entry["context"] == ""


# --------------------------------------------------------------------------- #
# 12. Skill handler 闸门：fork + agent → 前台派发
# --------------------------------------------------------------------------- #


async def test_skill_with_fork_and_agent_spawns_foreground_subagent() -> None:
    """Skill 有 context: fork + agent: reviewer → handler 前台派发 reviewer 子代理。"""
    catalog = SkillCatalog(
        [
            {
                "name": "review",
                "description": "d",
                "instructions": "评审这份代码。",
                "context": "fork",
                "agent": "reviewer",
            }
        ]
    )
    runner = _FakeRunner()
    context = _tool_context(catalog, runner=runner, agents=_reviewer_catalog())
    tool = _skill_tool()
    outcome = await tool.handler(context, {"skill": "review"})
    assert outcome.ok
    assert len(runner.calls) == 1
    mode, request = runner.calls[0]
    assert mode == "foreground"
    assert request.goal == "评审这份代码。"
    assert request.agent_name == "reviewer"
    assert outcome.data.get("status") == "forked"
    assert outcome.data.get("agent_type") == "reviewer"


# --------------------------------------------------------------------------- #
# 13. Skill handler 闸门：fork 无 agent → 默认 general-purpose
# --------------------------------------------------------------------------- #


async def test_skill_with_fork_no_agent_uses_default() -> None:
    """Skill 有 context: fork 但没 agent → 前台派发默认 general-purpose。"""
    catalog = SkillCatalog(
        [
            {
                "name": "fork-only",
                "description": "d",
                "instructions": "执行。",
                "context": "fork",
            }
        ]
    )
    runner = _FakeRunner()
    context = _tool_context(catalog, runner=runner, agents=_reviewer_catalog())
    tool = _skill_tool()
    outcome = await tool.handler(context, {"skill": "fork-only"})
    assert outcome.ok
    assert len(runner.calls) == 1
    mode, request = runner.calls[0]
    assert mode == "foreground"
    assert request.agent_name == "general-purpose"
    assert outcome.data.get("status") == "forked"


# --------------------------------------------------------------------------- #
# 14. Skill handler 闸门：agent 无 fork → 不派发
# --------------------------------------------------------------------------- #


async def test_skill_with_agent_but_no_fork_returns_body() -> None:
    """Skill 有 agent: reviewer 但没 context: fork → 不派发，返回正文注入主线程。

    这是 #303 与 #312 的最大分歧点：agent 字段只是"fork 时用哪个类型"，
    没有 fork 时它不触发派发。
    """
    catalog = SkillCatalog(
        [
            {
                "name": "review",
                "description": "d",
                "instructions": "评审这份代码。",
                "agent": "reviewer",
            }
        ]
    )
    runner = _FakeRunner()
    context = _tool_context(catalog, runner=runner, agents=_reviewer_catalog())
    tool = _skill_tool()
    outcome = await tool.handler(context, {"skill": "review"})
    assert outcome.ok
    assert len(runner.calls) == 0
    assert "评审这份代码。" in outcome.output


# --------------------------------------------------------------------------- #
# 15. Skill handler 闸门：background 无 fork → 不派发
# --------------------------------------------------------------------------- #


async def test_skill_with_background_but_no_fork_returns_body() -> None:
    """Skill 有 background: true 但没 context: fork → 不派发，返回正文。"""
    catalog = SkillCatalog(
        [
            {
                "name": "bg",
                "description": "d",
                "instructions": "后台跑的任务。",
                "background": True,
            }
        ]
    )
    runner = _FakeRunner()
    context = _tool_context(catalog, runner=runner, agents=_reviewer_catalog())
    tool = _skill_tool()
    outcome = await tool.handler(context, {"skill": "bg"})
    assert outcome.ok
    assert len(runner.calls) == 0
    assert "后台跑的任务。" in outcome.output


# --------------------------------------------------------------------------- #
# 16. Skill handler 闸门：fork + background → 后台派发
# --------------------------------------------------------------------------- #


async def test_skill_with_fork_and_background_spawns_background_subagent() -> None:
    """Skill 有 context: fork + background: true → 后台派发子代理。"""
    catalog = SkillCatalog(
        [
            {
                "name": "bg-fork",
                "description": "d",
                "instructions": "后台跑的任务。",
                "context": "fork",
                "background": True,
            }
        ]
    )
    runner = _FakeRunner()
    context = _tool_context(catalog, runner=runner, agents=_reviewer_catalog())
    tool = _skill_tool()
    outcome = await tool.handler(context, {"skill": "bg-fork"})
    assert outcome.ok
    assert len(runner.calls) == 1
    mode, request = runner.calls[0]
    assert mode == "background"
    assert outcome.data.get("status") == "forked"
    assert outcome.data.get("background") is True
    assert outcome.data.get("agent_id") == "bg-agent-1"


# --------------------------------------------------------------------------- #
# 17. _resolve_agent 行为不变（回归护栏）
# --------------------------------------------------------------------------- #


def test_resolve_agent_from_catalog() -> None:
    """_resolve_agent 从目录里解析已知类型。"""
    from blank_api.services.tools.delegate import _resolve_agent

    catalog = _reviewer_catalog()
    context = _make_runtime_context(catalog)
    definition, error = _resolve_agent(context, "reviewer")
    assert error == ""
    assert definition is not None
    assert definition.name == "reviewer"


def test_resolve_agent_empty_falls_back_to_builtin() -> None:
    """_resolve_agent 对空名回落到内置 general-purpose。"""
    from blank_api.services.tools.delegate import _resolve_agent

    catalog = _reviewer_catalog()
    context = _make_runtime_context(catalog)
    definition, error = _resolve_agent(context, "")
    assert error == ""
    assert definition is not None
    assert definition.name == "general-purpose"