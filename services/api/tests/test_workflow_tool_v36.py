"""Q 批第 5b 项：Workflow 工具对齐 Claude。

判据分四层：

- 脚本契约：meta 必须是纯字面量；名字/描述/阶段有界且可读；正文里出现占位符
  字符串不会被二次替换；长度上限与 NUL 拒绝。
- 编排语义：agent()/parallel()/pipeline()/phase()、args、结构化结果、缓存续跑、
  子代理硬上限；脚本不能联网、不能写文件。
- 工具面：常驻/延迟登记、高风险、别名可检索、task_output/task_stop 认 wf_ 句柄。
- 接线：opt-in 闸门、缺 Node 的结构化缺口、脚本落盘与 git exclude、启动事件、
  结束通知只投递一次、任务收尾取消运行。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.services.agent import workflow as wf
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.runtime import AgentRuntime
from blank_api.services.agent.stall import StallBreaker, structured_missing_tool
from blank_api.services.permissions import READ_ONLY_TOOLS
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.delegate import _task_output, _task_stop
from blank_api.services.tools.registry import (
    ALIASES,
    DEFERRED_TOOLS,
    PARALLEL_SAFE_TOOLS,
    ToolRegistry,
)
from blank_api.services.tools.workflow import NODE_GAP, workflow_tools

from tests.test_agent_v04 import (  # noqa: F401  (env 是端到端夹具)
    API,
    create_project,
    create_provider,
    env,
)

TOOL = {tool.name: tool for tool in workflow_tools()}["workflow"]

SCRIPT_HEAD = (
    "export const meta = {\n"
    "  name: 'demo',\n"
    "  description: '演示脚本',\n"
    "  phases: [{ title: 'Review' }, { title: 'Verify', detail: '复核' }],\n"
    "}\n"
)

_node = wf.find_node()
requires_node = pytest.mark.skipif(not _node, reason="本机没有 Node.js，工作流集成用例跳过")


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def add(self, event: str, phase: str, payload: dict) -> None:
        self.events.append((event, phase, payload))


@pytest.fixture(autouse=True)
def _stub_node_probe(monkeypatch):
    """工具层用例不依赖本机是否装了 Node：探测结果显式固定，缺口的用例再覆盖它。"""
    monkeypatch.setattr(wf, "find_node", lambda: "node")


class FakeRunner:
    """按 goal 返回固定文本的后台子代理；记录每次派发。"""

    def __init__(self, *, slow: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.slow = slow

    async def spawn_background(self, request):
        agent_id = f"agent-{len(self.calls) + 1}"
        self.calls.append((agent_id, request.goal))
        return agent_id, None

    async def output(self, agent_id, block=True, timeout=60.0):
        if self.slow:
            await asyncio.sleep(30)
        await asyncio.sleep(0.005)
        goal = dict(self.calls).get(agent_id, "")
        if "给出 JSON" in goal or "JSON Schema" in goal:
            return {"agent_id": agent_id, "status": "completed", "ok": True, "text": '{"ok": true}'}
        return {
            "agent_id": agent_id,
            "status": "completed",
            "ok": True,
            "text": f"结论[{agent_id}]",
        }

    async def stop(self, agent_id):
        return {"status": "cancelled"}


async def _finish(task_id: str, run, timeout: float = 30.0) -> dict:
    try:
        return await wf.output(task_id, run.workflow_id, block=True, timeout=timeout)
    finally:
        await wf.aclose(task_id)


def _start(script: str, *, task_id: str, runner=None, args=None, cache=None, recorder=None):
    meta, body = wf.parse_meta(script)
    return wf.start_workflow(
        task_id=task_id,
        meta=meta,
        body=body,
        args=args,
        script_path=".blank/workflows/.runs/x.mjs",
        runner=runner or FakeRunner(),
        recorder=recorder,
        node=_node,
        resume_cache=cache,
    )


# --------------------------------------------------------------------------- #
# 1. 脚本契约：meta 纯字面量、有界、可读
# --------------------------------------------------------------------------- #
def test_parse_meta_accepts_canonical_script_and_keeps_body():
    script = SCRIPT_HEAD + "phase('Review')\nreturn 1;\n"
    meta, body = wf.parse_meta(script)
    assert meta["name"] == "demo"
    assert meta["description"] == "演示脚本"
    assert meta["phases"] == [{"title": "Review"}, {"title": "Verify", "detail": "复核"}]
    assert body.lstrip().startswith("phase('Review')")


def test_parse_meta_allows_leading_comments_and_trailing_semicolon():
    script = "// 说明\n/* 块 */\nexport const meta = { name: 'n', description: 'd' };\nreturn 1"
    meta, body = wf.parse_meta(script)
    assert meta == {"name": "n", "description": "d"}
    assert body.startswith("\nreturn 1")


@pytest.mark.parametrize(
    "script, needle",
    [
        ("", "不能为空"),
        ("const x = 1\nexport const meta = { name: 'n', description: 'd' }", "必须以"),
        ("export const meta = { name: n, description: 'd' }", "纯字面量"),
        ("export const meta = { name: 'n', description: `x${y}` }", "纯字面量"),
        ("export const meta = { name: 'n', description: 'd', extra: 1 }", "不认识的字段"),
        ("export const meta = { name: '', description: 'd' }", "name 不能为空"),
        ("export const meta = { name: 'n', description: '' }", "description 不能为空"),
        ("export const meta = { name: 'n', description: 'a\\nb' }", "必须是一行"),
        ("export const meta = { name: 'n', description: 'd', phases: [{}] }", "缺少 title"),
        (
            "export const meta = { name: 'n', description: 'd', phases: "
            "[{ title: 'A' }, { title: 'A' }] }",
            "title 重复",
        ),
        (
            "export const meta = { name: 'n', description: 'd', phases: [{ title: 'A', x: 1 }] }",
            "不认识的字段",
        ),
    ],
)
def test_parse_meta_rejects_broken_scripts_with_a_specific_reason(script, needle):
    with pytest.raises(wf.WorkflowError) as error:
        wf.parse_meta(script)
    assert needle in str(error.value)


def test_parse_meta_rejects_nul_and_oversized_scripts():
    with pytest.raises(wf.WorkflowError) as error:
        wf.parse_meta("export const meta = { name: 'n', description: 'd' }\n\x00")
    assert "NUL" in str(error.value)
    oversized = "export const meta = { name: 'n', description: 'd' }\n" + "x" * wf.MAX_SCRIPT_CHARS
    with pytest.raises(wf.WorkflowError) as error:
        wf.parse_meta(oversized)
    assert "长度上限" in str(error.value)


def test_parse_meta_handles_escapes_and_string_keys():
    script = (
        "export const meta = { \"name\": 'a\\'b', description: \"q\\\"q\", "
        "phases: [{ 'title': 'A\\u0042' }] }"
    )
    meta, _ = wf.parse_meta(script)
    assert meta["name"] == "a'b"
    assert meta["description"] == 'q"q'
    assert meta["phases"] == [{"title": "AB"}]


def test_build_module_does_not_double_substitute_placeholder_text():
    meta = {"name": "n", "description": "d"}
    body = "const a = '__BODY__'; const b = '__META__'; const c = args;"
    module = wf.build_module(meta, body, {"marker": "__ARGS__"})
    assert "'__BODY__'" in module
    assert "'__META__'" in module
    assert '"marker": "__ARGS__"' in module
    assert "\x00" not in module


# --------------------------------------------------------------------------- #
# 2. 结构化结果与缓存键
# --------------------------------------------------------------------------- #
SCHEMA = {
    "type": "object",
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
}


def test_parse_structured_accepts_fenced_and_embedded_json():
    assert wf.parse_structured('```json\n{"ok": true}\n```', SCHEMA) == {"ok": True}
    assert wf.parse_structured('前缀 {"ok": false} 后缀', SCHEMA) == {"ok": False}


@pytest.mark.parametrize(
    "text, needle",
    [
        ("not json", "可解析的 JSON"),
        ('{"nope": 1}', "缺少字段"),
        ('{"ok": "yes"}', "类型不符"),
    ],
)
def test_parse_structured_rejects_shapes_that_do_not_match_schema(text, needle):
    with pytest.raises(wf.WorkflowError) as error:
        wf.parse_structured(text, SCHEMA)
    assert needle in str(error.value)


def test_schema_integer_does_not_accept_boolean():
    with pytest.raises(wf.WorkflowError) as error:
        wf.parse_structured('{"n": true}', {"type": "object", "properties": {"n": {"type": "integer"}}})
    assert "布尔" in str(error.value)


def test_call_key_is_stable_across_dict_order_but_changes_with_input():
    first = wf.call_signature("p", {"b": 2, "a": 1})
    second = wf.call_signature("p", {"a": 1, "b": 2})
    assert first == second
    assert wf.call_signature("p2", {"a": 1, "b": 2}) != first
    assert wf.call_signature("p", {"a": 2, "b": 2}) != first


# --------------------------------------------------------------------------- #
# 3. 脚本来源与落盘
# --------------------------------------------------------------------------- #
def test_read_script_path_rejects_absolute_wrong_suffix_and_escape(tmp_path):
    (tmp_path / "a.mjs").write_text("x", encoding="utf-8")
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path.parent / "outside.mjs").write_text("x", encoding="utf-8")
    assert wf.read_script_path(tmp_path, "a.mjs") == "x"
    for value, needle in [
        (str(tmp_path / "a.mjs"), "工作区相对路径"),
        ("a.txt", ".mjs 或 .js"),
        ("../outside.mjs", "不可用"),
        ("", "不能为空"),
    ]:
        with pytest.raises(wf.WorkflowError) as error:
            wf.read_script_path(tmp_path, value)
        assert needle in str(error.value)


def test_named_scripts_list_and_missing_name_reports_available(tmp_path):
    root = tmp_path / "repo"
    (root / ".blank" / "workflows").mkdir(parents=True)
    (root / ".blank" / "workflows" / "b.mjs").write_text("b", encoding="utf-8")
    (root / ".blank" / "workflows" / "a.mjs").write_text("a", encoding="utf-8")
    assert wf.named_scripts(root) == ["a", "b"]
    assert wf.load_named_script(root, "a") == "a"
    with pytest.raises(wf.WorkflowError) as error:
        wf.load_named_script(root, "missing")
    assert "可用：a、b" in str(error.value)
    with pytest.raises(wf.WorkflowError):
        wf.load_named_script(root, "bad/name")


def test_persist_script_writes_under_runs_and_excludes_it_from_git(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git" / "info").mkdir(parents=True)
    first = wf.persist_script(root, "wf_abcdef123456", "export const meta = { name: 'n', description: 'd' }\n")
    assert first == ".blank/workflows/.runs/wf_abcdef123456.mjs"
    saved = root / first
    assert saved.read_text(encoding="utf-8").startswith("export const meta")
    exclude = (root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert exclude.count(wf.EXCLUDE_LINE) == 1
    # 幂等：再写一次不应重复追加
    wf.persist_script(root, "wf_abcdef123456", "x")
    assert (root / ".git" / "info" / "exclude").read_text(encoding="utf-8").count(
        wf.EXCLUDE_LINE
    ) == 1


def test_ensure_excluded_follows_gitdir_file(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    target = tmp_path / "actual-git"
    target.mkdir()
    (root / ".git").write_text(f"gitdir: {target}\n", encoding="utf-8")
    wf.ensure_excluded(root)
    assert (target / "info" / "exclude").read_text(encoding="utf-8").strip() == wf.EXCLUDE_LINE


# --------------------------------------------------------------------------- #
# 4. 工具面与注册表
# --------------------------------------------------------------------------- #
def test_tool_surface_matches_claude_contract():
    assert TOOL.name == "workflow"
    assert TOOL.risk_level == "high" and TOOL.requires_approval
    schema = TOOL.input_schema
    assert "required" not in schema
    assert schema["additionalProperties"] is False
    props = schema["properties"]
    assert props["script"]["maxLength"] == wf.MAX_SCRIPT_CHARS
    assert props["resumeFromRunId"]["pattern"] == wf.RESUME_PATTERN.pattern
    assert {"script", "name", "description", "title", "args", "scriptPath", "resumeFromRunId"} <= set(props)
    assert "多代理" in TOOL.description and "10 个" in TOOL.description


def test_registry_defers_workflow_and_search_activates_it():
    assert "workflow" in DEFERRED_TOOLS
    assert "workflow" not in PARALLEL_SAFE_TOOLS
    assert "workflow" not in READ_ONLY_TOOLS
    assert "多代理" in ALIASES["workflow"]
    registry = ToolRegistry(build_default_tools())
    assert "workflow" not in registry.visible_names()
    matched, added = registry.search("用工作流多代理编排")
    names = [tool.name for tool in matched]
    assert "workflow" in names
    assert "workflow" in added
    assert "workflow" in registry.visible_names()


# --------------------------------------------------------------------------- #
# 5. 真实 Node 集成：编排语义与边界
# --------------------------------------------------------------------------- #
@requires_node
async def test_real_node_parallel_pipeline_phase_args_and_schema():
    script = SCRIPT_HEAD + """
