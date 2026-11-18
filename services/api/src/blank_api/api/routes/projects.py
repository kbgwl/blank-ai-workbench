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
from blank_api.services.workspace import git_branch
from blank_api.schemas.project import (
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    WorkspaceCreate,
    WorkspaceOut,
)
from blank_api.services.commands_defs import discover_commands
from blank_api.services.output_styles_defs import discover_output_styles
from blank_api.services.plugins_defs import (
    PROJECT_PLUGINS_DIR,
    discover_plugins,
    user_plugins_dir,
)
from blank_api.services.settings import SettingsStore
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
        branch=git_branch(workspace.path),
    )


def project_out(project: Project) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        name=project.name,
        description=project.description,
        # 手写构造必须逐字段带出来：ProjectOut 里 pinned/position 有默认值，
        # 漏写不会报错，只会永远显示 False/0（本轮实测踩过）。
        pinned=project.pinned,
        position=project.position,
        workspaces=[workspace_out(w) for w in project.workspaces],
    )


@router.get("", response_model=list[ProjectOut])
async def list_projects(db: DbSession) -> list[ProjectOut]:
    rows = (
        await db.execute(
            select(Project).options(selectinload(Project.workspaces)).order_by(Project.pinned.desc(), Project.position.asc(), Project.created_at.desc())
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


@router.get("/{project_id}/commands")
async def list_project_commands(project_id: str, db: DbSession) -> dict:
    """该工作区可用的自定义斜杠命令（用户层 + 项目层，项目同名覆盖）。

    只返回审计信息与一句话说明，不返回正文：前端要做的是把名字和
    ``argument-hint`` 列出来，正文等用户真的敲下这条命令再展开。
    """
    project = await load_project(db, project_id)
    workspace = project.active_workspace
    return discover_commands(workspace.path if workspace is not None else None).as_dict()


@router.get("/{project_id}/output-styles")
async def list_project_output_styles(project_id: str, db: DbSession) -> dict:
    """该工作区可用的输出样式（用户层 + 项目层），以及当前选中/实际生效的是哪个。

    和 commands 一样只返回审计信息，不返回正文：正文会整段进系统提示，
    列表接口没有理由把它端到前端。``active`` 与运行时一致——认不出的名字
    静默回落 default。
    """
    project = await load_project(db, project_id)
    workspace = project.active_workspace
    discovery = discover_output_styles(workspace.path if workspace is not None else None)
    layered = SettingsStore().read_layered(workspace.path if workspace is not None else None)
    selected = str(layered.settings.get("output_style") or "default")
    active = discovery.resolve(selected)
    return {
        **discovery.as_dict(),
        "selected": selected,
        "active": active.name if active is not None else "default",
    }


@router.get("/{project_id}/plugins")
async def list_project_plugins(project_id: str, db: DbSession) -> dict:
    """该工作区可用的插件包（用户层 + 项目层，项目同名覆盖）。

    只返回清单与审计面：启用状态来自设置级联（``enabledPlugins`` /
    ``plugins.enabled``），组件只给数量，正文仍由各加载器在任务启动时读取。
    ``userDir`` 是用户层插件的绝对路径；``projectDir`` 给工作区下的相对路径，
    不回显宿主绝对路径（与清单里的 ``path`` 同一打码口径）。
    """
    project = await load_project(db, project_id)
    workspace = project.active_workspace
    root = workspace.path if workspace is not None else None
    catalog = discover_plugins(root)
    return {
        **catalog.as_dict(),
        "userDir": str(user_plugins_dir()),
        "projectDir": str(PROJECT_PLUGINS_DIR) if root is not None else "",
    }


@router.get("/{project_id}/settings")
async def read_project_settings(project_id: str, db: DbSession) -> dict:
    """该工作区叠出来的生效设置与来源。

    设置页写的是用户层；项目层/本机层随仓库走，可以覆盖偏好段。安全段
    （``safety`` / ``permissions`` / ``sandbox`` / ``guardian`` / ``features``）只认
    用户层与管理策略层——项目文件里写了也会出现在 ``layers[].ignored``。
    返回的是生效值 + 审计面，不返回任何一层文件的原始文本。
    """
    project = await load_project(db, project_id)
    workspace = project.active_workspace
    layered = SettingsStore().read_layered(workspace.path if workspace is not None else None)
    return {
        "settings": layered.settings,
        **layered.as_dict(),
        "user_path": mask_path(str(SettingsStore().path)),
    }


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(project_id: str, payload: ProjectUpdate, db: DbSession) -> ProjectOut:
    project = await load_project(db, project_id)
    if payload.name is not None:
        project.name = payload.name
    if payload.description is not None:
        project.description = payload.description
    if payload.pinned is not None:
        project.pinned = payload.pinned
    if payload.position is not None:
        project.position = payload.position
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
