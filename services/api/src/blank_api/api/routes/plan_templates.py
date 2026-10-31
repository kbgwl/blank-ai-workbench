"""计划模板：把一次任务的计划固化成可再次套用的对象。

解决的问题很具体——同类工作（发布一个版本、接一个新接口、排查一类线上问题）
每次都要重新拆一遍计划，拆完就扔。模板让它变成"选一个再按需改"。

接口刻意只有五个动作：列、建、读、改、删，外加一个 `from-task` 从任务快照。
不做"模板继承/组合"：真到了需要组合的复杂度，用的人已经该直接改步骤了。
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from blank_api.api.auth import require_token
from blank_api.api.errors import ApiError
from blank_api.api.routes.projects import load_project
from blank_api.deps import DbSession
from blank_api.models.plan_template import PlanTemplate
from blank_api.models.task import Task
from blank_api.schemas.plan import PlanTemplateIn, PlanTemplateOut, PlanTemplateUpdate

router = APIRouter(tags=["plans"], dependencies=[Depends(require_token)])


async def _load(db: DbSession, template_id: str) -> PlanTemplate:
    template = await db.get(PlanTemplate, template_id)
    if template is None:
        raise ApiError("plan_template_not_found", "计划模板不存在", 404)
    return template


@router.get("/projects/{project_id}/plan-templates", response_model=list[PlanTemplateOut])
async def list_plan_templates(
    project_id: str,
    db: DbSession,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[PlanTemplateOut]:
    """常用在前。`usage_count` 是这份模板到底有用没用的唯一证据，
    按它排比按创建时间排更接近"我想要哪个"。"""
    await load_project(db, project_id)
    stmt = (
        select(PlanTemplate)
        .where(PlanTemplate.project_id == project_id)
        .order_by(PlanTemplate.usage_count.desc(), PlanTemplate.created_at.desc())
        .limit(limit)
    )
    return [
        PlanTemplateOut.model_validate(item) for item in (await db.execute(stmt)).scalars()
    ]


@router.post(
    "/projects/{project_id}/plan-templates", response_model=PlanTemplateOut, status_code=201
)
async def create_plan_template(
    project_id: str, payload: PlanTemplateIn, db: DbSession
) -> PlanTemplateOut:
    project = await load_project(db, project_id)
    template = PlanTemplate(
        id=str(uuid.uuid4()),
        project_id=project.id,
        name=payload.name,
        description=payload.description,
        goal_spec=payload.goal_spec or {},
        steps=payload.steps,
        milestones=payload.milestones,
        usage_count=0,
        source_task_id=None,
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)
    return PlanTemplateOut.model_validate(template)


@router.get("/plan-templates/{template_id}", response_model=PlanTemplateOut)
async def get_plan_template(template_id: str, db: DbSession) -> PlanTemplateOut:
    return PlanTemplateOut.model_validate(await _load(db, template_id))


@router.patch("/plan-templates/{template_id}", response_model=PlanTemplateOut)
async def update_plan_template(
    template_id: str, payload: PlanTemplateUpdate, db: DbSession
) -> PlanTemplateOut:
    template = await _load(db, template_id)
    for key in ("name", "description", "goal_spec", "steps", "milestones"):
        value = getattr(payload, key)
        if value is not None:
            setattr(template, key, value)
    await db.commit()
    await db.refresh(template)
    return PlanTemplateOut.model_validate(template)


@router.delete("/plan-templates/{template_id}", status_code=204)
async def delete_plan_template(template_id: str, db: DbSession) -> None:
    template = await _load(db, template_id)
    await db.delete(template)
    await db.commit()
    return None


@router.post(
    "/tasks/{task_id}/plan-template", response_model=PlanTemplateOut, status_code=201
)
async def save_task_plan_as_template(
    task_id: str, payload: PlanTemplateUpdate, db: DbSession
) -> PlanTemplateOut:
    """把某个任务的当前计划存成模板。

    这是模板的主要来源：用户看见一份跑通的计划，说"以后都按这个来"。
    状态一律抹成 pending——模板存的是做法，不是这一次做到哪了。
    """
    task = await db.get(Task, task_id)
    if task is None:
        raise ApiError("task_not_found", "任务不存在", 404)

    steps = [
        {**step, "status": "pending", "evidence": ""}
        for step in (task.plan_steps or [])
        if isinstance(step, dict)
    ]
    if not steps:
        raise ApiError("plan_empty", "这条任务还没有计划步骤，没什么可存的", 400)

    template = PlanTemplate(
        id=str(uuid.uuid4()),
        project_id=task.project_id,
        name=(payload.name or task.title)[:120],
        description=payload.description or "",
        goal_spec=dict(task.goal_spec or {}),
        steps=steps,
        milestones=[
            {**item, "status": "pending"}
            for item in (task.milestones or [])
            if isinstance(item, dict)
        ],
        usage_count=0,
        source_task_id=task.id,
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)
    return PlanTemplateOut.model_validate(template)
