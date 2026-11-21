"""enter_worktree / exit_worktree：任务级工作树会话，对齐 Claude 的 EnterWorktree。

这两个工具只做两件事：换掉这个任务的工作根，以及（可选）在建好的树里保留/删除工作。
所有工具都从 ``ToolContext.root`` 取路径、cwd 与安全策略的路径基准，所以换根只换
这一处——shell、文件工具、git、子代理装配线会一起跟过去，不存在"一半在旧树里"。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from blank_api.services import worktree_session
from blank_api.services.settings import SettingsStore
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok
from blank_api.services.tools.file_state import FileStateCache


def _exit_risk(arguments: dict) -> str:
    # 删除是不可逆的：走审批；keep 只是切回原工作区。
    return "high" if str(arguments.get("action") or "") == "remove" else "medium"


def _restore_risk(arguments: dict) -> str:
    # 只看不动：低。真回滚会退掉提交与未提交改动：高。
    return "high" if str(arguments.get("action") or "") == "restore" else "low"


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


def _base_ref(context: ToolContext) -> str:
    """worktree.baseRef：按 用户 < 项目 < 本机 < 管理 叠一次，读生效值。"""
    settings = SettingsStore().read_layered(context.root).settings
    section = settings.get("worktree") if isinstance(settings, dict) else None
    # 分层合并在 settings_layers 里已经把 baseRef/base_ref 折成同一个键；
    # 这里只读生效值，不再自己猜别名，避免两层解释不一致。
    value = str((section or {}).get("base_ref") or "").strip().lower()
    if value not in worktree_session.BASE_REFS:
        value = worktree_session.BASE_REFS[0]
    return value


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
            base_ref=_base_ref(context),
        )
    except ValueError as exc:
        return fail("error", str(exc))

    _switch_root(context, session.root)
    await _record(context, "worktree.entered", {**session.as_dict(), "mode": "path" if path else "name"})

    if session.owned:
        text = (
            f"已进入 worktree「{session.name}」（分支 {session.branch}，"
            f"基准 {session.base_ref}，位置 {session.display}）。"
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
    # 写者唯一没守住时必须说出来。这类"流程通了但性质没达成"最容易被当成成功：
    # 进去之后一切照旧，直到两个任务的改动互相覆盖才发现。不改成拒绝——.git 只读
    # 的环境里用户仍要能干活——但模型必须知道自己没有独占。
    if session.owner_status == worktree_session.OWNER_UNAVAILABLE:
        text += (
            "\n注意：这棵工作树的独占登记没能完成（读不到或写不进它的 git 目录），"
            "所以无法确认没有别的任务在同时写它。改动前先确认一下，"
            "有冲突的话换一棵用 name 新建的树。"
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


async def _restore_worktree(context: ToolContext, arguments: dict) -> ToolOutcome:
    """回滚到进树时的基准：先看清改了什么（plan），再退（restore）。

    分两步不是为了好看：``restore`` 会退掉已提交的改动，属于不可逆动作，
    调用方得先看到那份改动清单再决定。``plan`` 只读。
    """
    action = str(arguments.get("action") or "").strip()
    if action not in ("plan", "restore"):
        return fail("error", "action 只能是 plan 或 restore")
    raw_paths = arguments.get("paths")
    if raw_paths is None:
        paths: list[str] | None = None
    elif isinstance(raw_paths, list):
        paths = [str(item) for item in raw_paths]
    else:
        return fail("error", "paths 必须是路径数组；整棵回滚不传这个参数")
    name = str(arguments.get("worktree") or "").strip()

    try:
        if action == "plan":
            report = await asyncio.to_thread(
                worktree_session.plan_restore,
                context.task_id,
                name=name,
                search=Path(context.root),
            )
        else:
            report = await asyncio.to_thread(
                worktree_session.restore,
                context.task_id,
                paths=paths,
                name=name,
                search=Path(context.root),
            )
    except ValueError as exc:
        return fail("error", str(exc))

    if action == "plan":
        if not report.changed:
            return ok(
                f"相对基准 {report.baseline[:8]} 没有改动，无需回滚。",
                baseline=report.baseline,
                changed=[],
                clean=report.clean,
                action=action,
            )
        listed = "\n".join(f"- {item}" for item in report.changed[:40])
        more = "" if len(report.changed) <= 40 else f"\n…（共 {len(report.changed)} 个）"
        return ok(
            f"相对基准 {report.baseline[:8]} 有 {len(report.changed)} 个路径被改动：\n"
            f"{listed}{more}\n"
            '确认要回滚就再调用一次 action="restore"；只退其中几个用 paths 指定。',
            baseline=report.baseline,
            changed=report.changed,
            clean=report.clean,
            revision=report.revision,
            action=action,
        )

    session = worktree_session.get(context.task_id)
    if session is not None:
        # 盘上的内容已经变了：文件读缓存必须作废，否则下一次读取拿到的还是旧版本。
        _switch_root(context, session.root)
    await _record(
        context,
        "worktree.restored",
        {
            "action": action,
            "baseline": report.baseline,
            "changed": report.changed,
            "restored": report.restored,
            "removed": report.removed,
            "commits_dropped": report.commits_dropped,
            "dirty_after": report.dirty_after,
            "revision": report.revision,
            "clean": report.clean,
        },
    )

    parts = [f"已回滚到基准 {report.baseline[:8]}"]
    if report.restored:
        parts.append(f"恢复 {len(report.restored)} 个文件")
    if report.removed:
        parts.append(f"删除 {len(report.removed)} 个基准之后新建的文件")
    if report.commits_dropped:
        parts.append(f"退掉 {report.commits_dropped} 个提交")
    text = "，".join(parts) + "。"
    if report.clean:
        text += "工作树已干净。"
    else:
        text += (
            f"但仍有 {len(report.dirty_after)} 个路径有改动（可能是基准之前就存在的未跟踪文件）："
            + "、".join(report.dirty_after[:5])
        )
    return ok(
        text,
        baseline=report.baseline,
        changed=report.changed,
        restored=report.restored,
        removed=report.removed,
        commits_dropped=report.commits_dropped,
        dirty_after=report.dirty_after,
        revision=report.revision,
        clean=report.clean,
        action=action,
    )


def worktree_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="enter_worktree",
            description=(
                "创建或进入一个隔离的 git worktree，之后本任务的工具都在那棵树里执行。"
                "只在用户或项目指令明确要求 worktree 时使用；name 新建（新分支），"
                "path 进入 git worktree list 里已有的那棵。新建时基准由设置 "
                "worktree.baseRef 决定：fresh（默认）从 origin/<默认分支>，head 从当前"
                "本地 HEAD；没有可用的远端跟踪分支时回落本地 HEAD。"
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
        ToolDefinition(
            name="restore_worktree",
            description=(
                "把工作树回滚到 enter_worktree 时记下的基准。action=\"plan\" 先列相对基准"
                "改动的路径（含提交过的与 shell 新建的），action=\"restore\" 才真的回滚——"
                "整棵回滚会退掉本任务在这棵树里做的提交。只退部分文件用 paths 指定。"
                "没有基准线的树（老版本或手动建的）会拒绝，不做猜测性回滚。"
            ),
            input_schema={
                "type": "object",
                "required": ["action"],
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["plan", "restore"],
                        "description": "plan 只看改动清单；restore 真的回滚",
                    },
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "只回滚这些路径（工作树内相对路径）；不传表示整棵回滚",
                    },
                    "worktree": {
                        "type": "string",
                        "description": "不在会话里时，用 .blank-worktrees/ 下的目录名指定那棵树",
                    },
                },
            },
            risk_level="high",
            risk_for=_restore_risk,
            handler=_restore_worktree,
            timeout_seconds=180.0,
        ),
    ]
