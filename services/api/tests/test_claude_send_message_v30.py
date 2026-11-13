"""P5：子代理续聊（对齐 Claude Code 的 SendMessage）。

Claude Code 的 Agent 工具原文：「Use SendMessage with the agent's ID or name to
continue a previously spawned agent with its context intact; a new Agent call
starts fresh.」这里钉五件事：

1. **续聊保留上下文**：第二次模型调用能看到第一轮的 goal、结论与系统提示，
   且系统提示只出现一次；
2. **新 spawn 不带旧上下文**：并行的两条子代理线程互不串味；
3. **未知句柄如实失败**：没有上下文就直说，不猜、不返回别人的结论；
4. **隔离子代理拒绝续聊**：副本已清理，续聊等于在真工作区里接着答一个它以为
   还在副本里的问题——明确拒绝，而不是悄悄换成真实根；
5. **还在后台跑的先取结论**：running 的句柄不能续聊，提示先 task_output。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from blank_api.services.models.base import AdapterConfig, ModelAdapter, ModelResponse, Usage
from blank_api.services.subagent import SubAgentRunner
from blank_api.services.tools.delegate import delegate_tools
from blank_api.services.tools.registry import ALIASES, DEFERRED_TOOLS, ToolRegistry


class ScriptedAdapter(ModelAdapter):
    """按调用次序返回预设文本，并把每次模型调用看到的完整消息快照留下来。"""

    def __init__(self, texts: list[str], delay: float = 0.0) -> None:
        super().__init__(
            AdapterConfig(
                name="scripted-v30",
                kind="openai",
                base_url="http://localhost",
                api_key="",
                model="scripted-v30",
                supports_tools=False,
            )
        )
        self.texts = list(texts)
        self.delay = delay
        self.calls: list[list[dict]] = []

    async def invoke(self, messages, **kwargs) -> ModelResponse:  # pragma: no cover
        return ModelResponse(text=self.texts[0], usage=Usage(1, 1), model="scripted-v30")

    async def stream(self, messages, **kwargs):  # pragma: no cover
        yield ""

    async def bind_tools(self, specs, messages, **kwargs) -> ModelResponse:
        snapshot = [
            {
                "role": str(item.get("role") or "") if isinstance(item, dict) else getattr(item, "role", ""),
                "content": str(item.get("content") or "")
                if isinstance(item, dict)
                else str(getattr(item, "content", "") or ""),
            }
            for item in messages
        ]
        self.calls.append(snapshot)
        if self.delay:
            await asyncio.sleep(self.delay)
        index = min(len(self.calls) - 1, len(self.texts) - 1)
        return ModelResponse(text=self.texts[index], usage=Usage(5, 2), model="scripted-v30")


class FakeRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.run_id = ""
        self.origin_run_id = ""

    async def add(self, event: str, node: str, payload: dict) -> None:
        self.events.append((event, payload))


def _session(root: Path, adapter: ModelAdapter, *, task_id: str):
    return SimpleNamespace(
        adapter=adapter,
        recorder=FakeRecorder(),
        trace_id="thread-v30",
        root_thread_id="thread-v30",
        depth=0,
        agent_path="root",
        sandbox_tier="workspace",
        session_factory=None,
        provider_id="",
        provider_name="",
        compactor=None,
        turn_effort="",
        context=SimpleNamespace(root=Path(root), task_id=task_id),
    )


def _context(workspace: Path, runner: SubAgentRunner, task_id: str) -> SimpleNamespace:
    return SimpleNamespace(root=Path(workspace), task_id=task_id, subagent_runner=runner)


def _tools() -> dict:
    return {item.name: item for item in delegate_tools()}


# --------------------------------------------------------------------------- #
# 1. 续聊保留上下文
# --------------------------------------------------------------------------- #
async def test_send_message_continues_with_the_previous_context(workspace: Path) -> None:
    task_id = "task-v30-continue"
    adapter = ScriptedAdapter(["第一轮结论：main.py 只有一行", "续聊结论：它导入了 os"])
    session = _session(workspace, adapter, task_id=task_id)
    runner = SubAgentRunner().bind(session)
    tools = _tools()
    try:
        spawned = await tools["spawn_agent"].handler(
            _context(workspace, runner, task_id),
            {"goal": "查一下 main.py", "run_in_background": False},
        )
        assert spawned.ok is True, spawned.output
        agent_id = spawned.data["agent_id"]
        assert "第一轮结论" in spawned.output

        second = await tools["send_message"].handler(
            _context(workspace, runner, task_id),
            {"agent_id": agent_id, "message": "那它导入了什么"},
        )
        assert second.ok is True, second.message
        assert "续聊结论" in second.output
        assert second.data.get("summary") == "续聊结论：它导入了 os"
        assert second.data.get("agent_id") == agent_id

        assert len(adapter.calls) == 2, adapter.calls
        followup = adapter.calls[1]
        # 系统提示只在最前面一次：重复注入会把上一轮的规矩重说一遍。
        assert sum(1 for item in followup if item["role"] == "system") == 1
        joined = "\n".join(item["content"] for item in followup)
        assert "查一下 main.py" in joined
        assert "第一轮结论" in joined
        assert followup[-1]["role"] == "user"
        assert "那它导入了什么" in followup[-1]["content"]
        assert "你在一个临时隔离副本里工作" not in joined
    finally:
        await runner.aclose(task_id)


# --------------------------------------------------------------------------- #
# 2. 新 spawn 是全新上下文
# --------------------------------------------------------------------------- #
async def test_new_spawn_starts_fresh_without_old_context(workspace: Path) -> None:
    task_id = "task-v30-fresh"
    adapter = ScriptedAdapter(["旧任务结论", "新任务结论"])
    session = _session(workspace, adapter, task_id=task_id)
    runner = SubAgentRunner().bind(session)
    tools = _tools()
    try:
        first = await tools["spawn_agent"].handler(
            _context(workspace, runner, task_id),
            {"goal": "旧任务：查 main.py", "run_in_background": False},
        )
        assert first.ok is True
        second = await tools["spawn_agent"].handler(
            _context(workspace, runner, task_id),
            {"goal": "新任务：查 notes.md", "run_in_background": False},
        )
        assert second.ok is True
        assert second.data["agent_id"] != first.data["agent_id"]

        fresh = "\n".join(item["content"] for item in adapter.calls[1])
        assert "旧任务：查 main.py" not in fresh
        assert "旧任务结论" not in fresh
        assert "新任务：查 notes.md" in fresh
    finally:
        await runner.aclose(task_id)


# --------------------------------------------------------------------------- #
# 3. 未知句柄如实失败
# --------------------------------------------------------------------------- #
async def test_unknown_agent_id_fails_honestly(workspace: Path) -> None:
    task_id = "task-v30-unknown"
    session = _session(workspace, ScriptedAdapter(["无"]), task_id=task_id)
    runner = SubAgentRunner().bind(session)
    try:
        result = await runner.send_message("agent-nope", "再问一句")
        assert result.ok is False
        assert "没有该子代理的上下文" in result.error
        assert "agent-nope" in result.error

        outcome = await _tools()["send_message"].handler(
            _context(workspace, runner, task_id),
            {"agent_id": "agent-nope", "message": "再问一句"},
        )
        assert outcome.ok is False
        assert "没有该子代理的上下文" in outcome.message
    finally:
        await runner.aclose(task_id)


# --------------------------------------------------------------------------- #
# 4. 隔离子代理不能续聊
# --------------------------------------------------------------------------- #
async def test_isolated_agent_refuses_followup(workspace: Path) -> None:
    task_id = "task-v30-isolate"
    session = _session(workspace, ScriptedAdapter(["隔离里查完了"]), task_id=task_id)
    runner = SubAgentRunner().bind(session)
    try:
        spawned = await _tools()["spawn_agent"].handler(
            _context(workspace, runner, task_id),
            {"goal": "在副本里查一下", "isolate": True, "run_in_background": False},
        )
        assert spawned.ok is True, spawned.output
        agent_id = spawned.data["agent_id"]

        result = await runner.send_message(agent_id, "继续问")
        assert result.ok is False
        assert "隔离副本" in result.error
        assert "无法续聊" in result.error
    finally:
        await runner.aclose(task_id)


async def test_task_finish_drops_cached_conversations(workspace: Path) -> None:
    """任务收尾之后没有合法的续聊入口；对话缓存必须跟着句柄一起摘掉。"""
    import blank_api.services.subagent as subagent_module

    task_id = "task-v30-aclose"
    session = _session(workspace, ScriptedAdapter(["查完了"]), task_id=task_id)
    runner = SubAgentRunner().bind(session)
    spawned = await _tools()["spawn_agent"].handler(
        _context(workspace, runner, task_id),
        {"goal": "查一下", "run_in_background": False},
    )
    assert spawned.ok is True
    assert task_id in subagent_module._CONVERSATIONS
    await runner.aclose(task_id)
    assert task_id not in subagent_module._CONVERSATIONS


# --------------------------------------------------------------------------- #
# 5. 还在后台跑的句柄先取结论
# --------------------------------------------------------------------------- #
async def test_running_agent_must_be_collected_before_followup(workspace: Path) -> None:
    task_id = "task-v30-running"
    session = _session(workspace, ScriptedAdapter(["慢活结论"], delay=1.0), task_id=task_id)
    runner = SubAgentRunner().bind(session)
    try:
        spawned = await _tools()["spawn_agent"].handler(
            _context(workspace, runner, task_id), {"goal": "慢活"}
        )
        assert spawned.ok is True
        agent_id = spawned.data["agent_id"]

        result = await runner.send_message(agent_id, "接着问")
        assert result.ok is False
        assert "还在后台运行" in result.error
        assert "task_output" in result.error
    finally:
        await runner.aclose(task_id)


# --------------------------------------------------------------------------- #
# 6. 工具面与注册表
# --------------------------------------------------------------------------- #
def test_send_message_is_registered_and_searchable() -> None:
    tools = _tools()
    assert "send_message" in tools
    schema = tools["send_message"].input_schema
    assert set(schema.get("required") or []) == {"agent_id", "message"}
    description = tools["send_message"].description
    assert "全新上下文" in description
    assert "不能续聊" in description

    assert "send_message" in DEFERRED_TOOLS
    assert "续聊" in ALIASES["send_message"]
    registry = ToolRegistry(delegate_tools(), core=[])
    assert "send_message" not in registry.visible_names()
    hits, _added = registry.search("续聊子代理")
    assert "send_message" in {tool.name for tool in hits}


def test_system_prompt_teaches_followup_instead_of_respawning() -> None:
    from blank_api.services.agent.graph import SYSTEM_PROMPT

    assert "send_message" in SYSTEM_PROMPT
    assert "全新上下文" in SYSTEM_PROMPT


def test_followup_usage_is_counted_as_delegated() -> None:
    """续聊花的也是主线程预算之外的钱，必须进委派用量表而不是消失。"""
    from blank_api.services.agent.graph import DELEGATE_TOOLS, _delegated_usage

    assert "send_message" in DELEGATE_TOOLS
    assert _delegated_usage(
        "send_message", {"usage": {"prompt_tokens": 3, "completion_tokens": 2, "latency_ms": 9}}
    ) == {"prompt_tokens": 3, "completion_tokens": 2}
