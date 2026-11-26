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
from blank_api.services.agent.dispatch import route
from blank_api.services.agent.scenarios import (
    BRAINSTORM,
    DEV,
    DOCS,
    DAILY,
    REVERSE,
    ScenarioPolicy,
    WRITE_TOOLS,
    excluded_tools,
    list_scenarios,
    resolve,
    system_prompt,
    write_allowed,
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
    # 编程场景有自己的纪律段（对照 Claude Code），但绝不带记忆写入段。
    dev_prompt = system_prompt(DEV, memory_opt_in=True)
    assert "Claude Code" in dev_prompt
    assert "记住习惯" not in dev_prompt


def test_readonly_scenarios_deny_write_tools():
    # daily 已放开为 Codex 全能 agent（能读写跑命令），不再是只读场景。
    # 历史遗留的 brainstorm 仍是只读。
    scenario = resolve("brainstorm")
    denied = excluded_tools(scenario)
    assert {"apply_patch", "run_shell", "run_tests", "git"} <= denied


def test_scenario_policies_are_distinct_but_keep_the_safety_floor():
    # 编程：代码规划 + 代码验证 + 满预算；日常：简洁规划 + 通用验证 + 收紧预算。
    assert DEV.policy.planning == "code"
    assert DEV.policy.verification == "code"
    assert DEV.policy.budget_scale == 1.0
    assert DAILY.policy.planning == "general"
    assert DAILY.policy.verification == "general"
    assert DAILY.policy.budget_scale < 1.0
    # 日常的“通用验证”不是不验证：涉及代码/页面仍要真跑，安全下限不降。
    assert "真跑验证" in system_prompt(DAILY)

    items = {item["key"]: item for item in list_scenarios()}
    assert items["dev"]["planning"] == "code"
    assert items["daily"]["planning"] == "general"
    assert items["daily"]["budget_scale"] < items["dev"]["budget_scale"]


def test_scenario_budget_scale_actually_shrinks_daily_budget():
    dev = route("实现一个功能", scenario="dev")
    daily = route("实现一个功能", scenario="daily")

    assert daily.budget.max_steps < dev.budget.max_steps
    assert daily.budget.max_seconds < dev.budget.max_seconds


def test_scenario_policy_rejects_invalid_values():
    for kwargs in ({"planning": "guess"}, {"verification": "vibes"}, {"budget_scale": 1.5}):
        try:
            ScenarioPolicy(**kwargs)
        except ValueError:
            continue
        raise AssertionError(f"应当拒绝非法策略：{kwargs}")


def test_daily_prompt_keeps_the_codex_style_discipline():
    """日常对标 Codex 的四条：少问、最简做法、做完核对、先给结论。

    这四条是「会聊天但不动手」与 Codex 日常体感最明显的分界；钉在提示词里，
    以后改措辞不能悄悄丢。
    """
    prompt = DAILY.system_prompt
    assert "直接动手" in prompt
    assert "一次只问一个" in prompt
    assert "标准库" in prompt
    assert "做完必须核对结果" in prompt
    assert "先给结论" in prompt
    assert "简体中文" in prompt


def test_scenario_catalog_is_stable():
    items = list_scenarios()
    # 产品只开两个场景：编程与日常。文档 / 头脑风暴 / 逆向仍留在 SCENARIOS 里
    # 供老任务跑完，但不再出现在选择器里，也不接受新建。
    assert [item["key"] for item in items] == ["dev", "daily"]
    assert [item["label"] for item in items] == ["编程", "日常"]
    # 编程对照 Claude Code 的纪律，日常对照 Codex 的通用 agent 口径。
    assert "Claude Code" in DEV.system_prompt
    assert "Codex" in DAILY.system_prompt
    # 只有日常场景带记忆开关——前端按这个字段决定显示与否。
    assert [item["key"] for item in items if item["memory_allowed"]] == ["daily"]


# ── 端到端 ──────────────────────────────────────────────────────────────────


async def test_scenarios_endpoint_lists_catalog(env):  # noqa: F811
    resp = await env.client.get(f"{API}/scenarios")
    assert resp.status_code == 200
    keys = [item["key"] for item in resp.json()]
    assert keys[0] == "dev" and "daily" in keys


async def test_daily_scenario_allows_and_runs_write_tools(env, workspace):  # noqa: F811
    """日常场景放开为 Codex 全能 agent：写类工具可见，模型调用真的执行。"""
    await create_provider(env)
    project_id = await create_project(env, workspace)
    resp = await env.client.post(
        f"{API}/projects/{project_id}/tasks",
        json={
            "title": "日常改文件",
            "goal": "把 README 的 x 改成 y",
            "acceptance_criteria": [],
            "max_steps": 6,
            "scenario": "daily",
        },
    )
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["id"]
    assert (await _task_row(task_id)).scenario == "daily"

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
    # 放开的核心可判定事实：写类工具进下发列表（模型看得见），且调用真的发生、
    # 没有在意图判定时被拒。mock 工具只记账不写盘，所以断言到"调用成功"为止。
    assert "apply_patch" in started["tools"]
    calls = await env.tool_calls(task_id)
    patch_calls = [row for row in calls if row.name == "apply_patch"]
    assert patch_calls
    row = await _task_row(task_id)
    assert row.status == "succeeded"
    # 老版本这里会是一句"日常场景不会直接修改文件"的拒绝，现在不该再出现。
    assert "日常场景不会直接修改文件" not in (row.result_summary or "")


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
        f"{API}/tasks/{task_id}/run", json={"scenario": "daily"}
    )
    assert run.status_code == 200, run.text
    assert (await _started(task_id))["scenario"] == "daily"
    assert (await _task_row(task_id)).scenario == "daily"

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
    # 编程：读写代码 + 跑测试全开；记忆仍禁（日常习惯不进编程会话）。
    assert excluded_tools(DEV) == frozenset({"memory_write"})
    assert "Claude Code" in DEV.system_prompt
    assert "最小必要" in DEV.system_prompt

    # 文档：能落文档文件（allow_write 保持开），但提示词约束「先提纲、不重构代码」。
    # v164：权限细化到文件类型——写文档要保留 apply_patch，跑命令/跑测试/git 拿掉，
    # 因为它们能绕开扩展名限制改任何东西。R1：monitor 也执行命令，同样拿掉。
    assert excluded_tools(DOCS) == frozenset(
        {"memory_write", "run_shell", "run_tests", "git", "monitor"}
    )
    assert "apply_patch" not in excluded_tools(DOCS)
    assert write_allowed(DOCS, "README.md")
    assert write_allowed(DOCS, "docs/guide.txt")
    assert not write_allowed(DOCS, "src/main.py")
    assert not write_allowed(DOCS, "package.json")
    assert "提纲" in DOCS.system_prompt
    assert "不做无关重构" in DOCS.system_prompt

    # 日常已放开为 Codex 全能 agent：读写、命令、测试、git 全开，只禁记忆写入
    # （记忆写入仍要用户显式选"记住"）。头脑风暴保持只读。
    assert excluded_tools(DAILY) == frozenset({"memory_write"})
    assert write_allowed(DAILY, "README.md")
    assert write_allowed(DAILY, "src/main.py")
    assert "通用 agent" in DAILY.system_prompt

    for readonly in (BRAINSTORM,):
        denied = excluded_tools(readonly)
        assert WRITE_TOOLS <= denied, readonly.key
        assert "memory_write" in denied

    # 「记住习惯」只在日常出现，且必须用户显式选择。
    assert DAILY.memory_allowed and not BRAINSTORM.memory_allowed and not REVERSE.memory_allowed
    assert "memory_write" in system_prompt(DAILY, memory_opt_in=True)
    assert "敏感事实" in system_prompt(DAILY, memory_opt_in=True)

    # 逆向实验室：读为主但结论要落分析笔记（保留写入），提示词强制证据与「未确认」标注。
    # v164：不跑项目测试、不提交、不跑 shell——这三件事都在改被分析的那个仓库；
    # 写笔记走独立分析区（见 runtime 的 write_root 与网关的路径重定向）。
    # R1：monitor 也是「在被分析仓库里执行东西」，一并拿掉。
    assert excluded_tools(REVERSE) == frozenset(
        {"memory_write", "run_shell", "run_tests", "git", "monitor"}
    )
    assert write_allowed(REVERSE, "analysis/report.md")
    assert not write_allowed(REVERSE, "sample.exe")
    assert "证据" in REVERSE.system_prompt
    assert "未确认" in REVERSE.system_prompt

    # 每个场景都给前端输入框提示（§6「切换场景同步更新输入框提示」的后端半边）。
    for item in list_scenarios():
        assert item["input_hint"], item["key"]
        assert item["allow_write"] == resolve(item["key"]).allow_write
