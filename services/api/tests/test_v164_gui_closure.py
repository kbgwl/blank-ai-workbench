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


async def test_daily_write_request_gets_explicit_refusal(env, workspace):  # noqa: F811
    """日常 + 改文件：给一句确定的拒绝，不进工具循环，不产生任何工具调用。"""
    await create_provider(env)
    project_id = await create_project(env, workspace)
    # 模型脚本里塞一个「改文件」的回合：如果执行层漏进来，这里会留下 apply_patch 行。
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
    assert "日常场景不会直接修改文件" in (row.result_summary or "")
    assert "请切换到开发场景" in (row.result_summary or "")
    # 一个写工具都没跑。
    assert await _tool_rows(task_id) == []
    # 拒绝是显式记录的，不是"悄悄跳过"。
    refused = await _events(task_id, "scenario.refused")
    assert refused and refused[-1]["scenario"] == "daily"


async def test_daily_delete_request_is_refused(env, workspace):  # noqa: F811
    await create_provider(env)
    project_id = await create_project(env, workspace)
    env.mock.reset([{"content": "计划：\n1. 删除文件"}, {"content": "删了"}, {"content": "完成"}])
    task_id = await _run_task(env, project_id, "把 temp 目录下的旧文件删掉", "daily")
    row = await _row(task_id)
    assert row.status == "succeeded"
    assert "日常场景不会直接修改文件" in (row.result_summary or "")
    assert await _tool_rows(task_id) == []


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


async def test_docs_scenario_writes_markdown_but_not_code(env, workspace):  # noqa: F811
    """文档场景：README.md 能改，main.py 被网关拒掉，磁盘上的源码一个字节没动。"""
    await create_provider(env)
    project_id = await create_project(env, workspace)
    readme = workspace / "README.md"
    readme.write_text("旧说明\n", encoding="utf-8")
    code = workspace / "main.py"
    code.write_text("MAX_RETRY = 3\n", encoding="utf-8")

    env.mock.reset(
        [
            {"content": '{"intent": "act", "steps": ["读取 README", "写入说明"]}'},
            {
                "content": "写入",
                "tool_calls": [
                    call("apply_patch", {"path": "README.md", "patch": "+++ b/README.md\n@@ -1,1 +1,1 @@\n-旧说明\n+新说明\n"})
                ],
            },
            {"content": "完成"},
        ]
    )
    ok_task = await _run_task(env, project_id, "更新 README.md 的说明文字", "docs")
    assert "新说明" in readme.read_text(encoding="utf-8")
    ok_rows = await _tool_rows(ok_task)
    assert not [row for row in ok_rows if row.name == "apply_patch" and row.status == "denied"]

    env.mock.reset(
        [
            {"content": '{"intent": "act", "steps": ["读取 main.py", "修改常量"]}'},
            {
                "content": "改常量",
                "tool_calls": [
                    call("apply_patch", {"path": "main.py", "patch": "+++ b/main.py\n@@ -1,1 +1,1 @@\n-MAX_RETRY = 3\n+MAX_RETRY = 5\n"})
                ],
            },
            {"content": "完成"},
        ]
    )
    blocked_task = await _run_task(env, project_id, "把 main.py 里的 MAX_RETRY 改成 5", "docs")
    # 源码没被改：拒绝发生在写之前。
    assert code.read_text(encoding="utf-8") == "MAX_RETRY = 3\n"
    denied = [
        row
        for row in await _tool_rows(blocked_task)
        if row.name == "apply_patch" and row.status == "denied"
    ]
    assert denied, "文档场景没有拦住对 .py 的写入"
    assert "只能写入" in (denied[0].result_summary or "")
    assert "开发场景" in (denied[0].result_summary or "")


async def test_reverse_scenario_rejects_binary_and_code_writes(env):  # noqa: F811
    """逆向：分析笔记之外的可写类型一律拒绝（这里直接问网关，不跑模型）。"""
    gateway = ToolGateway([], defer=False, scenario="reverse")
    assert not gateway._path_allowed("apply_patch", {"path": "sample.exe", "patch": ""})
    assert not gateway._path_allowed("apply_patch", {"path": "run.py", "patch": ""})
    assert gateway._path_allowed("apply_patch", {"path": "analysis/report.md", "patch": ""})


async def test_reverse_writes_go_to_analysis_workspace(env, workspace, tmp_path, monkeypatch):  # noqa: F811
    """逆向：笔记落到独立分析区，原始样本一个字节没动，工作区里没有新增产物。"""
    analysis_dir = tmp_path / "analysis"
    monkeypatch.setenv("BLANK_ANALYSIS_DIR", str(analysis_dir))
    await create_provider(env)
    project_id = await create_project(env, workspace)
    sample = workspace / "sample.py"
    sample.write_text("print('hi')\n", encoding="utf-8")
    before = hashlib.md5(sample.read_bytes()).hexdigest()

    env.mock.reset(
        [
            {"content": '{"intent": "act", "steps": ["读取 sample.py", "写分析笔记"]}'},
            {"content": "先读", "tool_calls": [call("read_file", {"path": "sample.py"})]},
            {
                "content": "写笔记",
                "tool_calls": [
                    call(
                        "apply_patch",
                        {"path": "note.md", "patch": "+++ b/note.md\n@@ -0,0 +1,1 @@\n+# 分析笔记\n"},
                    )
                ],
            },
            {"content": "完成"},
        ]
    )
    task_id = await _run_task(env, project_id, "分析 sample.py 的行为并写分析笔记", "reverse")

    produced = analysis_dir / task_id / "note.md"
    assert produced.exists(), f"分析笔记没有落到独立分析区：{produced}"
    assert "# 分析笔记" in produced.read_text(encoding="utf-8")
    # 原始样本与工作区：没有新增 note.md，样本 md5 不变。
    assert not (workspace / "note.md").exists()
    assert hashlib.md5(sample.read_bytes()).hexdigest() == before


def test_dispatch_snapshot_pins_sandbox_tier():
    """沙箱档位进快照，并且脏数据只能往紧里收。"""
    from blank_api.services.agent.dispatch import ExecutionSnapshot, route

    snapshot = route("看看这个文件", scenario="reverse", sandbox_tier="workspace-net")
    assert snapshot.sandbox_tier == "workspace-net"
    assert snapshot.as_dict()["sandbox_tier"] == "workspace-net"

    dirty = ExecutionSnapshot.from_dict({**snapshot.as_dict(), "sandbox_tier": "root"})
    assert dirty.sandbox_tier == "workspace"
