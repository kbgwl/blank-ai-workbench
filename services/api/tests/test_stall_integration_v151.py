"""空转断路器的端到端测试：真链路、真工具、真事件流。

规则测试（`test_stall_v151.py`）证明"判定对不对"，这里证明"接线通不通"：
判定结论必须真的改变执行（第三次同样的失败调用不再跑），真的进上下文（模型能读到
下一步），真的留痕（`tool.blocked` 事件 + 台账里那条 denied）。

场景直接照搬真实账本里代价最高的那一个任务：`apply_patch` 连发空参数。
"""

from __future__ import annotations

import json
import uuid

import pytest

from blank_api.db import get_session_factory
from blank_api.models.model_provider import ModelProvider
from blank_api.models.task import ToolCall
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.models.registry import build_adapter
from sqlalchemy import select

from tests.test_agent_v04 import Env, call, create_project, create_provider, create_task, env  # noqa: F401


async def _provider_row(provider_id: str) -> ModelProvider:
    async with get_session_factory()() as session:
        return await session.get(ModelProvider, provider_id)


def _empty_patch(step: int) -> dict:
    """模型发一次 `apply_patch` 但参数是空对象——真实账本里连发 4 次。"""
    return {"content": f"第 {step} 次尝试打补丁", "tool_calls": [call("apply_patch", {})]}


async def test_repeated_empty_arguments_are_stopped(env: Env, workspace) -> None:  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["把 main.py 改掉"], max_steps=8)
    # 每一次模型调用都给同一份空参数：第 1 次执行失败，第 2 次放行但带提示，第 3 次拦。
    env.mock.reset([{"content": "执行计划：\n1. 打补丁"}, _empty_patch(2), _empty_patch(3), _empty_patch(4)])

    evidence = await AgentRuntime(get_session_factory()).run(
        RunRequest(
            task_id=task_id,
            provider_id=provider_id,
            provider_name="mock-vendor",
            adapter=build_adapter(await _provider_row(provider_id), env.mock.factory),
            root=workspace,
            title="空参数重试",
            goal="把 main.py 改掉",
            acceptance_criteria=["把 main.py 改掉"],
            max_steps=8,
        )
    )

    timeline = evidence["timeline"]
    blocked = [event for event in timeline if event["type"] == "tool.blocked"]
    assert blocked, "第三次同样的失败调用必须被拦，否则空转回路原样存在"
    assert blocked[0]["payload"]["tool"] == "apply_patch"
    assert blocked[0]["payload"]["kind"] == "missing_arg"
    # 参数正文不进事件，只留指纹：拦截理由要说清"第几次同样的调用"，
    # 但没必要把参数再抄一份进时间线。
    assert blocked[0]["payload"]["signature"].startswith("apply_patch:")

    calls = [row for row in await env.tool_calls(task_id) if row.name == "apply_patch"]
    # 前两次真的执行了（都因缺参数失败），第三次连执行都没有。
    # 第三条必须是 `blocked` 而不是 `denied`：`denied` 在整条链上的意思都是
    # "用户/策略不许做"，被空转拦下冒充它，收尾会把"没跑"当成"没获批"跳过校验，
    # 任务反而被判成功——这是接入断路器时真实踩到的回归。
    assert len(calls) >= 3
    assert [row.status for row in calls[:3]] == ["error", "error", "blocked"]


async def test_first_failure_carries_the_next_step(env: Env, workspace) -> None:  # noqa: F811
    """第一次失败后的回执必须带"这个工具要什么"，否则第二次只能照原样再发一遍。"""
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["把 main.py 改掉"], max_steps=6)
    env.mock.reset([{"content": "执行计划：\n1. 打补丁"}, _empty_patch(2), _empty_patch(3)])

    await AgentRuntime(get_session_factory()).run(
        RunRequest(
            task_id=task_id,
            provider_id=provider_id,
            provider_name="mock-vendor",
            adapter=build_adapter(await _provider_row(provider_id), env.mock.factory),
            root=workspace,
            title="缺参数提示",
            goal="把 main.py 改掉",
            acceptance_criteria=["把 main.py 改掉"],
            max_steps=6,
        )
    )

    # 第二次请求模型的上下文里必须已经出现"下一步"与参数名：这是"提示真的进了上下文"
    # 的硬证据，不能只看网关返回。不指定第几个请求，是因为前面还有一次意图判定调用。
    requests = env.mock.requests
    assert len(requests) >= 3
    assert any("【下一步】" in json.dumps(item, ensure_ascii=False) for item in requests)
    assert any("patch" in json.dumps(item, ensure_ascii=False) for item in requests)


async def test_blocked_call_does_not_kill_the_task(env: Env, workspace) -> None:  # noqa: F811
    """拦住之后任务还得能继续做完：断路器的目的是让它换路，不是让任务死在这里。"""
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["创建 note.txt 并写入 ok"], max_steps=8)
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 打补丁"},
            _empty_patch(2),
            _empty_patch(3),
            _empty_patch(4),
            # 被拦之后改用正确的补丁形状，任务继续。
            {
                "content": "按提示重写补丁",
                "tool_calls": [
                    call(
                        "apply_patch",
                        {"patch": "--- /dev/null\n+++ b/note.txt\n@@ -0,0 +1,1 @@\n+ok\n"},
                    )
                ],
            },
            {"content": "已写出 note.txt"},
        ]
    )

    evidence = await AgentRuntime(get_session_factory()).run(
        RunRequest(
            task_id=task_id,
            provider_id=provider_id,
            provider_name="mock-vendor",
            adapter=build_adapter(await _provider_row(provider_id), env.mock.factory),
            root=workspace,
            title="拦住后换路",
            goal="创建 note.txt 并写入 ok",
            acceptance_criteria=["创建 note.txt 并写入 ok"],
            max_steps=8,
        )
    )

    assert (workspace / "note.txt").read_text(encoding="utf-8").strip() == "ok"
    assert any(event["type"] == "tool.blocked" for event in evidence["timeline"])
    assert any(row.name == "apply_patch" and row.status == "ok" for row in await env.tool_calls(task_id))
