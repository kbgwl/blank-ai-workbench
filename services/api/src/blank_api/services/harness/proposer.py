"""Propose candidate harnesses: rules by default, a model when one is available.

A proposal is a *whole tree*, not a patch. Whole trees are cheap here (a handful
of small text files), and they make every candidate independently validatable
and independently measurable — the optimizer never has to reason about which
earlier edit a later one was layered on.

Two proposers ship:

* :class:`RuleProposer` is deterministic. It encodes the handful of behaviours
  that reliably move the five-part score (read first, verify after, stay
  minimal) and seeds long-term memory from the domain library.
* :class:`ChatProposer` asks a model for JSON candidates and falls back to the
  rule proposer when the response cannot be parsed or fails validation. A
  malformed proposal should never end an optimization run.

Neither proposer can touch security policy: the surface is text files only.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping, Protocol

from blank_api.services.harness import spec
from blank_api.services.harness.library import Library

PLAYBOOK = "task-playbook"

WORKFLOW_BULLETS = (
    "动手前先读现状：相关文件、现有测试和调用方；不要凭文件名猜内容。",
    "只改完成任务必需的地方，不做顺手重构；发现无关问题先记下，不现场修。",
    "改完立刻运行项目已有的验证命令（优先 run_tests），用输出说话，不说“应该通过”。",
    "失败时先读报错定位根因；同一处修两次仍不过就换思路，不要重复同一条命令。",
    "结论只写工具记录、Diff 或测试输出能核对的内容；没跑过的验证要明确说没跑。",
)

GUARDRAIL_BULLETS = (
    "不放宽安全规则、审批规则和沙箱策略，也不通过禁用或跳过测试来让结果变绿。",
    "不读取、打印、写入或提交密钥、令牌和 .env 内容；需要凭证时让用户配置。",
    "删除、覆盖、推送、发布之前确认目标；不可逆操作先征得同意。",
    "工具被拒绝时换一种合规做法，不尝试绕过审批或安全边界。",
)

TOOL_BULLETS = (
    "定位：先用 search_code 找符号或字符串，再用 read_file 读完整上下文，不要只凭一行搜索结果改代码。",
    "编辑：用 apply_patch 小步修改，一次只改一个逻辑点；改完读回 Diff 确认实际落盘内容。",
    "验证：改动后运行 run_tests；报错先看第一条根因，再决定改代码还是补测试。",
    "命令：run_shell 只用于构建、检查和版本控制，不要用它绕过文件工具的审批。",
    "调研：确定要读哪些文件再批量读；大仓库里先 list_files 缩小范围。",
)

_DOMAIN_SKILL = """---
description: {description}
---

# 同类任务执行清单

1. 先读任务涉及的文件与测试，确认现有契约，再设计最小改动。
2. 每完成一个逻辑点就验证一次；不要攒到最后一次性运行。
3. 失败时按报错定位根因，修复后重跑同一验证命令。
4. 收尾时列出改动的文件、运行过的验证和仍未验证的部分。
{lessons}{failures}"""


def sanitize(text: str) -> str:
    """Remove eval material markers before they can enter a harness."""
    out = _redact(text)
    return " ".join(out.split())


def _redact(text: str) -> str:
    """Redact markers but keep line breaks, for evidence prompts."""
    out = str(text or "")
    for marker in spec.LEAK_MARKERS:
        out = out.replace(marker, "评测材料")
    return re.sub(r"\bT\d{2}\b", "同类任务", out)


def _append_lines(tree: dict[str, str], rel: str, heading: str, bullets: tuple[str, ...]) -> bool:
    """Append missing bullets without crossing the component's cap."""
    component = spec.component_for(rel)
    if component is None:
        return False
    body = tree.get(rel, "").rstrip()
    if not body:
        body = heading
    added = False
    for bullet in bullets:
        line = f"- {bullet}"
        if line in body:
            continue
        candidate = f"{body}\n{line}"
        if len(candidate) + 1 > component.limit:
            break
        body = candidate
        added = True
    if added:
        tree[rel] = body + "\n"
    return added


def _add_default_workflow(tree: dict[str, str]) -> bool:
    return _append_lines(tree, "systemprompt.md", "# 工作方式", WORKFLOW_BULLETS)


def _add_guardrails(tree: dict[str, str]) -> bool:
    return _append_lines(tree, "guardrails.md", "# 硬性约束", GUARDRAIL_BULLETS)


def _add_tool_guidance(tree: dict[str, str]) -> bool:
    return _append_lines(tree, "tool_descriptions/tool_guidance.md", "# 工具使用", TOOL_BULLETS)


_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def _usefulness(item: Mapping[str, object]) -> tuple[int, int]:
    helpful = int(item.get("helpful") or 0)
    harmful = int(item.get("harmful") or 0)
    rank = _CONFIDENCE_RANK.get(str(item.get("confidence") or "").lower(), 0)
    return helpful - harmful, rank


