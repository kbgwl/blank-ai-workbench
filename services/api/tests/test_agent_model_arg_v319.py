"""#319 Agent 工具入参的 ``model`` 覆盖。

上游 Agent 工具 schema（``subagent_type`` 4299449 附近）里的 ``model`` 是可选字符串：
``Optional model override for this agent. Takes precedence over the agent definition's
model frontmatter and the configured default subagent model. If omitted, uses the agent
definition's model, else the default (inherits from the parent...). Ignored for
subagent_type: "fork"``（4299605 / 4299889）。运行期在同一处把工具模型带进派发，
fork 形态直接丢弃（``model:Jt?void 0:we``，4311121）。

#315 只移植了「定义里的 model」这一层；本批判据钉住工具入参这一层：

1. **schema**：``spawn_agent`` 暴露 ``model``，批量任务的文档列出 ``model``；
2. **优先级**：入参 ``model`` 压过定义 ``model``；不传时仍用定义；显式空串 /
   ``inherit`` 表示回落父会话（定义值被压掉）；非字符串 fail-closed；
3. **fork**：入参 model 被忽略（fork 恒继承父模型）；
4. **批量与续聊**：每个子任务各自携带 model，续聊沿用第一轮的生效值；
5. **端到端**：mock 供应商在子代理那次请求里看到的 ``model`` 是工具入参值。
"""

from __future__ import annotations

import json
from pathlib import Path

from blank_api.services.subagent import SubAgentRequest, SubAgentRunner
from blank_api.services.tools.delegate import (
    _parse_tasks,
    delegate_tools,
)

from tests.test_agent_defs_v44 import (  # noqa: F401  (_agent/_write/_discover/_context 复用)
    _agent,
    _context,
    _discover,
    _write,
)
from tests.test_agent_model_v315 import (  # noqa: F401  (env 等夹具)
    ModelRecordingAdapter,
    _session,
)
from tests.test_agent_v04 import (  # noqa: F401  (env 等夹具)
    API,
    create_provider,
    create_project,
    create_task,
    env,
)
from tests.test_claude_fork_v30 import FakeRunner


def _tools() -> dict:
    return {item.name: item for item in delegate_tools()}


def _fast(tmp_path: Path, *, model: str = "mock-fast") -> Path:
    return _write(
        tmp_path / ".blank" / "agents" / "fast.md",
        _agent(name="fast", extra=f"model: {model}\n"),
    )


# --------------------------------------------------------------------------- #
# 1. schema
# --------------------------------------------------------------------------- #

def test_spawn_agent_schema_exposes_model():
    props = _tools()["spawn_agent"].input_schema["properties"]
    assert props["model"]["type"] == "string", props


def test_spawn_agents_schema_mentions_model():
    tasks = _tools()["spawn_agents"].input_schema["properties"]["tasks"]
    assert "model" in tasks["description"], tasks


# --------------------------------------------------------------------------- #
# 2. 优先级与边界
# --------------------------------------------------------------------------- #

async def test_tool_model_overrides_definition(tmp_path: Path):
    _fast(tmp_path)
    catalog = _discover(tmp_path)
    runner = FakeRunner()
    outcome = await _tools()["spawn_agent"].handler(
        _context(tmp_path, runner, catalog),
        {
            "goal": "看看",
            "subagent_type": "fast",
            "model": "mock-tool",
            "run_in_background": False,
        },
    )
    assert outcome.ok, outcome.message
    assert runner.requests[-1].model == "mock-tool"


async def test_omitted_model_keeps_definition(tmp_path: Path):
    _fast(tmp_path)
    catalog = _discover(tmp_path)
    runner = FakeRunner()
    outcome = await _tools()["spawn_agent"].handler(
        _context(tmp_path, runner, catalog),
        {"goal": "看看", "subagent_type": "fast", "run_in_background": False},
    )
    assert outcome.ok, outcome.message
    assert runner.requests[-1].model == "mock-fast"


