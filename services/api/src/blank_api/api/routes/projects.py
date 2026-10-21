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
from blank_api.services.uploads import UploadStore
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
    # 附件按项目分目录，删项目要把它们一起带走：留在磁盘上的图既没人能访问到，
    # 也不会被任何清理逻辑认领，最后就是一堆不知道属于谁的文件。
    UploadStore().clear(project.id)


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
        # 第一绑必然是活动的；此后不再自动抢，改由用户显式切换（activate）。
        # 但历史数据里可能一条活动都没有（旧版解绑不会重指派），那种情况下要补上，
        # 否则新绑的这个在界面上也不是活动态，用户以为没绑成功。
        is_active=not any(w.is_active for w in project.workspaces),
    )
    db.add(workspace)
    await db.commit()
    return workspace_out(workspace)


@router.post("/{project_id}/workspaces/{workspace_id}/activate", response_model=ProjectOut)
async def activate_workspace(project_id: str, workspace_id: str, db: DbSession) -> ProjectOut:
    """切换这个项目"当前在哪个目录里干活"。

    项目可以绑多个目录（比如前后端各一个），但一轮任务只能落在一个根上：快照、
    回滚、路径权限都按根算。所以是"切换"而不是"多选"——同时活着的根有两个，
    回滚时就不知道该往哪个目录里写。
    """
    project = await load_project(db, project_id)
    target = next((w for w in project.workspaces if w.id == workspace_id), None)
    if target is None:
        raise ApiError(code="workspace_not_found", message="工作区不存在", status_code=404)
    for workspace in project.workspaces:
        workspace.is_active = workspace.id == target.id
    await db.commit()
    return project_out(project)


@router.get("/{project_id}/workspaces/{workspace_id}/path")
async def reveal_workspace_path(project_id: str, workspace_id: str, db: DbSession) -> dict:
    """显式要一次真实路径。

    列表接口一律走 ``mask_path``：截图、投屏、分享给人看的时候，主机上装了什么是
    不该顺手泄露的东西。但用户自己配目录时得看得见填的是哪——否则没法判断
    "绑对了没有"。所以真路径只在被点时单独给一次，不做成默认返回字段。
    """
    project = await load_project(db, project_id)
    workspace = next((w for w in project.workspaces if w.id == workspace_id), None)
    if workspace is None:
        raise ApiError(code="workspace_not_found", message="工作区不存在", status_code=404)
    return {"id": workspace.id, "path": workspace.path}


@router.delete("/{project_id}/workspaces/{workspace_id}", status_code=204)
async def unbind_workspace(project_id: str, workspace_id: str, db: DbSession) -> None:
    project = await load_project(db, project_id)
    workspace = next((w for w in project.workspaces if w.id == workspace_id), None)
    if workspace is None:
        raise ApiError(code="workspace_not_found", message="工作区不存在", status_code=404)
    was_active = workspace.is_active
    remaining = [w for w in project.workspaces if w.id != workspace.id]
    await db.delete(workspace)
    # 解掉的正好是活动目录时必须补选一个：不补的话剩下的那些 is_active 全是 false，
    # 界面上显示"0 个活动"，而任务其实还在按 workspaces[0] 兜底跑——看到的状态和行为对不上。
    if was_active and remaining:
        remaining[0].is_active = True
    await db.commit()
