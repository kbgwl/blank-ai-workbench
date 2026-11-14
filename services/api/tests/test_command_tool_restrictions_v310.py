"""#310：命令/Skill 的 ``allowed-tools`` / ``disallowed-tools`` 强制约束。

对齐 Claude Code 的语义：一份命令或 Skill 声明了 ``allowed-tools`` 后，本轮
后续步骤的工具池只保留交集；声明了 ``disallowed-tools`` 则从池里移除。
两者可同时声明，先交集再减去禁止项。``*`` 出现在禁止项里按"全部禁用"处理。

覆盖两层：
1. **可见层**：``graph.py`` 构建 ``specs`` 时按 ``turn_state`` 过滤，模型看不见
   被禁工具；
2. **执行层**：``gateway.execute`` 硬拦不在允许集内的工具，模型凭记忆硬调也
   跑不了。

两条入口：
- 用户侧 ``/name args`` 经 ``expand_user_input`` 展开，``command_info`` 携带
  工具约束，运行时从 ``request.expansion`` 初始化 ``turn_state``；
- 模型侧经 ``skill`` 工具加载，``_skill`` handler 写入 ``turn_state``。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.db import get_session_factory
from blank_api.models.task import Task
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.commands_defs import discover_commands, expand_user_input
from blank_api.services.models.registry import build_adapter
from blank_api.services.permissions import PermissionPolicy
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome
from blank_api.services.tools.gateway import ToolGateway
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


class _NoopSession:
    def add(self, *args, **kwargs):
        pass

    async def commit(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _noop_session_factory():
    return _NoopSession()


# --------------------------------------------------------------------------- #
# 帮助
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate_user_prompt_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    empty = tmp_path / "_no-user-prompts"
    empty.mkdir()
    monkeypatch.setenv("BLANK_USER_COMMANDS_DIR", str(empty / "commands"))
    monkeypatch.setenv("BLANK_USER_SKILLS_DIR", str(empty / "skills"))
    yield


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _command(body: str = "按命令正文执行。", extra: str = "") -> str:
    return f"---\ndescription: 命令说明\n{extra}---\n{body}"


def _write_command(root: Path, relative: str, text: str) -> Path:
    return _write(root / ".blank" / "commands" / relative, text)


def _discover(root: Path):
    env_map = {"BLANK_USER_COMMANDS_DIR": str(root.parent / "no-user-commands")}
    return discover_commands(root, env=env_map)


def _expand(root: Path, text: str, **kwargs):
    env_map = {"BLANK_USER_COMMANDS_DIR": str(root.parent / "no-user-commands")}
    return expand_user_input(text, root=root, env=env_map, **kwargs)


def _tool():
    return next(item for item in skill_tools() if item.name == "skill")


def _context(catalog, **kw) -> SimpleNamespace:
    defaults = dict(
        root=Path("."),
        task_id="task-v310",
        skills=catalog,
        recorder=FakeRecorder(),
        turn_state={},
        permissions=PermissionPolicy(),
        safety=None,
        extra_secrets=(),
        session_factory=_noop_session_factory,
        trace_id="trace-v310",
        sandbox_tier="",
        team_scope={},
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _messages_text(messages) -> str:
    parts = []
    for item in messages:
        if isinstance(item, dict):
            parts.append(str(item.get("content") or ""))
        else:
            parts.append(str(getattr(item, "content", "") or ""))
    return "\n".join(parts)


async def _provider_row(provider_id: str):
    from blank_api.models.model_provider import ModelProvider

    async with get_session_factory()() as session:
        return await session.get(ModelProvider, provider_id)


# --------------------------------------------------------------------------- #
# 1. expand_user_input：command_info 携带工具约束
# --------------------------------------------------------------------------- #
def test_expand_user_input_carries_allowed_tools(tmp_path: Path) -> None:
    _write_command(
        tmp_path,
        "deploy.md",
        "---\ndescription: 上线\nallowed-tools: read_file, run_shell\n---\n上线\n",
    )
    expanded, info = _expand(tmp_path, "/deploy")
    assert info is not None
    assert info["allowed_tools"] == ["read_file", "run_shell"]


def test_expand_user_input_carries_disallowed_tools(tmp_path: Path) -> None:
    _write_command(
        tmp_path,
        "deploy.md",
        "---\ndescription: 上线\ndisallowed-tools: write_file\n---\n上线\n",
    )
    expanded, info = _expand(tmp_path, "/deploy")
    assert info is not None
    assert info["disallowed_tools"] == ["write_file"]


def test_expand_user_input_carries_both_when_both_declared(tmp_path: Path) -> None:
    _write_command(
        tmp_path,
        "deploy.md",
        "---\n"
        "description: 上线\n"
        "allowed-tools: read_file, run_shell\n"
        "disallowed-tools: run_shell\n"
        "---\n上线\n",
    )
    _, info = _expand(tmp_path, "/deploy")
    assert info is not None
    assert info["allowed_tools"] == ["read_file", "run_shell"]
    assert info["disallowed_tools"] == ["run_shell"]


def test_expand_user_input_carries_empty_tool_keys_when_not_declared(tmp_path: Path) -> None:
    _write_command(tmp_path, "plain.md", _command())
    _, info = _expand(tmp_path, "/plain")
    assert info is not None
    assert info.get("allowed_tools") == []
    assert info.get("disallowed_tools") == []


# --------------------------------------------------------------------------- #
# 2. _skill handler：写入 turn_state
# --------------------------------------------------------------------------- #
async def test_skill_handler_writes_allowed_tools_to_turn_state(tmp_path: Path) -> None:
    _write_command(
        tmp_path,
        "deploy.md",
        "---\ndescription: 上线\nallowed-tools: read_file\n---\n上线\n",
    )
    found = _discover(tmp_path)
    catalog = SkillCatalog([item.as_entry() for item in found.entries.values()])
    ctx = _context(catalog)

    await _tool().handler(ctx, {"skill": "deploy"})

    assert ctx.turn_state.get("skill_allowed_tools") == ("read_file",)


async def test_skill_handler_writes_disallowed_tools_to_turn_state(tmp_path: Path) -> None:
    _write_command(
        tmp_path,
        "deploy.md",
        "---\ndescription: 上线\ndisallowed-tools: write_file, edit_file\n---\n上线\n",
    )
    found = _discover(tmp_path)
    catalog = SkillCatalog([item.as_entry() for item in found.entries.values()])
    ctx = _context(catalog)

    await _tool().handler(ctx, {"skill": "deploy"})

    assert ctx.turn_state.get("skill_disallowed_tools") == ("write_file", "edit_file")


async def test_skill_handler_writes_both_to_turn_state(tmp_path: Path) -> None:
    _write_command(
        tmp_path,
        "deploy.md",
        "---\n"
        "description: 上线\n"
        "allowed-tools: read_file, run_shell\n"
        "disallowed-tools: run_shell\n"
        "---\n上线\n",
    )
    found = _discover(tmp_path)
    catalog = SkillCatalog([item.as_entry() for item in found.entries.values()])
    ctx = _context(catalog)

    await _tool().handler(ctx, {"skill": "deploy"})

    assert ctx.turn_state.get("skill_allowed_tools") == ("read_file", "run_shell")
    assert ctx.turn_state.get("skill_disallowed_tools") == ("run_shell",)


async def test_skill_handler_without_tool_fields_leaves_turn_state_clean(tmp_path: Path) -> None:
    _write_command(tmp_path, "plain.md", _command())
    found = _discover(tmp_path)
    catalog = SkillCatalog([item.as_entry() for item in found.entries.values()])
    ctx = _context(catalog)

    await _tool().handler(ctx, {"skill": "plain"})

    assert "skill_allowed_tools" not in ctx.turn_state
    assert "skill_disallowed_tools" not in ctx.turn_state


# --------------------------------------------------------------------------- #
# 3. gateway 执行层：硬拦不在允许集内的工具
# --------------------------------------------------------------------------- #
def _make_gateway(tool_names=("read_file", "write_file", "run_shell")) -> ToolGateway:
    tools = [
        ToolDefinition(
            name=name,
            description=name,
            input_schema={"type": "object", "properties": {}},
            risk_level="low",
            handler=lambda ctx, args: None,  # type: ignore[arg-type]
        )
        for name in tool_names
    ]
    return ToolGateway(tools, defer=False)


async def test_gateway_refuses_tool_not_in_allowed_set() -> None:
    gateway = _make_gateway()
    ctx = _context(None, turn_state={"skill_allowed_tools": ("read_file",)})
    result = await gateway.execute("write_file", {}, ctx)
    assert result["ok"] is False
    assert result["status"] == "denied"
    assert "allowed-tools" in result.get("message", "") or "允许" in result.get("message", "")


async def test_gateway_allows_tool_in_allowed_set() -> None:
    gateway = _make_gateway()
    ctx = _context(None, turn_state={"skill_allowed_tools": ("read_file",)})
    # read_file 的 handler 返回 None，但 gateway 不会因为 handler 返回值而拒绝；
    # 这里只验证它没有被 allowed-tools 拦下（走到 handler 执行说明过了闸门）。
    # 用一个能跑的 handler 替代 lambda None，避免 _finish 里的异常路径。
    tools = [
        ToolDefinition(
            name="read_file",
            description="read",
            input_schema={"type": "object", "properties": {}},
            risk_level="low",
            handler=_noop_handler,
        )
    ]
    gateway2 = ToolGateway(tools, defer=False)
    result = await gateway2.execute("read_file", {}, ctx)
    assert result["ok"] is True


async def _noop_handler(ctx: ToolContext, args: dict) -> ToolOutcome:
    from blank_api.services.tools.base import ok
    return ok("done")


async def test_gateway_refuses_tool_in_disallowed_set() -> None:
    gateway = _make_gateway()
    ctx = _context(None, turn_state={"skill_disallowed_tools": ("write_file",)})
    result = await gateway.execute("write_file", {}, ctx)
    assert result["ok"] is False
    assert result["status"] == "denied"
    assert "disallowed" in result.get("message", "").lower() or "禁止" in result.get("message", "")


async def test_gateway_star_in_disallowed_blocks_all() -> None:
    gateway = _make_gateway()
    ctx = _context(None, turn_state={"skill_disallowed_tools": ("*",)})
    for name in ("read_file", "write_file", "run_shell"):
        result = await gateway.execute(name, {}, ctx)
        assert result["ok"] is False, name
        assert result["status"] == "denied", name


async def test_gateway_no_turn_state_restriction_passes_through() -> None:
    gateway = _make_gateway(tool_names=("read_file",))
    ctx = _context(None, turn_state={})
    tools = [
        ToolDefinition(
            name="read_file",
            description="read",
            input_schema={"type": "object", "properties": {}},
            risk_level="low",
            handler=_noop_handler,
        )
    ]
    gateway2 = ToolGateway(tools, defer=False)
    result = await gateway2.execute("read_file", {}, ctx)
    assert result["ok"] is True


# --------------------------------------------------------------------------- #
# 4. graph.py specs 过滤：模型看不见被禁工具
# --------------------------------------------------------------------------- #
def test_specs_filter_removes_disallowed_tools() -> None:
    """turn_state 里有 disallowed_tools 时，specs 里不出现该工具。"""
    from blank_api.services.agent.graph import filter_specs_by_turn_state

    specs = [
        SimpleNamespace(name="read_file"),
        SimpleNamespace(name="write_file"),
        SimpleNamespace(name="run_shell"),
    ]
    filtered = filter_specs_by_turn_state(specs, {"skill_disallowed_tools": ("write_file",)})
    names = [s.name for s in filtered]
    assert "write_file" not in names
    assert "read_file" in names
    assert "run_shell" in names


def test_specs_filter_narrows_to_allowed_tools() -> None:
    from blank_api.services.agent.graph import filter_specs_by_turn_state

    specs = [
        SimpleNamespace(name="read_file"),
        SimpleNamespace(name="write_file"),
        SimpleNamespace(name="run_shell"),
    ]
    filtered = filter_specs_by_turn_state(specs, {"skill_allowed_tools": ("read_file",)})
    names = [s.name for s in filtered]
    assert names == ["read_file"]


def test_specs_filter_applies_both_allowed_and_disallowed() -> None:
    from blank_api.services.agent.graph import filter_specs_by_turn_state

    specs = [
        SimpleNamespace(name="read_file"),
        SimpleNamespace(name="write_file"),
        SimpleNamespace(name="run_shell"),
    ]
    filtered = filter_specs_by_turn_state(
        specs,
        {"skill_allowed_tools": ("read_file", "write_file", "run_shell"),
         "skill_disallowed_tools": ("write_file",)},
    )
    names = [s.name for s in filtered]
    assert "write_file" not in names
    assert "read_file" in names
    assert "run_shell" in names


def test_specs_filter_star_in_disallowed_returns_empty() -> None:
    from blank_api.services.agent.graph import filter_specs_by_turn_state

    specs = [SimpleNamespace(name="read_file")]
    filtered = filter_specs_by_turn_state(specs, {"skill_disallowed_tools": ("*",)})
    assert filtered == []


def test_specs_filter_no_restriction_returns_all() -> None:
    from blank_api.services.agent.graph import filter_specs_by_turn_state

    specs = [SimpleNamespace(name="read_file"), SimpleNamespace(name="write_file")]
    filtered = filter_specs_by_turn_state(specs, {})
    assert len(filtered) == 2


# --------------------------------------------------------------------------- #
# 5. 运行时接线：用户侧 /name 展开后工具池收窄
# --------------------------------------------------------------------------- #
async def test_runtime_narrows_tool_pool_from_user_command(
    env, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLANK_USER_COMMANDS_DIR", str(tmp_path / "empty-user-commands"))
    _write_command(
        workspace,
        "check.md",
        "---\ndescription: 检查\nallowed-tools: read_file\n---\n只读检查。",
    )
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["检查完成"])
    expanded_goal, command_info = expand_user_input(
        "/check",
        root=workspace,
        session_id=task_id,
        env={"BLANK_USER_COMMANDS_DIR": str(tmp_path / "empty-user-commands")},
    )
    assert command_info is not None
    env.mock.reset([{"content": "检查完成"}])

    evidence = await AgentRuntime(get_session_factory()).run(
        RunRequest(
            task_id=task_id,
            provider_id=provider_id,
            provider_name="mock-vendor",
            adapter=build_adapter(await _provider_row(provider_id), env.mock.factory),
            root=workspace,
            goal=expanded_goal,
            acceptance_criteria=["检查完成"],
            max_steps=3,
            expansion=command_info,
        )
    )

    assert evidence["task"]["status"] == "succeeded", evidence["task"]
    # 模型这一轮下发的工具表里不该出现 write_file（allowed-tools 只放了 read_file）。
    main_index = next(
        index
        for index, request in enumerate(env.mock.requests)
        if "read_file" in {item["function"]["name"] for item in request.get("tools", [])}
    )
    tool_names = {item["function"]["name"] for item in env.mock.requests[main_index]["tools"]}
    assert "read_file" in tool_names
    assert "write_file" not in tool_names


# --------------------------------------------------------------------------- #
# 6. 运行时接线：模型侧 skill 工具加载后工具池收窄
# --------------------------------------------------------------------------- #
async def test_runtime_narrows_tool_pool_after_skill_loaded(
    env, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLANK_USER_COMMANDS_DIR", str(tmp_path / "empty-user-commands"))
    _write_command(
        workspace,
        "safe.md",
        "---\ndescription: 安全检查\ndisallowed-tools: write_file\n---\n只读检查。",
    )
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["检查完成"])
    env.mock.reset(
        [
            {"content": "加载安全检查", "tool_calls": [call("skill", {"skill": "safe"})]},
            {"content": "检查完成"},
        ]
    )

    evidence = await AgentRuntime(get_session_factory()).run(
        RunRequest(
            task_id=task_id,
            provider_id=provider_id,
            provider_name="mock-vendor",
            adapter=build_adapter(await _provider_row(provider_id), env.mock.factory),
            root=workspace,
            goal="做安全检查",
            acceptance_criteria=["检查完成"],
            max_steps=5,
        )
    )

    assert evidence["task"]["status"] == "succeeded", evidence["task"]
    # skill 加载之后的那一步，工具表里不该有 write_file。
    after_skill = [
        request
        for request in env.mock.requests
        if any(
            item.get("role") == "tool" and "safe" in str(item.get("content", ""))
            for item in request.get("messages", [])
        )
    ]
    assert after_skill, "没找到 skill 加载后的请求"
    for request in after_skill:
        tool_names = {item["function"]["name"] for item in request.get("tools", [])}
        assert "write_file" not in tool_names, tool_names
