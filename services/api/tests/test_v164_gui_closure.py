# -*- coding: utf-8 -*-
"""v164 最终闭环：关窗可见、日常不答非所问、文档按类型授权、逆向独立分析区。

这四条都有同一个失败模式：**单看每一层都对，合起来用户看到的是错的**。
写工具禁掉了，但模型照样在说话（答非所问）；任务建出来了，但人关掉窗口就找不回；
文档场景能写文件，于是也能顺手改 main.py。所以这里的断言全部落在"外部可观察的
结果"上：任务状态、交付文本、工具调用行、会话归属，而不是内部的某个标志位。
"""

from __future__ import annotations

import asyncio
import hashlib

from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.task import AgentEvent, Task, ToolCall
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome
from blank_api.services.tools.gateway import ToolGateway

from tests.test_agent_v04 import (  # noqa: F401  (复用阶段3 的夹具)
    API,
    call,
    create_project,
    create_provider,
    env,
)


async def _wait_terminal(task_id: str, seconds: float = 15.0) -> str:
    """等后端自主启动的 run 跑到终态（v0.10.1 §2 的轮询替身）。"""
    deadline = asyncio.get_event_loop().time() + seconds
    while asyncio.get_event_loop().time() < deadline:
        row = await _row(task_id)
        if row.status in ("succeeded", "failed", "cancelled"):
            return str(row.status)
        await asyncio.sleep(0.1)
    return str((await _row(task_id)).status)


async def _run_task(env, project_id: str, goal: str, scenario: str, *, max_steps: int = 6) -> str:
    resp = await env.client.post(
        f"{API}/projects/{project_id}/tasks",
        json={
            "title": goal[:40],
            "goal": goal,
            "acceptance_criteria": [],
            "max_steps": max_steps,
            "scenario": scenario,
        },
    )
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["id"]
    run = await env.client.post(f"{API}/tasks/{task_id}/run", json={})
    assert run.status_code == 200, run.text
    return task_id


async def _row(task_id: str) -> Task:
    async with get_session_factory()() as session:
        row = await session.get(Task, task_id)
        assert row is not None
        return row


async def _tool_rows(task_id: str) -> list[ToolCall]:
    async with get_session_factory()() as session:
        return list(
            (
                await session.execute(select(ToolCall).where(ToolCall.task_id == task_id))
            ).scalars()
        )


async def _events(task_id: str, kind: str) -> list[dict]:
    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(
                    select(AgentEvent.payload)
                    .where(AgentEvent.task_id == task_id, AgentEvent.type == kind)
                    .order_by(AgentEvent.seq)
                )
            ).scalars()
        )
    return [dict(row or {}) for row in rows]


# ── §3 日常场景：动手的请求要明确说不，而不是接着上文乱说 ──────────────────


async def test_daily_write_request_runs_tools(env, workspace):  # noqa: F811
    """日常放开为 Codex 全能 agent：改文件的请求进工具循环、写工具真的跑。"""
    await create_provider(env)
    project_id = await create_project(env, workspace)
    env.mock.reset(
        [
            {"content": "计划：\n1. 修改 config.py"},
            {
                "content": "改好了",
                "tool_calls": [call("apply_patch", {"path": "config.py", "patch": "+++ b/config.py\n@@\n-A\n+B\n"})],
            },
            {"content": "完成"},
        ]
    )
    task_id = await _run_task(env, project_id, "把 config.py 里的 MAX_RETRY 改成 5", "daily")
    row = await _row(task_id)
    assert row.status == "succeeded"
    # 写工具真的被调用，不再有"日常场景拒绝"的记录。
    tools = await _tool_rows(task_id)
    assert [t.name for t in tools if t.name == "apply_patch"]
    assert await _events(task_id, "scenario.refused") == []


async def test_daily_delete_request_runs_tools(env, workspace):  # noqa: F811
    # 删除类动作在日常场景也会进工具循环（是否批准由权限层/审批卡决定，不是场景层拒绝）。
    await create_provider(env)
    project_id = await create_project(env, workspace)
    env.mock.reset([{"content": "计划：\n1. 删除文件"}, {"content": "删了"}, {"content": "完成"}])
    task_id = await _run_task(env, project_id, "把 temp 目录下的旧文件删掉", "daily")
    row = await _row(task_id)
    assert row.status == "succeeded"
    assert await _events(task_id, "scenario.refused") == []


async def test_daily_answer_uses_current_turn_only(env, workspace):  # noqa: F811
    """同一条会话连问两句：第二句的交付必须是第二句的答案，不能复述第一句。"""
    await create_provider(env)
    thread = await env.client.post(
        f"{API}/projects/{await create_project(env, workspace)}/threads",
        json={"title": "日常问答"},
    )
    assert thread.status_code == 201, thread.text
    thread_id = thread.json()["id"]

    env.mock.reset([{"content": '{"intent": "answer", "answer": "第一问的答案：甲"}', "tool_calls": []}])
    first = await env.client.post(
        f"{API}/threads/{thread_id}/turns",
        json={"title": "问题一", "goal": "介绍一下你自己", "scenario": "daily"},
    )
    first_id = first.json()["id"]
    # v0.10.1 §2：后端已就地启动，前端（和这里）不再补一枪 /run——那会变成
    # 第二次执行，把 mock 的脚本步数提前耗光。
    assert await _wait_terminal(first_id) == "succeeded"
    assert "甲" in ((await _row(first_id)).result_summary or "")

    env.mock.reset([{"content": '{"intent": "answer", "answer": "第二问的答案：乙"}', "tool_calls": []}])
    second = await env.client.post(
        f"{API}/threads/{thread_id}/turns",
        json={"title": "问题二", "goal": "再讲一个别的", "scenario": "daily"},
    )
    second_id = second.json()["id"]
    assert await _wait_terminal(second_id) == "succeeded"
    summary = (await _row(second_id)).result_summary or ""
    assert "乙" in summary
    assert "甲" not in summary, "第二轮把上一轮的答案又讲了一遍"


