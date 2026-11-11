"""enter_worktree / exit_worktree：任务级工作树会话，对齐 Claude 的 EnterWorktree。

这两个工具只做两件事：换掉这个任务的工作根，以及（可选）在建好的树里保留/删除工作。
所有工具都从 ``ToolContext.root`` 取路径、cwd 与安全策略的路径基准，所以换根只换
这一处——shell、文件工具、git、子代理装配线会一起跟过去，不存在"一半在旧树里"。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from blank_api.services import worktree_session
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok
from blank_api.services.tools.file_state import FileStateCache


def _exit_risk(arguments: dict) -> str:
    # 删除是不可逆的：走审批；keep 只是切回原工作区。
    return "high" if str(arguments.get("action") or "") == "remove" else "medium"


def _switch_root(context: ToolContext, root: Path) -> None:
    """把任务根切到 root。

    ToolContext 是 frozen dataclass：这里改的是"这个任务此刻在哪"这一格会话状态，
    和 harness/library.py 的归一化同一手法。读后写状态必须一起换——相对路径在
    两棵树里指向不同文件，"在 A 里读过"不能当"在 B 里读过"。
    """
    object.__setattr__(context, "root", Path(root))
    if context.file_state is not None:
        object.__setattr__(context, "file_state", FileStateCache())


async def _record(context: ToolContext, event: str, payload: dict) -> None:
    recorder = getattr(context, "recorder", None)
    if recorder is not None:
        await recorder.add(event, "worktree", payload)


async def _enter_worktree(context: ToolContext, arguments: dict) -> ToolOutcome:
    name = str(arguments.get("name") or "").strip()
    path = str(arguments.get("path") or "").strip()
    try:
        session = await asyncio.to_thread(
            worktree_session.enter,
            Path(context.root),
            context.task_id,
            name=name,
            path=path,
        )
    except ValueError as exc:
        return fail("error", str(exc))

    _switch_root(context, session.root)
    await _record(context, "worktree.entered", {**session.as_dict(), "mode": "path" if path else "name"})

    if session.owned:
        text = (
            f"已进入 worktree「{session.name}」（分支 {session.branch}，位置 {session.display}）。"
            "接下来所有工具调用都在这个工作树里执行；"
            '结束后用 exit_worktree(action="keep") 保留改动，或 action="remove" 删除。'
        )
    else:
        text = (
            f"已进入已有 worktree「{session.name}」（位置 {session.display}"
            f"{'，分支 ' + session.branch if session.branch else ''}）。"
            "接下来所有工具调用都在这个工作树里执行；这棵树不是本任务创建的，"
            '退出时只能 action="keep"。'
        )
    return ok(text, **session.tool_data())


async def _exit_worktree(context: ToolContext, arguments: dict) -> ToolOutcome:
    action = str(arguments.get("action") or "").strip()
    discard = bool(arguments.get("discard_changes"))
    try:
        report = await asyncio.to_thread(
            worktree_session.exit_session,
            context.task_id,
            action=action,
            discard_changes=discard,
        )
    except ValueError as exc:
        return fail("error", str(exc))

    session = report.session
    if session is None:
        return ok("当前不在 worktree 会话里，无需退出。", action=action, noop=True)
    _switch_root(context, session.origin)
    await _record(
        context,
        "worktree.exited",
        {
            **session.as_dict(),
            "action": action,
            "removed": report.removed,
            "branch_deleted": report.branch_deleted,
            "branch_error": report.branch_error,
        },
    )

    if report.removed:
        text = (
            f"已退出并删除 worktree「{session.name}」"
            f"{'及其分支 ' + session.branch if report.branch_deleted else ''}，回到原来的工作区。"
        )
        if session.branch and not report.branch_deleted:
            text += f"分支 {session.branch} 未能删除：{report.branch_error}"
    else:
        text = (
            f"已退出 worktree「{session.name}」，工作树保留在 {session.display}"
            f"{'（分支 ' + session.branch + '）' if session.branch else ''}，回到原来的工作区。"
        )
    return ok(
        text,
        action=action,
        removed=report.removed,
        branch=session.branch,
        path=session.display,
        branch_deleted=report.branch_deleted,
        branch_error=report.branch_error,
    )


def worktree_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="enter_worktree",
            description=(
                "创建或进入一个隔离的 git worktree，之后本任务的工具都在那棵树里执行。"
                "只在用户或项目指令明确要求 worktree 时使用；name 新建（新分支），"
                "path 进入 git worktree list 里已有的那棵。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "新 worktree 的名字：字母/数字/点/下划线/短横线，最多 64 字符；不传自动生成",
                    },
                    "path": {
                        "type": "string",
                        "description": "进入一棵已经存在的 worktree（必须是 git worktree list 列出的）",
                    },
                },
            },
            risk_level="medium",
            handler=_enter_worktree,
            timeout_seconds=180.0,
        ),
        ToolDefinition(
            name="exit_worktree",
            description=(
                "退出当前 worktree 会话并回到原工作区。keep 保留工作树；"
                "remove 删除本任务创建的工作树及其分支——有未提交改动或未合并提交时会拒绝，"
                "确认丢弃才传 discard_changes=true。path 进入的工作树只能 keep。"
            ),
            input_schema={
                "type": "object",
                "required": ["action"],
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["keep", "remove"],
                        "description": "keep 保留工作树；remove 删除它",
                    },
                    "discard_changes": {
                        "type": "boolean",
                        "description": "remove 时连同未提交改动一起丢弃；默认 false",
                    },
                },
            },
            risk_level="medium",
            risk_for=_exit_risk,
            handler=_exit_worktree,
            timeout_seconds=180.0,
        ),
    ]
