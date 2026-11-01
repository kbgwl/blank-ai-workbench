# -*- coding: utf-8 -*-
"""交付回复的七态快照（docs/workbuddy-followup-v153.md §10）。

回复正文出自模型，快照钉的不是措辞而是**结构与纪律**——每种状态下
交付载荷必须长什么样、不许说什么：

1. 成功：succeeded + 验收全覆盖，交付文本非空；
2. 失败：failed，交付文本不许出现「已完成」；
3. 等待授权：waiting_approval + pending_approvals 非空；
4. 等待回答：waiting_approval + pending_questions 非空（且不是审批）；
5. 测试跳过：validation.status=skipped + passed=None + unverified 提示；
6. 测试未验证：没有 validation 事件 + 「未执行测试验证」明示；
7. 部分完成：验收一真一假，未验证的逐条点名。

禁令（§10）只有结构上可测的两条落在这里：失败/未验证不许冒充成功，
pending 的东西不许写成终态。
"""

from __future__ import annotations

import json

from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.evidence import build_evidence

from tests.test_agent_v04 import (  # noqa: F401  (复用夹具)
    API,
    add_tests_dir,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)


def _verdict() -> dict:
    return {
        "content": json.dumps(
            {"approved": True, "findings": [], "summary": "实现与验收一致"}, ensure_ascii=False
        )
    }


async def _evidence(task_id: str) -> dict:
    return await build_evidence(get_session_factory(), task_id)


async def test_snapshot_success(env, workspace):  # noqa: F811
    """成功：succeeded + 验收全覆盖 + 交付文本非空。"""
    add_tests_dir(workspace)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    criteria = ["改了 main.py", "输出包含 patched"]
    task_id = await create_task(env, project_id, criteria, max_steps=8)
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 应用补丁"},
            {"content": "执行", "tool_calls": [call("apply_patch", {"path": "main.py", "patch": "--- a/main.py\n+++ b/main.py\n@@ -1,1 +1,2 @@\n-print('hi')\n+print('hi')\n+print('patched')\n"})]},
            {"content": "补丁已应用"},
            _verdict(),
            {
                "content": json.dumps(
                    {
                        "summary": "登录脚本输出新增一行 patched 标记",
                        "acceptance": [
                            {"criterion": criteria[0], "covered": True, "evidence": "apply_patch 成功"},
                            {"criterion": criteria[1], "covered": True, "evidence": "文件含 patched"},
                        ],
                    },
                    ensure_ascii=False,
                )
            },
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    body = resp.json()
    assert body["task"]["status"] == "succeeded", {k: body["task"].get(k) for k in ("status", "failure_reason", "steps_used")}
    evidence = body["evidence"]
    assert evidence["task"]["result_summary"].strip()
    covered = {item["criterion"]: item["covered"] for item in evidence["acceptance"]}
    assert all(covered.values()), covered
    # 成功态不许自己报未验证风险里带"待审批"。
    assert not any("待审批" in item for item in evidence.get("unverified_risks", []))


async def test_snapshot_failure_never_claims_done(env, workspace):  # noqa: F811
    """失败：任务 failed，交付/证据里不许冒出「已完成」。"""
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env, tools=False)
    task_id = await create_task(env, project_id, ["每一步都成功"])
    env.mock.reset([{"content": "执行计划：\n1. 读文件"}])
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 502, resp.text
    body = (await env.client.get(f"{API}/tasks/{task_id}")).json()
    assert body["status"] == "failed", body
    evidence = (await env.client.get(f"{API}/tasks/{task_id}/evidence")).json()
    text = json.dumps(evidence["task"].get("result_summary") or "", ensure_ascii=False) + json.dumps(
        evidence.get("unverified_risks", []), ensure_ascii=False
    )
    assert "已完成" not in text, "失败任务的交付文本不许说「已完成」"


async def test_snapshot_waiting_approval(env, workspace):  # noqa: F811
    """等待授权：waiting_approval + pending_approvals 非空 + 未验证风险点名。"""
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["删除 notes.txt"], max_steps=6)
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 删文件"},
            {"content": "删除", "tool_calls": [call("run_shell", {"command": "rm -rf notes.txt"})]},
        ]
    )
    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id}
    )
    body = resp.json()
    assert body["task"]["status"] == "waiting_approval"
    pending = body["evidence"]["pending_approvals"]
    assert pending and pending[0]["tool"] == "run_shell"
    assert body["evidence"]["pending_questions"] == []
    assert any("待审批" in item for item in body["evidence"].get("unverified_risks", []))


