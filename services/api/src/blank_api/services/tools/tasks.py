"""任务清单：TaskCreate / TaskGet / TaskUpdate / TaskList。

对标 Claude Code 2.1.271 的 Task* 工具（`sdk-tools.d.ts` 的输入/输出契约 + `claude.exe`
里的嵌入提示）。从客户端实现里核实过的语义：

- 任务落盘，一个任务一个 ``<id>.json``；id 是十进制数字、单调递增，删除后**不复用**
  （高水位单独记）。TaskList 按数字 id 升序，不是文件名字典序。
- ``blocks`` / ``blockedBy`` 是互为引用的两张表：给 A 加 ``addBlocks=["B"]`` 会同时写
  A.blocks 和 B.blockedBy；删除任务时把其它任务里对它的引用一并清掉。
- ``metadata`` 是合并而不是覆盖：值为 ``null`` 表示删掉这个键。
- ``TaskList`` 隐藏 ``metadata._internal`` 为真的任务，并把 blockedBy 里已完成的任务
  过滤掉——已经做完的阻塞者不再是阻塞。
- ``TaskCreated`` / ``TaskCompleted`` 是可阻断钩子：创建被拒要把刚建的任务删回去，
  完成被拒时**任何字段都不落盘**。

与 ``update_plan`` 的分工：``update_plan`` 是给用户看的阶段计划（同一时刻只有一个
in_progress）；任务清单是执行侧的工作项，允许依赖图与并行。两家工具并存正是 Claude
的做法（TodoWrite 与 Task* 同时存在）。

存储默认 ``~/.blank/tasks/<task_id>/``（团队任务按 group_id 共享一份），
``BLANK_TASKS_DIR`` 可改根目录。任务清单不进工作区：它不属于仓库内容。
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import uuid
from pathlib import Path

from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok

TASKS_DIR_ENV = "BLANK_TASKS_DIR"
HIGH_WATER_FILE = ".high_water"
MAX_ID_ATTEMPTS = 50
STATUSES = ("pending", "in_progress", "completed")
UPDATE_STATUSES = STATUSES + ("deleted",)
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class TaskStoreError(RuntimeError):
    """任务清单落盘失败：磁盘不可写、id 分配重试用尽等。"""


# 同一目录的写入串行化。键带 loop 身份：不同事件循环不能共用一把 asyncio.Lock。
_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}


def _lock_for(directory: Path) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = (id(loop), str(directory))
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", value or "").strip("._")
    return cleaned[:80] or "default"


def _store_key(context: ToolContext) -> str:
    """团队任务共享一份清单（按群），普通任务按根任务隔离。"""
    team = getattr(context, "team_scope", None) or {}
    group = str(team.get("group_id") or "") if isinstance(team, dict) else ""
    if group:
        return f"team-{_safe_name(group)}"
    return _safe_name(str(getattr(context, "task_id", "") or ""))


def _dir_for(context: ToolContext) -> Path:
    base = os.environ.get(TASKS_DIR_ENV) or str(Path.home() / ".blank" / "tasks")
    return Path(base) / _store_key(context)


def _task_number(task_id: str) -> int | None:
    try:
        return int(task_id)
    except (TypeError, ValueError):
        return None


def _valid_id(task_id: str) -> bool:
    return bool(SAFE_ID.fullmatch(task_id or ""))


def _normalize(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    task_id = str(raw.get("id") or "")
    if not _valid_id(task_id) or not isinstance(raw.get("subject"), str):
        return None
    blocks = raw.get("blocks")
    blocked = raw.get("blockedBy")
    task = dict(raw)
    task["id"] = task_id
    task["description"] = str(raw.get("description") or "")
    task["status"] = raw.get("status") if raw.get("status") in STATUSES else "pending"
    task["blocks"] = [str(item) for item in blocks] if isinstance(blocks, list) else []
    task["blockedBy"] = [str(item) for item in blocked] if isinstance(blocked, list) else []
    metadata = raw.get("metadata")
    if isinstance(metadata, dict):
        task["metadata"] = dict(metadata)
    else:
        task.pop("metadata", None)
    return task


def _read_task(directory: Path, task_id: str) -> dict | None:
    if not _valid_id(task_id):
        return None
    try:
        raw = json.loads((directory / f"{task_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    task = _normalize(raw)
    if task is None or task["id"] != task_id:
        return None
    return task


def _all_tasks(directory: Path) -> list[dict]:
    if not directory.exists():
        return []
    tasks: list[dict] = []
    try:
        paths = list(directory.glob("*.json"))
    except OSError:
        return []
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        task = _normalize(raw)
        if task is not None:
            tasks.append(task)
    tasks.sort(key=lambda item: (_task_number(item["id"]) or 0, item["id"]))
    return tasks


def _write_task(directory: Path, task: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{task['id']}.json"
    scratch = directory / f".{task['id']}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        scratch.write_text(
            json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        os.replace(scratch, target)
    except OSError as error:
        try:
            scratch.unlink()
        except OSError:
            pass
        raise TaskStoreError(f"任务清单写入失败：{error}") from error


def _high_water(directory: Path) -> int:
    highest = 0
    try:
        data = json.loads((directory / HIGH_WATER_FILE).read_text(encoding="utf-8"))
        highest = max(0, int(data.get("highest") or 0))
    except (OSError, ValueError, TypeError, AttributeError):
        highest = 0
    for task in _all_tasks(directory):
        highest = max(highest, _task_number(task["id"]) or 0)
    return highest


def _set_high_water(directory: Path, highest: int) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / HIGH_WATER_FILE).write_text(
            json.dumps({"highest": int(highest)}, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except OSError as error:
        raise TaskStoreError(f"任务清单高水位写入失败：{error}") from error


def _claim_task(directory: Path, fields: dict) -> dict:
    """分配下一个 id 并以独占方式落盘；已被占用的 id 顺延，绝不覆盖。"""
    directory.mkdir(parents=True, exist_ok=True)
    highest = _high_water(directory)
    for _ in range(MAX_ID_ATTEMPTS):
        candidate = highest + 1
        task = {"id": str(candidate), **fields}
        path = directory / f"{candidate}.json"
        try:
            handle = open(path, "x", encoding="utf-8", newline="\n")
        except FileExistsError:
            highest = candidate
            continue
        except OSError as error:
            raise TaskStoreError(f"任务清单写入失败：{error}") from error
        with handle:
            handle.write(json.dumps(task, ensure_ascii=False, indent=2) + "\n")
        _set_high_water(directory, candidate)
        return task
    raise TaskStoreError(
        f"连续 {MAX_ID_ATTEMPTS} 次都没拿到空闲任务 id：目录里的 {highest + 1} 号文件"
        "存在但不是可读任务，先清理该目录再重试"
    )


def _delete_task(directory: Path, task_id: str, *, cleanup_refs: bool = True) -> bool:
    if not _valid_id(task_id):
        return False
    try:
        (directory / f"{task_id}.json").unlink()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise TaskStoreError(f"任务删除失败：{error}") from error
    number = _task_number(task_id)
    if number is not None:
        try:
            if number > _high_water(directory):
                _set_high_water(directory, number)
        except TaskStoreError:
            pass
    if cleanup_refs:
        for other in _all_tasks(directory):
            blocks = [item for item in other["blocks"] if item != task_id]
            blocked = [item for item in other["blockedBy"] if item != task_id]
            if blocks != other["blocks"] or blocked != other["blockedBy"]:
                other["blocks"] = blocks
                other["blockedBy"] = blocked
                _write_task(directory, other)
    return True


def _link(directory: Path, blocker_id: str, blocked_id: str) -> bool:
    """建立 blocker -> blocked 的双向引用；任一端不存在时什么都不做。"""
    blocker = _read_task(directory, blocker_id)
    blocked = _read_task(directory, blocked_id)
    if blocker is None or blocked is None:
        return False
    if blocked_id not in blocker["blocks"]:
        blocker["blocks"].append(blocked_id)
        _write_task(directory, blocker)
    if blocker_id not in blocked["blockedBy"]:
        blocked["blockedBy"].append(blocker_id)
        _write_task(directory, blocked)
    return True


def _string_list(value: object) -> list[str] | None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return list(value)


async def _hook_denials(context: ToolContext, event: str, task: dict) -> list[str]:
    hooks = getattr(context, "hooks", None)
    if hooks is None:
        return []
    results = await hooks.run(
        event,
        str(task.get("subject") or ""),
        {
            "task_id": task.get("id"),
            "subject": task.get("subject"),
            "description": task.get("description"),
        },
    )
    return [
        result.reason or f"{event} 钩子阻止了这次操作"
        for result in results
        if getattr(result, "denied", False)
    ]


# --------------------------------------------------------------------------- #
# 工具处理
# --------------------------------------------------------------------------- #


async def _task_create(context: ToolContext, arguments: dict) -> ToolOutcome:
    subject = str(arguments.get("subject") or "").strip()
    description = str(arguments.get("description") or "").strip()
    if not subject:
        return fail("error", "TaskCreate 需要 subject：一句话说清要做什么")
    if not description:
        return fail("error", "TaskCreate 需要 description：把要做什么、怎么算做完写清楚")
    active_form = arguments.get("activeForm")
    if active_form is not None and not isinstance(active_form, str):
        return fail("error", "activeForm 必须是字符串")
    metadata = arguments.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        return fail("error", "metadata 必须是 JSON 对象")

    fields: dict = {
        "subject": subject,
        "description": description,
        "status": "pending",
        "blocks": [],
        "blockedBy": [],
    }
    if active_form is not None:
        fields["activeForm"] = active_form
    if metadata is not None:
        fields["metadata"] = dict(metadata)

    directory = _dir_for(context)
    async with _lock_for(directory):
        try:
            task = _claim_task(directory, fields)
        except TaskStoreError as error:
            return fail("error", str(error))
        reasons = await _hook_denials(context, "task_created", task)
        if reasons:
            try:
                _delete_task(directory, task["id"], cleanup_refs=False)
            except TaskStoreError:
                pass
            return fail("denied", "TaskCreated 钩子阻止了创建：\n" + "\n".join(reasons))
    return ok(
        f"Task #{task['id']} created successfully: {task['subject']}",
        task={"id": task["id"], "subject": task["subject"]},
    )


async def _task_get(context: ToolContext, arguments: dict) -> ToolOutcome:
    task_id = str(arguments.get("taskId") or "").strip()
    if not task_id:
        return fail("error", "TaskGet 需要 taskId")
    directory = _dir_for(context)
    async with _lock_for(directory):
        task = _read_task(directory, task_id)
    if task is None:
        return ok(f"Task #{task_id} not found", task=None)
    lines = [
        f"Task #{task['id']}: {task['subject']}",
        f"Status: {task['status']}",
        f"Description: {task['description']}",
    ]
    if task["blockedBy"]:
        lines.append("Blocked by: " + ", ".join(f"#{item}" for item in task["blockedBy"]))
    if task["blocks"]:
        lines.append("Blocks: " + ", ".join(f"#{item}" for item in task["blocks"]))
    return ok(
        "\n".join(lines),
        task={
            "id": task["id"],
            "subject": task["subject"],
            "description": task["description"],
            "status": task["status"],
            "blocks": task["blocks"],
            "blockedBy": task["blockedBy"],
        },
    )


async def _task_update(context: ToolContext, arguments: dict) -> ToolOutcome:
    task_id = str(arguments.get("taskId") or "").strip()
    if not task_id:
        return fail("error", "TaskUpdate 需要 taskId")
    status = arguments.get("status")
    if status is not None and status not in UPDATE_STATUSES:
        return fail("error", "status 只能是 pending / in_progress / completed / deleted 之一")
    for key in ("subject", "description", "activeForm", "owner"):
        if key in arguments and not isinstance(arguments[key], str):
            return fail("error", f"{key} 必须是字符串")
    add_blocks = _string_list(arguments.get("addBlocks"))
    if arguments.get("addBlocks") is not None and add_blocks is None:
        return fail("error", "addBlocks 必须是字符串数组")
    add_blocked = _string_list(arguments.get("addBlockedBy"))
    if arguments.get("addBlockedBy") is not None and add_blocked is None:
        return fail("error", "addBlockedBy 必须是字符串数组")
    metadata = arguments.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        return fail("error", "metadata 必须是 JSON 对象")

    directory = _dir_for(context)
    async with _lock_for(directory):
        task = _read_task(directory, task_id)
        if task is None:
            return fail("error", f"Task #{task_id} not found")
        original = copy.deepcopy(task)
        updated = copy.deepcopy(task)
        fields: list[str] = []
        status_change: dict | None = None

        if "subject" in arguments and arguments["subject"] != original.get("subject"):
            updated["subject"] = str(arguments["subject"])
            fields.append("subject")
        if "description" in arguments and arguments["description"] != original.get("description"):
            updated["description"] = str(arguments["description"])
            fields.append("description")
        if "activeForm" in arguments and arguments["activeForm"] != original.get("activeForm"):
            updated["activeForm"] = str(arguments["activeForm"])
            fields.append("activeForm")
        if "owner" in arguments and arguments["owner"] != original.get("owner"):
            updated["owner"] = str(arguments["owner"])
            fields.append("owner")
        team = getattr(context, "team_scope", None) or {}
        member = str(team.get("bot_id") or "") if isinstance(team, dict) else ""
        if (
            status == "in_progress"
            and "owner" not in arguments
            and not original.get("owner")
            and member
        ):
            # 团队任务认领时自动署名，和 Claude 的 task_assignment 一致。
            updated["owner"] = member
            fields.append("owner")
        if metadata is not None:
            merged = dict(original.get("metadata") or {})
            for key, value in metadata.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            updated["metadata"] = merged
            fields.append("metadata")

        if status == "deleted":
            try:
                deleted = _delete_task(directory, task_id)
            except TaskStoreError as error:
                return fail("error", str(error))
            if not deleted:
                return fail("error", f"Task #{task_id} not found")
            return ok(
                f"Updated task #{task_id} deleted",
                success=True,
                taskId=task_id,
                updatedFields=["deleted"],
                statusChange={"from": original["status"], "to": "deleted"},
            )

        if status is not None and status != original["status"]:
            if status == "completed":
                reasons = await _hook_denials(context, "task_completed", updated)
                if reasons:
                    return fail("denied", "\n".join(reasons))
            updated["status"] = status
            fields.append("status")
            status_change = {"from": original["status"], "to": status}

        if updated != original:
            try:
                _write_task(directory, updated)
            except TaskStoreError as error:
                return fail("error", str(error))

        link_fields: list[str] = []
        if add_blocks:
            fresh = [item for item in add_blocks if item not in original["blocks"]]
            for target in fresh:
                try:
                    _link(directory, task_id, target)
                except TaskStoreError as error:
                    return fail("error", str(error))
            if fresh:
                link_fields.append("blocks")
        if add_blocked:
            fresh = [item for item in add_blocked if item not in original["blockedBy"]]
            for blocker in fresh:
                try:
                    _link(directory, blocker, task_id)
                except TaskStoreError as error:
                    return fail("error", str(error))
            if fresh:
                link_fields.append("blockedBy")
        fields.extend(link_fields)

    message = f"Updated task #{task_id} {', '.join(fields)}".rstrip()
    data = {"success": True, "taskId": task_id, "updatedFields": fields}
    if status_change is not None:
        data["statusChange"] = status_change
    return ok(message, **data)


async def _task_list(context: ToolContext, arguments: dict) -> ToolOutcome:
    directory = _dir_for(context)
    async with _lock_for(directory):
        tasks = _all_tasks(directory)
    visible = [
        task for task in tasks if not bool((task.get("metadata") or {}).get("_internal"))
    ]
    completed = {task["id"] for task in visible if task["status"] == "completed"}
    rows: list[dict] = []
    for task in visible:
        row = {
            "id": task["id"],
            "subject": task["subject"],
            "status": task["status"],
            "blockedBy": [item for item in task["blockedBy"] if item not in completed],
        }
        if task.get("owner"):
            row["owner"] = task["owner"]
        rows.append(row)
    if not rows:
        return ok("No tasks found", tasks=[])
    lines = []
    for row in rows:
        owner = f" ({row['owner']})" if row.get("owner") else ""
        blocked = (
            " [blocked by " + ", ".join(f"#{item}" for item in row["blockedBy"]) + "]"
            if row["blockedBy"]
            else ""
        )
        lines.append(f"#{row['id']} [{row['status']}] {row['subject']}{owner}{blocked}")
    return ok("\n".join(lines), tasks=rows)


# --------------------------------------------------------------------------- #
# 工具定义
# --------------------------------------------------------------------------- #

_CREATE_DESCRIPTION = (
    "为当前编程会话创建结构化任务清单，用来跟踪进度、组织多步工作，也把执行到哪一步"
    "展示给用户。\n\n"
    "适用：需要 3 个以上步骤、需要细致规划、计划模式、用户明确要求任务清单、"
    "用户一次给出多条要做的事；收到新指令时立刻把要求落成任务；"
    "开始做某个任务前先把它标成 in_progress；做完立刻标 completed，并补上过程中"
    "发现的新任务。\n\n"
    "不适用：只有一个直白的小任务、三步以内能做完、纯对话或纯信息性回答。"
    "这种时候直接做，不要为了用工具而用。\n\n"
    "字段：subject（祈使句标题）、description（要做什么）、activeForm（进行时文案，"
    "in_progress 时展示，如「正在跑测试」）、metadata（任意附加数据）。"
    "所有任务创建时都是 pending；依赖关系创建后用 TaskUpdate 的 addBlocks / addBlockedBy 连。"
    "创建前先 TaskList 看看有没有重复任务。"
)

_GET_DESCRIPTION = (
    "按 id 取任务完整详情。开始做一个任务之前、或要弄清任务之间的依赖"
    "（它阻塞谁、谁阻塞它）时使用。返回 subject、description、status、blocks、blockedBy。"
    "取到任务后先确认 blockedBy 为空再开工；整体概览用 TaskList。"
)

_UPDATE_DESCRIPTION = (
    "更新任务清单里的一个任务。\n\n"
    "标记完成：做完、不再需要或被替代时 status=completed；只有完全做完才标 completed——"
    "测试红、只做一半、有未解决错误、缺文件/依赖时保持 in_progress。"
    "卡住就新建任务写清要解决什么。\n\n"
    "删除：status=deleted 永久移除并清掉其它任务对它的引用。\n\n"
    "可更新 status/subject/description/activeForm/owner/metadata（按键合并，置 null 删键）/"
    "addBlocks/addBlockedBy。更新前先 TaskGet 读最新状态，避免拿旧快照覆盖。"
)

_LIST_DESCRIPTION = (
    "列出任务清单里所有任务。用来找可做的工作（pending、没有 owner、未被阻塞）、"
    "看整体进度、找被依赖卡住的任务。完成任务后用它发现刚解锁的工作。"
    "有多个可做任务时**优先按 id 从小到大**做，早创建的任务通常给后面的工作铺好了上下文。"
    "摘要包含 subject、status、owner 与 blockedBy（已完成的任务不再计入阻塞）。"
    "看不到任何任务时返回 No tasks found。"
)


def task_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="task_create",
            description=_CREATE_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "任务的简短标题（祈使句）"},
                    "description": {"type": "string", "description": "要做什么"},
                    "activeForm": {
                        "type": "string",
                        "description": "in_progress 时展示的进行时文案，如「正在跑测试」",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "附加在任务上的任意元数据",
                    },
                },
                "required": ["subject", "description"],
            },
            risk_level="low",
            handler=_task_create,
        ),
        ToolDefinition(
            name="task_get",
            description=_GET_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "taskId": {"type": "string", "description": "要读取的任务 id"},
                },
                "required": ["taskId"],
            },
            risk_level="low",
            handler=_task_get,
        ),
        ToolDefinition(
            name="task_update",
            description=_UPDATE_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "taskId": {"type": "string", "description": "要更新的任务 id"},
                    "subject": {"type": "string", "description": "新的任务标题"},
                    "description": {"type": "string", "description": "新的任务描述"},
                    "activeForm": {
                        "type": "string",
                        "description": "in_progress 时展示的进行时文案",
                    },
                    "status": {
                        "type": "string",
                        "enum": list(UPDATE_STATUSES),
                        "description": "新状态；deleted 表示永久移除",
                    },
                    "addBlocks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "本任务阻塞的任务 id 列表",
                    },
                    "addBlockedBy": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "阻塞本任务的任务 id 列表",
                    },
                    "owner": {"type": "string", "description": "新的任务负责人"},
                    "metadata": {
                        "type": "object",
                        "description": "要合并进任务的元数据；置 null 删除该键",
                    },
                },
                "required": ["taskId"],
            },
            risk_level="low",
            handler=_task_update,
        ),
        ToolDefinition(
            name="task_list",
            description=_LIST_DESCRIPTION,
            input_schema={"type": "object", "properties": {}},
            risk_level="low",
            handler=_task_list,
        ),
    ]
