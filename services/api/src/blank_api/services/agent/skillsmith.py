"""把"他做事的方式"沉淀成 Skill。

## 为什么是 Skill，不是又一条提示

偏好条目是一句话，进了 system prompt 只是"注意什么"。而"像他"多半是**程序性**的：
先做什么、做到什么程度算完、每个结论要带什么。这类东西用一行话装不下，硬塞进提示
会变成几十条并列的规则，模型读完记住三条。

Skill 正好是这个形状：**按需加载、有触发条件、正文可以写清步骤**。WorkBuddy 自己的
做法就是这个——本机 26 个 Skill 里 **17 个是 agent 自己写的**（frontmatter 标
`agent_created: true`），名字都是踩过的坑（`codex-windows-sandbox-fix`、
`claude-code-context-fix`）。

## 和"模型自己提 skill"的区别

我们已经有 `propose_skills`（模型主动提，人点采用才生效）。这一条是**确定性**的：

- 料来自使用者模型（他亲口说的标准 + 原话），不是模型临场想出来的；
- 内容由 `STANDARDS` 表展开，不经过生成，所以每次结果一样、可复查；
- 同样走 `status=proposed`，**人点采用才生效**——因为"把严谨展开成这四步"
  是我们的理解，不是他的原话。

## 不做的事

不自动采用。展开是理解，理解会偏。让它先以提案出现，他看一眼就知道我们理解对没对。
"""

from __future__ import annotations

import re
from typing import Iterable

from blank_api.services.agent.persona import (
    STANDARDS,
    PersonaStore,
    clauses_of,
    standards_of,
)

# 标准 → Skill 名（Skill 名有格式约束：小写字母/数字，用连字符分段）。
SKILL_NAMES = {
    "rigor": "work-rigorously",
    "concise": "work-concisely",
}

# 怎么算做到。与 persona.conformance 的 clause key 一一对应——
# 写了判据却没人核，就是又一次"字段建了没人读"。
_CLAUSE_NOTES = {
    "change_is_verified": "没改文件记 `unverifiable`（不记失败）；改了却没跑验证记 `failed`。",
    "no_bare_claims": '结论里的"应该/大概/可能"没有对应的"未验证"说明时记 `failed`。',
    "report_is_traceable": "结论里看不到具体路径或命令时记 `failed`。",
    "failures_are_admitted": "本轮有失败或跳过、结论里却只写\"全部通过\"时记 `failed`。",
    "report_is_short": "结论超过 900 字记 `failed`。",
    "no_padding": '出现"已完成如下工作""综上所述"这类填充句时记 `failed`。',
}


def _yaml_str(text: str) -> str:
    """把一句话塞进 YAML 标量。

    必须转义：description 里常带中文冒号（"执行任务：改完必须验证："），
    YAML 会把 `: ` 当键值分隔符，整块 frontmatter 解析失败——技能会被加载成
    "没有描述"的样子，而且不报错。里面的引号同理。
    """
    body = str(text).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{body}"'


def _judgement(context: dict) -> str:
    """他会怎么判——**只写有记录的部分**。

    这一段是最容易变成编造的地方：写"他偏好稳妥"听起来很像，但我们没有依据。
    所以三条各自独立判断，没有记录就明说"暂无"，把空白留着。
    空白比编造有用——它会提醒我们"这块还没学到"。
    """
    rows = (
        ("什么必须先问", context.get("asks_first"), "暂无记录——还没见你在这类事上叫停过"),
        ("用什么验收", context.get("verifies_with"), "暂无记录——还没见过你认可的一条验证命令"),
        ("什么时候该停", context.get("stops_when"), "暂无记录——暂按默认：同一处失败两次就停"),
    )
    lines: list[str] = []
    for label, values, empty in rows:
        picked = [str(value).strip() for value in (values or []) if str(value).strip()][:3]
        if picked:
            lines.append(f"- **{label}**：" + "；".join(picked))
        else:
            lines.append(f"- **{label}**：{empty}")
    return "\n".join(lines)