def add_memory(
    tree: dict[str, str],
    lessons: list[dict],
    *,
    failures: list[dict] | None = None,
    limit: int = 6,
) -> bool:
    component = spec.component_for("LongTermMEMORY.md")
    if component is None:
        return False
    body = tree.get("LongTermMEMORY.md", "").rstrip() or "# 长期记忆"
    added = False

    # ACE 式排序：被验证有用的条目先进入 always-on 记忆，避免只按写入顺序。
    for item in sorted(lessons, key=_usefulness, reverse=True)[:limit]:
        cue = sanitize(item.get("cue", ""))
        lesson = sanitize(item.get("lesson", ""))
        if not cue or not lesson:
            continue
        line = f"- 同类任务（{cue}）：{lesson}"
        if line in body:
            continue
        candidate = f"{body}\n{line}"
        if len(candidate) + 1 > component.limit:
            break
        body = candidate
        added = True

    # 失败记录只作为警示，不与已验证做法混在同一组规则里。
    negative = [item for item in (failures or []) if item.get("cue") and item.get("lesson")]
    if negative:
        heading = "## 失败教训（仅作警示）"
        if heading not in body:
            candidate = f"{body}\n{heading}"
            if len(candidate) + 1 <= component.limit:
                body = candidate
        for item in sorted(negative, key=_usefulness)[: max(1, limit // 2)]:
            cue = sanitize(item.get("cue", ""))
            lesson = sanitize(item.get("lesson", ""))
            if not cue or not lesson:
                continue
            line = f"- 避免（{cue}）：{lesson}"
            if line in body:
                continue
            candidate = f"{body}\n{line}"
            if len(candidate) + 1 > component.limit:
                break
            body = candidate
            added = True

    if added:
        tree["LongTermMEMORY.md"] = body + "\n"
    return added


def add_playbook(
    tree: dict[str, str],
    domain: str,
    lessons: list[dict],
    *,
    failures: list[dict] | None = None,
) -> bool:
    rel = f"skills/{PLAYBOOK}/SKILL.md"
    if rel in tree:
        return False
    component = spec.component_for(rel)
    if component is None:
        return False
    where = sanitize(domain) or "同类"
    lesson_lines = []
    for item in sorted(lessons, key=_usefulness, reverse=True)[:4]:
        cue = sanitize(item.get("cue", ""))
        lesson = sanitize(item.get("lesson", ""))
        if cue and lesson:
            lesson_lines.append(f"- 遇到过「{cue}」：{lesson}")
    failure_lines = []
    for item in sorted(failures or [], key=_usefulness)[:3]:
        cue = sanitize(item.get("cue", ""))
        lesson = sanitize(item.get("lesson", ""))
        if cue and lesson:
            failure_lines.append(f"- 不要只做「{cue}」：{lesson}")
    body = _DOMAIN_SKILL.format(
        description=f"处理「{where}」类任务时的固定步骤与检查点",
        lessons=("\n".join(lesson_lines) + "\n" if lesson_lines else ""),
        failures=(
            "\n## 常见失败\n" + "\n".join(failure_lines) + "\n" if failure_lines else ""
        ),
    )
    if len(body) > component.limit:
        body = _DOMAIN_SKILL.format(
            description=f"处理「{where}」类任务时的固定步骤与检查点",
            lessons=("\n".join(lesson_lines) + "\n" if lesson_lines else ""),
            failures="",
        )
    if len(body) > component.limit:
        return False
    tree[rel] = body
    return True


@dataclass
class Candidate:
    label: str
    tree: dict[str, str]
    why: str = ""
    # 推荐器自报的来源，用于报告里区分规则与模型
    origin: str = "rule"

    def fingerprint(self) -> str:
        return json.dumps(self.tree, ensure_ascii=False, sort_keys=True)

    def candidate_key(self) -> str:
        return self.fingerprint()


@dataclass
class ProposalRequest:
    base: Mapping[str, str] = field(default_factory=dict)
    domain: str = ""
    title: str = ""
    count: int = 3
    library: Library | None = None
    # 真实探测摘录（失败/低分），供 ReflectionProposer 归因；规则提案器忽略它。
    failures: list[dict] = field(default_factory=list)


class Proposer(Protocol):
    async def propose(self, request: ProposalRequest) -> list[Candidate]: ...


class RuleProposer:
    """Deterministic candidates, ordered richest first.

    The optimizer probes them in this order, so with ``count=1`` it still tests
    a complete, domain-aware harness rather than an arbitrary subset.
    """

    async def propose(self, request: ProposalRequest) -> list[Candidate]:
        base = {str(rel): str(body) for rel, body in request.base.items()}
        lessons = request.library.lessons() if request.library is not None else []
        failures = request.library.failure_lessons() if request.library is not None else []
        count = max(1, int(request.count or 1))

        variants: list[Candidate] = []

        domain_tree = _copy(base)
        _add_default_workflow(domain_tree)
        _add_guardrails(domain_tree)
        _add_tool_guidance(domain_tree)
        memory = add_memory(domain_tree, lessons, failures=failures)
        skill = add_playbook(domain_tree, request.domain, lessons, failures=failures)
        variants.append(
            Candidate(
                label="domain",
                tree=domain_tree,
                why=(
                    "工作方式 + 硬性约束 + 工具说明"
                    + ("，并把本领域既往经验写入长期记忆" if memory else "")
                    + ("，生成同类任务技能清单" if skill else "")
                ),
            )
        )

        guided_tree = _copy(base)
        _add_default_workflow(guided_tree)
        _add_guardrails(guided_tree)
        _add_tool_guidance(guided_tree)
        variants.append(
            Candidate(
                label="guided",
                tree=guided_tree,
                why="工作方式 + 硬性约束 + 工具说明（不带经验库）",
            )
        )

        workflow_tree = _copy(base)
        _add_default_workflow(workflow_tree)
        variants.append(
            Candidate(label="workflow", tree=workflow_tree, why="只补工作方式，测试最少提示是否足够")
        )

        guarded_tree = _copy(base)
        _add_default_workflow(guarded_tree)
        _add_guardrails(guarded_tree)
        variants.append(
            Candidate(label="guarded", tree=guarded_tree, why="工作方式 + 硬性约束，不带工具说明")
        )

        out: list[Candidate] = []
        seen: set[str] = set()
        for candidate in variants:
            fingerprint = candidate.fingerprint()
            if fingerprint in seen:
                continue
            try:
                spec.validate(candidate.tree)
            except spec.HarnessError:
                continue
            seen.add(fingerprint)
            out.append(candidate)
            if len(out) >= count:
                break
        return out


_CHAT_PROMPT = """你在为一个编码 agent 优化它的项目 harness。harness 由若干文本文件组成，
会进入系统提示（always-on）或作为按需资源被读取。请提出 {count} 个互不相同的候选，
每个候选是一棵完整、可直接落盘的树。

约束：
- 只使用这些文件：systemprompt.md、guardrails.md、LongTermMEMORY.md、
  tool_descriptions/tool_guidance.md、sub_agents/<name>/AGENT.md、
  skills/<name>/SKILL.md、skills/<name>/scripts/*.py、scripts/<name>.py。
- 单文件上限 4000 字符（guardrails/记忆 2000），always-on 合计不超过 6000。
- 不能放宽安全规则、审批规则或验收条件；不能出现评测材料、任务编号或期望文件名。
- 候选要针对下面的任务领域；不要照抄经验库，要提炼可迁移的做法。

当前 harness（JSON，rel → 内容）：
{base}

任务领域：{domain}
任务标题：{title}

经验库：
{library}

只输出 JSON，不要解释：
{{"candidates":[{{"label":"短名","why":"为什么这样改","files":{{"systemprompt.md":"全文"}}}}]}}
"""


class ChatProposer:
    """Ask a model for candidates; fall back to rules on any failure."""

    def __init__(
        self,
        chat: Callable[[str], Awaitable[str]],
        *,
        fallback: Proposer | None = None,
    ) -> None:
        self.chat = chat
        self.fallback = fallback

    async def propose(self, request: ProposalRequest) -> list[Candidate]:
        library_text = (
            request.library.context(limit=4_000) if request.library is not None else "（空）"
        )
        prompt = _CHAT_PROMPT.format(
            count=max(1, int(request.count or 1)),
            base=json.dumps(request.base, ensure_ascii=False, indent=2, sort_keys=True),
            domain=request.domain or "未标注",
            title=request.title or "未标注",
            library=library_text or "（空）",
        )
        try:
            raw = await self.chat(prompt)
        except Exception:
            raw = ""
        candidates = _parse_candidates(raw, base=request.base, origin="model")
        if not candidates and self.fallback is not None:
            return await self.fallback.propose(request)
        return candidates[: max(1, int(request.count or 1))]


_REFLECTION_PROMPT = """你在根据真实执行轨迹改进一个编码 agent 的项目 harness。
不要凭空增加规则；只修改证据指向的组件。

当前 harness（JSON，rel → 内容）：
{base}

本轮仍未通过的主任务证据（已脱敏；包含验证输出与工具轨迹）：
{evidence}

任务领域：{domain}
任务标题：{title}

经验库（含失败记录，失败记录只作反例）：
{library}

要求：
- 每个候选是一棵完整树；未修改的文件可以省略，系统会与当前树合并。
- 每条改动都要在 why 里引用具体证据：哪条验证输出或哪条轨迹。
- 不能放宽安全规则、审批规则、沙箱或验收条件；不能出现评测材料、任务编号或期望文件名。
- 不要照抄经验库；提炼可迁移的做法，优先修根因而不是加一句“要小心”。
- 只输出 JSON：
{{"candidates":[{{"label":"短名","why":"引用了哪条证据、改了什么","files":{{"systemprompt.md":"全文"}}}}]}}
"""


class ReflectionProposer:
    """GEPA 式反思提案：从真实失败轨迹归因，而不是从模板猜规则.

    It is deliberately a *separate* proposer. The optimizer runs a first probe
    round, hands the unresolved tasks to this class, and measures whatever comes
    back. A model that cannot ground an edit in evidence is ignored; the run
    falls back to the already-measured candidates.
    """

    def __init__(
        self,
        chat: Callable[[str], Awaitable[str]],
        *,
        fallback: Proposer | None = None,
        evidence_limit: int = 12_000,
    ) -> None:
        self.chat = chat
        self.fallback = fallback
        self.evidence_limit = max(1_000, int(evidence_limit))

    async def propose(self, request: ProposalRequest) -> list[Candidate]:
        if not request.failures:
            if self.fallback is not None:
                return await self.fallback.propose(request)
            return []
        evidence = render_evidence(request.failures, limit=self.evidence_limit)
        if not evidence:
            if self.fallback is not None:
                return await self.fallback.propose(request)
            return []
        library_text = (
            request.library.context(limit=4_000) if request.library is not None else "（空）"
        )
        prompt = _REFLECTION_PROMPT.format(
            base=json.dumps(request.base, ensure_ascii=False, indent=2, sort_keys=True),
            evidence=evidence,
            domain=request.domain or "未标注",
            title=request.title or "未标注",
            library=library_text or "（空）",
        )
        try:
            raw = await self.chat(prompt)
        except Exception:
            raw = ""
        candidates = _parse_candidates(raw, base=request.base, origin="reflection")
        if not candidates and self.fallback is not None:
            return await self.fallback.propose(request)
        return candidates[: max(1, int(request.count or 1))]


def render_evidence(items: list[dict], *, limit: int = 12_000) -> str:
    """Compact, de-identified probe evidence for a reflection prompt."""
    lines: list[str] = []
    size = 0
    for item in items:
        sample_id = sanitize(item.get("sample_id", "")) or "任务"
        label = sanitize(item.get("label", "")) or "候选"
        head = f"### {sample_id} / {label}"
        body: list[str] = [
            head,
            f"- verify_ok: {bool(item.get('verify_ok'))}",
            f"- changed: {sanitize(', '.join(item.get('changed') or [])) or '（无）'}",
            f"- expected: {sanitize(', '.join(item.get('expect_files') or [])) or '（未标注）'}",
        ]
        if item.get("score") is not None:
            body.append(f"- score: {item.get('score')}")
        verify_output = _redact(item.get("verify_output", ""))
        if verify_output:
            body.append(f"- verify output:\n```\n{verify_output[:2_500]}\n```")
        trajectory = _redact(item.get("trajectory", ""))
        if trajectory:
            body.append(f"- trajectory:\n```\n{trajectory[:2_500]}\n```")
        error = _redact(item.get("error", ""))
        if error:
            body.append(f"- error: {error[:800]}")
        block = "\n".join(body)
        if size + len(block) > limit:
            break
        lines.append(block)
        size += len(block) + 2
    return "\n\n".join(lines)


def _copy(tree: Mapping[str, str]) -> dict[str, str]:
    return {str(rel): str(body) for rel, body in tree.items()}


def _parse_candidates(
    raw: str,
    *,
    base: Mapping[str, str] | None = None,
    origin: str = "model",
) -> list[Candidate]:
    payload = _extract_json(raw)
    if not isinstance(payload, dict):
        return []
    items = payload.get("candidates")
    if not isinstance(items, list):
        return []
    out: list[Candidate] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        files = item.get("files")
        if not isinstance(files, dict) or not files:
            continue
        # 模型可以只返回改动过的文件；合并回当前树后仍是一棵完整、可独立测量的树。
        tree = {str(rel): str(body) for rel, body in (base or {}).items()}
        tree.update({str(rel): str(body) for rel, body in files.items()})
        try:
            spec.validate(tree)
        except spec.HarnessError:
            continue
        candidate = Candidate(
            label=str(item.get("label") or f"model-{index + 1}")[:40],
            tree=tree,
            why=str(item.get("why") or "")[:1_000],
            origin=origin,
        )
        fingerprint = candidate.fingerprint()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        out.append(candidate)
    return out


def _extract_json(raw: str) -> object:
    text = str(raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
