"""自学习的查看与撤销：经验、使用者模型、体检告警。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from blank_api.api.auth import require_token
from blank_api.api.errors import ApiError
from blank_api.api.routes.projects import load_project
from blank_api.deps import DbSession
from blank_api.services import learning
from blank_api.services.workspace import mask_path

router = APIRouter(prefix="/projects", tags=["learning"], dependencies=[Depends(require_token)])


def _workspace(project):
    workspace = project.active_workspace
    if workspace is None:
        raise ApiError(code="workspace_missing", message="项目还没有绑定工作区", status_code=409)
    return workspace


@router.get("/{project_id}/learning")
async def read_learning(project_id: str, db: DbSession) -> dict:
    project = await load_project(db, project_id)
    workspace = project.active_workspace
    if workspace is None:
        return learning.empty_snapshot()
    return learning.snapshot(workspace.path)


@router.post("/{project_id}/learning/experience/{fingerprint}/retire")
async def retire_experience(project_id: str, fingerprint: str, db: DbSession) -> dict:
    project = await load_project(db, project_id)
    workspace = _workspace(project)
    retired = learning.retire_experience(workspace.path, fingerprint)
    if not retired:
        raise ApiError(code="learning_entry_not_found", message="没有找到这条经验", status_code=404)
    return {"retired": retired}


@router.post("/{project_id}/learning/persona/{fingerprint}/retire")
async def retire_preference(project_id: str, fingerprint: str, db: DbSession) -> dict:
    project = await load_project(db, project_id)
    workspace = _workspace(project)
    if not learning.retire_preference(workspace.path, fingerprint):
        raise ApiError(code="learning_entry_not_found", message="没有找到这条偏好", status_code=404)
    return {"ok": True, "fingerprint": fingerprint, "workspace": mask_path(workspace.path)}


@router.post("/{project_id}/learning/persona/{fingerprint}/promote")
async def promote_preference(project_id: str, fingerprint: str, db: DbSession) -> dict:
    project = await load_project(db, project_id)
    workspace = _workspace(project)
    if not learning.promote_preference(workspace.path, fingerprint):
        raise ApiError(
            code="learning_entry_not_found",
            message="没有找到这条偏好，或它处于冲突/已退役状态",
            status_code=404,
        )
    return {"ok": True, "fingerprint": fingerprint, "workspace": mask_path(workspace.path)}
