"""propose_skills: 把「这次做下来值得沉淀成 Skill 的东西」交给人审（对齐 Claude 的 ProposeSkills）。

Claude 的提案不是直接改技能库：它写一行 ``kind=new|improvement`` 的提案，
用户在界面上逐条采用或丢弃。这里照同一条线走：

- 落点复用 ``skills`` 表，``status="proposed"``；运行时的目录只取 ``active``，
  所以提案在被人点"采用"之前不会影响任何一轮的执行。
- 提案元数据放独立列 ``metadata``（kind/target/description/evidence），
  不塞 ``history``——rollback 把 history 当 ``[{version, instructions}]`` 读。
- ``skillMd`` 的 frontmatter 只用来校验结构；保存时正文是全部说明，
  name/description 取自字段本身，其余 frontmatter 键不保留（与 Claude 一致）。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from blank_api.models.skill import Skill
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok
from blank_api.services.tools.skills import SkillCatalog

MAX_PROPOSALS = 3
NAME_CHARS = 64
# 一句话说明的目标长度是 200；超过只提示，不拒绝——真正会撑爆存储的是 1024 以上。
DESCRIPTION_GOAL = 200
DESCRIPTION_CHARS = 1024
MAX_EVIDENCE = 20
EVIDENCE_CHARS = 500
MAX_SKILL_MD = 20_000

_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_KINDS = ("new", "improvement")


def _split_frontmatter(text: str) -> tuple[dict[str, str], str, str]:
    """`skillMd` → (frontmatter, 正文, 问题)。问题非空表示它不是一个能保存的 Skill。"""
    lines = str(text or "").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, "", "skillMd 必须以 YAML frontmatter（第一行 ---）开头"
    end = -1
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() in ("---", "..."):
            end = index
            break
    if end < 0:
        return {}, "", "skillMd 的 frontmatter 没有闭合（缺少第二个 ---）"
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"').strip("'")
    body = "\n".join(lines[end + 1 :]).strip()
    if not body:
        return meta, "", "skillMd 的正文为空：frontmatter 之后要写这个技能具体怎么做"
    return meta, body, ""


def normalize_proposals(arguments: dict) -> tuple[list[dict], str]:
    """校验并归一化 1-3 条提案。返回 (提案, 问题)；问题非空时提案不可用。

    只卡 Claude 契约里能判定的部分：名字形状、kind、improvement 的 target、
    description 的长度与尖括号、skillMd 的 frontmatter+正文。证据条数只做上限。
    """
    raw = arguments.get("proposals")
    if not isinstance(raw, list) or not raw:
        return [], "propose_skills 需要 proposals：一次提交 1-3 条技能提案。"
    if len(raw) > MAX_PROPOSALS:
        return [], f"一次最多提交 {MAX_PROPOSALS} 条提案，收到 {len(raw)} 条。"
    proposals: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        label = f"第 {index + 1} 条提案"
        if not isinstance(item, dict):
            return [], f"{label}不是对象。"
        name = str(item.get("name") or "").strip()
        if not name:
            return [], f"{label}缺少 name。"
        if len(name) > NAME_CHARS:
            return [], f"{label}的 name 超过 {NAME_CHARS} 字符。"
        if not _NAME.match(name):
            return [], (
                f"{label}的 name「{name}」不是 kebab-case：只能用小写字母、数字和连字符，"
                "例如 release-check。"
            )
        if name in seen:
            return [], f"同一次提案里 name 重复：「{name}」。"
        seen.add(name)
        kind = str(item.get("kind") or "new").strip().lower()
        if kind not in _KINDS:
            return [], f"{label}的 kind 只能是 new 或 improvement，收到「{kind}」。"
        target = str(item.get("target") or "").strip()
        if kind == "improvement":
            if not target:
                return [], f"{label}是 improvement，必须给 target：要改进哪个已有 Skill。"
            if target == name:
                return [], f"{label}的 target 不能等于 name：改进提案应指向已有 Skill，name 是新版本的名字。"
        elif target:
            return [], f"{label}是 new，不该带 target（target 只用于 improvement）。"
        description = str(item.get("description") or "").strip()
        if not description:
            return [], f"{label}缺少 description：用一句话说清这条 Skill 让人怎么做。"
        if len(description) > DESCRIPTION_CHARS:
            return [], (
                f"{label}的 description 超过 {DESCRIPTION_CHARS} 字符；"
                f"它应当是目标 {DESCRIPTION_GOAL} 字以内的一句话。"
            )
        if "<" in description or ">" in description:
            return [], f"{label}的 description 不能包含尖括号（< >）。"
        skill_md = str(item.get("skillMd") or item.get("skill_md") or "")
        if len(skill_md) > MAX_SKILL_MD:
            return [], f"{label}的 skillMd 超过 {MAX_SKILL_MD} 字符，请把流程写短。"
        meta, body, problem = _split_frontmatter(skill_md)
        if problem:
            return [], f"{label}：{problem}。"
        raw_evidence = item.get("evidence")
        evidence: list[str] = []
        if isinstance(raw_evidence, list):
            for entry in raw_evidence[:MAX_EVIDENCE]:
                text = str(entry or "").strip()
                if text:
                    evidence.append(text[:EVIDENCE_CHARS])
        proposals.append(
            {
                "name": name,
                "kind": kind,
                "target": target,
                "description": description,
                "evidence": evidence,
                "body": body,
                # 只留作审计：frontmatter 里的 name/description 不参与保存，
                # 与 Claude "其余键不保留" 的口径一致。
                "frontmatter_keys": sorted(meta),
            }
        )
    return proposals, ""


async def store_proposals(
    session_factory, proposals: list[dict], *, source: str = "agent"
) -> list[dict]:
    """把规范化后的提案写进 `skills` 表（status=proposed），返回落库摘要。

    抽出来是因为除了模型自己提（`propose_skills`），还有人主动说了一句
    "做事要严谨"之后由 `skillsmith` 确定性地沉淀成 Skill 这条路径。
    两条路径的入库形状必须一致，否则界面上会出现两种"提案"。
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stored: list[dict] = []
    async with session_factory() as db:
        for item in proposals:
            row = Skill(
                id=str(uuid.uuid4()),
                name=item["name"],
                instructions=item["body"],
                status="proposed",
                version=1,
                history=[],
                metadata_json={
                    "kind": item["kind"],
                    "target": item["target"],
                    "description": item["description"],
                    "evidence": item["evidence"],
                    "frontmatter_keys": item["frontmatter_keys"],
                    "proposed_at": now.isoformat(),
                    # 谁写的这一条。`persona` = 从他的做事标准展开的，
                    # 不是模型自己想到的——两者在界面上该能分开看。
                    "source": source,
                },
            )
            db.add(row)
            stored.append(
                {
                    "id": row.id,
                    "name": item["name"],
                    "kind": item["kind"],
                    "target": item["target"],
                    "description": item["description"],
                }
            )
        await db.commit()
    return stored


