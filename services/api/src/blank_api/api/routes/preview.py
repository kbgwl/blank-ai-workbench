"""工作区应用预览：启动/查询/停止一个长驻的 ``node <文件>`` 服务。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends

from blank_api.api.auth import require_token
from blank_api.api.errors import ApiError
from blank_api.api.routes.projects import load_project
from blank_api.deps import DbSession
from blank_api.services.preview import PreviewError, preview_manager
from blank_api.services.workspace import mask_path

router = APIRouter(prefix="/projects", tags=["preview"], dependencies=[Depends(require_token)])


def _workspace(project):
    workspace = project.active_workspace
    if workspace is None:
        raise ApiError(code="workspace_missing", message="项目还没有绑定工作区", status_code=409)
    return workspace


@router.post("/{project_id}/preview")
async def start_preview(project_id: str, db: DbSession) -> dict:
    project = await load_project(db, project_id)
    workspace = _workspace(project)
    try:
        data = await preview_manager.start(project_id, Path(workspace.path))
    except PreviewError as exc:
        raise ApiError(code=exc.code, message=exc.message, status_code=exc.status) from exc
    # 宿主路径不回显；URL 只含回环地址与随机端口，可以给前端。
    return {**data, "workspace": mask_path(workspace.path)}


@router.get("/{project_id}/preview")
async def read_preview(project_id: str, db: DbSession) -> dict:
    project = await load_project(db, project_id)
    _workspace(project)
    return await preview_manager.status(project_id)


@router.delete("/{project_id}/preview")
async def stop_preview(project_id: str, db: DbSession) -> dict:
    project = await load_project(db, project_id)
    _workspace(project)
    return await preview_manager.stop(project_id)
