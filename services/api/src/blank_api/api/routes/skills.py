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


@router.post("/{skill_id}/accept", response_model=SkillOut)
async def accept_skill_proposal(skill_id: str, db: DbSession) -> SkillOut:
    """采用一条技能提案（status == "proposed"）。

    - improvement：按 metadata.target 找到既有 Skill，把正文换代并记进 history
      （与 PATCH 同一种回滚账），然后删掉提案行；目标 Skill 的启用状态不变。
    - new（或已存在同名 Skill）：同名合并；否则提案行转成 draft，等待用户再决定
      启用（catalog 只吃 active）——采用不等于启用，这一步不能替用户做主。
    """
    proposal = await db.get(Skill, skill_id)
    if proposal is None:
        raise ApiError(code="skill_not_found", message="Skill 不存在", status_code=404)
    if proposal.status != "proposed":
        raise ApiError(code="skill_not_proposed", message="这条记忆不是待采用的提案", status_code=409)
    meta = dict(proposal.metadata_json or {})
    kind = str(meta.get("kind") or "new")
    target_name = str(meta.get("target") or "").strip()
    if kind == "improvement" and not target_name:
        raise ApiError(
            code="skill_proposal_invalid", message="改进提案缺少目标 Skill", status_code=409
        )
    lookup = target_name or str(proposal.name or "")
    existing = (
        await db.execute(
            select(Skill)
            .where(Skill.name == lookup, Skill.id != proposal.id, Skill.status != "proposed")
            .order_by(Skill.updated_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if existing is None and kind == "improvement":
        raise ApiError(
            code="skill_target_not_found",
            message=f"目标 Skill「{lookup}」已不存在，无法合并；可以丢弃这条提案",
            status_code=409,
        )
    now = datetime.now(timezone.utc).isoformat()
    description = str(meta.get("description") or "")
    if existing is not None:
        before = str(existing.instructions or "")
        if before and before != proposal.instructions:
            history = list(existing.history or [])
            history.append({"version": existing.version, "instructions": before})
            existing.history = history[-20:]
            existing.instructions = proposal.instructions
            existing.version += 1
        merged = dict(existing.metadata_json or {})
        if description:
            merged["description"] = description
        if meta.get("evidence"):
            merged["evidence"] = meta["evidence"]
        merged["adopted_from"] = proposal.id
        merged["adopted_at"] = now
        existing.metadata_json = merged
        await db.delete(proposal)
        await db.commit()
        await db.refresh(existing)
        return SkillOut.model_validate(existing)
    proposal.status = "draft"
    proposal.metadata_json = {**meta, "accepted_at": now}
    await db.commit()
    await db.refresh(proposal)
    return SkillOut.model_validate(proposal)


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
    # updated_at 是 onupdate=func.now()：提交后那一列在会话里是"待读回"状态，
    # 直接 model_validate 会在同步上下文里触发惰性加载，当场 MissingGreenlet → 500。
    await db.refresh(skill)
    return SkillOut.model_validate(skill)


@router.post("/{skill_id}/disable", response_model=SkillOut)
async def disable_skill(skill_id: str, db: DbSession) -> SkillOut:
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise ApiError(code="skill_not_found", message="Skill 不存在", status_code=404)
    skill.status = "disabled"
    await db.commit()
    await db.refresh(skill)
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
    await db.refresh(skill)
    return SkillOut.model_validate(skill)


@router.delete("/{skill_id}", status_code=204)
async def delete_skill(skill_id: str, db: DbSession) -> None:
    """物理删除一条记忆（Skill）。

    与 disable 的区别：disable 只是"不再注入系统提示"，行还留着；删除是
    用户明确说"这条不该存在"（比如记错了、或含敏感内容）——留着它，下次
    接受相似候选时去重逻辑还会把旧正文当既有事实。
    """
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise ApiError(code="skill_not_found", message="Skill 不存在", status_code=404)
    await db.delete(skill)
    await db.commit()
