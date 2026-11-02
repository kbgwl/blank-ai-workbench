"""创建任务时的模型选择校验。

真实场景：界面上的模型下拉显示「默认模型」，但 localStorage 里还留着上一次用过的
临时供应商 id。任务照这个 id 落库，一直到运行时才失败，报「模型供应商不存在」——
用户既不知道是哪一步错的，也无从下手改。这里锁住"新建时就挡掉并说清怎么改"。
"""

from __future__ import annotations

import uuid

from tests.test_agent_v04 import (  # noqa: F401  (复用阶段3 的夹具)
    API,
    create_project,
    create_provider,
    env,
)


def _payload(provider_id: str | None) -> dict:
    body = {
        "title": "改一行代码",
        "goal": "把 greeting 改成 hi",
        "acceptance_criteria": ["测试通过"],
    }
    if provider_id is not None:
        body["provider_id"] = provider_id
    return body


async def test_create_task_rejects_dangling_provider(env, workspace):  # noqa: F811
    project_id = await create_project(env, workspace)
    resp = await env.client.post(
        f"{API}/projects/{project_id}/tasks", json=_payload(str(uuid.uuid4()))
    )
    assert resp.status_code == 400, resp.text
    message = resp.json()["error"]["message"]
    assert "重新选择模型" in message


async def test_create_task_accepts_existing_provider(env, workspace):  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    resp = await env.client.post(
        f"{API}/projects/{project_id}/tasks", json=_payload(provider_id)
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["provider_id"] == provider_id


async def test_create_task_still_allows_no_provider(env, workspace):  # noqa: F811
    """不传 provider 是合法用法：运行时按第一个启用的供应商走。"""
    project_id = await create_project(env, workspace)
    resp = await env.client.post(f"{API}/projects/{project_id}/tasks", json=_payload(None))
    assert resp.status_code == 201, resp.text
