# -*- coding: utf-8 -*-
"""端到端：切换模型后，下一轮请求必须真的用新模型。

对应升级任务书 §4.2 验收。链路是三段：前端把选中的供应商存进 localStorage 并随
createTurn 传给后端（``Task.provider_id``），run 请求可以不传（用存档）也可以
显式覆盖；模型适配器由 ``_resolve_provider`` 解析。这里用脚本化假模型走完整条图，
断言证据取自 ``task.started`` 事件——它记录了当轮真正使用的 provider 与 model。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.task import AgentEvent

from tests.test_agent_v04 import (  # noqa: F401  (复用阶段3 的夹具)
    API,
    call,
    create_project,
    env,
)


async def _create_task(env, project_id: str, provider_id: str | None) -> str:
    body: dict = {
        "title": "给 main.py 增加输出",
        "goal": "修改 main.py 并运行测试",
        "acceptance_criteria": [],
        "max_steps": 6,
    }
    if provider_id:
        body["provider_id"] = provider_id
    resp = await env.client.post(f"{API}/projects/{project_id}/tasks", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _make_provider(env, model: str) -> str:
    resp = await env.client.post(
        f"{API}/providers",
        json={
            "name": f"vendor-{model}-{uuid.uuid4().hex[:6]}",
            "kind": "openai_compatible",
            "base_url": "https://mock.example/v1",
            "model": model,
            "api_key": "sk-mocksecret123456",
            "supports_tools": True,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _started(task_id: str) -> dict:
    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(
                    select(AgentEvent)
                    .where(AgentEvent.task_id == task_id, AgentEvent.type == "task.started")
                    .order_by(AgentEvent.seq)
                )
            ).scalars()
        )
    assert rows, "任务没有留下 task.started 事件"
    return rows[-1].payload


def _two_calls(env) -> None:
    """两步就收工的最小脚本：调一次只读工具，然后总结完成。"""
    env.mock.reset(
        [
            {"content": "计划：\n1. 查看 README"},
            {"content": "看一下", "tool_calls": [call("read_file", {"path": "README.md"})]},
            {"content": "完成"},
        ]
    )


async def test_task_provider_from_creation_is_used_for_the_run(env, workspace):  # noqa: F811
    """创建时选的模型（前端 createTurn 链路）在本轮 run 真的生效。"""
    project_id = await create_project(env, workspace)
    provider_a = await _make_provider(env, "mock-alpha")
    task_id = await _create_task(env, project_id, provider_a)
    _two_calls(env)

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={})
    assert resp.status_code == 200, resp.text

    started = await _started(task_id)
    assert started["model"] == "mock-alpha", started.get("model")


async def test_run_request_can_override_the_stored_provider(env, workspace):  # noqa: F811
    """run 请求显式指定的供应商优先于任务存档——UI 切换后立即生效的路径。"""
    project_id = await create_project(env, workspace)
    provider_a = await _make_provider(env, "mock-alpha")
    provider_b = await _make_provider(env, "mock-beta")
    task_id = await _create_task(env, project_id, provider_a)
    _two_calls(env)

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run", json={"provider_id": provider_b}
    )
    assert resp.status_code == 200, resp.text

    started = await _started(task_id)
    assert started["model"] == "mock-beta", started.get("model")


async def test_next_turn_uses_the_new_choice_not_the_old_one(env, workspace):  # noqa: F811
    """先跑 A 再切 B：第二轮不能还停在 A。"""
    project_id = await create_project(env, workspace)
    provider_a = await _make_provider(env, "mock-alpha")
    provider_b = await _make_provider(env, "mock-beta")

    first = await _create_task(env, project_id, provider_a)
    _two_calls(env)
    assert (await env.client.post(f"{API}/tasks/{first}/run", json={})).status_code == 200
    assert (await _started(first))["model"] == "mock-alpha"

    second = await _create_task(env, project_id, provider_b)
    _two_calls(env)
    assert (await env.client.post(f"{API}/tasks/{second}/run", json={})).status_code == 200
    assert (await _started(second))["model"] == "mock-beta"
