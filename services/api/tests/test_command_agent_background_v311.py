"""#311：命令侧 ``agent`` / ``background`` 派发子代理。

对齐 Claude Code 的语义：命令 frontmatter 声明 ``agent``（子代理类型名）或
``background: true`` 时，``expand_user_input`` 展开出的正文不再注入主代理
循环，而是派发给一个子代理执行——``agent`` 决定用哪个定义，``background``
决定前台等结果还是后台跑。两者都没声明时走常规路径（注入主代理）。

上游锚点：``vQt(e,n){if(n||_c()||Te())return!1;return e.background??!0}``
判定是否派发；``EQt`` 后台派发；``agent`` 决定 ``agentType``。

覆盖三段：
1. **声明携带**：``CommandDefinition`` 字段、frontmatter 解析、``as_entry``、
   ``expand_user_input`` 的 info；
2. **不支持清单**：命令侧不再把 ``agent``/``background`` 当"不支持"字段；
3. **运行时派发**：声明了 agent 或 background 时，运行时走子代理而不是
   ``graph.ainvoke``；前台 ``__call__``、后台 ``spawn_background``、无声明
   不派发、未知类型 fail-closed。

钉的不是「字段存在」——每条判据指向一个会走形的具体行为：解析丢了字段、
目录不携带、info 不带、运行时不派发、无声明误派发、未知类型不报错。
改前这些用例要么 AttributeError、要么断言失败。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.db import get_session_factory
from blank_api.models.task import Task
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.agents_defs import AgentCatalog, AgentDefinition
from blank_api.services.commands_defs import (
    COMMAND_UNSUPPORTED_FIELDS,
    CommandDefinition,
    discover_commands,
    expand_user_input,
)
from blank_api.services.models.registry import build_adapter
from blank_api.services.subagent import SubAgentResult
from blank_api.services.tools.base import ToolContext

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
        task_id="task-v311",
        recorder=FakeRecorder(),
    )


# --------------------------------------------------------------------------- #
# 1. CommandDefinition 携带 agent / background
# --------------------------------------------------------------------------- #


def test_command_definition_defaults_agent_background_empty() -> None:
    """默认值：agent 空串、background False——不声明就是不派发。"""
    definition = CommandDefinition(name="x", description="d", instructions="body")
    assert definition.agent == ""
    assert definition.background is False


def test_command_definition_carries_agent_background() -> None:
    """显式设置时，两个字段原样携带。"""
    definition = CommandDefinition(
        name="x", description="d", instructions="body", agent="reviewer", background=True
    )
    assert definition.agent == "reviewer"
    assert definition.background is True


# --------------------------------------------------------------------------- #
# 2. frontmatter 解析
# --------------------------------------------------------------------------- #


def test_parse_agent_from_frontmatter(tmp_path: Path) -> None:
    """frontmatter 里的 agent 被解析进 CommandDefinition。"""
    _write_command(
        tmp_path,
        "review.md",
        _command_md("评审。", extra="agent: reviewer\n"),
    )
    definition = _discover(tmp_path).entries["review"]
    assert definition.agent == "reviewer"
    assert definition.background is False


def test_parse_agent_null_leaves_empty(tmp_path: Path) -> None:
    """agent: null 等同不声明——留空、不告警。"""
    _write_command(
        tmp_path,
        "null-agent.md",
        _command_md("执行。", extra="agent:\n"),
    )
    definition = _discover(tmp_path).entries["null-agent"]
    assert definition.agent == ""


def test_parse_agent_non_string_warns_and_ignores(tmp_path: Path) -> None:
    """agent 写成非字符串非 null：告警丢弃，不猜值。"""
    _write_command(
        tmp_path,
        "bad-agent.md",
        _command_md("执行。", extra="agent: 123\n"),
    )
    definition = _discover(tmp_path).entries["bad-agent"]
    assert definition.agent == ""
    assert any("agent" in w for w in definition.warnings)


def test_parse_background_true(tmp_path: Path) -> None:
    """background: true 被解析为 True。"""
    _write_command(
        tmp_path,
        "bg.md",
        _command_md("后台执行。", extra="background: true\n"),
    )
    definition = _discover(tmp_path).entries["bg"]
    assert definition.background is True


def test_parse_background_false(tmp_path: Path) -> None:
    """background: false 等同不声明。"""
    _write_command(
        tmp_path,
        "no-bg.md",
        _command_md("前台执行。", extra="background: false\n"),
    )
    definition = _discover(tmp_path).entries["no-bg"]
    assert definition.background is False


def test_parse_background_invalid_warns(tmp_path: Path) -> None:
    """background 写成非布尔：告警丢弃，默认 False。"""
    _write_command(
        tmp_path,
        "bad-bg.md",
        _command_md("执行。", extra="background: yes-please\n"),
    )
    definition = _discover(tmp_path).entries["bad-bg"]
    assert definition.background is False
    assert any("background" in w for w in definition.warnings)


def test_parse_agent_background_together(tmp_path: Path) -> None:
    """agent + background 同时声明：两个都解析。"""
    _write_command(
        tmp_path,
        "both.md",
        _command_md("评审并后台。", extra="agent: reviewer\nbackground: true\n"),
    )
    definition = _discover(tmp_path).entries["both"]
    assert definition.agent == "reviewer"
    assert definition.background is True


# --------------------------------------------------------------------------- #
# 3. as_entry 携带 agent / background
# --------------------------------------------------------------------------- #


def test_as_entry_carries_agent_background() -> None:
    """as_entry 的返回 dict 里带 agent 与 background——目录消费方看得见。"""
    definition = CommandDefinition(
        name="x", description="d", instructions="body", agent="reviewer", background=True
    )
    entry = definition.as_entry()
    assert entry["agent"] == "reviewer"
    assert entry["background"] is True


def test_as_entry_defaults_agent_background() -> None:
    """没声明时 as_entry 里 agent 空串、background False。"""
    definition = CommandDefinition(name="x", description="d", instructions="body")
    entry = definition.as_entry()
    assert entry["agent"] == ""
    assert entry["background"] is False


# --------------------------------------------------------------------------- #
# 4. expand_user_input 的 info 携带 agent / background
# --------------------------------------------------------------------------- #


def test_expand_carries_agent_background(tmp_path: Path) -> None:
    """展开 /name 时，info 里带 agent 与 background——运行时从 expansion 读。"""
    _write_command(
        tmp_path,
        "review.md",
        _command_md("评审。", extra="agent: reviewer\nbackground: true\n"),
    )
    _, info = _expand(tmp_path, "/review")
    assert info is not None
    assert info["agent"] == "reviewer"
    assert info["background"] is True


def test_expand_without_agent_background_carries_defaults(tmp_path: Path) -> None:
    """没声明 agent/background 时，info 里带默认值（空串 / False）。"""
    _write_command(tmp_path, "plain.md", _command_md("执行。"))
    _, info = _expand(tmp_path, "/plain")
    assert info is not None
    assert info["agent"] == ""
    assert info["background"] is False


def test_expand_non_command_has_no_agent_background(tmp_path: Path) -> None:
    """非斜杠命令文本不展开：info 为 None，不携带 agent/background。"""
    _, info = _expand(tmp_path, "普通文本")
    assert info is None


# --------------------------------------------------------------------------- #
# 5. 命令侧不支持清单排除 agent / background
# --------------------------------------------------------------------------- #


def test_command_unsupported_fields_excludes_agent_background() -> None:
    """命令侧已支持 agent/background——它们不再出现在"不支持"清单里。"""
    assert "agent" not in COMMAND_UNSUPPORTED_FIELDS
    assert "background" not in COMMAND_UNSUPPORTED_FIELDS


def test_command_ignored_does_not_flag_agent_background(tmp_path: Path) -> None:
    """声明了 agent/background 的命令，ignored 里不含这两个键。"""
    _write_command(
        tmp_path,
        "ok.md",
        _command_md("执行。", extra="agent: reviewer\nbackground: true\n"),
    )
    definition = _discover(tmp_path).entries["ok"]
    assert "agent" not in definition.ignored
    assert "background" not in definition.ignored


# --------------------------------------------------------------------------- #
# 6. 运行时派发：声明了 agent → 前台子代理，不走 graph.ainvoke
# --------------------------------------------------------------------------- #


async def test_runtime_dispatches_foreground_when_agent_set(
    env, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """命令声明 agent: reviewer（不声明 background）→ 前台派发子代理，模型不被调用。"""
    monkeypatch.setenv("BLANK_USER_COMMANDS_DIR", str(tmp_path / "empty-user-commands"))
    _write_agent(workspace, "reviewer.md", _agent_md())
    _write_command(
        workspace,
        "review.md",
        _command_md("评审这份代码。", extra="agent: reviewer\n"),
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
    # 重置 mock 并打桩子代理运行器
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
    # 子代理运行器被前台调用一次
    runner = _FakeRunner.instances[-1]
    assert len(runner.calls) == 1
    kind, request = runner.calls[0]
    assert kind == "foreground"
    # 请求的 goal 是展开后的正文，agent_name 是 reviewer
    assert request.goal == expanded_goal
    assert request.agent_name == "reviewer"
    # 模型没被调用——走的是子代理不是 graph.ainvoke
    assert env.mock.requests == []


# --------------------------------------------------------------------------- #
# 7. 运行时派发：background: true → 后台子代理
# --------------------------------------------------------------------------- #


async def test_runtime_dispatches_background_when_background_true(
    env, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """命令声明 background: true → 后台派发子代理（spawn_background）。"""
    monkeypatch.setenv("BLANK_USER_COMMANDS_DIR", str(tmp_path / "empty-user-commands"))
    _write_command(
        workspace,
        "bg.md",
        _command_md("后台执行。", extra="background: true\n"),
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
# 8. 运行时派发：无 agent / background → 常规路径（不派发）
# --------------------------------------------------------------------------- #


async def test_runtime_no_dispatch_without_agent_background(
    env, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """命令没声明 agent/background → 不派发子代理，走常规 graph.ainvoke。"""
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
    # 子代理运行器没被调用——走的是主代理循环
    runner = _FakeRunner.instances[-1]
    assert runner.calls == []
    # 模型被调用了——常规路径
    assert len(env.mock.requests) >= 1


# --------------------------------------------------------------------------- #
# 9. 运行时派发：未知 agent 类型 → fail-closed
# --------------------------------------------------------------------------- #


async def test_runtime_fails_closed_on_unknown_agent(
    env, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """命令声明 agent: nonexistent → 任务失败，不静默降级为常规执行。"""
    monkeypatch.setenv("BLANK_USER_COMMANDS_DIR", str(tmp_path / "empty-user-commands"))
    _write_command(
        workspace,
        "bad.md",
        _command_md("执行。", extra="agent: nonexistent\n"),
    )
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["执行完成"])
    expanded_goal, command_info = expand_user_input(
        "/bad",
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

    assert evidence["task"]["status"] == "failed", evidence["task"]
    # 子代理运行器没被调用——在解析阶段就 fail-closed 了
    runner = _FakeRunner.instances[-1]
    assert runner.calls == []
    # 模型也没被调用——没有降级到常规路径
    assert env.mock.requests == []


# --------------------------------------------------------------------------- #
# 10. _resolve_agent 对命令侧 context 的行为
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


def test_resolve_agent_unknown_fails_closed() -> None:
    """_resolve_agent 对未知类型返回错误，不返回 None 定义。"""
    from blank_api.services.tools.delegate import _resolve_agent

    catalog = _reviewer_catalog()
    context = _make_runtime_context(catalog)
    definition, error = _resolve_agent(context, "nonexistent")
    assert definition is None
    assert "nonexistent" in error


def test_resolve_agent_empty_falls_back_to_builtin() -> None:
    """_resolve_agent 对空名回落到内置 general-purpose。"""
    from blank_api.services.tools.delegate import _resolve_agent

    catalog = _reviewer_catalog()
    context = _make_runtime_context(catalog)
    definition, error = _resolve_agent(context, "")
    assert error == ""
    assert definition is not None
    assert definition.name == "general-purpose"
