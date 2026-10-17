"""端到端：模型自己决定加载工具，然后真的用上了。

单元测试只能证明"注册表会激活"，证明不了"下一轮模型调用真的看得见它"。
这里用脚本化的假模型走完整条图：计划 → tool_search → 调用被加载的工具 → 总结，
并检查事件日志里留下了"当时有哪些工具可见、哪些还没加载"的证据。
"""

from __future__ import annotations

from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.task import AgentEvent

from tests.test_agent_v04 import (  # noqa: F401  (复用阶段3 的夹具)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)


async def _events(task_id: str, event_type: str) -> list[AgentEvent]:
    async with get_session_factory()() as session:
        return list(
            (
                await session.execute(
                    select(AgentEvent)
                    .where(AgentEvent.task_id == task_id, AgentEvent.type == event_type)
                    .order_by(AgentEvent.seq)
                )
            ).scalars()
        )


async def test_model_loads_git_through_tool_search_then_uses_it(env, workspace):  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["查看版本库状态"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 加载 git 能力\n2. 查看状态"},
            {
                "content": "先加载版本控制能力",
                "tool_calls": [call("tool_search", {"query": "提交"})],
            },
            {
                "content": "现在查看状态",
                "tool_calls": [call("git", {"subcommand": "status"})],
            },
            {"content": "已查看版本库状态"},
            {"content": "完成"},
        ]
    )

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text

    # 1) 开局的下发列表里 git 不在，但被登记为"待加载"
    started = (await _events(task_id, "task.started"))[-1].payload
    assert "git" in started["tools_deferred"]
    assert "git" not in started["tools"]
    assert "tool_search" in started["tools"]

    # 2) tool_search 的结果里带了激活清单和可直接调用的 schema
    calls = await env.tool_calls(task_id)
    search = [row for row in calls if row.name == "tool_search"]
    assert search, "模型没有真的调到 tool_search"
    assert search[0].result_summary is not None

    # 3) git 真的被执行过（不是"未注册的工具"）
    git_rows = [row for row in calls if row.name == "git"]
    assert git_rows, "加载之后模型仍然没能调用 git"
    # 工作区是假 .git，git 自己可能报错；这里要确认的是"调用被受理并执行了"，
    # 而不是被当成未注册工具挡下来
    assert "未注册" not in (git_rows[0].result_summary or "")
    assert git_rows[0].status in ("ok", "failed", "error")


async def test_task_started_records_the_water_line(env, workspace):  # noqa: F811
    """水位必须写进事件：出问题时能回答"当时按多大的窗口在算"。"""
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, [])
    env.mock.reset([{"content": "计划：\n1. 查看 README"}, {"content": "完成"}, {"content": "完成"}])

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    started = (await _events(task_id, "task.started"))[-1].payload
    compaction = started["compaction"]
    assert compaction["trigger_ratio"] > 0
    assert compaction["reserve_tokens"] > 0
    # 窗口未知时为 0：说明这次是按字符预算判定的，不是装作知道窗口
    assert compaction["context_window"] >= 0
    assert compaction["trigger_at"] >= 0
