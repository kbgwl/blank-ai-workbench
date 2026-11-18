import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import select

from blank_api.api.auth import require_token
from blank_api.api.errors import ApiError
from blank_api.deps import DbSession
from blank_api.models.skill import MemoryCandidate, Skill
from blank_api.models.project import Workspace
from blank_api.schemas.skill import (
    MarketHitOut,
    MarketInstallIn,
    MarketCatalogOut,
    MarketInstallRepoIn,
    MarketSyncIn,
    MarketItemOut,
    MarketListOut,
    MarketSearchOut,
    MarketSourceOut,
    MemoryCandidateCreate,
    MemoryCandidateOut,
    SkillInstallIn,
    SkillInstallOut,
    SkillLibraryItem,
    SkillLibraryOut,
    SkillOut,
    SkillUninstallIn,
    SkillUpdate,
    SkillUsageOut,
)
from blank_api.services.skill_install import (
    SkillInstallError,
    install_skill,
    read_source_stamp,
    slugify_name,
    uninstall_skill,
)
from blank_api.services.market_cache import (
    CatalogEntry,
    ensure_package,
    read_catalog,
    search_catalog,
    sync_from_url,
    write_catalog,
)
from blank_api.services.skill_market import (
    MarketError,
    list_curated,
    list_market,
    preset_sources,
    repo_zip_url,
    search_market,
)
from blank_api.services.skill_usage import skill_usage
from blank_api.services.skills_defs import PROJECT_SKILLS_DIR, discover_library, user_skills_dir

router = APIRouter(prefix="/skills", tags=["skills"], dependencies=[Depends(require_token)])

# 用量归因窗口：与 Claude 的 `/skills` 列表口径一致（7 天）。
USAGE_DAYS = 7
# 实时搜索的硬上限：超过就回落到本机目录，宁可给快照也不让界面卡住。
NETWORK_SEARCH_TIMEOUT = 6.0


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


@router.get("/library", response_model=SkillLibraryOut)
async def skill_library(db: DbSession) -> SkillLibraryOut:
    """现在真正能被模型加载的技能有哪些。

    技能页此前只读库里的 ``skills`` 表（用户审过的记忆技能），于是"技能"看起来
    只有几条——而运行时实际加载的是磁盘上的 SKILL.md（内置 / 用户 / 项目）。
    这个接口把真实目录吐出来，页面上"能用什么"和"模型能用什么"才是同一份数据。

    项目级技能挂在具体工作区上，这里把所有工作区各扫一遍：只取其中一个会
    让"我在另一个项目里写的技能"凭空消失。
    """
    roots = await _workspace_roots(db)
    discovery = discover_library(roots)
    usage = await skill_usage(db, days=USAGE_DAYS)
    items = []
    for item in discovery.entries.values():
        stamp = read_source_stamp(Path(item.path).parent) if item.path else {}
        items.append(
            SkillLibraryItem(
                name=item.name,
                display_name=item.display_name,
                description=item.description,
                when_to_use=item.when_to_use,
                allowed_tools=list(item.allowed_tools),
                paths=list(item.paths),
                model_invocable=item.model_invocable,
                source=item.source,
                path=item.path,
                installed_from=str(stamp.get("source") or ""),
                installed_at=str(stamp.get("installed_at") or ""),
            )
        )
    counts: dict[str, int] = {}
    for item in items:
        counts[item.source] = counts.get(item.source, 0) + 1
    return SkillLibraryOut(
        items=items,
        counts=counts,
        usage={name: SkillUsageOut(**row.as_dict()) for name, row in usage.items()},
        usage_days=USAGE_DAYS,
        root=", ".join(roots[:3]),
        errors=list(discovery.errors),
        warnings=list(discovery.warnings),
    )


@router.post("/install", response_model=SkillInstallOut, status_code=201)
async def install_skill_endpoint(payload: SkillInstallIn, db: DbSession) -> SkillInstallOut:
    """装一份技能到用户级或项目级目录。

    这是唯一一个"从外部内容写本机磁盘"的入口，安全边界全在
    ``services/skill_install.py`` 的模块文档里（路径必须落在技能根内、名字白名单、
    体积上限、zip 只解普通文件、落盘前先解析一遍）。
    """
    project_root = await _project_root(db, payload.project_id) if payload.scope == "project" else None
    try:
        result = install_skill(
            payload.source,
            scope=payload.scope,
            project_root=project_root,
            name=payload.name,
            overwrite=payload.overwrite,
        )
    except SkillInstallError as exc:
        raise ApiError(code=exc.code, message=exc.message, status_code=exc.status_code) from exc
    return SkillInstallOut(**result)


