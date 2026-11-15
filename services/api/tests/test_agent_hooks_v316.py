"""#316 子代理定义 frontmatter 的 ``hooks``：解析、请求携带与作用域注册。

上游语义（本批现场取证）：

* agent 定义里的 ``hooks`` 在该子代理的派发点注册进子会话的钩子注册表，
  **注册发生在 SubagentStart 之后**——定义里写的 SubagentStart 钩子看不到
  自己这次启动；
* 注册时把 ``Stop`` 改写成 ``SubagentStop``（``Converting Stop hook to
  SubagentStop``），其余事件原样注册；
* 守卫类事件（PreToolUse / PermissionRequest）的条目坏掉，或把这两个事件
  写在顶层（不在 hooks 内），整份定义判为不可加载；非守卫事件的坏条目只
  丢弃并告警，定义照常加载。
* 插件 agent 的 hooks 仍按上游 Skn 忽略。

本地多守一条：定义钩子只属于这个子代理。它不能泄漏到父会话的下一轮，
父会话 settings 里的钩子在子代理里仍然照常跑；父 runner 的配置不被改写。

每条判据都钉在「拿掉实现就红」的位置上。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from blank_api.services.agents_defs import UNSUPPORTED_FIELDS, load_definition
from blank_api.services.hooks import HookRunner, HookSpec
from blank_api.services.models.base import (
    AdapterConfig,
    ModelAdapter,
    ModelResponse,
    Usage,
)
from blank_api.services.subagent import SubAgentRequest, SubAgentRunner, run_subagent
from blank_api.services.tools.base import ToolDefinition, ok
from blank_api.services.tools.delegate import (
    _apply_definition,
    _parse_tasks,
    delegate_tools,
)

from tests.test_agent_defs_v44 import (  # noqa: F401  (_agent/_write/_discover 复用)
    _agent,
    _context,
    _discover,
    _write,
)
from tests.test_agent_v04 import (  # noqa: F401  (env 等夹具)
    API,
    create_provider,
    create_project,
    create_task,
    env,
)
from tests.test_claude_fork_v30 import FakeRunner, _FakeSession
from tests.test_claude_send_message_v30 import ScriptedAdapter
from tests.test_hook_events_v52 import _hooks_env, _lines, _log_hook


def _spec(command: str, *, match: str = "") -> HookSpec:
    return HookSpec(kind="command", command=command, match=match, source="agent:fast")


def _runner(specs: dict[str, list[dict]]):
    runner = HookRunner.from_env(json.dumps(specs), settings=None)
    assert not runner.warnings, runner.warnings
    return runner


class _Rec:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []
        self.run_id = ""
        self.origin_run_id = ""

    async def add(self, event: str, node: str, payload: dict) -> None:
        self.events.append((event, node, dict(payload)))

    def named(self, event: str) -> list[dict]:
        return [payload for name, _node, payload in self.events if name == event]


class _ToolCallAdapter(ModelAdapter):
    """按脚本返回模型响应；子代理第一步调一个低风险假工具。"""

    def __init__(self, script: list[ModelResponse]) -> None:
        super().__init__(
            AdapterConfig(
                name="hooks-v316",
                kind="openai_compatible",
                base_url="",
                api_key="",
                model="hooks-v316",
                supports_tools=True,
            )
        )
        self.script = script
        self.calls = 0

    async def invoke(self, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def stream(self, messages, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def bind_tools(self, specs, messages, **kwargs):
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return item


async def _probe(_context, _arguments):  # pragma: no cover - 只证明钩子真的跑了
    return ok("pong")


# 高风险工具：默认模式下会走 PermissionRequest 钩子，这是子代理工具管线里
# 真正会执行守卫钩子的那一段。
RISKY = ToolDefinition(
    name="risky",
    description="高风险探针工具",
    input_schema={"type": "object", "properties": {}, "required": []},
    risk_level="high",
    handler=_probe,
)


async def _run_child(
    root: Path,
    *,
    hooks,
    agent_hooks: dict[str, tuple[HookSpec, ...]],
    adapter=None,
    tools: list | None = None,
    recorder: _Rec | None = None,
    start_hooks: bool = True,
    isolate: bool = False,
):
    return await run_subagent(
        adapter=adapter or ScriptedAdapter(["子代理结论"]),
        tools=list(tools or []),
        root=root,
        task_id="task-v316",
        session_factory=_FakeSession,
        recorder=recorder or _Rec(),
        request=SubAgentRequest(
            goal="核对 README",
            max_steps=3,
            agent_name="fast",
            agent_hooks=agent_hooks,
            isolate=isolate,
        ),
        agent_path="root/fast",
        depth=1,
        parent_thread_id="root-thread",
        agent_id="agent-v316",
        hooks=hooks,
        start_hooks=start_hooks,
    )


# --------------------------------------------------------------------------- #
# 1. 解析：合法 hooks 进定义，守卫事件的坏结构拒绝整份定义
# --------------------------------------------------------------------------- #

def test_parse_hooks_keeps_specs_and_hides_commands(tmp_path: Path):
    path = _write(
        tmp_path / "agents" / "hooked.md",
        _agent(
            name="hooked",
            extra=(
                "hooks:\n"
                "  PreToolUse:\n"
                "    - matcher: Write|Edit\n"
                "      hooks:\n"
                "        - type: command\n"
                "          command: \"guard-secret-token\"\n"
                "  SubagentStop:\n"
                "    - hooks:\n"
                "        - type: command\n"
                "          command: \"on-stop\"\n"
            ),
        ),
    )
    definition, problems = load_definition(path)
    assert not problems, problems
    assert definition is not None
    specs = definition.hooks["pre_tool_use"]
    assert len(specs) == 1
    assert specs[0].command == "guard-secret-token"
    assert specs[0].match == "Write|Edit"
    assert specs[0].source == "agent:hooked"
    assert [item.command for item in definition.hooks["subagent_stop"]] == ["on-stop"]
    # 审计面只说"每个事件几条"，绝不复制命令正文（命令里可能有 token）。
    audit = definition.as_dict()
    assert audit["hooks"] == {"pre_tool_use": 1, "subagent_stop": 1}
    assert "guard-secret-token" not in json.dumps(audit, ensure_ascii=False)


def test_parse_stop_stays_stop_until_registration(tmp_path: Path):
    """Stop→SubagentStop 是注册时的改写；解析层保留定义里的原事件名。"""
    path = _write(
        tmp_path / "agents" / "stop.md",
        _agent(
            name="stoppy",
            extra=(
                "hooks:\n"
                "  Stop:\n"
                "    - hooks:\n"
                "        - type: command\n"
                "          command: \"on-stop\"\n"
            ),
        ),
    )
    definition, problems = load_definition(path)
    assert not problems, problems
    assert definition is not None
    assert "stop" in definition.hooks
    assert "subagent_stop" not in definition.hooks


def test_top_level_pretooluse_rejects_definition(tmp_path: Path):
    path = _write(
        tmp_path / "agents" / "toplevel.md",
        _agent(
            name="toplevel",
            extra=(
                "PreToolUse:\n"
                "  - hooks:\n"
                "      - type: command\n"
                "        command: \"guard\"\n"
            ),
        ),
    )
    definition, problems = load_definition(path)
    assert definition is None
    assert any("PreToolUse" in item or "顶层" in item for item in problems), problems


def test_hooks_not_mapping_rejects_definition(tmp_path: Path):
    path = _write(
        tmp_path / "agents" / "badshape.md",
        _agent(name="badshape", extra='hooks: "not-a-mapping"\n'),
    )
    definition, problems = load_definition(path)
    assert definition is None
    assert any("hooks" in item for item in problems), problems


def test_broken_guard_entry_rejects_definition(tmp_path: Path):
    path = _write(
        tmp_path / "agents" / "badguard.md",
        _agent(
            name="badguard",
            extra=(
                "hooks:\n"
                "  PreToolUse:\n"
                "    - hooks:\n"
                "        - type: command\n"
            ),
        ),
    )
    definition, problems = load_definition(path)
    assert definition is None
    assert any("PreToolUse" in item or "hooks" in item for item in problems), problems


def test_broken_non_guard_entry_is_dropped_and_agent_loads(tmp_path: Path):
    path = _write(
        tmp_path / "agents" / "mixed.md",
        _agent(
            name="mixed",
            extra=(
                "hooks:\n"
                "  PostToolUse:\n"
                "    - hooks:\n"
                "        - type: command\n"
                "  SubagentStop:\n"
                "    - hooks:\n"
                "        - type: command\n"
                "          command: \"still-here\"\n"
            ),
        ),
    )
    definition, problems = load_definition(path)
    assert definition is not None, problems
    assert any("PostToolUse" in item for item in problems), problems
    assert "post_tool_use" not in definition.hooks
    assert [item.command for item in definition.hooks["subagent_stop"]] == ["still-here"]


def test_unknown_event_is_dropped_but_agent_loads(tmp_path: Path):
    path = _write(
        tmp_path / "agents" / "unknown.md",
        _agent(
            name="unknown",
            extra=(
                "hooks:\n"
                "  NotARealEvent:\n"
                "    - hooks:\n"
                "        - type: command\n"
                "          command: \"nope\"\n"
            ),
        ),
    )
    definition, problems = load_definition(path)
    assert definition is not None, problems
    assert any("NotARealEvent" in item for item in problems), problems
    assert definition.hooks == {}


def test_plugin_agent_still_ignores_hooks(tmp_path: Path):
    path = _write(
        tmp_path / "agents" / "plug.md",
        "---\nhooks:\n  Stop:\n    - hooks:\n        - type: command\n"
        "          command: \"nope\"\n---\n插件正文。\n",
    )
    definition, problems = load_definition(path, source="plugin:demo")
    assert definition is not None, problems
    assert definition.hooks == {}
    assert "hooks" in definition.ignored
    assert any("hooks" in item and "忽略" in item for item in problems), problems


def test_hooks_no_longer_unsupported(tmp_path: Path):
    assert "hooks" not in UNSUPPORTED_FIELDS
    path = _write(
        tmp_path / "agents" / "plain.md",
        _agent(name="plain", extra="model: mock-fast\n"),
    )
    definition, problems = load_definition(path)
    assert not problems, problems
    assert definition is not None
    assert definition.hooks == {}
    assert "hooks" not in definition.ignored


# --------------------------------------------------------------------------- #
# 2. 请求组装：定义钩子挂进请求，批量派发与续聊都带上
# --------------------------------------------------------------------------- #

def test_apply_definition_carries_hooks(tmp_path: Path):
    path = _write(
        tmp_path / "agents" / "hooked.md",
        _agent(
            name="hooked",
            extra=(
                "hooks:\n"
                "  Stop:\n"
                "    - hooks:\n"
                "        - type: command\n"
                "          command: \"on-stop\"\n"
            ),
        ),
    )
    definition, _ = load_definition(path)
    request = _apply_definition(SubAgentRequest(goal="看看"), definition)
    assert [item.command for item in request.agent_hooks["stop"]] == ["on-stop"]


def test_normalized_keeps_agent_hooks():
    spec = _spec("on-stop")
    request = SubAgentRequest(goal="看看", agent_hooks={"stop": (spec,)}).normalized()
    assert request.agent_hooks["stop"][0].command == "on-stop"


async def test_spawn_agents_batch_carries_hooks(tmp_path: Path):
    _write(
        tmp_path / ".blank" / "agents" / "fast.md",
        _agent(
            name="fast",
            extra=(
                "hooks:\n"
                "  Stop:\n"
                "    - hooks:\n"
                "        - type: command\n"
                "          command: \"on-stop\"\n"
            ),
        ),
    )
    catalog = _discover(tmp_path)
    requests, error = _parse_tasks(
        [{"goal": "看看", "subagent_type": "fast"}],
        _context(tmp_path, FakeRunner(), catalog),
    )
    assert not error
    assert [item.command for item in requests[0].agent_hooks["stop"]] == ["on-stop"]


# --------------------------------------------------------------------------- #
# 3. 运行期：定义钩子只作用于这个子代理
# --------------------------------------------------------------------------- #

async def test_definition_guard_hook_runs_inside_child_tool_loop(
    tmp_path: Path,
):
    """定义里的守卫钩子真的在子代理自己的工具管线里跑：高风险调用问一次。"""

    command, log = _log_hook(
        tmp_path,
        "def-guard",
        body="print(json.dumps({'decision': 'allow', 'reason': 'definition hook'}))\n",
    )
    parent = _runner({})
    adapter = _ToolCallAdapter(
        [
            ModelResponse(
                text="",
                usage=Usage(10, 5),
                model="hooks-v316",
                tool_calls=[
                    {
                        "id": "call-risky",
                        "type": "function",
                        "function": {"name": "risky", "arguments": "{}"},
                    }
                ],
            ),
            ModelResponse(text="子代理结论", usage=Usage(5, 2), model="hooks-v316"),
        ]
    )
    result = await _run_child(
        tmp_path,
        hooks=parent,
        agent_hooks={"permission_request": (_spec(command),)},
        adapter=adapter,
        tools=[RISKY],
    )
    assert result.ok, result.error
    rows = _lines(log)
    assert len(rows) == 1, rows
    assert rows[0]["hook_event_name"] == "permission_request"
    assert rows[0]["tool_name"] == "risky"
    # 定义钩子没有注册进父 runner。
    assert not parent.hooks.get("permission_request")


async def test_definition_stop_is_registered_as_subagent_stop(
    tmp_path: Path,
):
    definition_command, definition_log = _log_hook(tmp_path, "def-stop")
    parent_command, parent_log = _log_hook(tmp_path, "parent-stop")
    parent = _runner({"SubagentStop": [{"command": parent_command, "timeout": 20}]})
    recorder = _Rec()

    result = await _run_child(
        tmp_path,
        hooks=parent,
        agent_hooks={"stop": (_spec(definition_command),)},
        recorder=recorder,
    )
    assert result.ok, result.error

    rows = _lines(definition_log)
    assert [row["hook_event_name"] for row in rows] == ["subagent_stop"]
    # 父会话 settings 里的 SubagentStop 在子代理里照常跑，两条互不替代。
    assert [row["hook_event_name"] for row in _lines(parent_log)] == ["subagent_stop"]
    registered = recorder.named("subagent.hooks_registered")
    assert registered, [name for name, _node, _payload in recorder.events]
    assert registered[0]["events"] == {"subagent_stop": 1}
    assert registered[0]["converted"] == ["stop"]
    # 注册是作用域内的：父 runner 的映射没有多出定义里的钩子。
    assert [item.command for item in parent.hooks["subagent_stop"]] == [parent_command]
    assert "stop" not in parent.hooks


async def test_definition_notification_runs_on_child_completion(
    tmp_path: Path,
):
    command, log = _log_hook(tmp_path, "def-notify")
    parent = _runner({})

    result = await _run_child(
        tmp_path,
        hooks=parent,
        agent_hooks={"notification": (_spec(command),)},
    )
    assert result.ok, result.error
    rows = _lines(log)
    assert len(rows) == 1, rows
    assert rows[0]["hook_event_name"] == "notification"
    # 通知也是子代理作用域内的事件：父 runner 的映射里没有它。
    assert not parent.hooks.get("notification")


async def test_definition_worktree_remove_hook_runs_in_child_scope(
    tmp_path: Path,
):
    """隔离副本清理也走子作用域：定义里的 worktree_remove 在副本删掉前跑一次。"""

    command, log = _log_hook(
        tmp_path,
        "def-remove",
        body="print(json.dumps({'decision': 'allow'}))\n",
    )
    parent = _runner({})

    result = await _run_child(
        tmp_path,
        hooks=parent,
        agent_hooks={"worktree_remove": (_spec(command),)},
        isolate=True,
    )
    assert result.ok, result.error
    rows = _lines(log)
    assert len(rows) == 1, rows
    assert rows[0]["hook_event_name"] == "worktree_remove"
    assert not parent.hooks.get("worktree_remove")


async def test_definition_hooks_do_not_see_their_own_subagent_start(
    tmp_path: Path,
):
    definition_command, definition_log = _log_hook(tmp_path, "def-start")
    parent_command, parent_log = _log_hook(tmp_path, "parent-start")
    parent = _runner({"SubagentStart": [{"command": parent_command, "timeout": 20}]})

    result = await _run_child(
        tmp_path,
        hooks=parent,
        agent_hooks={"subagent_start": (_spec(definition_command),)},
    )
    assert result.ok, result.error
    # 父会话的 SubagentStart 先跑（注册发生在它之后）。
    assert [row["hook_event_name"] for row in _lines(parent_log)] == ["subagent_start"]
    assert not definition_log.exists(), "定义钩子注册发生在 SubagentStart 之后，不该看到自己这次启动"


async def test_definition_hooks_respect_disable_all_hooks(tmp_path: Path):
    command, log = _log_hook(tmp_path, "disabled")
    parent = HookRunner.from_env(None, settings=None, disabled=True)
    recorder = _Rec()
    result = await _run_child(
        tmp_path,
        hooks=parent,
        agent_hooks={"stop": (_spec(command),)},
        recorder=recorder,
    )
    assert result.ok, result.error
    assert not log.exists(), "disableAllHooks 下定义钩子也不能复活"
    assert not recorder.named("subagent.hooks_registered")


async def test_hooks_survive_send_message(tmp_path: Path):
    command, log = _log_hook(tmp_path, "def-continue")
    parent = _runner({})
    session = SimpleNamespace(
        adapter=ScriptedAdapter(["第一轮结论", "续聊结论"]),
        recorder=_Rec(),
        trace_id="thread-v316",
        root_thread_id="thread-v316",
        depth=0,
        agent_path="root",
        sandbox_tier="workspace",
        session_factory=_FakeSession,
        provider_id="",
        provider_name="",
        compactor=None,
        hooks=parent,
        context=SimpleNamespace(
            root=tmp_path,
            task_id="task-v316-continue",
            skills=None,
        ),
    )
    runner = SubAgentRunner().bind(session)
    first = await runner(
        SubAgentRequest(
            goal="核对 README",
            max_steps=3,
            agent_name="fast",
            agent_hooks={"stop": (_spec(command),)},
        )
    )
    assert first.ok, first.error
    second = await runner.send_message(first.agent_id, "接着说")
    assert second.ok, second.error
    assert [row["hook_event_name"] for row in _lines(log)] == [
        "subagent_stop",
        "subagent_stop",
    ]


# --------------------------------------------------------------------------- #
# 4. 端到端：运行时派发后定义钩子只在这个子代理里生效
# --------------------------------------------------------------------------- #

async def test_runtime_definition_stop_hook_does_not_fire_on_parent_turn(
    env, workspace: Path, tmp_path: Path, monkeypatch  # noqa: F811
):
    definition_command, definition_log = _log_hook(tmp_path, "e2e-def")
    parent_command, parent_log = _log_hook(tmp_path, "e2e-parent")
    _hooks_env(monkeypatch, stop=parent_command)
    _write(
        workspace / ".blank" / "agents" / "fast.md",
        _agent(
            name="fast",
            description="快速子代理",
            extra=(
                "hooks:\n"
                "  Stop:\n"
                "    - hooks:\n"
                "        - type: command\n"
                f"          command: '{definition_command}'\n"
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
            {"content": "子代理结论"},
            {"content": "完成"},
        ]
    )

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id}
    )
    assert resp.status_code == 200, resp.text

    definition_rows = _lines(definition_log)
    assert [row["hook_event_name"] for row in definition_rows] == ["subagent_stop"]
    # 父代理自己的 Stop 照常是 Stop；定义钩子没有被带进父会话。
    assert [row["hook_event_name"] for row in _lines(parent_log)] == ["stop"]
