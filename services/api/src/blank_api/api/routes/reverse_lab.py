"""`/reverse-lab` 的后端入口：REA MCP Adapter 的三条动作。

四个端点，一条链路：

* ``GET  /api/v1/reverse-lab/status``        —— 适配器的自述（连没连上、工具表全不全、边界事实）
* ``GET  /api/v1/reverse-lab/targets``       —— 经 ``scan_targets`` 工具列出真实候选实物
* ``GET  /api/v1/reverse-lab/strings``       —— 经 ``find_strings`` 工具取字符串线索
* ``POST /api/v1/reverse-lab/investigations`` —— 经 ``inspect_target`` 工具做一次有台账的调查

口径只有两条，都由判据守着（见 ``tests/test_rea_mcp_adapter_v108.py``）：

1. 适配器不可用时**明确报错**（503 + 服务端自己说的那句话），绝不返回半份工具表，
   也绝不返回一份"成功但什么都没有"的空清单。
2. 解析失败是**调查结果**而不是 HTTP 5xx：响应体带 ``state`` 与指名第几步、被解析对象
   是谁的 ``reason``，同一句话落进 AgentEvent 与 ToolCall 台账。

授权根 = 项目的活动工作区。REA 服务端的 ``--root`` 就是它，越出这条线的路径
在平台既有边界（``resolve_inside``）上就被 403 挡掉，不会走到子进程。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from blank_api.api.auth import require_token
from blank_api.api.errors import ApiError
from blank_api.api.routes.projects import load_project
from blank_api.db import get_session_factory
from blank_api.deps import DbSession
from blank_api.models.task import Task
from blank_api.schemas.task import TaskOut
from blank_api.services.agent.events import EventRecorder
from blank_api.services.rea import adapter
from blank_api.services.workspace import mask_path, resolve_inside

router = APIRouter(prefix="/reverse-lab", tags=["reverse-lab"], dependencies=[Depends(require_token)])


class InvestigationIn(BaseModel):
    project_id: str = Field(min_length=1, max_length=36)
    # 相对项目工作区的产物路径。绝对路径与 .. 由 resolve_inside 拒绝（403）。
    target: str = Field(min_length=1, max_length=500)
    title: str = ""


async def _workspace_root(db: DbSession, project_id: str) -> str:
    """REA 的授权根就是项目的活动工作区——不另外开一条能读宿主的路。"""
    workspace = (await load_project(db, project_id)).active_workspace
    if workspace is None:
        raise ApiError(code="workspace_not_bound", message="该项目尚未绑定工作区目录", status_code=409)
    return workspace.path


def _unavailable(status: dict) -> ApiError:
    """适配器不可用 → 一个说得清 503。

    选 503 而不是 500：这是"上游能力现在不在线"，用户下一步是去修服务端或改配置，
    而不是等我们查崩溃栈。消息里带的是服务端自己说的话（stderr 尾巴 / 工具表缺哪几个）。
    """
    return ApiError(
        code="rea_adapter_unavailable",
        message=(
            f"REA MCP Adapter 当前不可用（状态 {status.get('state')}）："
            f"{status.get('error') or '服务端没有给出原因'}"
        ),
        status_code=503,
    )


@router.get("/status")
async def get_status(
    db: DbSession,
    project_id: str = Query(min_length=1),
    refresh: bool = Query(default=False, description="true 时强制重连一次"),
) -> dict:
    root = await _workspace_root(db, project_id)
    connection = adapter.connection_for(root)
    status = await connection.ensure(refresh=refresh)
    body = status.as_dict()
    if body["state"] == adapter.STATE_FAILED or body["state"] == adapter.STATE_DISABLED:
        # 连上之后根目录被删是另一回事：这里只报状态，不因为"没连上"就报错——
        # /status 是界面用来显示黄条的地方，它自己必须始终能读。
        body["root_exists"] = False
    return {"project_id": project_id, "root": mask_path(root), "adapter": body}


async def _require_adapter(root: str) -> None:
    """能力在线先判。

    授权根被删/没挂载时，最该端出来的是服务端自己那句「授权根目录不存在或不是目录」；
    先走路径校验只会得到一句"路径不存在：."，把整条链路坏了说成用户挑错了文件。
    三个只读端点与调查端点必须同一顺序，否则同一场景两种读法。
    """
    _connection, status = await adapter.ready_connection(root)
    if not status.usable:
        raise _unavailable(status.as_dict())


def _call_error(report: dict, *, action: str) -> ApiError:
    """只读取证动作的失败映射。三种状态三种码，不许都糊成 500。"""
    state = str(report.get("state") or "")
    if state == adapter.STATE_ADAPTER_UNAVAILABLE:
        return _unavailable(report.get("adapter") or {"state": state})
    message = str(report.get("error") or "") or "服务端没有给出原因"
    if state == adapter.STATE_TARGET_UNAVAILABLE:
        return ApiError(
            code="rea_target_unavailable", message=f"{action}失败：{message}", status_code=404
        )
    if state == adapter.STATE_PARTIAL:
        return _unavailable(report.get("adapter") or {"state": state})
    return ApiError(code="rea_call_failed", message=f"{action}失败：{message}", status_code=400)


@router.get("/targets")
async def list_targets(
    db: DbSession,
    project_id: str = Query(min_length=1),
    path: str = Query(default=""),
    limit: int = Query(default=60, ge=1, le=400),
) -> dict:
    """候选实物来自服务端的 ``scan_targets`` 工具。空清单也要能说清为什么空。"""
    root = await _workspace_root(db, project_id)
    await _require_adapter(root)
    # 边界与"存在性"由平台先判：越出工作区是 403，不存在是 404，
    # 不该都被服务端那句"目标不可读"糊成一种。服务端里还有同一道检查（纵深防御）。
    resolve_inside(root, path)
    report = await adapter.scan_targets(root, path=path, limit=limit)
    if not report.get("ok"):
        raise _call_error(report, action="列举候选目标")
    return {
        "project_id": project_id,
        "root": mask_path(root),
        "path": path,
        "adapter": report["adapter"],
        "targets": report["result"].get("targets") or [],
        "truncated": report["result"].get("truncated") or 0,
        "note": report["result"].get("note") or "",
    }


@router.get("/strings")
async def read_strings(
    db: DbSession,
    project_id: str = Query(min_length=1),
    target: str = Query(min_length=1, max_length=500),
    min_length: int = Query(default=4, ge=2, le=64),
    limit: int = Query(default=80, ge=1, le=500),
) -> dict:
    root = await _workspace_root(db, project_id)
    await _require_adapter(root)
    resolve_inside(root, target)
    report = await adapter.find_strings(root, target, min_length=min_length, limit=limit)
    if not report.get("ok"):
        raise _call_error(report, action="抽取字符串线索")
    result = report["result"]
    return {
        "project_id": project_id,
        "target": target,
        "adapter": report["adapter"],
        "strings": result.get("strings") or [],
        "scanned_bytes": result.get("scanned_bytes"),
        "empty_reason": "" if (result.get("strings") or []) else "前导区间里没有达到长度的可读串（不等于目标没有内容）",
        "note": result.get("note") or "",
    }


@router.post("/investigations", status_code=201)
async def create_investigation(payload: InvestigationIn, db: DbSession) -> dict:
    """一次调查 = 一条 Task + 一次经网关的 MCP 工具调用 + 落库的事件与证据。

    这里刻意**不**把解析失败写成 HTTP 错误：调用真的发生了、台账真的落了，
    失败的是被解析的那个实物。把它们混成 500 就再也分不清"链路坏了"和"产物畸形"。
    """
    project = await load_project(db, payload.project_id)
    workspace = project.active_workspace
    if workspace is None:
        raise ApiError(code="workspace_not_bound", message="该项目尚未绑定工作区目录", status_code=409)
    root = workspace.path

    # 先确认能力在线，再校验目标路径（顺序与 _require_adapter 同一口径）。
    _connection, status = await adapter.ready_connection(root)
    if not status.usable:
        raise _unavailable(status.as_dict())

    # 边界仍在创建任务之前：越界（403）与不存在（404）都不该留下一条 Task 行
    resolve_inside(root, payload.target)

    task = Task(
        id=str(uuid.uuid4()),
        project_id=project.id,
        title=(payload.title or f"REA 逆向：{payload.target}")[:200],
        goal=f"用 REA MCP 工具解析 <{payload.target}>，产出可核对的结论（每条带 Evidence ID）",
        status="running",
        acceptance_criteria=[f"实物 <{payload.target}> 的每条结论都有 Evidence ID"],
        acceptance_results=[],
        max_steps=1,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    recorder = EventRecorder(get_session_factory(), task.id, trace_id=task.id)
    await recorder.add(
        "task.created",
        "reverse_lab",
        {"title": task.title, "origin": "reverse_lab", "target": payload.target, "root": mask_path(root)},
    )

    report = await adapter.investigate(
        session_factory=get_session_factory(),
        task_id=task.id,
        root=root,
        target=payload.target,
        project_id=project.id,
    )

    findings = list(report.get("findings") or [])
    reason = str(report.get("reason") or "")
    succeeded = report.get("state") == adapter.STATE_OK
    async with get_session_factory()() as session:
        row = await session.get(Task, task.id)
        if row is not None:
            row.status = "succeeded" if succeeded else "failed"
            row.result_summary = (
                f"{report.get('result', {}).get('format') or ''}｜{len(findings)} 条结论".strip("｜")
                if succeeded
                else reason[:2000]
            )
            row.failure_reason = "" if succeeded else reason[:2000]
            row.acceptance_results = (
                [
                    {
                        "criterion": f"{item.get('kind')}｜{item.get('value')}",
                        "covered": True,
                        "evidence": item.get("evidence_id"),
                        "source": "rea_mcp",
                    }
                    for item in findings[:20]
                ]
                if succeeded
                else [
                    {
                        "criterion": task.acceptance_criteria[0],
                        "covered": False,
                        "evidence": reason or f"调查未完成（{report.get('state')}）",
                        "source": "rea_mcp",
                    }
                ]
            )
            await session.commit()
    await recorder.add(
        "task.completed" if succeeded else "task.failed",
        "complete",
        {
            "status": "succeeded" if succeeded else "failed",
            "reason": "" if succeeded else reason,
            "summary": f"{len(findings)} 条结论" if succeeded else "",
            "state": report.get("state"),
            "tool": report.get("tool"),
            "tool_call_id": report.get("tool_call_id"),
        },
    )

    await db.refresh(task)
    return {
        "task": TaskOut.model_validate(task),
        "state": report.get("state"),
        "ok": bool(report.get("ok")),
        "reason": reason,
        "failure": report.get("failure") or {},
        "findings": findings,
        "result": report.get("result") or {},
        "adapter": report.get("adapter") or status.as_dict(),
        "evidence": {
            "tool": report.get("tool"),
            "tool_call_id": report.get("tool_call_id"),
            "boundary_kind": report.get("boundary_kind"),
            "risk_level": report.get("risk_level"),
            "duration_ms": report.get("duration_ms"),
            "events": f"/api/v1/tasks/{task.id}/events",
            "bundle": f"/api/v1/tasks/{task.id}/evidence",
        },
    }