phase('Review')
const dims = [{ k: 'a' }, { k: 'b' }]
const reviewed = await parallel(dims.map(d => () =>
  agent(`review ${d.k}`, { label: `r:${d.k}`, phase: 'Review' })
))
phase('Verify')
const verified = await pipeline(
  reviewed,
  r => agent(`给出 JSON：verify ${r}`, {
    label: 'v',
    phase: 'Verify',
    schema: { type: 'object', required: ['ok'], properties: { ok: { type: 'boolean' } } },
  }),
)
console.log('reviewed', reviewed.length)
return { reviewed, verified, args }
"""
    task_id = f"wf-task-{uuid.uuid4().hex[:8]}"
    rec = _Recorder()
    run = _start(
        script,
        task_id=task_id,
        runner=FakeRunner(),
        args={"q": [1, 2]},
        recorder=rec,
    )
    snap = await _finish(task_id, run)
    assert snap["status"] == "completed", snap["error"]
    result = json.loads(snap["result"])
    assert result["reviewed"] == ["结论[agent-1]", "结论[agent-2]"]
    assert result["verified"] == [{"ok": True}, {"ok": True}]
    assert result["args"] == {"q": [1, 2]}
    assert snap["phases"] == ["Review", "Verify"]
    assert snap["agents"] == 4 and snap["cached"] == 0
    assert any("reviewed 2" in line for line in snap["logs"])
    names = [event for event, _, _ in rec.events]
    assert names.count("workflow.phase") == 2
    assert names.count("workflow.agent_started") == 4
    assert names.count("workflow.agent_completed") == 4
    assert names[-1] == "workflow.completed"


@requires_node
async def test_real_node_notification_is_delivered_exactly_once():
    task_id = f"wf-task-{uuid.uuid4().hex[:8]}"
    run = _start(SCRIPT_HEAD + "return 'ok'", task_id=task_id)
    try:
        snap = await wf.output(task_id, run.workflow_id, block=True, timeout=30)
        assert snap["status"] == "completed"
        notes = wf.drain_notifications(task_id)
        assert [item["status"] for item in notes] == ["completed"]
        assert notes[0]["result"] == '"ok"'
        assert wf.drain_notifications(task_id) == []
    finally:
        await wf.aclose(task_id)


@requires_node
async def test_real_node_denies_file_writes_and_network():
    task_id = f"wf-task-{uuid.uuid4().hex[:8]}"
    fs_script = SCRIPT_HEAD + """
