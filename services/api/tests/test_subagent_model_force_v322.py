"""#322 ``CLAUDE_CODE_SUBAGENT_MODEL_FORCE``：强制所有子代理忽略模型覆盖。

上游取证（``grep -boF`` 现场复核）：

* Agent 工具 schema 在环境变量置位时直接剃掉 ``model``：
  ``let e=...; return a.CLAUDE_CODE_SUBAGENT_MODEL_FORCE?n.omit({model:!0}):n``
  （4302142）；
* 模型解析在置位时先把调用方的 ``n`` 置空：``re(n,e){if(a.CLAUDE_CODE_SUBAGENT_MODEL_FORCE)
  n=void 0; ...}``（36500302）与 ``H(n,e,o="tool"){if(a.CLAUDE_CODE_SUBAGENT_MODEL_FORCE)
  n=void 0; ...}``（36500731）——工具入参与定义里的 ``model`` 一起失效；
* Workflow agent 的覆盖被显式忽略并告警：``Workflow agent model "..." ignored:
  CLAUDE_CODE_SUBAGENT_MODEL_FORCE is set``（4582183）；
* changelog：``Added `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` to apply
  `CLAUDE_CODE_SUBAGENT_MODEL` (or the main model) to every subagent, ignoring
  per-spawn and agent-definition model overrides``（20210432）。

本机没有 ``CLAUDE_CODE_SUBAGENT_MODEL`` 这一层，因此置位后的生效值是「继承父会话模型」
（``request.model == ""``）。判据钉住四层：

1. **schema**：置位时 ``spawn_agent`` 不再暴露 ``model``，批量任务的文档不再声称
   ``model`` 压过定义；未置位时原样保留（两向都判，防止"永远删"也算绿）；
2. **单派**：工具入参 model 被忽略并留下可观测痕迹（``model_ignored`` + 文案），
   定义里的 model 同样被剥离；非字符串不再 fail-closed（它压根不该被使用）；
3. **批量**：每个子任务的 model 都被剥离；
4. **续聊**：第一轮生效值为空，第二轮沿用同一个空覆盖，不会悄悄把 model 捡回来。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blank_api.services.subagent import SubAgentRunner
from blank_api.services.tools.delegate import _parse_tasks, delegate_tools

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
    env,
)
from tests.test_claude_fork_v30 import FakeRunner

ENV = "CLAUDE_CODE_SUBAGENT_MODEL_FORCE"


def _tools() -> dict:
    return {item.name: item for item in delegate_tools()}


def _fast(tmp_path: Path, *, model: str = "mock-fast") -> Path:
    return _write(
        tmp_path / ".blank" / "agents" / "fast.md",
        _agent(name="fast", extra=f"model: {model}\n"),
    )


# --------------------------------------------------------------------------- #
# 1. schema：两向判
# --------------------------------------------------------------------------- #

def test_force_omits_model_from_spawn_agent_schema(monkeypatch):
    monkeypatch.setenv(ENV, "1")
    props = _tools()["spawn_agent"].input_schema["properties"]
    assert "model" not in props, props


def test_without_force_schema_keeps_model(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    props = _tools()["spawn_agent"].input_schema["properties"]
    assert props["model"]["type"] == "string", props


def test_force_rewrites_batch_model_doc(monkeypatch):
    monkeypatch.setenv(ENV, "1")
    tasks = _tools()["spawn_agents"].input_schema["properties"]["tasks"]
    assert ENV in tasks["description"], tasks
    assert "压过" not in tasks["description"], tasks
    monkeypatch.delenv(ENV, raising=False)
    fresh = _tools()["spawn_agents"].input_schema["properties"]["tasks"]
    assert "压过" in fresh["description"], fresh


# --------------------------------------------------------------------------- #
# 2. 单派：工具入参 / 定义 model 都被忽略
# --------------------------------------------------------------------------- #

async def test_force_ignores_tool_model_and_reports_it(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(ENV, "1")
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
    assert runner.requests[-1].model == ""
    assert outcome.data["model_ignored"] == "mock-tool"
    assert ENV in outcome.output, outcome.output


async def test_force_strips_definition_model(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    _fast(tmp_path)
    catalog = _discover(tmp_path)
    runner = FakeRunner()
    outcome = await _tools()["spawn_agent"].handler(
        _context(tmp_path, runner, catalog),
        {"goal": "看看", "subagent_type": "fast", "run_in_background": False},
    )
    assert outcome.ok, outcome.message
    # 定义里写了 mock-fast，但 FORCE 要求每个子代理都继承父会话模型。
    assert runner.requests[-1].model == ""
    assert "model_ignored" not in outcome.data


async def test_force_ignores_non_string_tool_model(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    _fast(tmp_path)
    catalog = _discover(tmp_path)
    runner = FakeRunner()
    outcome = await _tools()["spawn_agent"].handler(
        _context(tmp_path, runner, catalog),
        {"goal": "看看", "subagent_type": "fast", "model": 123, "run_in_background": False},
    )
    # 这个值不会被使用，就不该因为它把整次派发拒掉。
    assert outcome.ok, outcome.message
    assert runner.requests[-1].model == ""


async def test_force_background_spawn_ignores_and_reports(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    _fast(tmp_path)
    catalog = _discover(tmp_path)
    runner = FakeRunner()
    outcome = await _tools()["spawn_agent"].handler(
        _context(tmp_path, runner, catalog),
        {"goal": "看看", "subagent_type": "fast", "model": "mock-tool"},
    )
    assert outcome.ok, outcome.message
    assert runner.background_requests[-1].model == ""
    assert outcome.data["model_ignored"] == "mock-tool"
    assert ENV in outcome.output, outcome.output


# --------------------------------------------------------------------------- #
# 3. 批量：每个子任务的 model 都被剥离
# --------------------------------------------------------------------------- #

def test_force_strips_batch_item_models(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    _fast(tmp_path)
    requests, error = _parse_tasks(
        [
            {"goal": "甲", "subagent_type": "fast", "model": "mock-tool"},
            {"goal": "乙", "subagent_type": "fast", "model": 123},
            {"goal": "丙", "subagent_type": "fast"},
        ],
        _context(tmp_path, FakeRunner(), _discover(tmp_path)),
    )
    assert not error, error
    assert [item.model for item in requests] == ["", "", ""]


# --------------------------------------------------------------------------- #
# 4. 续聊沿用同一个空覆盖
# --------------------------------------------------------------------------- #

async def test_force_survives_continuation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(ENV, "1")
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
    assert adapter.models[:2] == ["", ""]