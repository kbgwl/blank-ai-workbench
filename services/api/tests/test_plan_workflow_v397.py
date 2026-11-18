"""v397：Claude Code 计划模式整链移植（计划文件 / 每轮提醒 / 恢复模式 / 只读架构师）。

对标 Claude Code 2.1.271（源码取证：``E:/projects/claude-reverse/report/``）：

* 计划模式靠**每轮附件**生效，不是系统提示写一次；完整版在第 1/6/11… 轮，
  其余给精简版（上游 ``FULL_REMINDER_EVERY_N_ATTACHMENTS = 5``）。
* 计划文件是计划模式下**唯一**可写的目标；它活过压缩与审批中断。
* 退出时回到**进入之前**那个模式（``prePlanMode``），不是一律回 default。
* 退出提醒、重进提醒各一次，防止模型在退出后继续自我约束、或在重进后推翻旧结论。
* 内置 ``plan`` 只读架构师子代理：工具表白名单，产出的方案要收口成
  「实现要点 / 关键文件 / 怎么验证」。
* 待办久未更新时催一次（10 轮一道闸，两道闸都要过）。

判据分两层：纯函数钉文案与节奏，端到端钉"提醒真的进了模型请求"。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.services import plan_mode
from blank_api.services.agent.graph import (
    TODO_TURNS_BETWEEN_REMINDERS,
    TODO_TURNS_SINCE_WRITE,
    _in_scratchpad,
    _turns_since_tool,
    turn_reminders,
)
from blank_api.services.agents_defs import (
    BUILTIN_PLAN,
    BUILTIN_PLAN_AGENT,
    PLAN_AGENT_TOOLS,
    discover_agents,
)
from blank_api.services.permissions import (
    MODE_ACCEPT_EDITS,
    MODE_DEFAULT,
    MODE_PLAN,
    READ_ONLY_TOOLS,
    PermissionPolicy,
)
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.plan import _enter_plan_mode, _exit_plan_mode

from tests.test_agent_v04 import (  # noqa: F401  (复用阶段3 的夹具)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)


def _tool(name: str):
    return next(tool for tool in build_default_tools() if tool.name == name)


def _context(tmp_path: Path, task_id: str = "task-1", mode: str = MODE_DEFAULT):
    policy = PermissionPolicy.build(mode)
    return SimpleNamespace(
        root=tmp_path,
        task_id=task_id,
        permissions=policy,
        turn_state={},
        plan_state={"steps": []},
    )


def _session(context, mode: str):
    return SimpleNamespace(
        permission_mode=mode,
        context=context,
        plan_reminder_turns=0,
        todo_nagged_at=0,
    )


# --------------------------------------------------------------------------- #
# 1. 计划文件：路径稳定、落点固定、读不出来不炸
# --------------------------------------------------------------------------- #
def test_plan_path_is_stable_and_sandbox_visible(tmp_path: Path) -> None:
    rel = plan_mode.plan_relpath("task-1")
    assert rel.startswith(plan_mode.PLAN_DIR + "/")
    assert rel.endswith(".md")
    # 同一个任务两次算出来必须一样：否则"上一轮写的计划"下一轮就找不到了。
    assert rel == plan_mode.plan_relpath("task-1")
    # trace 形状的键不能造出子目录。
    traced = plan_mode.plan_relpath("task:abc:attempt:2")
    # 只有目录那一层斜杠；任务 id 里的冒号被压成短横，不会造出子目录。
    assert traced.count("/") == plan_mode.PLAN_DIR.count("/") + 1
    assert ":" not in traced


def test_plan_read_and_exists_are_quiet_when_missing(tmp_path: Path) -> None:
    assert plan_mode.read_plan(tmp_path, "nope") == ""
    assert plan_mode.plan_exists(tmp_path, "nope") is False
    target = plan_mode.plan_path(tmp_path, "task-1")
    target.parent.mkdir(parents=True)
    target.write_text("1. 改 a.py\n", encoding="utf-8")
    assert plan_mode.plan_exists(tmp_path, "task-1") is True
    assert plan_mode.read_plan(tmp_path, "task-1") == "1. 改 a.py"


def test_plan_dir_is_not_a_deliverable() -> None:
    # 计划文件是 harness 自己的中间产物：算进"改动 N 个文件"就是凭空多一个件。
    assert _in_scratchpad(plan_mode.PLAN_DIR + "/task-1.md") is True
    assert _in_scratchpad(".blank/scratchpad/x.py") is True
    assert _in_scratchpad("src/app.py") is False


def test_plan_files_survive_workspace_cleanup(tmp_path: Path) -> None:
    """收尾清理不搬计划文件：它是跨轮次的上下文，不是一次性暂存。

    搬走的话，同一线程的下一轮会看到"计划文件还不存在"，只能从零重想——
    计划模式就退化成"每轮写一次计划"。
    """
    from blank_api.services.agent.workspace_state import externalize

    plan_file = tmp_path / plan_mode.PLAN_DIR / "keep.md"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text("1. 改 a.py", encoding="utf-8")

    result = externalize(tmp_path, "task-1", state_root=tmp_path / "state")

    assert plan_file.is_file()
    assert all(plan_mode.PLAN_DIR not in item["path"] for item in result["moved"])


# --------------------------------------------------------------------------- #
# 2. 提醒节奏：完整版 1/6/11…，其余精简
# --------------------------------------------------------------------------- #
def test_full_reminder_cadence_matches_upstream() -> None:
    full = [turn for turn in range(1, 13) if plan_mode.reminder_is_full(turn)]
    assert full == [1, 6, 11]


def test_full_and_sparse_carry_the_end_turn_rule() -> None:
    rel = plan_mode.plan_relpath("task-1")
    full = plan_mode.full_reminder(rel, exists=False)
    sparse = plan_mode.sparse_reminder(rel)
    for text in (full, sparse):
        # 收尾铁律：只能以 request_user_input 或 exit_plan_mode 结束。
        assert "request_user_input" in text
        assert "exit_plan_mode" in text
        assert rel in text
    assert "唯一允许编辑的文件" in full
    # 精简版不能把五个阶段又抄一遍，否则"精简"就没有意义。
    assert "### 阶段 1" not in sparse
    assert len(sparse) < len(full) / 3


# --------------------------------------------------------------------------- #
# 3. 进入：设备忘（pre_plan_mode / plan_file）、给计划文件路径
# --------------------------------------------------------------------------- #
async def test_enter_records_pre_mode_and_plan_file(tmp_path: Path) -> None:
    context = _context(tmp_path, mode=MODE_ACCEPT_EDITS)
    outcome = await _enter_plan_mode(context, {})
    assert outcome.ok is True
    assert context.permissions.mode == MODE_PLAN
    assert context.permissions.pre_plan_mode == MODE_ACCEPT_EDITS
    assert context.permissions.plan_file == plan_mode.plan_relpath("task-1")
    assert outcome.data["plan_path"] == context.permissions.plan_file
    assert context.permissions.plan_file in outcome.output


async def test_plan_mode_allows_writing_only_the_plan_file(tmp_path: Path) -> None:
    context = _context(tmp_path, mode=MODE_DEFAULT)
    await _enter_plan_mode(context, {})
    policy = context.permissions
    rel = policy.plan_file

    allowed = policy.decide(_tool("write_file"), {"path": rel, "content": "1. 改 a.py"}, "low")
    assert allowed.allowed is True, allowed
    # 同一个目录下的别的文件也不行：例外按"路径相等"判，不按目录。
    sibling = policy.decide(
        _tool("write_file"), {"path": plan_mode.PLAN_DIR + "/other.md", "content": "x"}, "low"
    )
    assert sibling.denied is True, sibling
    normal = policy.decide(_tool("edit_file"), {"path": "src/app.py", "patch": ""}, "low")
    assert normal.denied is True, normal
    shell = policy.decide(_tool("run_shell"), {"command": "echo hi"}, "low")
    assert shell.denied is True, shell


async def test_plan_mode_without_plan_file_stays_fully_read_only(tmp_path: Path) -> None:
    # 审批恢复路径上如果没把 plan_file 填回来，宁可只读，也不能放宽成"随便写"。
    policy = PermissionPolicy.build(MODE_PLAN)
    denied = policy.decide(_tool("write_file"), {"path": "a.md", "content": "x"}, "low")
    assert denied.denied is True


def test_spawn_agents_is_allowed_in_plan_mode() -> None:
    # 只读架构师要靠它派；子代理按父档位建策略，写类工具在那边一样被拒。
    assert "spawn_agents" in READ_ONLY_TOOLS


# --------------------------------------------------------------------------- #
# 4. 退出：恢复进入前的模式、计划从文件读、空计划不放行
# --------------------------------------------------------------------------- #
async def test_exit_restores_pre_plan_mode(tmp_path: Path) -> None:
    context = _context(tmp_path, mode=MODE_ACCEPT_EDITS)
    await _enter_plan_mode(context, {})
    outcome = await _exit_plan_mode(context, {"plan": "1. 改 a.py"})
    assert outcome.ok is True
    assert context.permissions.mode == MODE_ACCEPT_EDITS
    assert outcome.data["mode"] == MODE_ACCEPT_EDITS
    assert context.permissions.pre_plan_mode == ""
    assert context.turn_state.get("plan_exited") == context.permissions.plan_file


async def test_exit_reads_the_plan_file_when_argument_is_omitted(tmp_path: Path) -> None:
    context = _context(tmp_path)
    await _enter_plan_mode(context, {})
    target = plan_mode.plan_path(tmp_path, "task-1")
    target.parent.mkdir(parents=True)
    target.write_text("1. 改 a.py\n2. 跑 pytest", encoding="utf-8")

    outcome = await _exit_plan_mode(context, {})
    assert outcome.ok is True
    assert outcome.data["plan_from_file"] is True
    assert "跑 pytest" in outcome.data["plan"]


async def test_exit_refuses_an_empty_plan(tmp_path: Path) -> None:
    context = _context(tmp_path)
    await _enter_plan_mode(context, {})
    outcome = await _exit_plan_mode(context, {})
    assert outcome.ok is False
    assert context.permissions.mode == MODE_PLAN  # 没退出
    # 错误信息要直接告诉它怎么办，否则模型只会在同一处反复撞。
    assert "计划文件" in outcome.message


async def test_reentry_after_exit_asks_to_read_the_old_plan(tmp_path: Path) -> None:
    context = _context(tmp_path)
    await _enter_plan_mode(context, {})
    await _exit_plan_mode(context, {"plan": "1. 改 a.py"})
    await _enter_plan_mode(context, {})
    assert context.turn_state.get("plan_reentry") is True


# --------------------------------------------------------------------------- #
# 5. 每轮提醒：全/简交替、退出说一次、待办催办两道闸
# --------------------------------------------------------------------------- #
def test_reminders_alternate_full_and_sparse(tmp_path: Path) -> None:
    context = _context(tmp_path, mode=MODE_PLAN)
    session = _session(context, MODE_PLAN)
    notes = [turn_reminders(session, [])[0] for _ in range(6)]
    assert "### 阶段 1" in notes[0]
    assert "### 阶段 1" not in notes[1]
    assert "### 阶段 1" not in notes[4]
    assert "### 阶段 1" in notes[5]


async def test_exit_reminder_fires_once(tmp_path: Path) -> None:
    context = _context(tmp_path, mode=MODE_PLAN)
    await _enter_plan_mode(context, {})
    session = _session(context, MODE_PLAN)
    turn_reminders(session, [])
    context.permissions.mode = MODE_DEFAULT
    session.permission_mode = MODE_DEFAULT
    context.turn_state["plan_exited"] = context.permissions.plan_file

    first = turn_reminders(session, [])
    assert any("已退出计划模式" in note for note in first)
    second = turn_reminders(session, [])
    assert not any("已退出计划模式" in note for note in second)


def _planning_history(turns_since: int) -> list[dict]:
    history: list[dict] = [
        {"role": "assistant", "content": "开头", "tool_calls": [call("update_plan", {"steps": []})]}
    ]
    history.extend({"role": "assistant", "content": f"第 {i} 步"} for i in range(turns_since))
    return history


def test_turns_since_tool_counts_assistant_turns_only() -> None:
    history = _planning_history(3) + [{"role": "tool", "content": "回执"}] * 5
    assert _turns_since_tool(history, ("update_plan",)) == 3


def test_todo_nag_waits_for_both_gates(tmp_path: Path) -> None:
    context = _context(tmp_path)
    context.plan_state = {"steps": [{"step": "改 a.py", "status": "in_progress"}]}
    session = _session(context, MODE_DEFAULT)

    assert turn_reminders(session, _planning_history(TODO_TURNS_SINCE_WRITE - 1)) == []
    notes = turn_reminders(session, _planning_history(TODO_TURNS_SINCE_WRITE))
    assert any("没更新待办" in note for note in notes)
    assert "改 a.py" in notes[0]

    # 第二道闸：刚催过，再过 9 轮不该再催。
    again = turn_reminders(session, _planning_history(TODO_TURNS_SINCE_WRITE + 9))
    assert again == []
    later = turn_reminders(
        session, _planning_history(TODO_TURNS_SINCE_WRITE + TODO_TURNS_BETWEEN_REMINDERS)
    )
    assert any("没更新待办" in note for note in later)


def test_todo_nag_is_silent_in_plan_mode(tmp_path: Path) -> None:
    context = _context(tmp_path, mode=MODE_PLAN)
    session = _session(context, MODE_PLAN)
    notes = turn_reminders(session, _planning_history(30))
    assert len(notes) == 1
    assert "没更新待办" not in notes[0]


def test_plan_reference_keeps_the_plan_after_compaction() -> None:
    rel = plan_mode.plan_relpath("task-1")
    text = plan_mode.plan_reference(rel, "1. 改 a.py\n2. 跑测试")
    assert rel in text and "改 a.py" in text
    # 空计划不该硬塞一条空提醒。
    assert plan_mode.plan_reference(rel, "   ") == ""


# --------------------------------------------------------------------------- #
# 6. 只读架构师子代理
# --------------------------------------------------------------------------- #
def test_plan_agent_is_builtin_and_read_only(tmp_path: Path) -> None:
    catalog = discover_agents(tmp_path)
    assert BUILTIN_PLAN in catalog
    definition = catalog.get(BUILTIN_PLAN)
    assert definition is not None and definition.builtin
    assert definition.tools == PLAN_AGENT_TOOLS
    assert set(definition.tools) <= READ_ONLY_TOOLS
    # 写类工具一个都不能在表里：白名单漏一个就是"架构师顺手改了代码"。
    assert not {"write_file", "edit_file", "apply_patch", "run_shell"} & set(definition.tools)
    for section in ("实现要点", "关键文件", "怎么验证"):
        assert section in definition.instructions
    # 内置项顺序：默认项在最前，计划类型排在通用项之后。
    assert catalog.names()[:3] == ["general-purpose", "fork", "plan"]


def test_plan_agent_definition_is_stable_across_loads() -> None:
    # 定义是常量对象：任何一次派发都不该就地改它（改了就污染后续所有派发）。
    assert BUILTIN_PLAN_AGENT.tools == PLAN_AGENT_TOOLS
    assert BUILTIN_PLAN_AGENT.disallowed_tools == ()
    assert BUILTIN_PLAN_AGENT.permission_mode == ""


# --------------------------------------------------------------------------- #
# 7. 端到端：提醒真的进了模型请求，计划文件真的被写到了
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_plan_mode_reminder_reaches_the_model(env, workspace: Path) -> None:  # noqa: F811
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["先计划再动手"])
    plan_rel = plan_mode.plan_relpath(task_id)
    env.mock.reset(
        [
            {"content": "先只读调研。", "tool_calls": [call("read_file", {"path": "main.py"})]},
            {"content": "写计划", "tool_calls": [call("write_file", {"path": plan_rel, "content": "1. 改 main.py"})]},
            {"content": "交计划", "tool_calls": [call("exit_plan_mode", {})]},
            {"content": "已完成"},
            {"content": "已完成"},
        ]
    )
    resp = await env.client.post(
        f"{API}/tasks/{task_id}/run",
        json={"provider_id": provider_id, "permission_mode": MODE_PLAN},
    )
    assert resp.status_code == 200, resp.text

    sent = "\n".join(
        str(message.get("content") or "")
        for request in env.mock.requests
        for message in request.get("messages") or []
        if isinstance(message, dict)
    )
    assert "计划模式生效中" in sent
    assert "exit_plan_mode" in sent

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    written = [
        item["payload"]
        for item in events
        if item["type"] == "tool.completed" and item["payload"].get("tool") == "write_file"
    ]
    assert written and written[0]["ok"] is True, written
    # 计划文件真的落在工作区里（外面读不到，模型也读不到）。
    assert (workspace / plan_rel).is_file()
