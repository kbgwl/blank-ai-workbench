from fastapi import APIRouter, Depends, Query

from blank_api.api.auth import require_token
from blank_api.api.errors import ApiError
from blank_api.deps import DbSession
from blank_api.api.routes.projects import load_project
from blank_api.services.workspace import list_tree, mask_path, read_text_file

router = APIRouter(prefix="/projects/{project_id}", tags=["files"], dependencies=[Depends(require_token)])


async def _workspace_root(db: DbSession, project_id: str) -> str:
    workspace = (await load_project(db, project_id)).active_workspace
    if workspace is None:
        raise ApiError(code="workspace_not_bound", message="该项目尚未绑定工作区目录", status_code=409)
    return workspace.path


@router.get("/tree")
async def get_tree(
    db: DbSession,
    project_id: str,
    path: str = Query(default=""),
    depth: int = Query(default=2, ge=1, le=6),
) -> dict:
    root = await _workspace_root(db, project_id)
    entries = list_tree(root, path, max_depth=depth)
    return {"root": mask_path(root), "path": path, "entries": [vars(e) for e in entries]}


@router.get("/file")
async def get_file(db: DbSession, project_id: str, path: str = Query(min_length=1)) -> dict:
    root = await _workspace_root(db, project_id)
    return read_text_file(root, path)
