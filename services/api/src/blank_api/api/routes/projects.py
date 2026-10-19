import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from blank_api.api.auth import require_token
from blank_api.api.errors import ApiError
from blank_api.deps import DbSession
from blank_api.models.checkpoint import AgentCheckpoint
from blank_api.models.model_provider import ModelUsage
from blank_api.models.project import Project, Workspace
from blank_api.models.task import AgentEvent, Task, ToolCall
from blank_api.models.thread import Thread
from blank_api.schemas.project import (
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    WorkspaceCreate,
    WorkspaceOut,
)
from blank_api.services.workspace import mask_path, validate_workspace_path

router = APIRouter(prefix="/projects", tags=["projects"], dependencies=[Depends(require_token)])


async def load_project(db: AsyncSession, project_id: str) -> Project:
    """Fetch a project with workspaces eagerly loaded (async-safe)."""
    project = (
        await db.execute(
            select(Project).options(selectinload(Project.workspaces)).where(Project.id == project_id)
        )
    ).scalar_one_or_none()
    if project is None:
        raise ApiError(code="project_not_found", message="项目不存在", status_code=404)
    return project


def workspace_out(workspace: Workspace) -> WorkspaceOut:
    return WorkspaceOut(
        id=workspace.id,
        label=workspace.label,
        path=mask_path(workspace.path),
        is_active=workspace.is_active,
    )


def project_out(project: Project) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        name=project.name,
        description=project.description,
        workspaces=[workspace_out(w) for w in project.workspaces],
    )


@router.get("", response_model=list[ProjectOut])
async def list_projects(db: DbSession) -> list[ProjectOut]:
    rows = (
        await db.execute(
            select(Project).options(selectinload(Project.workspaces)).order_by(Project.created_at.desc())
        )
    ).scalars()
    return [project_out(p) for p in rows]


@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(payload: ProjectCreate, db: DbSession) -> ProjectOut:
    project = Project(
        id=str(uuid.uuid4()), name=payload.name, description=payload.description, workspaces=[]
    )
    db.add(project)
    await db.commit()
    return project_out(project)


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: str, db: DbSession) -> ProjectOut:
    return project_out(await load_project(db, project_id))


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(project_id: str, payload: ProjectUpdate, db: DbSession) -> ProjectOut:
    project = await load_project(db, project_id)
    if payload.name is not None:
        project.name = payload.name
    if payload.description is not None:
        project.description = payload.description
    await db.commit()
    return project_out(project)


@router.delete("/{project_id}", status_code=204)
async def delete_project(project_id: str, db: DbSession) -> None:
    project = await load_project(db, project_id)
    # 任务及其执行痕迹只靠 task_id 关联，删项目必须一并清掉，否则列表里会出现孤儿任务。
    task_ids = list((await db.execute(select(Task.id).where(Task.project_id == project.id))).scalars().all())
    if task_ids:
        for model in (AgentEvent, ToolCall, ModelUsage, AgentCheckpoint):
            await db.execute(delete(model).where(model.task_id.in_(task_ids)))
        await db.execute(delete(Task).where(Task.project_id == project.id))
    # 会话排在任务之后：tasks.thread_id 是指向它的外键，反过来删会先撞上引用。
    # 分叉出来的会话同属一个项目，也一并清掉，不留孤儿行。
    await db.execute(delete(Thread).where(Thread.project_id == project.id))
    await db.delete(project)
    await db.commit()


@router.post("/{project_id}/workspaces", response_model=WorkspaceOut, status_code=201)
async def bind_workspace(project_id: str, payload: WorkspaceCreate, db: DbSession) -> WorkspaceOut:
    project = await load_project(db, project_id)
    resolved = validate_workspace_path(payload.path)
    existing = next((w for w in project.workspaces if w.path == str(resolved)), None)
    if existing is not None:
        return workspace_out(existing)
    workspace = Workspace(
        id=str(uuid.uuid4()),
        project_id=project.id,
        path=str(resolved),
        label=payload.label or resolved.name,
        is_active=not project.workspaces,
    )
    db.add(workspace)
    await db.commit()
    return workspace_out(workspace)


@router.delete("/{project_id}/workspaces/{workspace_id}", status_code=204)
async def unbind_workspace(project_id: str, workspace_id: str, db: DbSession) -> None:
    project = await load_project(db, project_id)
    workspace = next((w for w in project.workspaces if w.id == workspace_id), None)
    if workspace is None:
        raise ApiError(code="workspace_not_found", message="工作区不存在", status_code=404)
    await db.delete(workspace)
    await db.commit()