async def _propose_skills(context: ToolContext, arguments: dict) -> ToolOutcome:
    proposals, problem = normalize_proposals(arguments)
    if problem:
        return fail("error", problem)
    catalog = getattr(context, "skills", None)
    names = set(catalog.names()) if isinstance(catalog, SkillCatalog) else set()
    loaded = set(catalog.loaded) if isinstance(catalog, SkillCatalog) else set()
    for item in proposals:
        if item["kind"] != "improvement":
            continue
        target = item["target"]
        if target not in names:
            available = "、".join(sorted(names)) if names else "（当前任务没有可用 Skill）"
            return fail(
                "error",
                f"改进提案的 target「{target}」不在本任务可用的 Skill 清单里。可用：{available}。",
            )
        if target not in loaded:
            # 没读过就提改进等于凭名字猜内容；先把正文加载进来再写。
            return fail(
                "error",
                f"提改进之前先用 skill 工具加载「{target}」的正文，再据实写提案。",
            )
    stored = await store_proposals(context.session_factory, proposals)
    recorder = getattr(context, "recorder", None)
    if recorder is not None:
        await recorder.add("skill.proposed", "skill", {"proposals": stored, "count": len(stored)})
    return ok(
        f"已向用户提交 {len(stored)} 条技能提案，等待他们逐条采用或丢弃。"
        "继续下一阶段，不要等他们回复——提案不阻塞当前任务。",
        proposalCount=len(stored),
        proposals=stored,
    )


def skill_proposal_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="propose_skills",
            description=(
                "把值得沉淀成 Skill 的做法写成提案交用户审阅。一次 1-3 条；采用前不生效。\n"
                "kind=new 新建；improvement 必须给 target 且先用 skill 加载过正文。\n"
                "name 用 kebab-case；description 一句话（≤200 字）；skillMd 必须含 --- frontmatter "
                "和正文；frontmatter 的其他键不保留。证据只放 evidence（文件/命令输出/用户原话），"
                "不要写进正文。提交后继续下一阶段，不要等回复。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "proposals": {
                        "type": "array",
                        "description": "1-3 条提案",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "description": "kebab-case 名称，例如 release-check",
                                },
                                "kind": {
                                    "type": "string",
                                    "enum": ["new", "improvement"],
                                    "description": "new=新建；improvement=改进已有 Skill",
                                },
                                "target": {
                                    "type": "string",
                                    "description": "improvement 必填：要改进的已有 Skill 名字",
                                },
                                "description": {
                                    "type": "string",
                                    "description": "一句话说明（目标 200 字以内，硬上限 1024），不含尖括号",
                                },
                                "evidence": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "支撑这条提案的证据（文件、输出、原话）",
                                },
                                "skillMd": {
                                    "type": "string",
                                    "description": "带 --- frontmatter 与正文的 Skill 全文",
                                },
                            },
                            "required": ["name", "description", "skillMd"],
                        },
                    }
                },
                "required": ["proposals"],
            },
            risk_level="low",
            handler=_propose_skills,
        )
    ]
