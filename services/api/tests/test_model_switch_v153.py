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

async def _make_thread(env, project_id: str) -> str:
    resp = await env.client.post(f"{API}/projects/{project_id}/threads", json={})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _add_turn(env, thread_id: str, title: str, goal: str, provider_id: str | None) -> str:
    # v0.10.1 起后端在创建轮次时就地启动（auto_start 监督者判定）。这几个用例
    # 验证的正是手动 /run 的行为，必须显式关掉自动启动，否则随后的 /run 撞 409。
    body: dict = {"title": title, "goal": goal, "acceptance_criteria": [], "max_steps": 6, "auto_start": False}
    if provider_id:
        body["provider_id"] = provider_id
    resp = await env.client.post(f"{API}/threads/{thread_id}/turns", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_switching_models_keeps_conversation_context(env, workspace):  # noqa: F811
    """§5.3.4：同一条会话里第一轮用 A、第二轮换 B，历史必须还在。

    上下文的真源是 thread 历史（history_for 拼进提示词），不跟着供应商走；
    断言取自第二轮真实发出的请求体——里面要能找到第一轮说过的原话，
    且 model 字段是 B 的（请求参数真的换了）。
    """
    project_id = await create_project(env, workspace)
    provider_a = await _make_provider(env, "mock-alpha")
    provider_b = await _make_provider(env, "mock-beta")
    thread_id = await _make_thread(env, project_id)

    marker = "九月的部署窗口定在周三上午"
    first = await _add_turn(env, thread_id, "第一轮", f"记住这个背景：{marker}", provider_a)
    _two_calls(env)
    assert (await env.client.post(f"{API}/tasks/{first}/run", json={})).status_code == 200
    first_run_requests = len(env.mock.requests)
    assert first_run_requests > 0

    second = await _add_turn(env, thread_id, "第二轮", "继续，基于上面的背景给结论", provider_b)
    _two_calls(env)
    assert (await env.client.post(f"{API}/tasks/{second}/run", json={})).status_code == 200

    second_run = env.mock.requests[first_run_requests:]
    assert second_run, "第二轮没有发出模型请求"
    # ① 请求参数真的换成了 B 的模型。
    assert all(item.get("model") == "mock-beta" for item in second_run)
    # ② 第一轮的用户消息还在第二轮的提示词里（上下文没丢）。
    assert any(marker in str(item) for item in second_run), "第二轮请求里找不到第一轮的背景原话"


async def test_multi_turn_keeps_provider_until_user_switches(env, workspace):  # noqa: F811
    """§5.3.5：用户不主动切换时，多轮任务用同一个供应商，不许悄悄漂移。

    两轮都不传 provider_id：解析走同一条默认路径（按名字取第一个启用的），
    两轮 task.started 记录的 provider/model 必须一致。
    """
    project_id = await create_project(env, workspace)
    await _make_provider(env, "mock-alpha")
    await _make_provider(env, "mock-beta")
    thread_id = await _make_thread(env, project_id)

    models: list[str] = []
    for index in (1, 2):
        task_id = await _add_turn(env, thread_id, f"第{index}轮", f"第{index}步：查看 README", None)
        _two_calls(env)
        assert (await env.client.post(f"{API}/tasks/{task_id}/run", json={})).status_code == 200
        started = await _started(task_id)
        models.append(str(started["model"]))
    assert models[0] == models[1], f"两轮模型不一致：{models}"