@router.post("/uninstall", response_model=dict)
async def uninstall_skill_endpoint(payload: SkillUninstallIn, db: DbSession) -> dict:
    """删掉一份已安装的技能（必须是含 SKILL.md 的目录）。

    优先按 ``path`` 定位：技能库里那条记录带着真实目录，多项目时按名字猜会删错。
    ``path`` 必须落在某个技能根内——用户级根 + 每个工作区的项目级根。
    """
    project_root = await _project_root(db, payload.project_id) if payload.scope == "project" else None
    roots = [user_skills_dir(), *[Path(root) / PROJECT_SKILLS_DIR for root in await _workspace_roots(db)]]
    try:
        return uninstall_skill(
            payload.name,
            scope=payload.scope,
            project_root=project_root,
            path=payload.path,
            roots=roots,
        )
    except SkillInstallError as exc:
        raise ApiError(code=exc.code, message=exc.message, status_code=exc.status_code) from exc


@router.get("/market/sources", response_model=list[MarketSourceOut])
async def market_sources() -> list[MarketSourceOut]:
    """可用的市场来源：官方预设 + 我们自己的清单 + 任意 GitHub / 市场地址。"""
    rows = [MarketSourceOut(**item) for item in preset_sources()]
    rows.append(MarketSourceOut(id="curated", label="我们的市场", kind="curated", repo=""))
    return rows


@router.get("/market", response_model=MarketListOut)
async def market_list(source: str, query: str = "") -> MarketListOut:
    """列一个市场来源里的技能。只读：不下载、不落盘。"""
    if source == "curated":
        items = [item for item in list_curated() if not query or query.lower() in item.name.lower()]
        return MarketListOut(
            source=source,
            items=[
                MarketItemOut(
                    id=item.id,
                    name=item.name,
                    description=item.description,
                    source_id=item.source_id,
                    source_label=item.source_label,
                )
                for item in items
            ],
        )
    try:
        items = await list_market(source, query=query)
    except MarketError as exc:
        raise ApiError(code=exc.code, message=exc.message, status_code=exc.status_code) from exc
    return MarketListOut(
        source=source,
        items=[
            MarketItemOut(
                id=item.id,
                name=item.name,
                description=item.description,
                source_id=item.source_id,
                source_label=item.source_label,
            )
            for item in items
        ],
    )


@router.get("/market/search", response_model=MarketSearchOut)
async def market_search(q: str, limit: int = 8) -> MarketSearchOut:
    """按一句话去 GitHub 找技能或工具。

    返回里带 stars——评分是唯一能从公开渠道拿到的质量信号，不给评分等于让模型
    凭标题猜。找不到就返回空列表，由调用方如实说"没找到"。
    """
    import asyncio

    # 外网调用必须有时限：DNS 卡住时 httpx 自己的 timeout 不生效（getaddrinfo
    # 不受它管），一次搜索能挂到几十秒——用户看到的就是"卡死"。
    try:
        hits = await asyncio.wait_for(
            asyncio.to_thread(search_market, q, limit=max(1, min(limit, 20))),
            timeout=NETWORK_SEARCH_TIMEOUT,
        )
    except (TimeoutError, MarketError) as exc:
        # 网络不可用不该等于"没有技能"：回落到本机目录，并如实说明数据是哪来的。
        catalog = read_catalog()
        rows = search_catalog(q, limit=limit)
        reason = "搜索超时" if isinstance(exc, TimeoutError) else getattr(exc, "message", "网络不可用")
        return MarketSearchOut(
            query=q,
            origin="local-cache",
            synced_at=catalog["synced_at"],
            notice=(
                f"{reason}，已改用本机目录（同步于 {catalog['synced_at'] or '尚未同步'}）。"
                if rows
                else f"{reason}，本机也还没有同步过市场目录：先同步一次，之后离线也能用。"
            ),
            items=[
                MarketHitOut(
                    kind="skill",
                    name=row.name,
                    description=row.summary,
                    repo=row.repo,
                    path=row.path,
                    stars=row.stars,
                    url=f"https://github.com/{row.repo}",
                    updated_at=row.checked_at,
                )
                for row in rows
            ],
        )
    return MarketSearchOut(
        query=q,
        items=[
            MarketHitOut(
                kind=hit.kind,
                name=hit.name,
                description=hit.description,
                repo=hit.repo,
                path=hit.path,
                stars=hit.stars,
                url=hit.url,
                updated_at=hit.updated_at,
            )
            for hit in hits
        ],
    )


