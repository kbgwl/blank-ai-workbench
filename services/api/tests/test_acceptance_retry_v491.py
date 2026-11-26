"""验收结论解析失败：先重试一次，两次都失败记“未判定”，不冒充“没覆盖”。

真机 T02（2026-11-21 evals 基线）：任务 succeeded、代码改对，验收结论却因为模型
回了一段非 JSON，被旧实现一次性标成 `covered=False`，四项全部变成“没覆盖”。
这不是“验收没过”，是“验收没做成”——两件事在界面上必须分开。
"""

from __future__ import annotations

import json

from tests.test_agent_v04 import (  # noqa: F401  (复用夹具)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)

CRITERION = "改了 main.py"


def _acceptance_ok() -> dict:
    return {
        "content": json.dumps(
            {
                "summary": "改好了",
                "acceptance": [
                    {"criterion": CRITERION, "covered": True, "evidence": "apply_patch 成功"}
                ],
            },
            ensure_ascii=False,
        )
    }


async def _run_with_acceptance(env, workspace, acceptance_responses):  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, [CRITERION], max_steps=8)
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 应用补丁"},
            {"content": "先读一遍", "tool_calls": [call("read_file", {"path": "main.py"})]},
            {
                "content": "执行",
                "tool_calls": [
                    call(
                        "apply_patch",
                        {
                            "path": "main.py",
                            "patch": "--- a/main.py\n+++ b/main.py\n@@ -1,1 +1,2 @@\n-print('hi')\n+print('hi')\n+print('patched')\n",
                        },
                    )
                ],
            },
            {"content": "补丁已应用"},
            *acceptance_responses,
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    return task_id, resp.json()


async def test_unparseable_acceptance_is_retried_once(env, workspace):  # noqa: F811
    task_id, body = await _run_with_acceptance(
        env,
        workspace,
        [
            # 第一次：合法 JSON，但缺少 acceptance 字段
            {"content": json.dumps({"summary": "改好了"})},
            _acceptance_ok(),
        ],
    )
    assert body["task"]["status"] == "succeeded", body["task"]
    acceptance = body["evidence"]["acceptance"]
    assert acceptance[0]["covered"] is True, acceptance

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    recorded = [item for item in events if item["type"] == "delivery.recorded"]
    assert recorded and recorded[-1]["payload"].get("mode") == "report", recorded


async def test_two_failures_are_unjudged_not_uncovered(env, workspace):  # noqa: F811
    task_id, body = await _run_with_acceptance(
        env,
        workspace,
        [
            {"content": "这不是 JSON"},
            {"content": "仍然不是 JSON"},
        ],
    )
    assert body["task"]["status"] == "succeeded", body["task"]
    acceptance = body["evidence"]["acceptance"]
    # 关键：是 None（未判定），不是 False（没覆盖）。
    assert acceptance[0]["covered"] is None, acceptance
    assert "未判定" in acceptance[0]["evidence"], acceptance

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    recorded = [item for item in events if item["type"] == "delivery.recorded"]
    assert recorded and recorded[-1]["payload"].get("mode") == "fallback", recorded
