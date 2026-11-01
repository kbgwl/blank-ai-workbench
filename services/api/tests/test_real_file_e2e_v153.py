# -*- coding: utf-8 -*-
"""真实文件执行端到端（docs/workbuddy-followup-v153.md §8）。

与既有 e2e 的差别：这里断言的是**真实世界的效果**，不是返回值——
- 文件在磁盘上真的被改了（不是只返回了模拟结果）；
- 测试命令真的执行了（真实 pytest 子进程，非沙箱桩）；
- 失败结果没有被包装成成功（第一次 run_tests 的失败如实落账）；
- 取消之后不再有后续写操作。

模型仍然是脚本化假模型（决定「做什么」），但工具全部打真工作区。
"""

from __future__ import annotations

import json

from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.task import FileChange, Task, ToolCall
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.models.registry import build_adapter

from tests.test_agent_v04 import (  # noqa: F401  (复用夹具)
    API,
    call,
    create_provider,
    create_project,
    create_task,
    env,
)

BUGGY_MAIN = "def add(a, b):\n    return a - b\n"
WRONG_FIX = (
    "--- a/main.py\n"
    "+++ b/main.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a - b\n"
    "+    return a * b\n"
)
RIGHT_FIX = (
    "--- a/main.py\n"
    "+++ b/main.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a * b\n"
    "+    return a + b\n"
)
TEST_FILE = "from main import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"

VERDICT = {
    "content": json.dumps(
        {"approved": True, "findings": [], "summary": "测试通过，实现与验收一致"},
        ensure_ascii=False,
    )
}
SUMMARY = {
    "content": json.dumps(
        {
            "summary": "已修复 add 并通过测试",
            "acceptance": [{"criterion": "add(1,2)==3", "covered": True, "evidence": "pytest 1 passed"}],
        },
        ensure_ascii=False,
    )
}


def _seed_project(root) -> None:
    (root / "main.py").write_text(BUGGY_MAIN, encoding="utf-8")
    (root / "test_main.py").write_text(TEST_FILE, encoding="utf-8")


async def _tool_rows(task_id: str) -> list[ToolCall]:
    async with get_session_factory()() as session:
        return list(
            (
                await session.execute(
                    select(ToolCall).where(ToolCall.task_id == task_id).order_by(ToolCall.created_at)
                )
            ).scalars()
        )


async def _change_rows(task_id: str) -> list[FileChange]:
    async with get_session_factory()() as session:
        return list(
            (
                await session.execute(
                    select(FileChange).where(FileChange.task_id == task_id).order_by(FileChange.created_at)
                )
            ).scalars()
        )


async def test_real_file_loop_read_break_test_fix_pass(env, workspace):  # noqa: F811
    """§8 全流程：读 → 改 → 测试失败 → 定位 → 再改 → 测试通过。"""
    _seed_project(workspace)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["add(1,2)==3"], max_steps=12)
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 读 main.py\n2. 修 add\n3. 跑测试"},
            {"content": "先读文件", "tool_calls": [call("read_file", {"path": "main.py"})]},
            {"content": "尝试修复", "tool_calls": [call("apply_patch", {"path": "main.py", "patch": WRONG_FIX})]},
            {"content": "跑一次测试看结果", "tool_calls": [call("run_tests", {})]},
            {
                "content": "测试失败：乘法不对，改为加法",
                "tool_calls": [call("apply_patch", {"path": "main.py", "patch": RIGHT_FIX})],
            },
            {"content": "再跑测试确认", "tool_calls": [call("run_tests", {})]},
            VERDICT,
            SUMMARY,
        ]
    )

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run",
        json={"provider_id": provider_id, "approve_high_risk": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task"]["status"] == "succeeded", body

    # ① 文件在磁盘上真的被修好了（磁盘是唯一真相，返回文本不算数）。
    assert "return a + b" in (workspace / "main.py").read_text(encoding="utf-8")
    assert "return a - b" not in (workspace / "main.py").read_text(encoding="utf-8")

    # ② 测试命令真的执行了两次：第一次失败如实落账，第二次才通过。
    rows = [row for row in await _tool_rows(task_id) if row.name == "run_tests"]
    assert [row.status for row in rows] == ["failed", "ok"], [row.status for row in rows]
    assert rows[0].result_summary, "失败那次必须留下可读的原因"
    # 失败没有被包装：第一次的输出里就是 pytest 的失败摘要。
    assert "1 failed" in (rows[0].result_summary or "") or rows[0].exit_code != 0

    # ③ 文件变更进了账本，diff 与磁盘一致。
    changes = [row for row in await _change_rows(task_id) if row.path.endswith("main.py")]
    assert changes, "改了文件却没有 FileChange 记录"
    assert any("return a + b" in (row.diff or "") for row in changes)


async def test_cancelled_task_writes_nothing_afterwards(env, workspace):  # noqa: F811
    """§8 第 10 步：取消之后，后续的写操作一次都不许发生。"""
    (workspace / "main.py").write_text(BUGGY_MAIN, encoding="utf-8")
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["取消后不再写"], max_steps=4)

    async with get_session_factory()() as session:
        task = await session.get(Task, task_id)
        task.status = "cancelled"
        await session.commit()

    await AgentRuntime(get_session_factory()).run(
        RunRequest(
            task_id=task_id,
            provider_id=provider_id,
            provider_name="mock-vendor",
            adapter=build_adapter(await _provider_row(provider_id), env.mock.factory),
            root=workspace,
            title="取消测试",
            goal="取消测试",
            acceptance_criteria=["取消后不再写"],
            max_steps=4,
        )
    )
    # 磁盘没动。
    assert (workspace / "main.py").read_text(encoding="utf-8") == BUGGY_MAIN
    # 账本里没有任何写类工具的执行记录。
    rows = await _tool_rows(task_id)
    assert all(row.name not in ("apply_patch", "run_shell") for row in rows), [row.name for row in rows]


async def _provider_row(provider_id: str):
    from blank_api.models.model_provider import ModelProvider

    async with get_session_factory()() as session:
        return await session.get(ModelProvider, provider_id)