@router.post("/market/install", response_model=SkillInstallOut, status_code=201)
async def market_install(payload: MarketInstallIn, db: DbSession) -> SkillInstallOut:
    """从市场装一条技能。

    顺序是 **先重新列一遍来源找到 install_ref，再用它下载**——不接受前端直接传
    URL/路径：那等于把"装什么"的决定权交回给浏览器，路径校验就白做了。
    """
    try:
        items = await list_market(payload.source)
        target = next((item for item in items if item.id == payload.item_id), None)
        if target is None:
            raise MarketError("market_item_not_found", "这个来源里没有该技能，可能已经改名或下线。", status_code=404)
        project_root = await _project_root(db, payload.project_id) if payload.scope == "project" else None
        result = install_skill(
            repo_zip_url(target.install_ref),
            scope=payload.scope,
            project_root=project_root,
            name=payload.name or slugify_name(target.id.rsplit("/", 1)[-1]),
            overwrite=payload.overwrite,
            prefix=str(target.install_ref.get("path") or ""),
        )
    except (MarketError, SkillInstallError) as exc:
        raise ApiError(code=exc.code, message=exc.message, status_code=exc.status_code) from exc
    return SkillInstallOut(**result)


@router.post("/market/sync", response_model=dict)
async def market_sync(payload: MarketSyncIn) -> dict:
    """把市场目录同步到本机（``~/.blank/market/catalog.json``）。

    这是"我们维护一个市场"的落点：目录由我们（或用户自己的镜像）提供，
    同步到本地之后**列表与安装都不再依赖网络**。
    """
    try:
        if payload.payload is not None:
            data = payload.payload
            source = payload.source or "inline"
        else:
            data, source = await sync_from_url(payload.url)
    except MarketError as exc:
        raise ApiError(code=exc.code, message=exc.message, status_code=exc.status_code) from exc
    catalog = write_catalog(data, source=source)
    return {
        "synced_at": catalog["synced_at"],
        "source": catalog["source"],
        "count": len(catalog["entries"]),
    }


@router.get("/market/catalog", response_model=MarketCatalogOut)
async def market_catalog(q: str = "", limit: int = 20) -> MarketCatalogOut:
    """读本地目录（离线可用）。没同步过就返回空，并带上 synced_at 为空这一事实。"""
    rows = search_catalog(q, limit=limit)
    data = read_catalog()
    return MarketCatalogOut(
        synced_at=data["synced_at"],
        source=data["source"],
        items=[
            MarketHitOut(
                kind="skill",
                name=row.name,
                description=row.summary,
                repo=row.repo,
                path=row.path,
                stars=row.stars,
                url=f"https://github.com/{row.repo}",
                updated_at=row.checked_at,
            )
            for row in rows
        ],
    )


@router.post("/market/install_repo", response_model=SkillInstallOut, status_code=201)
async def market_install_repo(payload: MarketInstallRepoIn, db: DbSession) -> SkillInstallOut:
    """按搜索结果直接装一条技能（repo + path）。

    为什么不复用 ``/market/install``：那个要求 ``source + item_id`` 且会重新列一遍
    来源，而搜索结果是"仓库里的某个目录"，没有对应的来源清单。这里只接受
    ``owner/repo`` 形状 + 目录前缀，仍然只从 codeload 下载，不接受任意 URL。
    """
    project_root = await _project_root(db, payload.project_id) if payload.scope == "project" else None
    # 先看本地目录里有没有这条的镜像包：有就装缓存，不再联网。
    cached = next(
        (row for row in read_catalog()["entries"] if row.repo == payload.repo and row.path == payload.path),
        None,
    )
    try:
        if cached is not None and cached.package:
            try:
                archive = ensure_package(cached)
                result = install_skill(
                    str(archive),
                    scope=payload.scope,
                    project_root=project_root,
                    name=payload.name or slugify_name(payload.path.rsplit("/", 1)[-1]),
                    overwrite=payload.overwrite,
                    prefix=cached.path,
                )
                return SkillInstallOut(**result)
            except MarketError:
                # 镜像包坏了/不在：退回仓库直连，别让用户卡住。
                pass
        result = install_skill(
            repo_zip_url({"repo": payload.repo, "ref": payload.ref}),
            scope=payload.scope,
            project_root=project_root,
            name=payload.name or slugify_name(payload.path.rsplit("/", 1)[-1]),
            overwrite=payload.overwrite,
            prefix=payload.path,
        )
    except (MarketError, SkillInstallError) as exc:
        raise ApiError(code=exc.code, message=exc.message, status_code=exc.status_code) from exc
    return SkillInstallOut(**result)


async def _workspace_roots(db: DbSession) -> list[str]:
    return [
        str(path)
        for path in (
            await db.execute(select(Workspace.path).where(Workspace.path != "").limit(30))
        ).scalars()
    ]


async def _project_root(db: DbSession, project_id: str | None) -> str:
    """项目级安装要落到"这个项目正在用的那个目录"。"""
    if project_id:
        row = (
            await db.execute(
                select(Workspace.path)
                .where(Workspace.project_id == project_id)
                .where(Workspace.path != "")
                .order_by(Workspace.is_active.desc(), Workspace.created_at)
                .limit(1)
            )
        ).first()
    else:
        row = (await db.execute(select(Workspace.path).where(Workspace.path != "").limit(1))).first()
    return str(row[0]) if row else ""


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
