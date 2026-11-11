"""P4：子代理默认后台跑（对齐 Claude Code 的 Agent 工具）。

Claude Code 的 Agent 工具原文：「Subagents run in the background by default;
you'll be notified when one completes. Pass `run_in_background: false` only when
your very next action depends on the result.」这里钉五件事：

1. **不传参就是后台**：立即拿到 agent_id，工具回执只说「还在跑」，不能把派出
   读成完成，也不能把子代理的结论提前塞进回执；
2. **显式关掉才同步**：`run_in_background=false`（旧名 `background=false`）等到
   结论再返回；
3. **收尾有界等待**：任务写交付之前先给后台子代理一段窗口，窗口内结束的结论
   必须进交付提示词，不能让收尾的 aclose 直接取消掉；
4. **等不到就点名**：窗口过完还在跑的显式取消，并在交付正文里写明哪几个被取消；
5. **不重复投递**：收尾已经给过的结论不再作为「后台已结束」通知重发一次。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import blank_api.services.subagent as subagent_module
from blank_api.services.agent.graph import summarize_result_node
from blank_api.services.subagent import SubAgentRequest, SubAgentRunner
from blank_api.services.tools.delegate import delegate_tools

from tests.test_background_subagent_v116 import SlowAdapter, _session  # noqa: F401  (env 是夹具)


class GoalAwareAdapter(SlowAdapter):
    """同一个会话里的两个子代理共用一个适配器；按 goal 里的「快」决定跑多久。

    收尾窗口的判据需要「一个已经结束、一个还在跑」，共享适配器没法靠构造参数区分，
    所以让适配器自己看子代理的目标。
    """

    async def bind_tools(self, specs, messages, **kwargs):
        body = " ".join(
            str(item.get("content") or "")
            if isinstance(item, dict)
            else str(getattr(item, "content", "") or "")
            for item in messages
        )
        self.delay = 0.05 if "快" in body else 1.0
        return await super().bind_tools(specs, messages, **kwargs)


def _spawn_tool():
    return {item.name: item for item in delegate_tools()}["spawn_agent"]


def _context(workspace: Path, runner: SubAgentRunner, task_id: str = "task-v29") -> SimpleNamespace:
    return SimpleNamespace(root=Path(workspace), task_id=task_id, subagent_runner=runner)


class CaptureSession:
    """只做 summarize 需要的那几件事：记事件、接住提示词、给一段交付正文。"""

    def __init__(self, runner: SubAgentRunner, text: str = "交付正文") -> None:
        self.run_id = "run-v29"
        self.readonly = True
        self.acceptance_criteria: list[str] = []
        self.acceptance_links: list[dict] = []
        self.recorded_questions: list[str] = []
        self.asked_facts = False
        self.events: list[tuple[str, dict]] = []
        self.prompts: list[str] = []
        self._text = text
        self.context = SimpleNamespace(subagent_runner=runner)
        self.recorder = self

    async def add(self, event: str, node: str, payload: dict) -> None:
        self.events.append((event, payload))

    async def ask(self, state, prompt: str, node: str, *, messages=None) -> dict:
        self.prompts.append(prompt)
        return {"text": self._text, "usage": {}, "messages": []}


# --------------------------------------------------------------------------- #
# 1. 默认后台
# --------------------------------------------------------------------------- #
async def test_spawn_agent_without_arguments_runs_in_background(workspace: Path) -> None:
    session = _session(workspace, SlowAdapter(1.0, text="子代理结论"), task_id="task-v29")
    runner = SubAgentRunner().bind(session)
    tool = _spawn_tool()
    try:
        started = time.perf_counter()
        outcome = await tool.handler(_context(workspace, runner), {"goal": "后台查清楚"})
        elapsed = time.perf_counter() - started
        # 子代理要跑 1 秒；不传参就必须立刻回来，否则「默认后台」只是换了措辞。
        assert outcome.ok is True, outcome.output
        assert elapsed < 0.5, elapsed
        assert outcome.data["status"] == "running"
        agent_id = outcome.data["agent_id"]
        # 派出 ≠ 完成：回执必须说清去哪儿取，且不能提前出现子代理的结论。
        assert "task_output" in outcome.output
        assert "还在运行" in outcome.output
        assert "子代理结论" not in outcome.output

        done = await runner.output(agent_id, block=True, timeout=10)
        assert done["status"] == "completed"
    finally:
        await runner.aclose("task-v29")


async def test_run_in_background_false_still_waits_for_the_conclusion(workspace: Path) -> None:
    session = _session(workspace, SlowAdapter(0.2, text="同步结论"), task_id="task-v29")
    runner = SubAgentRunner().bind(session)
    tool = _spawn_tool()
    try:
        started = time.perf_counter()
        outcome = await tool.handler(
            _context(workspace, runner),
            {"goal": "马上要结论", "run_in_background": False},
        )
        elapsed = time.perf_counter() - started
        assert outcome.ok is True, outcome.output
        assert elapsed >= 0.15, elapsed
        assert "同步结论" in outcome.output
        assert outcome.data.get("summary") == "同步结论"
        assert outcome.data.get("status") != "running"
    finally:
        await runner.aclose("task-v29")


async def test_old_background_false_alias_is_still_synchronous(workspace: Path) -> None:
    session = _session(workspace, SlowAdapter(0.15, text="旧参数结论"), task_id="task-v29")
    runner = SubAgentRunner().bind(session)
    tool = _spawn_tool()
    try:
        outcome = await tool.handler(
            _context(workspace, runner), {"goal": "旧参数名", "background": False}
        )
        assert outcome.ok is True, outcome.output
        assert "旧参数结论" in outcome.output
    finally:
        await runner.aclose("task-v29")


# --------------------------------------------------------------------------- #
# 2. 收尾窗口
# --------------------------------------------------------------------------- #
async def test_settle_background_keeps_finished_and_cancels_running(workspace: Path) -> None:
    session = _session(workspace, GoalAwareAdapter(0.05, text="快速结论"), task_id="task-v29")
    runner = SubAgentRunner().bind(session)
    try:
        fast, failure = await runner.spawn_background(SubAgentRequest(goal="快活", max_steps=1))
        assert failure is None and fast
        slow, failure = await runner.spawn_background(
            SubAgentRequest(goal="慢活", max_steps=1, timeout_seconds=120)
        )
        assert failure is None and slow
        # 窗口 0.3 秒：快活 0.05 秒结束，慢活要 1 秒——一定落在窗口外。
        outcome = await runner.settle_background(timeout=0.3)
        finished_ids = {item["agent_id"] for item in outcome["finished"]}
        cancelled_ids = {item["agent_id"] for item in outcome["cancelled"]}
        assert fast in finished_ids, outcome
        assert slow in cancelled_ids, outcome
        fast_item = next(item for item in outcome["finished"] if item["agent_id"] == fast)
        assert fast_item["status"] == "completed"
        assert fast_item["summary"] == "快速结论"
        slow_item = next(item for item in outcome["cancelled"] if item["agent_id"] == slow)
        assert slow_item["status"] == "cancelled"
        # 已经交付过的结论不再作为「后台已结束」通知重复投递。
        assert runner.drain_notifications() == []
    finally:
        await runner.aclose("task-v29")


async def test_summarize_injects_settled_conclusion_and_names_cancelled(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 窗口调到 0.3 秒：判据要跑得快，但取消路径必须是真发生的。
    monkeypatch.setattr(subagent_module, "BACKGROUND_SETTLE_SECONDS", 0.3)
    session = _session(workspace, GoalAwareAdapter(0.05, text="本地文件的结论"), task_id="task-v29")
    runner = SubAgentRunner().bind(session)
    capture = CaptureSession(runner)
    try:
        fast, _ = await runner.spawn_background(SubAgentRequest(goal="快查入口", max_steps=1))
        slow, _ = await runner.spawn_background(
            SubAgentRequest(goal="慢慢查", max_steps=1, timeout_seconds=120)
        )
        # 先等快活结束，确保 summarize 时它是「已完成」而不是「仍在跑」。
        await runner.output(fast, block=True, timeout=10)

        result = await summarize_result_node(
            {"messages": [], "tool_results": [], "usage": {}, "readonly": True}, capture
        )

        assert capture.prompts, "summarize 必须调用模型"
        prompt = capture.prompts[0]
        assert "本地文件的结论" in prompt, prompt[:2000]
        assert fast in prompt
        assert "已被取消" in prompt
        assert slow in prompt

        # 交付正文里点名被取消的子代理——模型漏写也不影响用户看到事实。
        summary = result["summary"]
        assert "已取消" in summary
        assert slow in summary

        settled = [payload for event, payload in capture.events if event == "subagent.settled"]
        assert settled, [event for event, _ in capture.events]
        assert fast in settled[0]["finished"]
        assert slow in settled[0]["cancelled"]
    finally:
        await runner.aclose("task-v29")


async def test_summarize_without_background_agents_is_unchanged(workspace: Path) -> None:
    """没有后台子代理时不得凭空多出一段「后台结论」。"""
    session = _session(workspace, SlowAdapter(0.01), task_id="task-v29")
    runner = SubAgentRunner().bind(session)
    capture = CaptureSession(runner, text="纯回答")
    result = await summarize_result_node(
        {"messages": [], "tool_results": [], "usage": {}, "readonly": True}, capture
    )
    assert result["summary"] == "纯回答"
    assert all("后台子代理" not in prompt for prompt in capture.prompts)
    assert not [event for event, _ in capture.events if event == "subagent.settled"]