async def test_empty_tool_model_falls_back_to_parent(tmp_path: Path):
    _fast(tmp_path)
    catalog = _discover(tmp_path)
    runner = FakeRunner()
    outcome = await _tools()["spawn_agent"].handler(
        _context(tmp_path, runner, catalog),
        {
            "goal": "看看",
            "subagent_type": "fast",
            "model": "",
            "run_in_background": False,
        },
    )
    assert outcome.ok, outcome.message
    # 显式空串 = 回落父会话模型，定义里的 mock-fast 被压掉。
    assert runner.requests[-1].model == ""


async def test_inherit_tool_model_is_kept(tmp_path: Path):
    _fast(tmp_path)
    catalog = _discover(tmp_path)
    runner = FakeRunner()
    outcome = await _tools()["spawn_agent"].handler(
        _context(tmp_path, runner, catalog),
        {
            "goal": "看看",
            "subagent_type": "fast",
            "model": "  inherit  ",
            "run_in_background": False,
        },
    )
    assert outcome.ok, outcome.message
    assert runner.requests[-1].model == "inherit"


async def test_non_string_model_fails_closed(tmp_path: Path):
    _fast(tmp_path)
    catalog = _discover(tmp_path)
    runner = FakeRunner()
    outcome = await _tools()["spawn_agent"].handler(
        _context(tmp_path, runner, catalog),
        {"goal": "看看", "subagent_type": "fast", "model": 123, "run_in_background": False},
    )
    assert not outcome.ok
    assert "model" in outcome.message
    # 参数错了就不该派出子代理。
    assert runner.requests == []


# --------------------------------------------------------------------------- #
# 3. fork 忽略工具模型
# --------------------------------------------------------------------------- #

async def test_fork_ignores_tool_model(tmp_path: Path):
    catalog = _discover(tmp_path)
    runner = FakeRunner()
    outcome = await _tools()["spawn_agent"].handler(
        _context(tmp_path, runner, catalog),
        {"goal": "接着看", "subagent_type": "fork", "model": "mock-tool"},
    )
    assert outcome.ok, outcome.message
    request = runner.requests[-1]
    assert request.fork is True
    assert request.model == ""


# --------------------------------------------------------------------------- #
# 4. 批量与续聊
# --------------------------------------------------------------------------- #

def test_parse_tasks_carries_per_task_model(tmp_path: Path):
    _fast(tmp_path)
    requests, error = _parse_tasks(
        [
            {"goal": "甲", "subagent_type": "fast", "model": "mock-tool"},
            {"goal": "乙", "subagent_type": "fast"},
        ],
        _context(tmp_path, FakeRunner(), _discover(tmp_path)),
    )
    assert not error
    assert [item.model for item in requests] == ["mock-tool", "mock-fast"]


def test_parse_tasks_rejects_non_string_model(tmp_path: Path):
    requests, error = _parse_tasks(
        [{"goal": "甲", "model": 123}],
        _context(tmp_path, FakeRunner(), _discover(tmp_path)),
    )
    assert requests == []
    assert "model" in error


async def test_tool_model_survives_continuation(tmp_path: Path):
    _fast(tmp_path)
    catalog = _discover(tmp_path)
    adapter = ModelRecordingAdapter()
    runner = SubAgentRunner().bind(_session(tmp_path, adapter))
    outcome = await _tools()["spawn_agent"].handler(
        _context(tmp_path, runner, catalog),
        {
            "goal": "看看",
            "subagent_type": "fast",
            "model": "mock-tool",
            "run_in_background": False,
        },
    )
    assert outcome.ok, outcome.message
    agent_id = outcome.data["agent_id"]
    second = await runner.send_message(agent_id, "接着说")
    assert second.ok, second.error
    assert adapter.models[:2] == ["mock-tool", "mock-tool"]


# --------------------------------------------------------------------------- #
# 5. 端到端
# --------------------------------------------------------------------------- #

async def test_runtime_subagent_uses_tool_model(env, workspace: Path):
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
                                    "model": "mock-tool-model",
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
    assert "mock-tool-model" in models, models
    # 工具入参压过定义：子代理那次不该再发 mock-fast。
    assert "mock-fast" not in models, models
    assert models[-1] == "mock-coder", models
