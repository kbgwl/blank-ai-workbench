"""Task 工具对齐 Claude 的判据（R3）。

四层，每层都能单独失败：

1. **落盘语义**：id 数字单调递增、删除后不复用、高水位单独记；TaskList 按数字 id
   升序而不是文件名字典序；坏文件跳过而不是让整张表消失。
2. **依赖图**：addBlocks / addBlockedBy 写双向引用；重复加不重复写；目标不存在时
   建不了链接但也不炸；删除任务会把其它任务里对它的引用一并清掉。
3. **展示口径**：metadata 合并（null 删键）、TaskList 隐藏 ``metadata._internal``、
   blockedBy 滤掉已完成任务、回执文案与 Claude 一致。
4. **钩子与边界**：TaskCreated 被拒要把刚建的任务删回去；TaskCompleted 被拒时任何
   字段都不落盘；真实子进程退出码 2 走同一套阻断语义；团队按群共享一份清单，普通
   任务按根任务隔离；taskId 带路径分隔符不许越出存储目录。

外部依赖只有 ``BLANK_TASKS_DIR``：每个用例把存储根指到 tmp_path，不碰用户 home。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from blank_api.services.hooks import (
    BLOCKING_EVENTS,
    HOOK_EVENTS,
    HookRunner,
    HookSpec,
)
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.base import ToolContext, validate_arguments
from blank_api.services.tools.registry import ALIASES, CORE_TOOLS, DEFERRED_TOOLS
from blank_api.services.tools.tasks import (
    HIGH_WATER_FILE,
    _dir_for,
    _high_water,
    _read_task,
    task_tools,
)

TOOL_NAMES = ("task_create", "task_get", "task_update", "task_list")


@pytest.fixture(autouse=True)
def tasks_root(tmp_path, monkeypatch):
    root = tmp_path / "tasks"
    monkeypatch.setenv("BLANK_TASKS_DIR", str(root))
    return root


def _context(task_id: str = "task-1", **extra) -> ToolContext:
    return ToolContext(root=Path("."), task_id=task_id, session_factory=None, **extra)


def _tools() -> dict:
    return {tool.name: tool for tool in build_default_tools()}


async def _call(name: str, context: ToolContext, arguments: dict | None = None):
    return await _tools()[name].handler(context, arguments or {})


async def _create(context: ToolContext, subject: str, description: str = "做点什么", **extra):
    arguments = {"subject": subject, "description": description, **extra}
    return await _call("task_create", context, arguments)


def _store_dir(context: ToolContext) -> Path:
    return _dir_for(context)


# --------------------------------------------------------------------------- #
# 1. 落盘语义
# --------------------------------------------------------------------------- #


async def test_create_assigns_sequential_ids_and_pending_status():
    context = _context()
    first = await _create(context, "搭骨架")
    second = await _create(context, "写判据")
    assert (first.data["task"]["id"], second.data["task"]["id"]) == ("1", "2")
    assert first.output == "Task #1 created successfully: 搭骨架"

    task = _read_task(_store_dir(context), "1")
    assert task == {
        "id": "1",
        "subject": "搭骨架",
        "description": "做点什么",
        "status": "pending",
        "blocks": [],
        "blockedBy": [],
    }


async def test_deleted_id_is_never_reused():
    context = _context()
    await _create(context, "一")
    await _create(context, "二")
    deleted = await _call("task_update", context, {"taskId": "2", "status": "deleted"})
    assert deleted.data["updatedFields"] == ["deleted"]
    third = await _create(context, "三")
    assert third.data["task"]["id"] == "3"
    # 高水位落盘：删掉 3 号之后下一次仍是 4，不是 3。
    await _call("task_update", context, {"taskId": "3", "status": "deleted"})
    fourth = await _create(context, "四")
    assert fourth.data["task"]["id"] == "4"
    # 崩溃窗口：建文件与写高水位之间断电，水位文件会缺一截。删除最高号任务时
    # 必须自己把水位抬回去，否则下一次创建会复用刚删掉的 id。
    (_store_dir(context) / HIGH_WATER_FILE).unlink()
    await _call("task_update", context, {"taskId": "4", "status": "deleted"})
    fifth = await _create(context, "五")
    assert fifth.data["task"]["id"] == "5"


async def test_high_water_survives_directory_reads(tasks_root):
    context = _context()
    await _create(context, "一")
    await _create(context, "二")
    assert _high_water(_store_dir(context)) == 2
    assert (tasks_root / "task-1" / HIGH_WATER_FILE).exists()


async def test_list_sorts_numbered_ids_not_lexicographically():
    context = _context()
    for index in range(12):
        await _create(context, f"任务{index + 1}")
    listed = await _call("task_list", context)
    assert [row["id"] for row in listed.data["tasks"]] == [str(n) for n in range(1, 13)]


async def test_corrupt_task_file_is_skipped_not_fatal():
    context = _context()
    await _create(context, "好任务")
    (_store_dir(context) / "9.json").write_text("{ broken", encoding="utf-8")
    listed = await _call("task_list", context)
    assert [row["id"] for row in listed.data["tasks"]] == ["1"]
    assert _read_task(_store_dir(context), "9") is None


async def test_path_traversal_task_id_is_rejected():
    context = _context()
    await _create(context, "一")
    # 穿越目标真的存在，且内容自称就是被请求的 id：这时只有 id 合法性能拦住它。
    outside = _store_dir(context).parent / "evil.json"
    outside.write_text(
        '{"id": "../evil", "subject": "外部任务", "status": "pending"}', encoding="utf-8"
    )
    outcome = await _call("task_get", context, {"taskId": "../evil"})
    assert outcome.ok is True and outcome.data["task"] is None
    assert outcome.output == "Task #../evil not found"


async def test_parallel_creates_allocate_distinct_ids():
    context = _context()
    outcomes = await asyncio.gather(*[_create(context, f"并发{i}") for i in range(5)])
    ids = sorted(int(item.data["task"]["id"]) for item in outcomes)
    assert ids == [1, 2, 3, 4, 5]
    assert len(list(_store_dir(context).glob("*.json"))) == 5


# --------------------------------------------------------------------------- #
# 2. 依赖图
# --------------------------------------------------------------------------- #


async def test_add_blocks_writes_both_sides():
    context = _context()
    await _create(context, "一")
    await _create(context, "二")
    outcome = await _call(
        "task_update", context, {"taskId": "1", "addBlocks": ["2"], "addBlockedBy": []}
    )
    assert outcome.data["updatedFields"] == ["blocks"]
    assert _read_task(_store_dir(context), "1")["blocks"] == ["2"]
    assert _read_task(_store_dir(context), "2")["blockedBy"] == ["1"]


async def test_add_blocked_by_writes_both_sides():
    context = _context()
    await _create(context, "一")
    await _create(context, "二")
    await _call("task_update", context, {"taskId": "2", "addBlockedBy": ["1"]})
    assert _read_task(_store_dir(context), "2")["blockedBy"] == ["1"]
    assert _read_task(_store_dir(context), "1")["blocks"] == ["2"]


async def test_duplicate_links_are_not_appended_twice():
    context = _context()
    await _create(context, "一")
    await _create(context, "二")
    await _call("task_update", context, {"taskId": "1", "addBlocks": ["2"]})
    again = await _call("task_update", context, {"taskId": "1", "addBlocks": ["2"]})
    assert again.data["updatedFields"] == []
    assert _read_task(_store_dir(context), "1")["blocks"] == ["2"]
    assert _read_task(_store_dir(context), "2")["blockedBy"] == ["1"]


async def test_link_to_missing_task_is_ignored_but_field_still_reported():
    context = _context()
    await _create(context, "一")
    outcome = await _call("task_update", context, {"taskId": "1", "addBlocks": ["99"]})
    # 与 Claude 一致：过滤后非空就把字段记进 updatedFields，链接本身失败不炸。
    assert outcome.data["updatedFields"] == ["blocks"]
    assert _read_task(_store_dir(context), "1")["blocks"] == []


async def test_delete_cleans_references_from_other_tasks():
    context = _context()
    await _create(context, "一")
    await _create(context, "二")
    await _create(context, "三")
    await _call("task_update", context, {"taskId": "2", "addBlocks": ["3"]})
    await _call("task_update", context, {"taskId": "2", "status": "deleted"})
    assert _read_task(_store_dir(context), "3")["blockedBy"] == []
    assert _read_task(_store_dir(context), "1")["blocks"] == []


async def test_get_renders_links_and_full_details():
    context = _context()
    await _create(context, "一", "先做这个")
    await _create(context, "二", "后做这个")
    await _call("task_update", context, {"taskId": "2", "addBlockedBy": ["1"]})
    outcome = await _call("task_get", context, {"taskId": "1"})
    assert outcome.output == (
        "Task #1: 一\nStatus: pending\nDescription: 先做这个\nBlocks: #2"
    )
    assert outcome.data["task"]["blocks"] == ["2"]
    blocked = await _call("task_get", context, {"taskId": "2"})
    assert blocked.output.endswith("Blocked by: #1")


# --------------------------------------------------------------------------- #
# 3. 展示口径
# --------------------------------------------------------------------------- #


async def test_update_reports_changed_fields_and_status_change():
    context = _context()
    await _create(context, "一")
    outcome = await _call(
        "task_update", context, {"taskId": "1", "subject": "新标题", "status": "in_progress"}
    )
    assert outcome.output == "Updated task #1 subject, status"
    assert outcome.data["statusChange"] == {"from": "pending", "to": "in_progress"}
    assert outcome.data["updatedFields"] == ["subject", "status"]


async def test_same_status_is_not_a_change():
    context = _context()
    await _create(context, "一")
    outcome = await _call("task_update", context, {"taskId": "1", "status": "pending"})
    assert outcome.data["updatedFields"] == []
    assert "statusChange" not in outcome.data
    assert outcome.output == "Updated task #1"


async def test_metadata_merges_and_null_deletes_key():
    context = _context()
    await _create(context, "一", metadata={"a": 1, "b": 2})
    await _call("task_update", context, {"taskId": "1", "metadata": {"b": None, "c": 3}})
    task = _read_task(_store_dir(context), "1")
    assert task["metadata"] == {"a": 1, "c": 3}


async def test_list_hides_internal_tasks_and_filters_completed_blockers():
    context = _context()
    await _create(context, "内部", metadata={"_internal": True})
    await _create(context, "一")
    await _create(context, "二")
    await _call("task_update", context, {"taskId": "2", "addBlocks": ["3"]})
    await _call("task_update", context, {"taskId": "2", "status": "completed"})
    listed = await _call("task_list", context)
    assert [row["id"] for row in listed.data["tasks"]] == ["2", "3"]
    assert listed.data["tasks"][1]["blockedBy"] == []
    assert listed.output == "#2 [completed] 一\n#3 [pending] 二"


async def test_list_renders_owner_and_open_blockers():
    context = _context()
    await _create(context, "一")
    await _create(context, "二")
    await _call("task_update", context, {"taskId": "1", "owner": "alice"})
    await _call("task_update", context, {"taskId": "2", "addBlockedBy": ["1"]})
    listed = await _call("task_list", context)
    assert listed.output == "#1 [pending] 一 (alice)\n#2 [pending] 二 [blocked by #1]"


async def test_list_without_tasks_says_so():
    listed = await _call("task_list", _context())
    assert listed.output == "No tasks found"
    assert listed.data["tasks"] == []


async def test_team_tasks_share_one_list_and_autoclaim_owner():
    member_a = _context("task-a", team_scope={"group_id": "g1", "bot_id": "bot-a"})
    member_b = _context("task-b", team_scope={"group_id": "g1", "bot_id": "bot-b"})
    outsider = _context("task-c", team_scope={"group_id": "g2", "bot_id": "bot-c"})

    created = await _create(member_a, "群任务")
    assert (await _call("task_list", member_b)).data["tasks"][0]["id"] == created.data["task"]["id"]
    await _call("task_update", member_b, {"taskId": "1", "status": "in_progress"})
    assert _read_task(_dir_for(member_b), "1")["owner"] == "bot-b"
    assert (await _call("task_list", outsider)).output == "No tasks found"


async def test_plain_tasks_do_not_share_across_task_ids():
    first = _context("task-1")
    second = _context("task-2")
    await _create(first, "一")
    assert (await _call("task_list", second)).output == "No tasks found"


# --------------------------------------------------------------------------- #
# 4. 参数与钩子
# --------------------------------------------------------------------------- #


async def test_create_requires_subject_and_description():
    context = _context()
    missing_subject = await _call("task_create", context, {"description": "x"})
    missing_description = await _call("task_create", context, {"subject": "x"})
    blank = await _call("task_create", context, {"subject": "  ", "description": "x"})
    assert missing_subject.status == missing_description.status == blank.status == "error"
    assert "subject" in missing_subject.message
    assert "description" in missing_description.message
    assert (await _call("task_list", context)).output == "No tasks found"


async def test_bad_metadata_and_link_types_are_rejected():
    context = _context()
    await _create(context, "一")
    bad_metadata = await _call("task_update", context, {"taskId": "1", "metadata": "x"})
    bad_links = await _call("task_update", context, {"taskId": "1", "addBlocks": [1]})
    bad_status = await _call("task_update", context, {"taskId": "1", "status": "done"})
    assert bad_metadata.status == bad_links.status == bad_status.status == "error"


async def test_update_missing_task_returns_error():
    outcome = await _call("task_update", _context(), {"taskId": "9", "status": "completed"})
    assert outcome.ok is False and outcome.status == "error"
    assert outcome.message == "Task #9 not found"


class _RecordingHooks:
    def __init__(self, denied: set[str] | None = None):
        self.denied = denied or set()
        self.calls: list[tuple[str, str, dict]] = []

    async def run(self, event: str, target: str = "", payload: dict | None = None):
        self.calls.append((event, target, dict(payload or {})))
        if event in self.denied:
            return [SimpleNamespace(denied=True, reason=f"{event} 被策略拒绝")]
        return [SimpleNamespace(denied=False, reason="")]


async def test_task_created_denial_rolls_back_the_new_task():
    hooks = _RecordingHooks(denied={"task_created"})
    context = _context(hooks=hooks)
    outcome = await _create(context, "一")
    assert outcome.ok is False and outcome.status == "denied"
    assert "task_created 被策略拒绝" in outcome.message
    assert list(_store_dir(context).glob("*.json")) == []
    assert hooks.calls[0][0] == "task_created"
    assert hooks.calls[0][2]["subject"] == "一"


async def test_task_completed_denial_leaves_every_field_untouched():
    hooks = _RecordingHooks(denied={"task_completed"})
    context = _context(hooks=hooks)
    await _create(context, "一")
    outcome = await _call(
        "task_update", context, {"taskId": "1", "subject": "改标题", "status": "completed"}
    )
    assert outcome.ok is False and outcome.status == "denied"
    task = _read_task(_store_dir(context), "1")
    assert task["status"] == "pending"
    assert task["subject"] == "一"
    assert [call[0] for call in hooks.calls] == ["task_created", "task_completed"]
    assert hooks.calls[-1][2]["task_id"] == "1"


async def test_hooks_absent_does_not_block_anything():
    context = _context()
    created = await _create(context, "一")
    completed = await _call("task_update", context, {"taskId": "1", "status": "completed"})
    assert created.ok and completed.ok


def _hook_script(tmp_path: Path, body: str) -> str:
    path = tmp_path / f"hook_{uuid.uuid4().hex}.py"
    path.write_text(body, encoding="utf-8", newline="\n")
    return f'"{sys.executable}" "{path}"'


async def test_real_task_created_hook_exit_code_2_blocks_and_rolls_back(tmp_path):
    script = _hook_script(
        tmp_path,
        "import json, sys\njson.load(sys.stdin)\nsys.stderr.write('blocked by policy')\n"
        "raise SystemExit(2)\n",
    )
    runner = HookRunner({"task_created": [HookSpec(command=script, timeout=20)]})
    context = _context(hooks=runner)
    outcome = await _create(context, "一")
    assert outcome.ok is False and outcome.status == "denied"
    assert "blocked by policy" in outcome.message
    assert list(_store_dir(context).glob("*.json")) == []


async def test_blocking_task_hook_ignores_async_flag(tmp_path):
    script = _hook_script(
        tmp_path, "import sys\nsys.stderr.write('still blocking')\nraise SystemExit(2)\n"
    )
    runner = HookRunner(
        {"task_completed": [HookSpec(command=script, timeout=20, async_run=True)]}
    )
    context = _context(hooks=runner)
    await _create(context, "一")
    outcome = await _call("task_update", context, {"taskId": "1", "status": "completed"})
    assert outcome.ok is False and outcome.status == "denied"
    assert "still blocking" in outcome.message
    assert _read_task(_store_dir(context), "1")["status"] == "pending"


def test_task_events_are_registered_as_blocking():
    assert {"task_created", "task_completed"} <= set(HOOK_EVENTS)
    assert {"task_created", "task_completed"} <= BLOCKING_EVENTS
    runner = HookRunner.from_env('{"task_created":[{"command":"echo hi"}]}')
    assert "task_created" in runner.hooks


# --------------------------------------------------------------------------- #
# 5. 工具面与接线
# --------------------------------------------------------------------------- #


def test_four_tools_are_registered_as_resident_low_risk():
    tools = _tools()
    for name in TOOL_NAMES:
        tool = tools[name]
        assert tool.risk_level == "low"
        assert tool.requires_approval is False
        assert name not in DEFERRED_TOOLS
        assert name not in CORE_TOOLS  # lean 视图不塞四张任务表
        assert name in ALIASES


def test_schemas_match_claude_contract():
    tools = _tools()
    create = tools["task_create"].input_schema
    assert create["required"] == ["subject", "description"]
    assert set(create["properties"]) == {"subject", "description", "activeForm", "metadata"}
    update = tools["task_update"].input_schema
    assert update["required"] == ["taskId"]
    assert update["properties"]["status"]["enum"] == [
        "pending",
        "in_progress",
        "completed",
        "deleted",
    ]
    assert "addBlocks" in update["properties"] and "addBlockedBy" in update["properties"]
    assert tools["task_list"].input_schema == {"type": "object", "properties": {}}

    assert validate_arguments(create, {"subject": "x"}) == "缺少必填参数：description"
    assert (
        validate_arguments(update, {"taskId": "1", "status": "done"})
        == "参数 status 只能是 pending/in_progress/completed/deleted 之一"
    )


def test_task_tools_have_unique_names_and_handlers():
    names = [tool.name for tool in task_tools()]
    assert names == list(TOOL_NAMES)
    assert all(callable(tool.handler) for tool in task_tools())
    assert len(set(names)) == 4