const fs = await import('node:fs/promises')
await fs.writeFile('escape.txt', 'x')
return 'wrote'
"""
    snap = await _finish(task_id, _start(fs_script, task_id=task_id))
    assert snap["status"] == "failed"
    assert "restricted" in snap["error"] or "ERR_ACCESS_DENIED" in snap["error"]

    net_script = SCRIPT_HEAD + "await fetch('https://example.com')\nreturn 'fetched'"
    snap = await _finish(task_id, _start(net_script, task_id=task_id))
    assert snap["status"] == "failed"
    assert "不能直接联网" in snap["error"]


@requires_node
async def test_real_node_rejects_unknown_phase_and_reports_syntax_errors():
    task_id = f"wf-task-{uuid.uuid4().hex[:8]}"
    snap = await _finish(task_id, _start(SCRIPT_HEAD + "phase('Nope')\nreturn 1", task_id=task_id))
    assert snap["status"] == "failed"
    assert "未知阶段" in snap["error"]

    snap = await _finish(task_id, _start(SCRIPT_HEAD + "const x = ;\n", task_id=task_id))
    assert snap["status"] == "failed"
    assert "退出码 1" in snap["error"]


@requires_node
async def test_real_node_resume_reuses_cached_agent_calls(monkeypatch):
    script = SCRIPT_HEAD + """
