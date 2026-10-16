import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select

from blank_api.api.auth import require_token
from blank_api.api.errors import ApiError
from blank_api.deps import DbSession
from blank_api.models.skill import MemoryCandidate, Skill
from blank_api.schemas.skill import (
    MemoryCandidateCreate,
    MemoryCandidateOut,
    SkillOut,
    SkillUpdate,
)

router = APIRouter(prefix="/skills", tags=["skills"], dependencies=[Depends(require_token)])


@router.post("/memory-candidates", response_model=MemoryCandidateOut, status_code=201)
async def create_memory_candidate(payload: MemoryCandidateCreate, db: DbSession) -> MemoryCandidateOut:
    candidate = MemoryCandidate(id=str(uuid.uuid4()), text=payload.text, source=payload.source)
    db.add(candidate)
    await db.commit()
    await db.refresh(candidate)
    return MemoryCandidateOut.model_validate(candidate)


@router.get("/memory-candidates", response_model=list[MemoryCandidateOut])
async def list_memory_candidates(db: DbSession) -> list[MemoryCandidateOut]:
    rows = (await db.execute(select(MemoryCandidate).order_by(MemoryCandidate.created_at.desc()))).scalars()
    return [MemoryCandidateOut.model_validate(row) for row in rows]


@router.post("/memory-candidates/{candidate_id}/accept", response_model=SkillOut, status_code=201)
async def accept_memory_candidate(candidate_id: str, db: DbSession) -> SkillOut:
    candidate = await db.get(MemoryCandidate, candidate_id)
    if candidate is None:
        raise ApiError(code="memory_candidate_not_found", message="记忆候选不存在", status_code=404)
    if candidate.status != "pending":
        raise ApiError(code="memory_candidate_reviewed", message="记忆候选已经审核过", status_code=409)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    candidate.status = "accepted"
    candidate.reviewed_at = now
    skill = Skill(
        id=str(uuid.uuid4()),
        name="日常习惯",
        instructions=candidate.text,
        status="draft",
        version=1,
        source_candidate_id=candidate.id,
        history=[],
    )
    db.add(skill)
    await db.commit()
    await db.refresh(skill)
    return SkillOut.model_validate(skill)


@router.post("/memory-candidates/{candidate_id}/reject", response_model=MemoryCandidateOut)
async def reject_memory_candidate(candidate_id: str, db: DbSession) -> MemoryCandidateOut:
    candidate = await db.get(MemoryCandidate, candidate_id)
    if candidate is None:
        raise ApiError(code="memory_candidate_not_found", message="记忆候选不存在", status_code=404)
    if candidate.status != "pending":
        raise ApiError(code="memory_candidate_reviewed", message="记忆候选已经审核过", status_code=409)
    candidate.status = "rejected"
    candidate.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    return MemoryCandidateOut.model_validate(candidate)


@router.get("", response_model=list[SkillOut])
async def list_skills(db: DbSession) -> list[SkillOut]:
    rows = (await db.execute(select(Skill).order_by(Skill.updated_at.desc()))).scalars()
    return [SkillOut.model_validate(row) for row in rows]


@router.patch("/{skill_id}", response_model=SkillOut)
async def update_skill(skill_id: str, payload: SkillUpdate, db: DbSession) -> SkillOut:
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise ApiError(code="skill_not_found", message="Skill 不存在", status_code=404)
    if payload.name is not None:
        skill.name = payload.name
    if payload.instructions is not None and payload.instructions != skill.instructions:
        history = list(skill.history or [])
        history.append({"version": skill.version, "instructions": skill.instructions})
        skill.history = history[-20:]
        skill.instructions = payload.instructions
        skill.version += 1
    await db.commit()
    await db.refresh(skill)
    return SkillOut.model_validate(skill)


@router.post("/{skill_id}/enable", response_model=SkillOut)
async def enable_skill(skill_id: str, db: DbSession) -> SkillOut:
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise ApiError(code="skill_not_found", message="Skill 不存在", status_code=404)
    skill.status = "active"
    await db.commit()
    return SkillOut.model_validate(skill)


@router.post("/{skill_id}/disable", response_model=SkillOut)
async def disable_skill(skill_id: str, db: DbSession) -> SkillOut:
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise ApiError(code="skill_not_found", message="Skill 不存在", status_code=404)
    skill.status = "disabled"
    await db.commit()
    return SkillOut.model_validate(skill)


@router.post("/{skill_id}/rollback", response_model=SkillOut)
async def rollback_skill(skill_id: str, db: DbSession) -> SkillOut:
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise ApiError(code="skill_not_found", message="Skill 不存在", status_code=404)
    history = list(skill.history or [])
    if not history:
        raise ApiError(code="skill_no_history", message="Skill 没有可回滚版本", status_code=409)
    previous = history.pop()
    skill.history = history
    skill.instructions = str(previous["instructions"])
    skill.version += 1
    await db.commit()
    return SkillOut.model_validate(skill)
