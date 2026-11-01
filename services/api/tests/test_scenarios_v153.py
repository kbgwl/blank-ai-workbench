# -*- coding: utf-8 -*-
"""场景系统端到端：配置解析、工具禁用与注入不碰用户正文。

验收对应升级任务书 §7：场景必须真正影响可用工具与行为；红线是
**用户 prompt 原样进模型**——所以这里的所有断言都围绕两件事：
场景切换改变了「工具看得见/调得动」，以及 ``task.started`` 如实记录了场景。
"""

from __future__ import annotations

from sqlalchemy import select

from blank_api.db import get_session_factory
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.scenarios import (
    BRAINSTORM,
    DEV,
    DOCS,
    DAILY,
    REVERSE,
    WRITE_TOOLS,
    excluded_tools,
    list_scenarios,
    resolve,
    system_prompt,
)

from tests.test_agent_v04 import (  # noqa: F401  (复用阶段3 的夹具)
    API,
    call,
    create_project,
    create_provider,
    env,
)


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


async def _task_row(task_id: str) -> Task:
    async with get_session_factory()() as session:
        row = await session.get(Task, task_id)
        assert row is not None
        return row


def _plan_then_done(env) -> None:
    env.mock.reset([{"content": "计划：\n1. 查看 README"}, {"content": "完成"}, {"content": "完成"}])


# ── 纯函数：配置矩阵 ────────────────────────────────────────────────────────


def test_unknown_scenario_falls_back_to_dev():
    assert resolve(None) is DEV
    assert resolve("") is DEV
    assert resolve("nonsense") is DEV


def test_only_daily_can_opt_into_memory():
    # 开发场景即使请求带了 opt_in 也不开放记忆：记忆只属于日常场景。
    assert "memory_write" in excluded_tools(DEV, memory_opt_in=True)
    assert "memory_write" not in excluded_tools(
        resolve("daily"), memory_opt_in=True
    )
    # 不记住 / 非日常场景一律禁写记忆。
    assert "memory_write" in excluded_tools(resolve("daily"), memory_opt_in=False)
    assert "memory_write" in system_prompt(
        resolve("daily"), memory_opt_in=True
    )
    assert system_prompt(DEV, memory_opt_in=True) == ""


def test_readonly_scenarios_deny_write_tools():
    for key in ("daily", "brainstorm"):
        scenario = resolve(key)
        denied = excluded_tools(scenario)
        assert {"apply_patch", "run_shell", "run_tests", "git"} <= denied, key


def test_scenario_catalog_is_stable():
    items = list_scenarios()
    assert [item["key"] for item in items] == [
        "dev", "docs", "daily", "brainstorm", "reverse",
    ]
    # 只有日常场景带记忆开关——前端按这个字段决定显示与否。
    assert [item["key"] for item in items if item["memory_allowed"]] == ["daily"]


# ── 端到端 ──────────────────────────────────────────────────────────────────


async def test_scenarios_endpoint_lists_catalog(env):  # noqa: F811
    resp = await env.client.get(f"{API}/scenarios")
    assert resp.status_code == 200
    keys = [item["key"] for item in resp.json()]
    assert keys[0] == "dev" and "daily" in keys


async def test_daily_scenario_hides_and_blocks_write_tools(env, workspace):  # noqa: F811
    """只读场景：写类工具从下发列表消失；模型硬调也会被网关拒绝。"""
    await create_provider(env)
    project_id = await create_project(env, workspace)
    resp = await env.client.post(
        f"{API}/projects/{project_id}/tasks",
        json={
            "title": "日常问答",
            # 带动作词，确保意图被判成 act 进工具循环；daily 场景要在循环里拦住写工具。
            "goal": "整理一下这个项目的介绍",
            "acceptance_criteria": [],
            "max_steps": 6,
            "scenario": "daily",
        },
    )
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["id"]
    assert (await _task_row(task_id)).scenario == "daily"

    # 模型无视场景，直接试图改文件——网关必须拒绝而不是照做。
    env.mock.reset(
        [
            {"content": "计划：\n1. 改文件"},
            {
                "content": "改一下",
                "tool_calls": [
                    call("apply_patch", {"path": "README.md", "patch": "*** Begin Patch\n*** Update File: README.md\n@@\n-x\n+y\n*** End Patch"})
                ],
            },
            {"content": "完成"},
        ]
    )
    run = await env.client.post(f"{API}/tasks/{task_id}/run", json={})
    assert run.status_code == 200, run.text

    started = await _started(task_id)
    assert started["scenario"] == "daily"
    # 写类工具不进下发列表，模型从一开始就不该看见它。
    assert "apply_patch" not in started["tools"]
    calls = await env.tool_calls(task_id)
    patch_rows = [row for row in calls if row.name == "apply_patch"]
    assert patch_rows, "模型没有尝试调用 apply_patch（脚本失效）"
    assert patch_rows[0].status == "denied"
    assert "当前场景不允许" in (patch_rows[0].result_summary or "")