const a = await agent('same prompt', { label: 'same' })
const b = await agent('another prompt', { label: 'other' })
return [a, b]
"""
    task_id = f"wf-task-{uuid.uuid4().hex[:8]}"
    first_runner = FakeRunner()
    first = await _finish(task_id, _start(script, task_id=task_id, runner=first_runner))
    assert first["status"] == "completed"
    assert len(first_runner.calls) == 2
    cache = wf.get_cache(first["workflow_id"])
    assert cache and len(cache) == 2

    second_runner = FakeRunner()
    second = await _finish(
        task_id,
        _start(script, task_id=task_id, runner=second_runner, cache=dict(cache)),
    )
    assert second["status"] == "completed"
    assert second["cached"] == 2 and second["agents"] == 0
    assert second_runner.calls == []
    assert second["result"] == first["result"]


@requires_node
async def test_real_node_agent_hard_limit_reports_the_number(monkeypatch):
    monkeypatch.setattr(wf, "MAX_AGENTS", 1)
    script = SCRIPT_HEAD + """
const out = []
for (const i of [1, 2]) {
  try { out.push(await agent(`call ${i}`)) }
  catch (error) { out.push('ERR:' + error.message) }
}
return out
"""
    task_id = f"wf-task-{uuid.uuid4().hex[:8]}"
    snap = await _finish(task_id, _start(script, task_id=task_id))
    assert snap["status"] == "completed", snap["error"]
    result = json.loads(snap["result"])
    assert result[0] == "结论[agent-1]"
    assert result[1].startswith("ERR:") and "最多派 1 个子代理" in result[1]


@requires_node
async def test_stop_cancels_a_running_workflow_and_reports_cancelled():
    task_id = f"wf-task-{uuid.uuid4().hex[:8]}"
    run = _start(SCRIPT_HEAD + "await agent('slow')\nreturn 1", task_id=task_id, runner=FakeRunner(slow=True))
    try:
        await asyncio.sleep(0.05)
        snap = await wf.stop(task_id, run.workflow_id)
        assert snap["status"] == "cancelled"
        routed = await _task_output(
            ToolContext(root=Path("."), task_id=task_id, session_factory=None),
            {"task_id": run.workflow_id, "block": False},
        )
        assert routed.ok is False and "取消" in (routed.message or "")
    finally:
        await wf.aclose(task_id)


@requires_node
async def test_output_and_stop_route_through_task_handles():
    task_id = f"wf-task-{uuid.uuid4().hex[:8]}"
    run = _start(SCRIPT_HEAD + "return 42", task_id=task_id)
    context = ToolContext(root=Path("."), task_id=task_id, session_factory=None)
    try:
        await asyncio.sleep(0.05)
        # 等待与取结果走 task_output（wf_ 前缀路由）
        outcome = await _task_output(context, {"task_id": run.workflow_id, "timeout": 10})
        assert outcome.ok is True
        assert outcome.output.strip() == "42"
        assert outcome.data["workflow_id"] == run.workflow_id
        stopped = await _task_stop(context, {"task_id": run.workflow_id})
        assert stopped.ok is True and "已经结束" in stopped.output
    finally:
        await wf.aclose(task_id)


async def test_unknown_workflow_handles_are_reported_as_not_found():
    context = ToolContext(root=Path("."), task_id="missing", session_factory=None)
    missing = "wf_doesnotexist"
    out = await _task_output(context, {"task_id": missing})
    assert out.ok is False and "没有找到" in (out.message or "")
    stopped = await _task_stop(context, {"task_id": missing})
    assert stopped.ok is False and "没有找到" in (stopped.message or "")
    assert await wf.output("missing", missing, block=False) == {
        "status": "not_found",
        "workflow_id": missing,
        "message": f"没有找到工作流 {missing}（后台句柄只在本进程内有效）",
    }


@requires_node
async def test_aclose_cancels_running_and_clears_bucket():
    task_id = f"wf-task-{uuid.uuid4().hex[:8]}"
    run = _start(SCRIPT_HEAD + "await agent('slow')\nreturn 1", task_id=task_id, runner=FakeRunner(slow=True))
    await asyncio.sleep(0.05)
    assert wf.get_run(task_id, run.workflow_id) is run
    assert await wf.aclose(task_id, reason="task_succeeded") == 1
    assert wf.get_run(task_id, run.workflow_id) is None


def test_notifications_do_not_create_empty_buckets():
    before = len(wf._RUNS)
    assert wf.drain_notifications("never-seen") == []
    assert wf.get_run("never-seen", "wf_x") is None
    assert len(wf._RUNS) == before


def test_node_gap_joins_the_structured_gap_ledger():
    """缺 Node 的回执要和 run_tests 缺工具链同一口径，否则停滞检测认不出来。"""
    gap = {
        "ok": False,
        "status": "skipped",
        "message": NODE_GAP,
        "data": {"missing_command": "node"},
    }
    assert structured_missing_tool(gap) == "node"
    breaker = StallBreaker()
    breaker.record("workflow", {"script": "..."}, gap)
    assert breaker.structured_missing == {"node"}
    # workflow 跑通 = Node 现在在了：结构化缺口整批撤掉，后续 shell 不再被封。
    breaker.record("workflow", {"script": "..."}, {"ok": True, "status": "running"})
    assert breaker.structured_missing == set()


@requires_node
async def test_finishing_the_task_cancels_leftover_workflows(env, workspace):
    from blank_api.db import get_session_factory

    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await _make_task(
        env, project_id, provider_id, title="工作流", goal="用工作流多代理审查"
    )
    factory = get_session_factory()
    run = _start(
        SCRIPT_HEAD + "await agent('slow')\nreturn 1",
        task_id=task_id,
        runner=FakeRunner(slow=True),
    )
    await asyncio.sleep(0.05)
    scope = SimpleNamespace(
        store=factory,
        task_id=task_id,
        recorder=EventRecorder(factory, task_id),
        session=SimpleNamespace(router=None),
        subagents=None,
    )
    await AgentRuntime(factory)._finish(scope, "succeeded", summary="完成", reason="")
    assert wf.get_run(task_id, run.workflow_id) is None, "任务结束后工作流还挂在桶里"
    assert run.task is not None and run.task.done(), "任务结束后脚本任务还在跑"


@requires_node
async def test_waiting_approval_keeps_workflow_alive(env, workspace):
    """等审批不是终点：恢复后还要用同一个句柄取结论，不能在这里取消。"""
    from blank_api.db import get_session_factory

    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await _make_task(
        env, project_id, provider_id, title="工作流", goal="用工作流多代理审查"
    )
    factory = get_session_factory()
    run = _start(
        SCRIPT_HEAD + "await agent('slow')\nreturn 1",
        task_id=task_id,
        runner=FakeRunner(slow=True),
    )
    try:
        await asyncio.sleep(0.05)
        scope = SimpleNamespace(
            store=factory,
            task_id=task_id,
            recorder=EventRecorder(factory, task_id),
            session=SimpleNamespace(router=None),
            subagents=None,
        )
        await AgentRuntime(factory)._finish(
            scope, "waiting_approval", summary="等待批准", reason=""
        )
        assert wf.get_run(task_id, run.workflow_id) is not None
        assert run.task is not None and not run.task.done()
    finally:
        await wf.aclose(task_id)


# --------------------------------------------------------------------------- #
# 6. 工具 handler：opt-in、缺口、来源优先级、落盘与续跑闸门
# --------------------------------------------------------------------------- #
async def _make_task(env, project_id: str, provider_id: str, *, title: str, goal: str) -> str:
    resp = await env.client.post(
        f"{API}/projects/{project_id}/tasks",
        json={
            "title": title,
            "goal": goal,
            "provider_id": provider_id,
            "scenario": "dev",
            "max_steps": 3,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _handler_context(root: Path, task_id: str, runner=None, recorder=None) -> ToolContext:
    from blank_api.db import get_session_factory

    return ToolContext(
        root=root,
        task_id=task_id,
        session_factory=get_session_factory(),
        subagent_runner=runner or FakeRunner(),
        recorder=recorder,
    )


def _stub_start(monkeypatch, captured: dict):
    def fake_start(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            workflow_id=kwargs["workflow_id"],
            name=kwargs["meta"]["name"],
            description=kwargs["meta"]["description"],
        )

    monkeypatch.setattr(wf, "start_workflow", fake_start)


async def test_handler_refuses_when_user_did_not_opt_in(env, workspace, monkeypatch):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await _make_task(
        env, project_id, provider_id, title="看一下 README", goal="读 README 并总结"
    )
    captured: dict = {}
    _stub_start(monkeypatch, captured)
    outcome = await TOOL.handler(
        _handler_context(workspace, task_id),
        {"script": SCRIPT_HEAD + "return 1"},
    )
    assert outcome.ok is False
    assert "明确要求多代理编排" in (outcome.message or "")
    assert captured == {}


async def test_handler_reports_missing_node_as_a_structured_gap(env, workspace, monkeypatch):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await _make_task(
        env, project_id, provider_id, title="多代理审查", goal="用工作流多代理审查仓库"
    )
    monkeypatch.setattr(wf, "find_node", lambda: "")
    outcome = await TOOL.handler(
        _handler_context(workspace, task_id),
        {"script": SCRIPT_HEAD + "return 1"},
    )
    assert outcome.ok is False
    assert outcome.status == "skipped"
    assert outcome.data["missing_command"] == "node"
    assert "BLANK_NODE_BIN" in (outcome.message or "")
    assert (outcome.message or "").startswith(NODE_GAP[:10])


async def test_handler_prefers_script_path_and_persists_the_script(env, workspace, monkeypatch):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await _make_task(
        env, project_id, provider_id, title="多代理", goal="用工作流并行检查"
    )
    source_dir = workspace / ".blank" / "workflows"
    source_dir.mkdir(parents=True)
    (source_dir / "from-disk.mjs").write_text(
        SCRIPT_HEAD + "return 'from-path'\n", encoding="utf-8"
    )
    captured: dict = {}
    _stub_start(monkeypatch, captured)
    recorder = _Recorder()
    outcome = await TOOL.handler(
        _handler_context(workspace, task_id, recorder=recorder),
        {
            "scriptPath": ".blank/workflows/from-disk.mjs",
            "script": SCRIPT_HEAD + "return 'inline'\n",
            "description": "ignored",
            "title": "ignored",
        },
    )
    assert outcome.ok is True, outcome.message
    assert captured["body"].strip() == "return 'from-path'"
    saved = workspace / captured["script_path"]
    assert saved.read_text(encoding="utf-8") == SCRIPT_HEAD + "return 'from-path'\n"
    assert excluded_line_present(workspace)
    assert outcome.data["script_path"] == captured["script_path"]
    assert outcome.data["phases"] == ["Review", "Verify"]
    assert outcome.data["workflow_id"] == captured["workflow_id"]
    assert [event for event, _, _ in recorder.events] == ["workflow.started"]
    started = recorder.events[0][2]
    assert started["source"] == ".blank/workflows/from-disk.mjs"
    assert started["phases"] == ["Review", "Verify"]


async def test_handler_resolves_named_workflow_and_rejects_unknown_name(env, workspace, monkeypatch):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await _make_task(
        env, project_id, provider_id, title="工作流", goal="运行已保存的工作流"
    )
    source_dir = workspace / ".blank" / "workflows"
    source_dir.mkdir(parents=True)
    (source_dir / "saved.mjs").write_text(
        "export const meta = { name: 'saved', description: 'd' }\nreturn 'saved'\n",
        encoding="utf-8",
    )
    captured: dict = {}
    _stub_start(monkeypatch, captured)
    outcome = await TOOL.handler(_handler_context(workspace, task_id), {"name": "saved"})
    assert outcome.ok is True, outcome.message
    assert captured["meta"] == {"name": "saved", "description": "d"}
    assert captured["body"].strip() == "return 'saved'"

    missing = await TOOL.handler(_handler_context(workspace, task_id), {"name": "nope"})
    assert missing.ok is False and "找不到预定义工作流" in (missing.message or "")


async def test_handler_requires_one_of_script_scriptpath_name(env, workspace, monkeypatch):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await _make_task(
        env, project_id, provider_id, title="工作流", goal="用工作流跑一下"
    )
    captured: dict = {}
    _stub_start(monkeypatch, captured)
    outcome = await TOOL.handler(_handler_context(workspace, task_id), {})
    assert outcome.ok is False
    message = outcome.message or ""
    assert "需要 script" in message and "scriptPath" in message and "name" in message
    assert captured == {}


async def test_handler_resume_gates(env, workspace, monkeypatch):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await _make_task(
        env, project_id, provider_id, title="工作流", goal="用工作流续跑"
    )
    captured: dict = {}
    _stub_start(monkeypatch, captured)
    context = _handler_context(workspace, task_id)
    script = {"script": SCRIPT_HEAD + "return 1"}

    bad = await TOOL.handler(context, {**script, "resumeFromRunId": "not-a-run"})
    assert bad.ok is False and "wf_ 开头" in (bad.message or "")

    unknown = await TOOL.handler(context, {**script, "resumeFromRunId": "wf_abcdefgh"})
    assert unknown.ok is False and "没有运行 wf_abcdefgh 的缓存" in (unknown.message or "")

    monkeypatch.setattr(
        wf, "get_run", lambda *_: SimpleNamespace(status="running")
    )
    running = await TOOL.handler(context, {**script, "resumeFromRunId": "wf_abcdefgh"})
    assert running.ok is False and "还在跑" in (running.message or "")

    monkeypatch.setattr(wf, "get_run", lambda *_: SimpleNamespace(status="completed"))
    cached = {"k": {"value": "v"}}
    monkeypatch.setattr(wf, "get_cache", lambda _id: cached)
    ok_run = await TOOL.handler(context, {**script, "resumeFromRunId": "wf_abcdefgh"})
    assert ok_run.ok is True, ok_run.message
    assert captured["resume_cache"] == cached
    assert captured["resume_cache"] is not cached  # 复制：不污染旧运行的缓存


async def test_handler_script_errors_are_returned_before_starting(env, workspace, monkeypatch):
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await _make_task(
        env, project_id, provider_id, title="工作流", goal="用工作流跑一下"
    )
    captured: dict = {}
    _stub_start(monkeypatch, captured)
    outcome = await TOOL.handler(
        _handler_context(workspace, task_id),
        {"script": "export const meta = { name: nope, description: 'd' }"},
    )
    assert outcome.ok is False and "纯字面量" in (outcome.message or "")
    assert captured == {}


def excluded_line_present(root: Path) -> bool:
    exclude = root / ".git" / "info" / "exclude"
    return exclude.is_file() and wf.EXCLUDE_LINE in exclude.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 7. 通知渲染
# --------------------------------------------------------------------------- #
def test_render_workflow_notifications_names_result_and_errors():
    from blank_api.services.agent.graph import render_workflow_notifications

    text = render_workflow_notifications(
        [
            {
                "workflow_id": "wf_abc12345",
                "name": "审查",
                "status": "completed",
                "ok": True,
                "agents": 3,
                "cached": 1,
                "result": '{"confirmed": 2}',
                "error": "",
            },
            {
                "workflow_id": "wf_def67890",
                "name": "构建",
                "status": "failed",
                "ok": False,
                "agents": 1,
                "cached": 0,
                "result": "",
                "error": "脚本失败",
            },
        ]
    )
    assert "wf_abc12345" in text and "wf_def67890" in text
    assert "缓存命中 1" in text
    assert "错误：脚本失败" in text
    assert '结果：{"confirmed": 2}' in text
    assert "task_output" in text
