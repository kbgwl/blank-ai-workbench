# -*- coding: utf-8 -*-
"""P 批：工具面对齐 Claude Code 的 Glob / Grep（search_code）。

Claude 的工具表里 Glob 与带丰富参数的 Grep 是编程场景最常用的两把锉刀；
这一批把它们补成**能失败的判据**，而不是只多几个 schema 字段：

1. glob 真的按文件名/相对路径匹配，支持花括号与 `**/` 命中根目录；
2. 搜索的三种 output_mode、glob/type 过滤、上下文、忽略大小写、只取命中段、
   跨行、offset/head_limit 都从参数一路走到结果；
3. broker（子进程边界）转发同一批参数——宿主能用什么，隔离通道就得能用什么；
4. ListAgents 能列出 running / completed 两种状态。
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace

from blank_api.services import subagent as subagent_mod
from blank_api.services.agent import graph as graph_module
from blank_api.services.permissions import READ_ONLY_TOOLS as PLAN_READ_ONLY_TOOLS
from blank_api.services.tools import fs_broker
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.delegate import delegate_tools
from blank_api.services.tools.filesystem import _glob_blocking, filesystem_tools
from blank_api.services.tools.registry import CORE_TOOLS, LEAN_TOOLS, PARALLEL_SAFE_TOOLS

from tests.test_agent_v04 import (  # noqa: F401  (env 是夹具，故意导入)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)


def _context(root: Path, **kwargs) -> ToolContext:
    return ToolContext(root=root, task_id="task-glob", session_factory=None, **kwargs)


def _handlers() -> dict:
    return {tool.name: tool for tool in filesystem_tools()}


# ── 1. Glob：注册、匹配口径与预算 ───────────────────────────────────────────


def test_glob_is_registered_in_every_read_only_view():
    """漏登记不会让工具消失，而是让它出现在不该出现的地方（或计划模式用不了）。"""
    assert "glob" in _handlers()
    assert "glob" in CORE_TOOLS and "glob" in LEAN_TOOLS
    assert "glob" in PARALLEL_SAFE_TOOLS
    assert "glob" in PLAN_READ_ONLY_TOOLS
    assert "glob" in graph_module.READONLY_TOOLS


async def test_glob_matches_filenames_and_skips_ignored_directories(workspace: Path):
    outcome = await _handlers()["glob"].handler(_context(workspace), {"pattern": "**/*.py"})
    assert outcome.ok is True, outcome
    assert sorted(outcome.data["files"]) == ["main.py", "src/core.py"]
    assert outcome.data["truncated"] is False

    every = await _handlers()["glob"].handler(_context(workspace), {"pattern": "**/*"})
    assert "logo.png" in every.data["files"]
    assert not any(path.startswith(".git/") for path in every.data["files"]), every.data["files"]


async def test_glob_supports_braces_and_matches_root_level_files(workspace: Path):
    """`**/*.md` 要能命中根部 README.md；`{a,b}` 要展开成两种后缀。"""
    root_md = await _handlers()["glob"].handler(_context(workspace), {"pattern": "**/*.md"})
    assert root_md.data["files"] == ["README.md"], root_md.data

    braces = await _handlers()["glob"].handler(
        _context(workspace), {"pattern": "**/*.{py,md}"}
    )
    assert sorted(braces.data["files"]) == ["README.md", "main.py", "src/core.py"]


async def test_glob_orders_by_mtime_and_honours_limit(workspace: Path):
    later = time.time() + 60
    os.utime(workspace / "main.py", (later, later))
    outcome = await _handlers()["glob"].handler(
        _context(workspace), {"pattern": "**/*.py", "limit": 10}
    )
    assert outcome.data["files"][0] == "main.py", outcome.data

    capped = await _handlers()["glob"].handler(
        _context(workspace), {"pattern": "**/*.py", "limit": 1}
    )
    assert capped.data["count"] == 1
    assert capped.data["truncated"] is True


async def test_glob_requires_a_pattern(workspace: Path):
    outcome = await _handlers()["glob"].handler(_context(workspace), {})
    assert outcome.ok is False and outcome.status == "error"
    assert "pattern" in outcome.message


# ── 2. search_code 的 output_mode 与过滤 ────────────────────────────────────


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_search_code_files_with_matches_returns_one_entry_per_file(workspace: Path):
    _write(workspace / "src" / "other.py", "VALUE = 2\nVALUE = 3\n")
    entries = _search_with(workspace, {"pattern": "VALUE", "output_mode": "files_with_matches"})
    assert sorted(entries.data["files"]) == ["src/core.py", "src/other.py"]
    assert entries.data["count"] == 2


def test_search_code_count_mode_counts_per_file(workspace: Path):
    _write(workspace / "src" / "other.py", "VALUE = 2\nVALUE = 3\n")
    entries = _search_with(workspace, {"pattern": "VALUE", "output_mode": "count"})
    counts = {item["path"]: item["count"] for item in entries.data["counts"]}
    assert counts == {"src/core.py": 1, "src/other.py": 2}
    assert "src/other.py: 2" in entries.output


def test_search_code_rejects_unknown_output_mode(workspace: Path):
    outcome = _search_with(workspace, {"pattern": "VALUE", "output_mode": "summary"})
    assert outcome.ok is False and outcome.status == "error"
    assert "content" in outcome.message and "files_with_matches" in outcome.message


def test_search_code_filters_by_glob_and_type(workspace: Path):
    only_src = _search_with(
        workspace, {"pattern": "VALUE", "glob": "src/*.py"}
    )
    assert [m["path"] for m in only_src.data["matches"]] == ["src/core.py"]

    no_md = _search_with(workspace, {"pattern": "VALUE", "glob": "*.md"})
    assert no_md.data["matches"] == []

    typed = _search_with(workspace, {"pattern": "VALUE", "type": "py"})
    assert [m["path"] for m in typed.data["matches"]] == ["src/core.py"]

    unknown = _search_with(workspace, {"pattern": "VALUE", "type": "brainfuck"})
    assert unknown.ok is False and unknown.status == "error"
    assert "未知文件类型" in unknown.message


def test_search_code_case_insensitive_and_only_matching(workspace: Path):
    lowered = _search_with(workspace, {"pattern": "value"})
    assert lowered.data["matches"] == []

    insensitive = _search_with(workspace, {"pattern": "value", "-i": True})
    assert [m["line"] for m in insensitive.data["matches"]] == [1]

    only = _search_with(workspace, {"pattern": "VALUE", "-o": True})
    assert only.data["matches"][0]["text"] == "VALUE"
    assert "src/core.py:1: VALUE" in only.output


# ── 3. 上下文、跨行与分页 ───────────────────────────────────────────────────


def test_search_code_context_lines_are_rendered_but_not_counted_as_hits(workspace: Path):
    _write(workspace / "src" / "core.py", "BEFORE\nVALUE = 1\nAFTER\n")
    outcome = _search_with(workspace, {"pattern": "VALUE", "-C": 1})
    assert outcome.output.splitlines() == [
        "src/core.py-1- BEFORE",
        "src/core.py:2: VALUE = 1",
        "src/core.py-3- AFTER",
    ]
    # data.matches 只放真命中：带上下文是展示，不是把行数虚报给下游。
    assert outcome.data["count"] == 1
    assert outcome.data["matches"] == [{"path": "src/core.py", "line": 2, "text": "VALUE = 1"}]


def test_search_code_multiline_only_matches_when_enabled(workspace: Path):
    _write(workspace / "src" / "pair.txt", "alpha\nbeta\n")
    args = {"pattern": r"alpha\nbeta", "mode": "regex"}
    assert _search_with(workspace, args).data["matches"] == []

    spans = _search_with(workspace, {**args, "multiline": True})
    assert [(m["path"], m["line"]) for m in spans.data["matches"]] == [("src/pair.txt", 1)]


def test_search_code_offset_and_head_limit_slice_the_result_list(workspace: Path):
    _write(workspace / "src" / "other.py", "VALUE = 2\nVALUE = 3\n")
    page = _search_with(
        workspace, {"pattern": "VALUE", "offset": 1, "head_limit": 1}
    )
    assert page.output.splitlines() == ["src/other.py:1: VALUE = 2"], page.output
    assert page.data["truncated"] is True


def test_search_code_line_numbers_can_be_turned_off(workspace: Path):
    outcome = _search_with(workspace, {"pattern": "VALUE", "-n": False})
    assert outcome.output == "src/core.py: VALUE = 1"


def _search_with(workspace: Path, arguments: dict):
    handler = _handlers()["search_code"].handler
    return asyncio.run(handler(_context(workspace), arguments))


# ── 4. broker 转发：宿主与子进程边界必须是同一套语义 ───────────────────────


def test_broker_dispatches_glob(workspace: Path):
    result = fs_broker._op_dispatch(
        workspace, {"op": "glob", "args": {"pattern": "**/*.{py,md}", "limit": 50}}
    )
    assert sorted(item["path"] for item in result["files"]) == [
        "README.md",
        "main.py",
        "src/core.py",
    ]


def test_broker_forwards_the_new_search_parameters(workspace: Path):
    result = fs_broker._op_dispatch(
        workspace,
        {
            "op": "search",
            "args": {
                "pattern": "value",
                "mode": "literal",
                "path": "",
                "max_matches": 50,
                "case_insensitive": True,
                "output_mode": "files_with_matches",
            },
        },
    )
    assert result["matches"] == [{"path": "src/core.py"}]


def test_broker_compile_regex_respects_case_insensitive(workspace: Path):
    result = fs_broker._op_dispatch(
        workspace,
        {
            "op": "search",
            "args": {"pattern": "value", "mode": "regex", "case_insensitive": True},
        },
    )
    assert [item["path"] for item in result["matches"]] == ["src/core.py"]


# ── 5. ListAgents ───────────────────────────────────────────────────────────


def _runner_for(task_id: str) -> subagent_mod.SubAgentRunner:
    session = SimpleNamespace(context=SimpleNamespace(task_id=task_id))
    return subagent_mod.SubAgentRunner().bind(session)


async def test_list_agents_reports_running_and_completed_handles():
    task_id = "task-list-agents"
    runner = _runner_for(task_id)

    done = subagent_mod._BackgroundAgent(
        agent_id="agent-done",
        task_id=task_id,
        request=subagent_mod.SubAgentRequest(goal="查日志"),
        agent_path="root/sub-1",
        depth=1,
        started_at=time.perf_counter(),
    )
    done.result = subagent_mod.SubAgentResult(ok=True, summary="日志正常", agent_path="root/sub-1")
    done.task = asyncio.create_task(asyncio.sleep(0))

    running = subagent_mod._BackgroundAgent(
        agent_id="agent-running",
        task_id=task_id,
        request=subagent_mod.SubAgentRequest(goal="跑测试"),
        agent_path="root/sub-2",
        depth=1,
        started_at=time.perf_counter(),
    )
    running.task = asyncio.create_task(asyncio.sleep(30))

    bucket = subagent_mod._BACKGROUND.setdefault(task_id, {})
    bucket[done.agent_id] = done
    bucket[running.agent_id] = running
    try:
        await done.task
        items = {item["agent_id"]: item for item in runner.list_agents()}
        assert items["agent-done"]["status"] == "completed"
        assert items["agent-done"]["summary"] == "日志正常"
        assert items["agent-running"]["status"] == "running"
    finally:
        running.task.cancel()
        await asyncio.gather(running.task, return_exceptions=True)
        subagent_mod._BACKGROUND.pop(task_id, None)


async def test_list_agents_tool_renders_status_per_handle():
    task_id = "task-list-agents-tool"
    runner = _runner_for(task_id)
    handle = subagent_mod._BackgroundAgent(
        agent_id="agent-1",
        task_id=task_id,
        request=subagent_mod.SubAgentRequest(goal="查配置"),
        agent_path="root/sub-1",
        depth=1,
        started_at=time.perf_counter(),
    )
    handle.result = subagent_mod.SubAgentResult(ok=True, summary="配置正常")
    handle.task = asyncio.create_task(asyncio.sleep(0))
    subagent_mod._BACKGROUND.setdefault(task_id, {})[handle.agent_id] = handle
    try:
        await handle.task
        tool = {item.name: item for item in delegate_tools()}["list_agents"]
        context = ToolContext(
            root=Path("."), task_id=task_id, session_factory=None, subagent_runner=runner
        )
        outcome = await tool.handler(context, {})
        assert outcome.ok is True
        assert "agent-1" in outcome.output and "[completed]" in outcome.output
        assert outcome.data["agents"][0]["status"] == "completed"
    finally:
        subagent_mod._BACKGROUND.pop(task_id, None)


async def test_list_agents_without_a_runner_fails_explicitly():
    tool = {item.name: item for item in delegate_tools()}["list_agents"]
    context = ToolContext(root=Path("."), task_id="t", session_factory=None)
    outcome = await tool.handler(context, {})
    assert outcome.ok is False and "没有启用后台子代理" in outcome.message


# ── 6. 端到端：模型真的能调到 glob ─────────────────────────────────────────


async def test_the_model_can_call_glob_end_to_end(env, workspace: Path):  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["找出所有 Python 文件"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 找 Python 文件"},
            {"content": "找一下", "tool_calls": [call("glob", {"pattern": "**/*.py"})]},
            {"content": "共有 main.py 与 src/core.py 两个 Python 文件。"},
        ]
    )

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "succeeded", resp.json()

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    completed = [
        event for event in events
        if event["type"] == "tool.completed" and event["payload"].get("tool") == "glob"
    ]
    assert completed, [event["type"] for event in events]
    assert "main.py" in str(completed[0]["payload"].get("summary") or completed[0]["payload"])