# ── §2 任务必须能追溯回一条会话 ────────────────────────────────────────────


async def test_create_task_binds_conversation(env, workspace):  # noqa: F811
    """独立任务入口也要有会话归属：没有它，重开窗口就无从找回这条任务。"""
    await create_provider(env)
    project_id = await create_project(env, workspace)
    resp = await env.client.post(
        f"{API}/projects/{project_id}/tasks",
        json={"title": "独立任务", "goal": "写个说明", "scenario": "dev"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["thread_id"], "任务没有会话归属"
    # 会话真的存在，而且能读出来。
    detail = await env.client.get(f"{API}/threads/{body['thread_id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["turns"][0]["id"] == body["id"]


async def test_active_tasks_and_status_expose_conversation(env, workspace):  # noqa: F811
    """等审批的任务必须在 active 列表里，并且能从 status 反查到会话。"""
    await create_provider(env)
    project_id = await create_project(env, workspace)
    env.mock.reset(
        [
            {"content": "计划：\n1. 改文件"},
            {
                "content": "开始改",
                "tool_calls": [call("apply_patch", {"path": "a.py", "patch": "+++ b/a.py\n@@\n-x\n+y\n"})],
            },
        ]
    )
    task_id = await _run_task(env, project_id, "修改 a.py 里的常量", "dev")

    async with get_session_factory()() as session:
        row = await session.get(Task, task_id)
        assert row is not None
        row.status = "waiting_approval"
        await session.commit()

    status = (await env.client.get(f"{API}/tasks/{task_id}/status")).json()
    assert status["conversation_id"], "状态接口没有给出会话归属"
    assert status["conversation_id"] == status["conversation_id"].strip()
    assert status["title"]

    active = (await env.client.get(f"{API}/tasks?project_id={project_id}&active=true")).json()
    assert any(row["id"] == task_id for row in active)


# ── §4 文档场景：按文件类型授权，而不是"能写/不能写" ───────────────────────


def test_docs_scenario_writes_markdown_but_not_code():  # noqa: F811
    """文档场景：.md 能写、.py 被网关拒。docs 已不在新任务可选场景里（两场景收敛），
    但历史任务仍按原场景跑完，所以按扩展名授权的能力要在网关层继续成立——
    直接问网关，不经 API 建任务（docs 已被 schemas 收窄，不再接受新建）。
    """
    gateway = ToolGateway([], defer=False, scenario="docs")
    assert gateway._path_allowed("apply_patch", {"path": "README.md", "patch": ""})
    assert gateway._path_allowed("apply_patch", {"path": "docs/guide.txt", "patch": ""})
    assert not gateway._path_allowed("apply_patch", {"path": "main.py", "patch": ""})
    assert not gateway._path_allowed("apply_patch", {"path": "package.json", "patch": ""})


async def test_reverse_scenario_rejects_binary_and_code_writes(env):  # noqa: F811
    """逆向：分析笔记之外的可写类型一律拒绝（这里直接问网关，不跑模型）。"""
    gateway = ToolGateway([], defer=False, scenario="reverse")
    assert not gateway._path_allowed("apply_patch", {"path": "sample.exe", "patch": ""})
    assert not gateway._path_allowed("apply_patch", {"path": "run.py", "patch": ""})
    assert gateway._path_allowed("apply_patch", {"path": "analysis/report.md", "patch": ""})


def test_reverse_writes_go_to_analysis_workspace(workspace, tmp_path, monkeypatch):  # noqa: F811
    """逆向：写根切到独立分析区，不在被分析的工作区里。

    reverse 已不在新任务可选场景里（两场景收敛），走不了 API 建任务，
    所以直接验它依赖的写根解析函数：analysis_root 必须落在工作区之外、按 task 隔离。
    """
    from blank_api.services.agent.analysis import analysis_root

    analysis_dir = tmp_path / "analysis"
    monkeypatch.setenv("BLANK_ANALYSIS_DIR", str(analysis_dir))
    root_a = analysis_root("task-aaa")
    root_b = analysis_root("task-bbb")

    assert root_a.is_dir()
    # 按 task 隔离：两次不同任务不共用目录。
    assert root_a != root_b
    # 落在工作区之外——一份 git status 不会把分析笔记当成被分析仓库的改动。
    assert workspace not in root_a.parents
    assert analysis_dir in root_a.parents


def test_dispatch_snapshot_pins_sandbox_tier():
    """沙箱档位进快照，并且脏数据只能往紧里收。"""
    from blank_api.services.agent.dispatch import ExecutionSnapshot, route

    snapshot = route("看看这个文件", scenario="reverse", sandbox_tier="workspace-net")
    assert snapshot.sandbox_tier == "workspace-net"
    assert snapshot.as_dict()["sandbox_tier"] == "workspace-net"

    dirty = ExecutionSnapshot.from_dict({**snapshot.as_dict(), "sandbox_tier": "root"})
    assert dirty.sandbox_tier == "workspace"
