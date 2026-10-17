"""P2 端到端：AGENTS.md 进系统提示、笔记跨窗口带回、记忆固化、分叉、插话。

单测保证每个部件自己说得通，这一层保证它们真的接进了运行时：模型实际收到的
messages、事件日志里留下的审计、以及分叉/插话这类会改运行轨迹的操作。
"""

from pathlib import Path

import json

from blank_api.services.memory import MemoryStore
from blank_api.services.notes import NoteStore

from tests.test_agent_v04 import (  # noqa: F401  (复用阶段3 的夹具)
    API,
    Env,
    add_tests_dir,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)

PATCH = "--- a/main.py\n+++ b/main.py\n@@ -1,1 +1,2 @@\n-print('hi')\n+print('hi')\n+print('patched')\n"


def system_text(payload: dict) -> str:
    return "\n".join(
        str(item.get("content") or "")
        for item in payload.get("messages") or []
        if item.get("role") == "system"
    )


async def _successful_run(env: Env, workspace: Path):  # noqa: F811
    """跑一个能真正判定为成功的任务：跑测试 → 审核通过 → 总结。"""
    add_tests_dir(workspace)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    criteria = ["测试通过"]
    task_id = await create_task(env, project_id, criteria)
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 运行测试"},
            {"content": "跑测试", "tool_calls": [call("run_tests", {})]},
            {
                "content": json.dumps(
                    {"approved": True, "findings": [], "summary": "实现与验收一致"},
                    ensure_ascii=False,
                )
            },
            {
                "content": json.dumps(
                    {
                        "summary": "已运行测试并通过",
                        "acceptance": [
                            {"criterion": criteria[0], "covered": True, "evidence": "pytest 1 passed"}
                        ],
                    },
                    ensure_ascii=False,
                )
            },
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    return task_id, provider_id, resp


async def test_agents_md_reaches_the_system_prompt(env: Env, workspace: Path) -> None:  # noqa: F811
    (workspace / "AGENTS.md").write_text("本项目所有注释用中文写\n", encoding="utf-8")
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读一下现状"])
    env.mock.reset([{"content": "已完成"}])

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text

    assert "本项目所有注释用中文写" in system_text(env.mock.requests[0])
    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    loaded = [event for event in events if event["type"] == "instructions.loaded"]
    assert loaded, events
    assert any("AGENTS.md" in item["path"] for item in loaded[0]["payload"]["layers"])
    # 环境快照也要留痕，否则"同一命令两种结果"无从解释
    snapshot = [event for event in events if event["type"] == "shell.snapshot"]
    assert snapshot and snapshot[0]["payload"]["policy"] in ("minimal", "strict", "inherit")


async def test_notes_survive_into_the_next_run(env: Env, workspace: Path) -> None:  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["记录进度"])
    env.mock.reset(
        [
            {
                "content": "先记一笔",
                "tool_calls": [
                    call("write_notes", {"content": "main.py 第 3 行还在改", "mode": "replace"})
                ],
            },
            {"content": "已完成"},
        ]
    )
    first = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert first.status_code == 200, first.text
    assert "main.py 第 3 行还在改" in NoteStore().read(task_id).content

    # 第二次运行相当于"换了一个上下文窗口"，交接笔记必须在第一次模型调用时带回来
    before = len(env.mock.requests)
    env.mock.reset([{"content": "继续"}])
    second = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert second.status_code == 200, second.text

    resumed_text = system_text(env.mock.requests[before])
    assert "跨窗口交接笔记" in resumed_text
    assert "main.py 第 3 行还在改" in resumed_text


async def test_successful_run_is_consolidated_into_memory(env: Env, workspace: Path) -> None:  # noqa: F811
    task_id, _provider, resp = await _successful_run(env, workspace)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task"]["status"] == "succeeded", body["task"]

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    consolidated = [event for event in events if event["type"] == "memory.consolidated"]
    assert consolidated, [event["type"] for event in events]
    assert consolidated[0]["payload"]["added"] >= 1

    text = MemoryStore().load(workspace)
    # 结论、验收、验证命令三类事实都要落进长期记忆；验收状态如实记录（本轮是未覆盖）
    assert "验收「测试通过」" in text
    assert "验证命令" in text and "pytest" in text
    assert MemoryStore().search(workspace, "测试")


async def test_fork_copies_state_and_records_lineage(env: Env, workspace: Path) -> None:  # noqa: F811
    task_id, _provider, run = await _successful_run(env, workspace)
    assert run.status_code == 200, run.text

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    source = next(
        event["payload"]["thread_id"] for event in events if event["type"] == "task.started"
    )
    reuse = await env.client.post(
        f"{API}/tasks/{task_id}/fork", json={"strategy": "trunk_reuse"}
    )
    assert reuse.status_code == 200, reuse.text
    forked = reuse.json()
    assert forked["thread_id"] != source
    assert forked["forked_from_thread_id"] == source
    assert forked["checkpoints_copied"] == 1

    fresh = await env.client.post(f"{API}/tasks/{task_id}/fork", json={"strategy": "trunk_new"})
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["checkpoints_copied"] == 0
    # 空树干也要占号，否则第三次分叉会拿到重复线程号
    assert fresh.json()["thread_id"] not in (source, forked["thread_id"])

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    forks = [event for event in events if event["type"] == "thread.forked"]
    assert len(forks) == 2
    assert {event["payload"]["strategy"] for event in forks} == {"trunk_reuse", "trunk_new"}


async def test_fork_rejects_unknown_strategy(env: Env, workspace: Path) -> None:  # noqa: F811
    project_id = await create_project(env, workspace)
    task_id = await create_task(env, project_id, ["改一下"])
    resp = await env.client.post(f"{API}/tasks/{task_id}/fork", json={"strategy": "sideways"})
    assert resp.status_code == 422


async def _waiting_push_task(env: Env, workspace: Path):  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["推送分支"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 推送 main"},
            {"content": "推送", "tool_calls": [call("run_shell", {"command": "git push origin main"})]},
            {"content": "推送已尝试"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "waiting_approval"
    return task_id, provider_id


async def test_steer_is_applied_on_the_next_model_call(env: Env, workspace: Path) -> None:  # noqa: F811
    task_id, provider_id = await _waiting_push_task(env, workspace)

    steered = await env.client.post(
        f"{API}/tasks/{task_id}/steer",
        json={"message": "别推 main，改推 feature/permission"},
    )
    assert steered.status_code == 200, steered.text
    assert steered.json()["queued"] is True

    before = len(env.mock.requests)
    approved = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={"approve": True, "provider_id": provider_id},
    )
    assert approved.status_code == 200, approved.text

    joined = "\n".join(
        str(item.get("content") or "") for item in env.mock.requests[before]["messages"]
    )
    assert "补充了以下指示" in joined
    assert "feature/permission" in joined

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    applied = [event for event in events if event["type"] == "steer.applied"]
    assert applied, [event["type"] for event in events]
    assert applied[0]["payload"]["chars"] > 0

    completed = [event for event in events if event["type"] in ("task.completed", "task.failed")]
    assert completed
    assert completed[0]["payload"]["steer"]["count"] == 1


async def test_steer_is_rejected_after_the_task_ended(env: Env, workspace: Path) -> None:  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["随便"])
    env.mock.reset([{"content": "已完成"}])
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text

    late = await env.client.post(f"{API}/tasks/{task_id}/steer", json={"message": "再改一下"})
    assert late.status_code == 409, late.text
