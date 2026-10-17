"""P1-4：子代理 + worktree 隔离。

子代理的价值在"独立的上下文窗口"，风险在"脱离监督的写权限"。所以这里
既验证隔离副本真的不碰真实工作区，也验证子代理没有第二套审批通道。
"""

from pathlib import Path

from blank_api.services.subagent import (
    ISOLATED_TOOLS,
    MAX_DEPTH,
    READ_ONLY_TOOLS,
    SubAgentRequest,
    SubAgentRunner,
)
from blank_api.services.worktree import create_worktree, remove_worktree, render_diff

from tests.test_agent_v04 import (
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,  # noqa: F401  (复用阶段3 的夹具)
)


# --------------------------------------------------------------------------- #
# 1. worktree
# --------------------------------------------------------------------------- #
def test_worktree_copies_workspace_and_reports_diff(workspace: Path) -> None:
    tree = create_worktree(workspace, label="probe")
    try:
        assert tree.path.is_dir()
        assert (tree.path / "main.py").read_text(encoding="utf-8") == "print('hi')\n"
        # 真实工作区不受影响
        (tree.path / "main.py").write_text("print('sub')\n", encoding="utf-8")
        (tree.path / "notes.md").write_text("新文件\n", encoding="utf-8")
        assert (workspace / "main.py").read_text(encoding="utf-8") == "print('hi')\n"
        diff = render_diff(tree)
        assert "print('sub')" in diff
        assert "notes.md" in diff
    finally:
        remove_worktree(tree)
    assert not tree.path.exists()


def test_worktree_excludes_heavy_directories(workspace: Path) -> None:
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "big.js").write_text("x", encoding="utf-8")
    tree = create_worktree(workspace, label="ignore")
    try:
        assert not (tree.path / "node_modules").exists()
    finally:
        remove_worktree(tree)


# --------------------------------------------------------------------------- #
# 2. 请求归一化与工具面
# --------------------------------------------------------------------------- #
def test_subagent_request_clamps_budget() -> None:
    request = SubAgentRequest(goal=" 查一下 ", max_steps=99, timeout_seconds=9999).normalized()
    assert request.goal == "查一下"
    assert request.max_steps == 8
    assert request.timeout_seconds == 300.0
    # 0 / 负数按"没设"处理，落到默认值；显式负数则夹到下限
    assert SubAgentRequest(goal="x", max_steps=0).normalized().max_steps == 4
    assert SubAgentRequest(goal="x", max_steps=-5).normalized().max_steps == 1


def test_subagent_tool_surface_has_no_recursion_and_no_approval_tools() -> None:
    assert "spawn_agent" not in READ_ONLY_TOOLS
    assert "spawn_agent" not in ISOLATED_TOOLS
    assert "request_user_input" not in ISOLATED_TOOLS
    # 只读面里不能出现写工具
    assert "apply_patch" not in READ_ONLY_TOOLS
    assert "run_shell" not in READ_ONLY_TOOLS
    assert MAX_DEPTH == 1


async def test_runner_refuses_without_a_bound_session() -> None:
    result = await SubAgentRunner()(SubAgentRequest(goal="x"))
    assert result.ok is False
    assert "未绑定" in result.error


# --------------------------------------------------------------------------- #
# 3. 端到端
# --------------------------------------------------------------------------- #
async def test_read_only_subagent_returns_a_summary(env, workspace: Path):  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["查清 main.py 的内容"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 派子代理侦察"},
            {
                "content": "派一个只读子代理看看",
                "tool_calls": [call("spawn_agent", {"goal": "读取 main.py 并复述内容"})],
            },
            # 子代理的模型调用（只读，可直接给结论）
            {"content": "main.py 里只有一行 print('hi')"},
            {"content": "子代理确认 main.py 只有 print('hi')"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    types = [event["type"] for event in events]
    assert "subagent.started" in types and "subagent.completed" in types, types

    started = [event for event in events if event["type"] == "subagent.started"][0]
    assert started["payload"]["depth"] == 1
    assert started["payload"]["agent_path"].startswith("root/sub-")
    assert started["payload"]["parent_thread_id"]

    calls = await env.tool_calls(task_id)
    spawned = [item for item in calls if item.name == "spawn_agent"][0]
    assert spawned.status == "ok"
    assert "print('hi')" in spawned.result_summary


async def test_isolated_subagent_writes_only_into_the_copy(env, workspace: Path):  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["先出方案再决定要不要改"])
    patch = "--- /dev/null\n+++ b/review.md\n@@ -0,0 +1,1 @@\n+待评审\n"
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 让子代理在副本里试改"},
            {
                "content": "派隔离子代理写方案",
                "tool_calls": [
                    call("spawn_agent", {"goal": "写一份 review.md 方案草稿", "isolate": True, "max_steps": 3})
                ],
            },
            # 子代理：写文件
            {"content": "写方案", "tool_calls": [call("apply_patch", {"path": "review.md", "patch": patch})]},
            {"content": "草稿已写在副本里"},
            {"content": "方案已产出，等你确认是否落地"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    # 真实工作区必须干净
    assert not (workspace / "review.md").exists()

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    types = [event["type"] for event in events]
    assert "worktree.created" in types and "worktree.removed" in types, types
    created = [event for event in events if event["type"] == "worktree.created"][0]
    assert created["payload"]["mode"] in ("git", "copy")

    calls = await env.tool_calls(task_id)
    spawned = [item for item in calls if item.name == "spawn_agent"][0]
    assert "review.md" in (spawned.arguments or {}).get("goal", "")
    assert spawned.status == "ok"
