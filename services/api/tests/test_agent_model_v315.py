"""#315 子代理定义的 `model` 覆盖。

上游 agent schema 的 ``model`` 是自由字符串：``Model override for this agent.
Use `inherit` to match the spawning conversation.``；派发时 ``TD`` 的解析顺序是
「调用方显式模型 > 定义里的 model > 父会话模型」，``inherit`` 取父模型；fork
形态恒继承父模型（``Jt ? "inherit" : we``）。本批移植定义这一层，并把它透传
到子代理的每一次模型调用上。

判据分四层，每条都钉在「拿掉实现就红」的位置：

1. **解析**：``model`` 原样保存、``inherit`` 保留、空白裁掉、非字符串告警并丢弃、
   ``null``/空串静默、插件 agent 同样生效；``model`` 从 ``UNSUPPORTED_FIELDS``
   移除，且不再进 ``ignored``；
2. **请求组装**：``_apply_definition`` 把定义里的 model 带进 ``SubAgentRequest``，
   批量派发同样携带；
3. **模型调用**：子代理的 ``bind_tools`` 真收到 ``model=``；``inherit`` 回落到
   父模型（空覆盖）；fork 忽略覆盖；续聊沿用第一轮的覆盖；
4. **端到端**：运行时派发文件型子代理后，mock 供应商在子代理那次请求里看到的
   ``model`` 是定义值，父代理仍是供应商配置值。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from blank_api.services.agents_defs import (
    UNSUPPORTED_FIELDS,
    load_definition,
)
from blank_api.services.models.base import (
    AdapterConfig,
    ModelAdapter,
    ModelResponse,
    Usage,
)
from blank_api.services.subagent import SubAgentRequest, SubAgentRunner
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
from tests.test_background_subagent_v116 import FakeRecorder
from tests.test_claude_fork_v30 import FakeRunner


class ModelRecordingAdapter(ModelAdapter):
    """记录每次 bind_tools 收到的 model 覆盖；固定回一段结论。"""

    def __init__(self) -> None:
        super().__init__(
            AdapterConfig(
                name="model-v315",
                kind="openai",
                base_url="http://localhost",
                api_key="",
                model="parent-model",
                supports_tools=True,
            )
        )
        self.models: list[str | None] = []

    async def invoke(self, messages, **kwargs) -> ModelResponse:  # pragma: no cover
        return ModelResponse(text="未用", usage=Usage(1, 1), model="parent-model")

    async def stream(self, messages, **kwargs):  # pragma: no cover
        yield ""

    async def bind_tools(self, specs, messages, **kwargs) -> ModelResponse:
        self.models.append(kwargs.get("model"))
        return ModelResponse(text="子代理结论", usage=Usage(5, 2), model="parent-model")


class _EmptyTotals:
    def one(self):
        return (0, 0, 0, 0, 0, 0, 0)


class _FakeSession:
    def add(self, _item) -> None:
        pass

    async def execute(self, _statement):
        return _EmptyTotals()

    async def commit(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _session(root: Path, adapter: ModelAdapter, *, snapshot: list[dict] | None = None):
    return SimpleNamespace(
        adapter=adapter,
        recorder=FakeRecorder(),
        trace_id="thread-v315",
        root_thread_id="thread-v315",
        depth=0,
        agent_path="root",
        sandbox_tier="workspace",
        session_factory=_FakeSession,
        provider_id="",
        provider_name="",
        compactor=None,
        conversation_snapshot=list(snapshot or []),
        context=SimpleNamespace(root=Path(root), task_id="task-v315", skills=None),
    )


# --------------------------------------------------------------------------- #
# 1. 解析
# --------------------------------------------------------------------------- #

def test_parse_model_verbatim(tmp_path: Path):
    path = _write(
        tmp_path / "agents" / "fast.md",
        _agent(name="fast", extra="model: mock-fast\n"),
    )
    definition, problems = load_definition(path)
    assert not problems
    assert definition is not None
    assert definition.model == "mock-fast"
    assert definition.as_dict()["model"] == "mock-fast"


def test_parse_model_inherit_is_kept(tmp_path: Path):
    path = _write(
        tmp_path / "agents" / "inherit.md",
        _agent(name="inherit-agent", extra="model: inherit\n"),
    )
    definition, problems = load_definition(path)
    assert not problems
    assert definition is not None
    # ``inherit`` 是语义值，不能被当成"没写"丢掉——派发时它决定回落父模型。
    assert definition.model == "inherit"
    assert definition.as_dict()["model"] == "inherit"


def test_parse_model_strips_whitespace(tmp_path: Path):
    path = _write(
        tmp_path / "agents" / "spaced.md",
        _agent(name="spaced", extra='model: "  mock-spaced  "\n'),
    )
    definition, problems = load_definition(path)
    assert not problems
    assert definition is not None
    assert definition.model == "mock-spaced"


def test_parse_model_non_string_warns_and_is_dropped(tmp_path: Path):
    path = _write(
        tmp_path / "agents" / "bad.md",
        _agent(name="bad-model", extra="model: 123\n"),
    )
    definition, problems = load_definition(path)
    assert definition is not None
    assert definition.model == ""
    assert any("model" in item for item in problems), problems
    # 告警已点名；它不是"移植了但没用上"的字段，不进 ignored。
    assert "model" not in definition.ignored


def test_parse_model_null_and_empty_are_silent(tmp_path: Path):
    null_path = _write(
        tmp_path / "agents" / "null.md",
        _agent(name="null-model", extra="model:\n"),
    )
    definition, problems = load_definition(null_path)
    assert not problems
    assert definition is not None and definition.model == ""

    empty_path = _write(
        tmp_path / "agents" / "empty.md",
        _agent(name="empty-model", extra='model: ""\n'),
    )
    definition, problems = load_definition(empty_path)
    assert not problems
    assert definition is not None and definition.model == ""


def test_model_no_longer_unsupported(tmp_path: Path):
    assert "model" not in UNSUPPORTED_FIELDS
    path = _write(
        tmp_path / "agents" / "ok.md",
        _agent(name="ok-model", extra="model: mock-fast\n"),
    )
    definition, _ = load_definition(path)
    assert definition is not None
    assert "model" not in definition.ignored
    assert "model" not in definition.as_dict().get("ignored", [])


def test_plugin_agent_model_applies(tmp_path: Path):
    path = _write(
        tmp_path / "agents" / "plug.md",
        "---\nmodel: mock-plugin-model\n---\n插件正文。\n",
    )
    definition, problems = load_definition(path, source="plugin:demo")
    assert definition is not None, problems
    # 上游 Skn 只忽略 permissionMode / hooks / mcpServers；model 照常生效。
    assert definition.model == "mock-plugin-model"
    assert not any("model" in item for item in problems), problems


# --------------------------------------------------------------------------- #
# 2. 请求组装
# --------------------------------------------------------------------------- #

def test_apply_definition_carries_model(tmp_path: Path):
    path = _write(
        tmp_path / "agents" / "fast.md",
        _agent(name="fast", extra="model: mock-fast\n"),
    )
    definition, _ = load_definition(path)
    request = _apply_definition(SubAgentRequest(goal="看看"), definition)
    assert request.model == "mock-fast"


def test_request_normalized_keeps_model():
    request = SubAgentRequest(goal="看看", model="  mock-fast  ").normalized()
    assert request.model == "mock-fast"


async def test_spawn_agent_request_keeps_definition_model(tmp_path: Path):
    _write(
        tmp_path / ".blank" / "agents" / "fast.md",
        _agent(name="fast", extra="model: mock-fast\n"),
    )
    catalog = _discover(tmp_path)
    runner = FakeRunner()
    tools = {item.name: item for item in delegate_tools()}
    outcome = await tools["spawn_agent"].handler(
        _context(tmp_path, runner, catalog),
        {"goal": "看看", "subagent_type": "fast", "run_in_background": False},
    )
    assert outcome.ok, outcome.message
    assert runner.requests[-1].model == "mock-fast"


async def test_spawn_agents_batch_carries_model(tmp_path: Path):
    _write(
        tmp_path / ".blank" / "agents" / "fast.md",
        _agent(name="fast", extra="model: mock-fast\n"),
    )
    catalog = _discover(tmp_path)
    requests, error = _parse_tasks(
        [{"goal": "看看", "subagent_type": "fast"}],
        _context(tmp_path, FakeRunner(), catalog),
    )
    assert not error
    assert requests[0].model == "mock-fast"


# --------------------------------------------------------------------------- #
# 3. 模型调用
# --------------------------------------------------------------------------- #

async def test_subagent_call_passes_model_override(tmp_path: Path):
    adapter = ModelRecordingAdapter()
    runner = SubAgentRunner().bind(_session(tmp_path, adapter))
    result = await runner(SubAgentRequest(goal="看看", model="mock-fast"))
    assert result.ok
    assert adapter.models and adapter.models[0] == "mock-fast"


async def test_inherit_falls_back_to_parent_model(tmp_path: Path):
    adapter = ModelRecordingAdapter()
    runner = SubAgentRunner().bind(_session(tmp_path, adapter))
    result = await runner(SubAgentRequest(goal="看看", model="inherit"))
    assert result.ok
    # ``inherit`` 不把字面量发给供应商：空覆盖 = 用供应商配置的那一个模型。
    assert adapter.models and adapter.models[0] in (None, "")


async def test_fork_ignores_model_override(tmp_path: Path):
    adapter = ModelRecordingAdapter()
    snapshot = [
        {"role": "system", "content": "父会话系统提示"},
        {"role": "user", "content": "修一下"},
        {"role": "assistant", "content": "看过了"},
    ]
    runner = SubAgentRunner().bind(_session(tmp_path, adapter, snapshot=snapshot))
    result = await runner(SubAgentRequest(goal="接着看", fork=True, model="mock-fast"))
    assert result.ok
    # 上游 ``Jt ? "inherit" : we``：fork 恒继承父会话模型。
    assert adapter.models and adapter.models[0] in (None, "")


async def test_started_event_reports_the_effective_override(tmp_path: Path):
    """审计事件报的是"这一轮真用的模型覆盖"：``inherit`` 一律记成空（=父模型）。"""
    adapter = ModelRecordingAdapter()
    session = _session(tmp_path, adapter)
    runner = SubAgentRunner().bind(session)

    assert (await runner(SubAgentRequest(goal="看看", model="mock-fast"))).ok
    assert (await runner(SubAgentRequest(goal="再看", model="inherit"))).ok

    starts = [payload for event, payload in session.recorder.events if event == "subagent.started"]
    assert [item["agent_model"] for item in starts] == ["mock-fast", ""]


async def test_send_message_keeps_model_override(tmp_path: Path):
    adapter = ModelRecordingAdapter()
    runner = SubAgentRunner().bind(_session(tmp_path, adapter))
    first = await runner(SubAgentRequest(goal="看看", model="mock-fast"))
    assert first.ok
    second = await runner.send_message(first.agent_id, "接着说")
    assert second.ok, second.error
    assert adapter.models[0] == "mock-fast"
    assert adapter.models[1] == "mock-fast"


# --------------------------------------------------------------------------- #
# 4. 端到端：运行时派发后供应商真的收到定义里的 model
# --------------------------------------------------------------------------- #

async def test_runtime_subagent_uses_definition_model(env, workspace: Path):
    _write(
        workspace / ".blank" / "agents" / "fast.md",
        _agent(name="fast", description="快速子代理", extra="model: mock-fast\n"),
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
    models = [str(item.get("model") or "") for item in env.mock.requests]
    assert models[0] == "mock-coder", models
    assert "mock-fast" in models, models
    # 子代理那一次之后，父代理回到自己的供应商模型。
    assert models[-1] == "mock-coder", models