async def test_memory_write_only_visible_in_daily_with_opt_in(env, workspace):  # noqa: F811
    """memory_write：日常+记住 → 可见；开发场景 → 即使请求带了 opt_in 也不可见。"""
    await create_provider(env)
    project_id = await create_project(env, workspace)
    task_id = None
    for scenario, opt_in, expected in (
        ("daily", True, True),
        ("dev", True, False),
        ("daily", False, False),
    ):
        resp = await env.client.post(
            f"{API}/projects/{project_id}/tasks",
            json={
                "title": f"记忆开关 {scenario}",
                "goal": "随便聊聊",
                "acceptance_criteria": [],
                "max_steps": 4,
                "scenario": scenario,
            },
        )
        assert resp.status_code == 201, resp.text
        task_id = resp.json()["id"]
        _plan_then_done(env)
        run = await env.client.post(
            f"{API}/tasks/{task_id}/run", json={"memory_opt_in": opt_in}
        )
        assert run.status_code == 200, run.text
        started = await _started(task_id)
        has_tool = "memory_write" in started["tools"] or "memory_write" in started["tools_deferred"]
        assert has_tool is expected, f"scenario={scenario} opt_in={opt_in}"


async def test_run_scenario_overrides_stored_one(env, workspace):  # noqa: F811
    """run 显式指定场景覆盖创建存档并写回——UI 切换立即生效的路径。"""
    await create_provider(env)
    project_id = await create_project(env, workspace)
    resp = await env.client.post(
        f"{API}/projects/{project_id}/tasks",
        json={
            "title": "场景覆盖",
            "goal": "看一眼 README",
            "acceptance_criteria": [],
            "max_steps": 4,
            "scenario": "dev",
        },
    )
    task_id = resp.json()["id"]
    _plan_then_done(env)
    run = await env.client.post(
        f"{API}/tasks/{task_id}/run", json={"scenario": "brainstorm"}
    )
    assert run.status_code == 200, run.text
    assert (await _started(task_id))["scenario"] == "brainstorm"
    assert (await _task_row(task_id)).scenario == "brainstorm"

async def test_memory_lifecycle_accept_then_delete(env):  # noqa: F811
    """§7.2 验收的闭环：候选可以查看、接受成记忆、再物理删除。"""
    text = "用户偏好：交付里先给结论再给依据"
    made = await env.client.post(
        f"{API}/skills/memory-candidates", json={"text": text, "source": "test"}
    )
    assert made.status_code == 201, made.text
    candidate_id = made.json()["id"]

    accepted = await env.client.post(f"{API}/skills/memory-candidates/{candidate_id}/accept")
    assert accepted.status_code == 201, accepted.text
    skill_id = accepted.json()["id"]

    listed = (await env.client.get(f"{API}/skills")).json()
    assert any(row["id"] == skill_id for row in listed)

    deleted = await env.client.delete(f"{API}/skills/{skill_id}")
    assert deleted.status_code == 204, deleted.text
    listed = (await env.client.get(f"{API}/skills")).json()
    assert not any(row["id"] == skill_id for row in listed)

    again = await env.client.delete(f"{API}/skills/{skill_id}")
    assert again.status_code == 404

def test_scenario_acceptance_matrix_followup153():
    """§6 验收矩阵：五个场景的权限与提示词规则逐条钉死。"""
    # 开发：读写代码 + 跑测试全开；记忆仍禁（日常习惯不进开发会话）。
    assert excluded_tools(DEV) == frozenset({"memory_write"})
    assert not DEV.system_prompt

    # 文档：能落文档文件（allow_write 保持开），但提示词约束「先提纲、不重构代码」。
    assert excluded_tools(DOCS) == frozenset({"memory_write"})
    assert "提纲" in DOCS.system_prompt
    assert "不做无关重构" in DOCS.system_prompt

    # 日常与头脑风暴：写类工具（改文件/命令/测试/git）全部禁用——不自动执行高风险操作。
    for readonly in (DAILY, BRAINSTORM):
        denied = excluded_tools(readonly)
        assert WRITE_TOOLS <= denied, readonly.key
        assert "memory_write" in denied

    # 「记住习惯」只在日常出现，且必须用户显式选择。
    assert DAILY.memory_allowed and not BRAINSTORM.memory_allowed and not REVERSE.memory_allowed
    assert "memory_write" in system_prompt(DAILY, memory_opt_in=True)
    assert "敏感事实" in system_prompt(DAILY, memory_opt_in=True)

    # 逆向实验室：读为主但结论要落分析笔记（保留写入），提示词强制证据与「未确认」标注。
    assert excluded_tools(REVERSE) == frozenset({"memory_write"})
    assert "证据" in REVERSE.system_prompt
    assert "未确认" in REVERSE.system_prompt

    # 每个场景都给前端输入框提示（§6「切换场景同步更新输入框提示」的后端半边）。
    for item in list_scenarios():
        assert item["input_hint"], item["key"]
        assert item["allow_write"] == resolve(item["key"]).allow_write