def draft(
    standard_keys: Iterable[str],
    quotes: dict[str, str],
    context: dict | None = None,
) -> list[dict]:
    """一个标准 → 一条 Skill 提案。

    `quotes` 是 标准 key → 他的原话。没有原话的标准不出提案：
    没有他的原话就没有"像他"这回事，只剩我们自己发明的规矩。
    """
    out: list[dict] = []
    for key in standard_keys:
        spec = STANDARDS.get(key)
        name = SKILL_NAMES.get(key)
        if not spec or not name:
            continue
        quote = str(quotes.get(key) or "").strip()
        if not quote:
            continue
        label = spec["label"]
        clauses = clauses_of([key])
        steps = "\n".join(
            f"{index}. {text}" for index, (_k, _c, text) in enumerate(clauses, start=1)
        )
        checks = "\n".join(
            f"- `{clause_key}`：{_CLAUSE_NOTES.get(clause_key, '按事实核对')}"
            for _k, clause_key, _t in clauses
        )
        body = f"""# 按他的方式做事：{label}

## 何时触发

每一轮任务都适用。这是这位使用者的**做事标准**，不是某个任务的细节——
它不挂在具体路径或报错上，任何任务都该这么干。

## 他的原话

> {quote}

这是他自己说的，不是我们从行为里猜的。

## 流程

{steps}

## 他会怎么判

下面这段不是我们猜的：只写有记录的部分，没记录就留空（留空本身是信息——
说明这块还没学到）。

{_judgement(context or {})}

## 怎么算做到

这一层能核对，不是口号。每轮收尾按事实逐条判 `passed / failed / unverifiable`
（事件 `standard.conformance`）：

{checks}

## 一句话

把"做完了"的标准定在**别人能照着复现**，而不是"我觉得可以了"。
"""
        description = (
            f"按这位使用者的做事标准「{label}」做事——"
            + "；".join(text for _k, _c, text in clauses[:3])
            + "。"
        )[:180]
        out.append(
            {
                "name": name,
                "kind": "new",
                "target": "",
                "description": description,
                "body": body,
                "evidence": [quote],
                "frontmatter_keys": ["agent_created", "source"],
                "skill_md": (
                    f"---\nname: {name}\n"
                    f"description: {_yaml_str(description)}\n"
                    f"agent_created: true\nsource: user-standard\n---\n\n{body}"
                ),
            }
        )
    return out


def drafts_for(store: PersonaStore, workspace: str) -> list[dict]:
    """读使用者模型，产出该沉淀的 Skill 提案。没有就返回空——那是正常结果。"""
    entries = store.load(workspace)
    keys = standards_of(entries)
    if not keys:
        return []
    quotes: dict[str, str] = {}
    for item in entries:
        if item.dimension != "standard":
            continue
        for key, spec in STANDARDS.items():
            if key in quotes:
                continue
            if any(trigger in item.rule for trigger in spec["triggers"]):
                quotes[key] = item.rule
    context = _context_for(entries, store.root)
    return draft(keys, quotes, context)


def _context_for(entries, experience_dir=None) -> dict:
    """从已有记录里凑"他怎么判"的三条。凑不出来就空着——不编。"""
    asks_first: list[str] = []
    stops_when: list[str] = []
    for item in entries:
        if item.status != "active":
            continue
        if item.dimension == "boundary":
            asks_first.append(item.rule)
        for quote in item.quotes:
            # 驳回时说的那句话，是"他叫停过什么"的直接记录。
            if quote.get("source") == "denial" and quote.get("text"):
                stops_when.append(str(quote["text"])[:80])

    verifies_with: list[str] = []
    try:
        from blank_api.services.agent.experience import ExperienceStore

        # 显式传目录：`ExperienceStore()` 的默认值是「相对 cwd 的 data/experience」，
        # 从仓库根跑脚本就指到了另一个地方，结果"经验库是空的"而文件其实在
        # services/api 下。这个坑在 `lint_experience.py` 上已经踩过一次。
        store = ExperienceStore(experience_dir) if experience_dir else ExperienceStore()
        for entry in store.load(_workspace_hint(entries)):
            if entry.kind != "procedure" or entry.status == "retired":
                continue
            match = re.search(r"`([^`]{4,200})`", entry.statement)
            if match:
                verifies_with.append(match.group(1))
    except Exception:  # noqa: BLE001 - 取不到验证命令就不写，不挡沉淀
        verifies_with = []
    return {"asks_first": asks_first, "stops_when": stops_when, "verifies_with": verifies_with}


def _workspace_hint(entries) -> str:
    """条目上带着作用域路径，用它去读经验库，不必再传一遍工作区。"""
    for item in entries:
        if item.scope_ref:
            return item.scope_ref
    return ""


async def stage(session_factory, drafts: list[dict], *, recorder=None) -> list[dict]:
    """把提案落库，**已存在的跳过**——同一个标准不该每跑一个任务提一次。"""
    if not drafts:
        return []
    # 惰性导入：`--crystallize` 只是预览，用系统的 python 就能跑，
    # 不该因为没装 sqlalchemy 而看不到自己的标准。
    from sqlalchemy import select

    from blank_api.models.skill import Skill

    names = [item["name"] for item in drafts]
    async with session_factory() as db:
        existing = set((await db.execute(select(Skill.name).where(Skill.name.in_(names)))).scalars())
    fresh = [item for item in drafts if item["name"] not in existing]
    if not fresh:
        return []
    from blank_api.services.tools.skill_proposals import store_proposals

    stored = await store_proposals(session_factory, fresh, source="persona")
    if recorder is not None:
        await recorder.add(
            "skill.crystallized",
            "complete",
            {"count": len(stored), "names": [item["name"] for item in stored]},
        )
    return stored
