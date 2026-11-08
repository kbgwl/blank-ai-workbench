# -*- coding: utf-8 -*-
"""v163 §7 集成测试：把各部件接起来跑，而不是各测各的。

任务书要求证明的链路是：

    Trigger → Router → Worker → Watchdog/通知 → 结构化状态

单测能证明「每个零件参数对不对」，证明不了「它们装在一起会不会互相踩」。
所以这里的用例都走真实 HTTP 接口 + 真实 worker 循环，只把模型换成 MockModel
（模型是外部系统，不是被测对象）。

三条链：

1. 触发器到点 → worker 认领 → 任务真的执行完 → 快照落库且不可被事后放宽；
2. 跑完之后 `/status` 给的是结构化事实（run/worker/快照/改动指纹/交付/用量），
   不是让前端从最后一句文本里猜；
3. 高风险调用被审批拦下时，`/status` 的 approval 与 task.status 同步，
   审批通过前不出现任何执行记录。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from blank_api import worker as worker_module
from blank_api.api.routes import tasks as task_routes
from blank_api.db import get_session_factory
from blank_api.models.run import AgentRun
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent import triggers as trg

from tests.test_agent_v04 import (  # noqa: F401  (复用端到端夹具)
    API,
    PATCH,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)

BACKGROUND = "BLANK_FEATURE_BACKGROUND_EXECUTION"
DISPATCH = "BLANK_FEATURE_SUPER_AGENT_DISPATCH"


def _use_mock_model(monkeypatch, env) -> None:
    """让 worker 路径也用 MockModel。

    worker 走的是 `_run_request(..., request=None)`，拿不到 `app.state.model_client_factory`，
    于是适配器会去连真实上游。这里把该模块里的 build_adapter 换成强制带 mock 工厂的版本——
    被测的是「任务有没有按快照跑完」，不是「上游通不通」。
    """
    real = task_routes.build_adapter
    monkeypatch.setattr(
        task_routes,
        "build_adapter",
        lambda provider, client_factory=None, **kwargs: real(
            provider, env.mock.factory, **kwargs
        ),
    )


async def _task(task_id: str) -> Task:
    async with get_session_factory()() as session:
        row = await session.get(Task, task_id)
        assert row is not None
        return row


async def _event_types(task_id: str) -> list[str]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(AgentEvent.type).where(AgentEvent.task_id == task_id).order_by(AgentEvent.seq)
            )
        ).scalars()
        return list(rows)


def _plan_steps(criteria: list[str]) -> list[dict]:
    """一个能真的改文件、真跑测试的最小剧本（含审核与总结两条 JSON）。"""
    import json as _json

    return [
        {"content": "执行计划：\n1. 读取 main.py\n2. 应用补丁\n3. 运行测试"},
        {
            "content": "开始执行",
            "tool_calls": [
                call("read_file", {"path": "main.py"}),
                call("apply_patch", {"path": "main.py", "patch": PATCH}),
            ],
        },
        {"content": "补丁已应用"},
        {
            "content": _json.dumps(
                {"approved": True, "findings": [], "summary": "实现与验收条件一致"},
                ensure_ascii=False,
            )
        },
        {
            "content": _json.dumps(
                {
                    "summary": "已修改 main.py 并通过测试",
                    "acceptance": [
                        {"criterion": item, "covered": True, "evidence": "apply_patch 成功且测试通过"}
                        for item in criteria
                    ],
                },
                ensure_ascii=False,
            )
        },
    ]


@pytest.fixture
def project(env, workspace: Path):
    (workspace / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (workspace / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    # 验证阶段跑 `pytest -q`：仓库里一个用例都没有时 pytest 退出码是 5（没有收集到用例），
    # 会被判成「测试未通过」。放一个真的会过的用例，验证的就是「测试通过」而不是「没测试」。
    tests = workspace / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test_demo.py").write_text("def test_demo():\n    assert True\n", encoding="utf-8")
    return workspace


# ── 1. Trigger → Router → Worker ────────────────────────────────────────────


async def test_trigger_to_router_to_worker_finishes_the_task(
    env, project: Path, monkeypatch
):
    """到点的触发器经 worker 跑完，一条任务只跑一次，快照留在任务上。

    这是**独立 worker** 链路：``BLANK_INPROCESS_RUNNER=0`` 让放行只入队，
    由这一轮的 ``run_once`` 认领。桌面形态（默认 1）会自己起跑，那条路由
    ``test_due_trigger_runs_itself_when_no_worker_will_claim_it`` 覆盖。
    """
    monkeypatch.setenv(BACKGROUND, "1")
    monkeypatch.setenv(DISPATCH, "1")
    monkeypatch.setenv("BLANK_INPROCESS_RUNNER", "0")
    _use_mock_model(monkeypatch, env)
    project_id = await create_project(env, project)
    provider_id = await create_provider(env)

    created = (
        await env.client.post(
            f"{API}/projects/{project_id}/triggers",
            json={
                "event_type": "schedule",
                "source": "timer",
                "goal": "给 main.py 增加一行输出并运行测试",
                "provider_id": provider_id,
                "dedupe_key": "nightly-1",
                "fire_at": (trg.utc_now() - timedelta(seconds=5)).isoformat(),
            },
        )
    ).json()
    assert created["created"] is True
    task_id = created["task_id"]
    env.mock.reset(_plan_steps(["main.py 输出 patched"]))

    handled = await worker_module.run_once("worker-chain")
    assert handled == 1, "到点的触发器应该被这一轮扫描认领并执行"

    row = await _task(task_id)
    assert row.status == "succeeded", f"任务失败原因：{row.failure_reason}"
    snapshot = dict(row.dispatch_snapshot or {})
    assert snapshot.get("snapshot_id"), "执行前必须留下派发快照"
    assert snapshot.get("intent") in {"execute", "answer", "ask_user", "schedule", "watch", "brainstorm"}
    assert snapshot.get("approval_required") is True, "快照永远不允许把审批关掉"

    types = await _event_types(task_id)
    assert "task.started" in types and "task.completed" in types
    assert "file.changed" in types

    # 第二轮不该再认领同一条任务：触发器已 fired，run 已写终态。
    assert await worker_module.run_once("worker-chain") == 0
    async with get_session_factory()() as session:
        run_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(AgentRun).where(AgentRun.task_id == task_id)
                )
            ).scalar_one()
        )
    assert run_count == 1, "同一件事不能留下第二条 run"

    # 后台任务的六类结构化事实：run / worker / 快照 / 改动 / 用量 / 交付
    status = (await env.client.get(f"{API}/tasks/{task_id}/status")).json()
    assert status["run_id"], "后台任务必须能指到具体哪条 run"
    assert status["worker_id"] == "worker-chain", "必须能回答「是谁在跑」"
    assert status["last_heartbeat"], "后台 run 必须留下最后一次心跳"
    assert status["trigger_id"] == created["trigger"]["id"], "必须能回答「谁把它叫起来的」"
    assert status["snapshot_id"] == snapshot["snapshot_id"]
    assert status["changes"]["paths"] == ["main.py"]
    assert status["tokens"]["total"] > 0
    assert status["delivery"]["present"] is True

    # 低打扰（§5）：任务出终态时必须留下「这次该不该打扰」的判定，而不是只有配置项。
    assert "notify.decided" in types
    assert status["notify"]["policy"], "状态里要能读到当前通知策略"
    assert status["notify"]["last"] is not None
    assert status["notify"]["last"]["event"] == "deliverable"
    assert status["notify"]["last"]["disturb"] is True or status["notify"]["last"]["policy"] != "always"

    # 固定四行交付（§5）：结论/改动/验证都来自事实，正文只有这四行里的几行。
    detail = (await env.client.get(f"{API}/tasks/{task_id}")).json()
    summary = detail["result_summary"] or detail["summary"]
    assert summary.splitlines()[0].startswith("完成：")
    assert "改动：1 个文件" in summary
    assert "验证：" in summary


# ── 2. 结构化状态是事实，不是文本推断 ──────────────────────────────────────


async def test_status_exposes_structured_evidence_after_a_finished_run(
    env, project: Path, monkeypatch
):
    """跑完之后 /status 能给出 run/worker/快照/改动指纹/交付/用量六类事实。"""
    monkeypatch.setenv(DISPATCH, "1")
    project_id = await create_project(env, project)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["main.py 输出 patched"])
    env.mock.reset(_plan_steps(["main.py 输出 patched"]))

    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "succeeded"

    status = (await env.client.get(f"{API}/tasks/{task_id}/status")).json()
    assert status["task_id"] == task_id
    assert status["status"] == "succeeded"
    # 前台 `/run` 不建 run 行（AgentRun 只在 queue.enqueue_run 里创建），所以这里如实是 null。
    # 后台任务那条链由上一个用例覆盖：它有 run_id / worker_id / trigger_id。
    assert status["run_id"] is None
    assert status["snapshot_id"], "必须带派发快照 id"
    assert status["current_step"] >= 1
    # 同前台口径：没有 run 行就没有心跳可读。后台任务的心跳由上一个用例覆盖。
    assert status["last_heartbeat"] is None
    # 最后事件是真正最后一条（收尾之后还可能有记忆固化这类事件），不是只看类型猜。
    assert status["last_event"]["type"]
    assert status["last_event"]["at"]
    assert status["model"] == "mock-coder"
    assert status["tokens"]["total"] > 0, "用量必须真的记下来，不能恒为 0"
    assert status["approval"] is None
    assert status["changes"]["count"] == 1
    assert status["changes"]["paths"] == ["main.py"]
    assert len(status["changes"]["diff_fingerprint"]) == 16, "Diff 指纹要能用来判断有没有变"
    assert status["delivery"]["present"] is True
    assert status["delivery"]["summary"]
    assert status["dispatch"]["intent"] and status["dispatch"]["scenario"]


# ── 3. 审批与任务状态同步 ──────────────────────────────────────────────────


async def test_status_shows_the_pending_approval_and_clears_it_after_decision(
    env, project: Path, monkeypatch
):
    """待审批时 /status 与 task.status 说的是同一件事；放行后才真的执行。"""
    monkeypatch.setenv(DISPATCH, "1")
    project_id = await create_project(env, project)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["删除 notes.txt"])
    (project / "notes.txt").write_text("keep me\n", encoding="utf-8")
    import json as _json

    env.mock.reset(
        [
            {"content": "执行计划：\n1. 删除 notes.txt"},
            {"content": "执行删除", "tool_calls": [call("run_shell", {"command": "rm -rf notes.txt"})]},
            {"content": "该操作被系统拒绝，等待人工审批"},
            # 审批通过后从这一步继续：审核 → 总结
            {"content": _json.dumps({"approved": True, "findings": [], "summary": "已按批准执行"}, ensure_ascii=False)},
            {
                "content": _json.dumps(
                    {"summary": "已删除 notes.txt", "acceptance": [{"criterion": "删除 notes.txt", "covered": True, "evidence": "run_shell 已批准执行"}]},
                    ensure_ascii=False,
                )
            },
        ]
    )

    body = (
        await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    ).json()
    assert body["task"]["status"] == "waiting_approval"
    pending = (await env.client.get(f"{API}/tasks/{task_id}/status")).json()
    assert pending["status"] == "waiting_approval"
    assert pending["approval"] is not None
    assert pending["approval"]["tool"] == "run_shell"
    assert pending["approval"]["waiting_since"], "等待超过阈值 Watchdog 要能读到起点"
    # 审批没过之前，被拦下的工具一条执行记录都不许有。
    assert pending["changes"]["count"] == 0
    assert (project / "notes.txt").exists()

    decided = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={"provider_id": provider_id, "approve": True, "scope": "once"},
    )
    assert decided.status_code == 200, decided.text

    after = (await env.client.get(f"{API}/tasks/{task_id}/status")).json()
    assert after["approval"] is None, "审批下完之后状态里不许还挂着旧卡片"
    assert after["status"] != "waiting_approval", "批准后任务必须真的往前走，而不是继续挂着"