async def test_snapshot_waiting_answer(env, workspace):  # noqa: F811
    """等待回答：waiting_approval（复用中断位）+ pending_questions 非空。"""
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["按选择的方案执行"], max_steps=6)
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 确认方案"},
            {
                "content": "方案不清楚，先问",
                "tool_calls": [
                    call(
                        "request_user_input",
                        {
                            "question": "用哪种方案？",
                            "header": "方案",
                            "options": [{"label": "方案 A"}, {"label": "方案 B"}],
                        },
                    )
                ],
            },
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    body = resp.json()
    assert body["task"]["status"] == "waiting_approval"
    questions = body["evidence"]["pending_questions"]
    assert questions and questions[0]["question"] == "用哪种方案？"
    # 提问不能被当成审批。
    assert body["evidence"]["pending_approvals"] == []


async def test_snapshot_validation_skipped_and_unverified(workspace):  # noqa: F811
    """测试跳过 / 测试未验证：passed=None 不许变成 True，unverified 明说。"""
    base_states = [
        # (task_id, 是否带 validation.completed 事件, 期望 unverified 片段)
        ("snap-skip", True, "未做自动化校验"),
        ("snap-never", False, "未执行测试验证"),
    ]
    for task_id, with_validation, expected_note in base_states:
        async with get_session_factory()() as session:
            session.add(Project(id=f"p-{task_id}", name="p"))
            session.add(
                Task(
                    id=task_id,
                    project_id=f"p-{task_id}",
                    title="t",
                    goal="g",
                    status="succeeded",
                    acceptance_results=[
                        {"criterion": "测试通过", "covered": False, "evidence": ""}
                    ],
                )
            )
            if with_validation:
                session.add(
                    AgentEvent(
                        id=f"{task_id}-val",
                        task_id=task_id,
                        type="validation.completed",
                        payload={
                            "command": "",
                            "passed": None,
                            "status": "skipped",
                            "exit_code": None,
                            "summary": {},
                            "output_tail": "",
                            "skipped_reason": "测试命令未获授权，未能执行",
                        },
                        seq=1,
                    )
                )
            await session.commit()

        evidence = await _evidence(task_id)
        validation = evidence.get("validation")
        if with_validation:
            assert validation is not None and validation["status"] == "skipped"
            # §10 禁令：跳过不许说成通过。
            assert validation.get("passed") is None
        else:
            assert not validation
        assert any(expected_note in item for item in evidence["unverified_risks"]), (
            task_id,
            evidence["unverified_risks"],
        )


async def test_snapshot_partial_completion(env, workspace):  # noqa: F811
    """部分完成：一条验收覆盖一条没覆盖，没覆盖的逐条点名。"""
    add_tests_dir(workspace)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    criteria = ["改了 main.py", "运行了测试"]
    task_id = await create_task(env, project_id, criteria, max_steps=8)
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 应用补丁"},
            {"content": "执行", "tool_calls": [call("apply_patch", {"path": "main.py", "patch": "--- a/main.py\n+++ b/main.py\n@@ -1,1 +1,2 @@\n-print('hi')\n+print('hi')\n+print('patched')\n"})]},
            {"content": "补丁已应用"},
            _verdict(),
            {
                "content": json.dumps(
                    {
                        "summary": "改了 main.py；测试这轮没跑",
                        "acceptance": [
                            {"criterion": criteria[0], "covered": True, "evidence": "apply_patch 成功"},
                            {"criterion": criteria[1], "covered": False, "evidence": ""},
                        ],
                    },
                    ensure_ascii=False,
                )
            },
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    body = resp.json()
    assert body["task"]["status"] == "succeeded", {k: body["task"].get(k) for k in ("status", "failure_reason", "steps_used")}
    covered = {item["criterion"]: item["covered"] for item in body["evidence"]["acceptance"]}
    assert covered == {criteria[0]: True, criteria[1]: False}
    # 未验证的那条必须被点名，不许静默。
    assert any(criteria[1] in item for item in body["evidence"].get("unverified_risks", []))
