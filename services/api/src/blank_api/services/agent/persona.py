"""使用者模型：学他怎么干活，不是学这个世界。

## 为什么要和 `experience.py` 分开

`experience.py` 学的是**世界**：这个仓库哪里会踩坑、哪条命令能验证。
它回答"这里会怎么坏"，触发条件是路径或报错签名。

这一层学的是**人**：他的工序、粒度、验证标准、表达偏好、决策边界。
它回答"他会怎么做"，触发条件是"这件事属于哪一类工作"——不随具体路径变。

两层混在一起会互相污染：把"他要求每次改动配判据"塞进经验库，会被路径匹配逻辑
过滤掉（它没有 trigger_paths）；把"npm test 会超时"塞进使用者模型，会让模型以为
"他不喜欢跑测试"——**这是两种完全不同的结论，后者是灾难性的误读**。

## 最容易跑偏的地方：从 agent 的成功反推用户偏好

这是本模块唯一必须守住的不变量：

**只有用户自己说过、写过的东西才能进使用者模型。**

反面例子：agent 跳过了测试 → 任务成功 → 归纳出"他喜欢快" → 下次跳更多。
这就是"越学越跑偏"的机制——它把 agent 自己的偷懒，学成了用户的偏好，
然后用自己的偏见喂养自己。2610.04083 已经证明这条路关不掉，只能从源头堵：
**每条偏好必须挂一句用户原话**（`quotes`），没有原话就不准入。

## 三条次一级的约束

1. **一次不算数**：同一句话要在 ≥2 个任务里出现过才算"一贯要求"。单次很可能是
   这次任务的临时约束（"这个文件先别动"），不是他干活的方式。
2. **两条打架就都不用**：同一个工作维度上出现方向相反的两条（"先测试再实现" /
   "先实现再补测试"），不是让模型自己挑——实测两条矛盾记忆同时被检索时遵从率
   63–72%，而遵从后成功率塌到 17–31%（2607.10608）。这种情况下**两条都不注入**，
   在体检里报出来让人定。
3. **与本次明确指示冲突时以本次为准**：写进注入文案。使用者模型是"他一贯怎样"，
   不是"他这次必须怎样"。把它当硬规则，agent 就会变得固执——那是漫画，不是像他。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from blank_api.services.agent.experience import _lock_for

# 一条偏好要跨几个任务出现才算"一贯"。1 次是临时约束，2 次才算规律。
MIN_OCCURRENCES = 2

# 一段话最多产出几条候选。一个任务描述里出现五条"要求"时，其中多半是步骤。
MAX_PER_TEXT = 2

# 同一维度同作用域最多同时注入几条。使用者的要求通常很短，多了就是噪音。
MAX_INJECTED = 4


# --------------------------------------------------------------------------- #
# 工作维度：没有它就没法判"两条是不是在说同一件事"
# --------------------------------------------------------------------------- #
#
# 为什么用固定维度而不是自由文本聚类：判断两条偏好是否冲突，需要知道它们说的是
# 不是同一件事。自由文本聚类要么靠模型（引入不确定性，且模型会编），要么靠词面
# 相似度（"先写测试"和"不要先写测试"词面极像，结论相反）。固定维度是能用规则判的。
#
# 标准类的说法。它们不描述某一步做什么，而是规定"整件事按什么标准做"。
# 这类话最值钱也最难落地：写成一句提示，模型会看起来严谨地汇报；
# 要真按他的方式做，得展开成工序并且**能被核对**（见 conformance）。
STANDARD_TRIGGERS = (
    "严谨", "细致", "认真", "扎实", "别糊弄", "不要糊弄", "踏踏实实",
    "简洁", "别啰嗦", "不要啰嗦", "精简", "简短", "别废话",
)

# 维度名 → (给模型看的标签, 判定关键词)
DIMENSIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    # 标准排最前：一句"做事要严谨"不是某一步的要求，是**整台工序**的要求。
    # 认成 boundary 也行，但那样它就只是一条并列的规则，不会被展开成工序。
    "standard": ("做事标准", STANDARD_TRIGGERS),
    "order": ("工序", ("先", "再", "然后", "之后", "最后", "接着")),
    "granularity": ("粒度", ("一次", "单个", "一个文件", "小步", "分批", "全量", "整个仓库")),
    "verify": ("验证", ("测试", "跑一遍", "回归", "判据", "验证", "自检", "断言")),
    "style": ("表达", ("注释", "命名", "简洁", "排版", "措辞", "冗余")),
    "boundary": ("边界", ("必须问", "先问", "别动", "不要动", "不许", "禁止", "先确认")),
    "delivery": ("交付", ("报告", "文档", "截图", "说明", "总结", "写清")),
    "tooling": ("工具选择", ("用 ", "改用", "直接用", "别用", "不要用", "换成")),
}

# 方向相反的标记：用来判"同一维度上的两条是不是打架"。
# `别` 前面常是好词（特别/区别/类别/分别），加后顾排除；`不需要/不用/不必`
# 必须先于正向词表命中，否则"命名用中文也不需要解释"会被读成正向要求——实测踩到。
_NEGATIVE = re.compile(
    r"(不要|不许|不用|不必|不需|无需|不需要|没有必要|禁止|不能|不可|勿|避免|严禁|(?<![特区类分个性])别)"
)
_POSITIVE = re.compile(r"(要|应该|需要|必须|记得|一定)")

# 祈使句的入口词：只保留带这些词的句子，其余当描述性文字丢掉。
_IMPERATIVE = re.compile(
    r"(先|再|然后|之后|最后|不要|别|不许|禁止|必须|应该|需要|记得|一定|用|改用|直接用|一次|只改|只跑)"
)

_TIMESTAMP = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 条目
# --------------------------------------------------------------------------- #

@dataclass
class Preference:
    """他的一贯要求。

    `rule` 必须是**用户原话归一化后的形式**，不是我们改写出来的祈使句。
    改写会把"他说的"变成"我们理解的他说的"——那是跑偏的起点。
    """

    rule: str
    dimension: str = "boundary"
    # 作用域：项目（默认）/ 路径前缀 / 全局。一次任务里学到的东西不该无条件全局生效。
    scope: str = "project"
    scope_ref: str = ""
    # 原话证据。**没有它就等于没准入**——这是整个模块的核心不变量。
    quotes: list[dict] = field(default_factory=list)
    occurrences: int = 1
    # 来源等级，决定它能不能**自己**生效：
    #   explicit  = 他明说的（驳回时写的理由）——重复两次即可算"一贯"，自动生效；
    #   harvested = 从任务描述里摘出来的——**永远要你确认**，见模块头的说明。
    origin: str = "explicit"
    confidence: float = 0.5
    status: str = "candidate"
    # 偏好比坑稳定得多：工序不会三个月就变。但"永久生效"是不行的——
    # 他换了做法之后，我们还在按两年前的规矩办，那时它就不是"像他"，是"像过去的他"。
    # 只有**在另一个任务里再次出现**（或人确认）才续期，单纯被注入不续期。
    expires_after_days: int = 180
    last_confirmed: str = field(default_factory=_TIMESTAMP)
    first_seen: str = field(default_factory=_TIMESTAMP)
    last_seen: str = field(default_factory=_TIMESTAMP)

    @property
    def fingerprint(self) -> str:
        """同一句话换个标点/多个空格，指纹必须一样。"""
        blob = "|".join([_squash(self.rule), self.scope, _norm_ref(self.scope_ref)])
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    @property
    def label(self) -> str:
        return DIMENSIONS.get(self.dimension, ("其它", ()))[0]

    def as_dict(self) -> dict:
        data = asdict(self)
        data["fingerprint"] = self.fingerprint
        data["label"] = self.label
        return data


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _norm_ref(ref: str) -> str:
    """作用域路径归一到绝对路径。

    不归一的话，写入用相对路径、读取用绝对路径，两边**永远匹配不上**——
    条目存在、状态是 active、就是注入不进去，而没有任何地方会报错。
    今天已经栽过两次这个形状（`hits/misses` 和 `expires_after_days`），
    所以这一层宁可多做一步。
    """
    text = str(ref or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).resolve()).lower()
    except OSError:
        return text.lower()


# --------------------------------------------------------------------------- #
# 抽取：只从用户原话里切祈使句
# --------------------------------------------------------------------------- #

# 一句话里"关于他怎么做"的片段，通常落在一个分句里。按标点切，不跨句拼。
_CLAUSE_SPLIT = re.compile(r"[。；;！!？?\n]+|(?<=[，,])")


def dimension_of(text: str) -> str:
    """这句话在说哪一类工作。判定顺序即优先级——先命中先得。

    `boundary` 排在最前：带"不许/必须先问"的句子哪怕同时提到测试，也是边界要求，
    边界比工序更硬，认错了代价更大。
    """
    body = str(text or "")
    for name in ("standard", "boundary", "order", "verify", "granularity", "style", "tooling", "delivery"):
        for keyword in DIMENSIONS[name][1]:
            if keyword in body:
                return name
    return "boundary"


def polar(text: str) -> int:
    """+1 正向要求 / −1 禁止 / 0 看不出方向。"""
    body = str(text or "")
    if _NEGATIVE.search(body):
        return -1
    if _POSITIVE.search(body):
        return 1
    return 0


# 含具体产物（带扩展名的路径、引号内容、行号）的分句，是**这次的任务**而不是
# 他的做事方式。"然后跑 run_tests 验证"是任务步骤；"改完必须跑验证"才是要求。
# 这条只对任务描述生效——驳回反馈里提工具名（"直接用 list_files"）恰恰是要求。
# 出现下面任何一项的分句，讲的是**这次做什么**，不是他一贯怎么做事：
# 带扩展名的路径、引号/反引号里的内容、两位数以上（版本/尺寸/数量）、
# 具体命令与工具名。
#
# 这条是拿 111 个真实任务描述试出来的：不加它，`必须用 run_shell 执行 echo smoke-ok`
# 会被当成"他一贯用 run_shell"，而那是脚本，不是偏好。工具名在这里要算任务细节——
# 从**驳回反馈**里提工具名（"直接用 list_files"）才是要求，所以两个来源用不同门。
_ARTIFACT = re.compile(
    r"[\w./-]+\.[A-Za-z]{1,6}\b"
    r"|「[^」]+」|\"[^\"]+\"|`[^`]+`"
    r"|\d{2,}"
    r"|\b(echo|rm|curl|npm|pytest|git|node|python|uvicorn|cd|ls|dir)\b"
    r"|\b(run_shell|run_tests|browser_check|list_files|write_file|edit_file|memory_write)\b"
)


# 明确的要求标记。任务描述里的"然后跑 run_tests 验证"是**这一步要做什么**，
# 而"改完必须跑回归"是**他一贯怎么验收**。区别就在有没有这类标记——
# 任务步骤很少说"必须"，说"必须"的通常是在提要求。实测：只靠"含具体产物"过滤，
# "然后跑 run_tests 验证"依然会漏进来（命令名不带扩展名）。
_RULE_MARKER = re.compile(r"(必须|务必|一定|不许|不要|别|禁止|严禁|记得|以后|每次|一律|统一|只能)")


def _looks_task_specific(clause: str) -> bool:
    return bool(_ARTIFACT.search(clause))


def _bigrams(text: str) -> set[str]:
    """中文字符二元组。没有分词器，二元组是最省事又能用的"说的是不是同一件事"的近似。"""
    body = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]+", " ", str(text or "")).lower()
    grams: set[str] = set()
    for chunk in body.split():
        if len(chunk) == 1:
            continue
        grams.update(chunk[i : i + 2] for i in range(len(chunk) - 1))
    return grams


def _same_subject(a: str, b: str) -> bool:
    """两条要求是不是在说同一件事。
    
    只判方向相反不够：实测把"不要调用任何工具"和"参数 command 必须是这一条"
    判成了互相矛盾——它们来自两段不同的冒烟脚本，根本不相干。方向相反 + 说的是
    同一件事，才叫自相矛盾。判"同一件事"用字符二元组重叠，没有分词器也够用：
    "必须跑测试" 与 "不要跑测试" 共享 {跑测, 测试}；上面那两句一个都不共享。
    """
    left, right = _bigrams(a), _bigrams(b)
    if not left or not right:
        return False
    shared = left & right
    return len(shared) >= 2 and len(shared) / min(len(left), len(right)) >= 0.3


def extract_instructions(text: str, *, task_specific: bool = False) -> list[str]:
    """从一段用户原话里切出"他对做法提的要求"。

    刻意只做切分与去噪，**不做改写**：
    - 保留原句（去掉引号与多余空白），因为原话是他自己的措辞；
    - 只留带祈使词的片段，描述性文字（"这个页面现在长这样"）不算要求；
    - 太短（<6 字）或太长（>120 字）的丢掉——短的是碎片，长的是整段需求不是规则。
    """
    out: list[str] = []
    for raw in _CLAUSE_SPLIT.split(str(text or "")):
        # split 用 lookbehind 保留了分隔符本身，所以尾部还要再剥一次标点。
        clause = raw.strip().strip("「」“”\"'*`-").strip("，,、；;：: ").strip()
        if not (4 <= len(clause) <= 120):
            continue
        # 标准类的说法（"做事要严谨"）里没有任何祈使词——它是**语气**不是命令。
        # 只按祈使词筛会把这一类全丢掉，而它们恰恰最影响"像不像他"。
        is_standard = any(trigger in clause for trigger in STANDARD_TRIGGERS)
        if not (_IMPERATIVE.search(clause) or is_standard):
            continue
        if task_specific and not (_RULE_MARKER.search(clause) or is_standard):
            # 从任务描述里抽时，没有要求标记的分句一律不算——那是在描述这一步做什么。
            continue
        if task_specific and _looks_task_specific(clause):
            continue
        squashed = _squash(clause)
        if squashed in out:
            continue
        out.append(squashed)
    return out[:MAX_PER_TEXT]


def preference_from_quote(
    quote: str,
    *,
    task_id: str = "",
    source: str = "goal",
    scope: str = "project",
    scope_ref: str = "",
    origin: str = "explicit",
) -> Preference | None:
    """一句用户原话 → 一条候选偏好。切不出要求就返回 None。"""
    rule = _squash(quote)
    if not rule:
        return None
    return Preference(
        rule=rule,
        dimension=dimension_of(rule),
        scope=scope,
        scope_ref=_norm_ref(scope_ref),
        quotes=[{"text": quote.strip()[:300], "task": task_id, "source": source}],
        origin=origin if origin in ("explicit", "harvested") else "explicit",
        occurrences=1,
        confidence=0.5,
        status="candidate",
    )


# --------------------------------------------------------------------------- #
# 合并：同一句确认，方向相反的不许共存
# --------------------------------------------------------------------------- #

def merge(existing: list[Preference], incoming: Preference) -> tuple[list[Preference], str]:
    """返回 (全量, 动作)。动作：added / confirmed / superseded。

    `superseded`：同一维度、同作用域、方向相反。此时**两条都留在库里**但都不 active，
    交给体检让人定。自动选一条是危险的——我们没有任何依据知道哪条是他现在的意思。
    """
    for index, item in enumerate(existing):
        if item.fingerprint == incoming.fingerprint:
            # 他又说了一遍同一件事：这才是"一贯"的证据。
            #
            # 但**必须是另一个任务**。同一个任务里重试两次、或者我把同一条命令跑了两遍，
            # 都只说明"这次重复了"，不说明"他一贯如此"。第一版只计数不看来源任务，
            # 一次任务内重放两次就能把它顶成 active——那是自问自答。
            known = {str(quote.get("task") or "") for quote in item.quotes}
            incoming_task = str((incoming.quotes[0].get("task") if incoming.quotes else "") or "")
            fresh_task = bool(incoming_task) and incoming_task not in known
            if fresh_task or not incoming_task:
                item.occurrences = int(item.occurrences) + 1
            item.last_seen = _TIMESTAMP()
            # 续期也只认"另一个任务里又出现了"。被注入本身不续期。
            if fresh_task:
                item.last_confirmed = _TIMESTAMP()
            item.quotes = (list(item.quotes) + incoming.quotes)[-20:]
            # 置信度随出现次数升，但**封顶 0.9**：从来不写 1.0，因为"永远正确"不存在。
            item.confidence = round(min(0.9, 0.5 + 0.15 * (item.occurrences - 1)), 2)
            # **只有他明说的才能自动生效。**
            #
            # 拿 111 个真实任务描述试出来的教训：任务描述里写着"必须用 run_shell 执行
            # echo smoke-ok"，重复了 26 次——因为那是我同一段冒烟脚本跑了 26 遍，
            # 不是他 26 次要求这么做。**重复不等于一贯，得看重复的是不是同一个人
            # 同一个意图。** 从描述里摘的东西重复再多次也只是"候选"，要人点一下头。
            if (
                item.occurrences >= MIN_OCCURRENCES
                and item.status == "candidate"
                and item.origin == "explicit"
            ):
                item.status = "active"
            return existing, "confirmed"

    for item in existing:
        if item.status == "retired":
            continue
        if item.dimension != incoming.dimension or item.scope != incoming.scope:
            continue
        if item.scope_ref.lower() != incoming.scope_ref.lower():
            continue
        # 只在**方向相反**时判冲突。
        #
        # 第一版还额外判了一条"工序维度任意两条互斥"，理由是"先写测试再实现"和
        # "先实现再补测试"两个方向都没有正负词。拿 111 个真实任务描述一跑：
        # **40 处冲突，整个库瘫掉**——因为真实任务描述本来就有好几个顺序步骤。
        #
        # 结论是"互斥"这件事该在**注入层**管（同维度只取一条，见 injectable），
        # 不该在入库层管：入库时判冲突会把候选直接压成 conflict，连晋级的机会都没了。
        # 入库只管"这句话是不是自相矛盾"，"该用哪一条"交给注入和体检。
        opposing = bool(
            polar(item.rule)
            and polar(incoming.rule)
            and polar(item.rule) != polar(incoming.rule)
            and _same_subject(item.rule, incoming.rule)
        )
        if opposing:
            # 打架：两条都压成 conflict，都不注入。见模块头的第 2 条约束。
            item.status = "conflict"
            incoming.status = "conflict"
            existing.append(incoming)
            return existing, "superseded"

    existing.append(incoming)
    return existing, "added"


# --------------------------------------------------------------------------- #
# 注入：只有 active 的、且没有打架的
# --------------------------------------------------------------------------- #

def is_expired(entry: Preference, now: datetime | None = None) -> bool:
    """这条偏好是不是该重新怀疑了。

    规则对标 Copilot Memory：默认到期失效，**只有再次被确认才续期，单纯被注入不续期**。
    后半句是关键——否则一条老偏好只要还挂在库里就永远活着，而他早就改做法了。

    缺时间戳或解析不出来时判**不过期**：宁可多留一条，不要因为缺个字段误杀。
    """
    days = int(entry.expires_after_days or 0)
    raw = str(entry.last_confirmed or "").strip()
    if days <= 0 or not raw:
        return False
    try:
        confirmed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if confirmed.tzinfo is None:
        confirmed = confirmed.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - confirmed > timedelta(days=days)


def injectable(entries: Iterable[Preference], *, scope_ref: str = "") -> list[Preference]:
    """能进提示的那几条：active、不是 conflict、没过期、作用域对得上。"""
    moment = datetime.now(timezone.utc)
    picked = [
        item
        for item in entries
        if item.status == "active"
        and item.dimension != "standard"  # 标准由 render_standards 展开成工序，不并列成规则
        and item.occurrences >= MIN_OCCURRENCES
        and not is_expired(item, moment)
    ]
    if scope_ref:
        wanted = _norm_ref(scope_ref)
        picked = [
            item
            for item in picked
            if item.scope == "global"
            or (item.scope == "project" and _norm_ref(item.scope_ref) in ("", wanted))
        ]
    picked.sort(key=lambda item: (-item.occurrences, -item.confidence))
    # 每个维度只取一条：同一维度上的多条通常是同一件事的不同说法，全塞进去
    # 会互相稀释（而提示里每多一条，就多一条可能被误用的东西）。
    # 取最常出现的那条——"最常出现"就是"最像他一贯做法"的直接证据。
    out: list[Preference] = []
    seen: set[str] = set()
    for item in picked:
        if item.dimension in seen:
            continue
        seen.add(item.dimension)
        out.append(item)
        if len(out) >= MAX_INJECTED:
            break
    return out


def render(entries: Iterable[Preference]) -> str:
    """渲染成提示块。**不要求引用、不要求复述**——实测强制引用会导致过度依赖。"""
    items = list(entries)
    if not items:
        return ""
    lines = ["这位使用者一贯的做事要求（都是他自己说过的话，按出现次数排序）："]
    for item in items:
        lines.append(f"- [{item.label}] {item.rule}")
    lines.append(
        "以上是**他一贯的做法**，不是这次任务的指令。与本次任务的明确要求冲突时，以本次为准；"
        "也不要为了迎合而变通安全规则或跳过验证。"
    )
    return "\n".join(lines)



# --------------------------------------------------------------------------- #
# 标准 → 工序 → 可核对
# --------------------------------------------------------------------------- #
#
# "做事要严谨"不是一条规则，是**整台工序**的标准。它也是最影响"像不像他"的东西：
# 同一个任务，严谨的人要的是"改之前先读、改完必须验、没验的明说未验"；
# 换个人可能只要"跑通就行"。
#
# 但把它当提示词里的一句话不管用——模型会**看起来严谨地汇报**：结论里全是
# "已完成""应该没问题"，一行证据没有。所以要展开成工序，而且每条**能被核对**，
# 收尾时按事实判 passed / failed / unverifiable。
#
# 一条界线：**展开的条款是我们的理解，不是他的原话。** 措辞里必须写明，
# 判出来的结果只报告、不拦——理解偏了要能被他一眼看出来并纠正。

STANDARDS: dict[str, dict] = {
    "rigor": {
        "label": "严谨",
        "triggers": ("严谨", "细致", "认真", "扎实", "别糊弄", "不要糊弄", "踏踏实实"),
        "clauses": (
            ("change_is_verified", "改完必须验证：把跑过的命令和退出码写出来"),
            ("no_bare_claims", '没有验证过的部分明说"未验证"，不要用"应该能跑"盖过去'),
            ("report_is_traceable", "结论里带具体路径或命令，让人能照着复现"),
            ("failures_are_admitted", "有失败或跳过就如实写，不要只说\"全部通过\""),
        ),
    },
    "concise": {
        "label": "简洁",
        "triggers": ("简洁", "别啰嗦", "不要啰嗦", "精简", "简短", "别废话"),
        "clauses": (
            ("report_is_short", "结论压到必要信息，不复述过程"),
            ("no_padding", '不写"已完成如下工作""综上所述"这类填充句'),
        ),
    },
}

# 没证据的判断：出现了就说明结论在猜。
_HEDGE = re.compile(r"(应该(能|可以|会|没问题)?|大概|可能|估计|貌似|看起来|似乎|理论上|不出意外)")
# 承认不确定 / 承认失败的说法。有它就不算"裸猜"。
_ADMITS = re.compile(r"(未验证|没验证|没有验证|尚未|跳过|没跑|没通过|失败|不确定|待确认|fail|error)", re.I)
_PATHY = re.compile(r"[\w./-]+\.(py|ts|tsx|js|jsx|html?|css|json|md|sql|ya?ml)\b")
_COMMANDY = re.compile(r"`[^`]+`|\b(pytest|npm|python|node|curl|git|uvicorn)\b")
_PADDING = re.compile(r"(已完成如下工作|综上所述|总的来说|希望对你有帮助|以上就是全部)")


def standards_of(entries: Iterable[Preference]) -> list[str]:
    """已生效的做事标准（从 active 的偏好里认）。"""
    found: list[str] = []
    for item in entries:
        if item.status != "active" or item.dimension != "standard":
            continue
        for key, spec in STANDARDS.items():
            if key in found:
                continue
            if any(trigger in item.rule for trigger in spec["triggers"]):
                found.append(key)
    return found


def clauses_of(keys: Iterable[str]) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for key in keys:
        spec = STANDARDS.get(key)
        if not spec:
            continue
        for clause_key, text in spec["clauses"]:
            out.append((key, clause_key, text))
    return out


def render_standards(keys: Iterable[str]) -> str:
    wanted = list(keys)
    if not wanted:
        return ""
    labels = "、".join(f"「{STANDARDS[key]['label']}」" for key in wanted if key in STANDARDS)
    lines = [f"他要求做事{labels}。按这个标准，工序应该是："]
    index = 0
    for _key, _clause, text in clauses_of(wanted):
        index += 1
        lines.append(f"{index}) {text}")
    lines.append(
        "（这些是**从他自己的要求里展开的工序**，不是他的原话。哪条理解偏了直接说。"
        "做到几条，收尾时会被逐条核对。）"
    )
    return "\n".join(lines)


def conformance(keys: Iterable[str], facts: dict) -> list[dict]:
    """按标准逐条核对这次交付。`unknown ≠ failed` 的纪律与 done_when 一致。"""
    out: list[dict] = []
    for standard_key, clause_key, text in clauses_of(keys):
        status, note = _check_clause(clause_key, facts)
        out.append(
            {
                "standard": standard_key,
                "clause": clause_key,
                "text": text,
                "status": status,
                "note": note,
            }
        )
    return out


def _check_clause(key: str, facts: dict) -> tuple[str, str]:
    changed = [str(path) for path in (facts.get("changed_paths") or []) if str(path)]
    report = str(facts.get("report") or "")
    validation = facts.get("validation") or {}
    failures = int(facts.get("failures") or 0)

    if key == "change_is_verified":
        if not changed:
            return "unverifiable", "本轮没有改动文件"
        passed = validation.get("exit_code") == 0 or str(validation.get("status") or "") in (
            "ok",
            "passed",
        )
        if passed:
            return "passed", f"{len(changed)} 个文件有验证记录"
        return "failed", "改了文件但没跑验证"

    if key == "no_bare_claims":
        if not report:
            return "unverifiable", "没有交付文本"
        if _HEDGE.search(report) and not _ADMITS.search(report):
            hit = _HEDGE.search(report)
            return "failed", f'结论里有没证据的判断："{(hit.group(0) if hit else "")}"'
        return "passed", "结论里没有裸猜"

    if key == "report_is_traceable":
        if not changed:
            return "unverifiable", "本轮没有改动文件"
        if _PATHY.search(report) or _COMMANDY.search(report):
            return "passed", "结论里带了路径或命令"
        return "failed", "结论看不出改了哪个文件、跑了什么"

    if key == "failures_are_admitted":
        if failures == 0:
            return "unverifiable", "本轮没有失败可报"
        if _ADMITS.search(report):
            return "passed", "失败/跳过写进了结论"
        return "failed", f"本轮有 {failures} 次失败或跳过，结论里没提"

    if key == "report_is_short":
        if not report:
            return "unverifiable", "没有交付文本"
        if len(report) <= 900:
            return "passed", f"{len(report)} 字"
        return "failed", f"{len(report)} 字，超过 900"

    if key == "no_padding":
        if not report:
            return "unverifiable", "没有交付文本"
        hit = _PADDING.search(report)
        if hit:
            return "failed", f'有填充句："{hit.group(0)}"'
        return "passed", "没有填充句"

    return "unverifiable", "没实现的检查"


# --------------------------------------------------------------------------- #
# 存储：一个工作区一份 JSONL（与经验库同一个目录，不同文件后缀）
# --------------------------------------------------------------------------- #

class PersonaStore:
    def __init__(self, root: Path | str | None = None) -> None:
        override = str(os.environ.get("BLANK_PERSONA_DIR") or "").strip()
        self.root = Path(root or override or "data/experience")

    def path_for(self, workspace: str | Path) -> Path:
        resolved = str(Path(workspace).resolve())
        slug = re.sub(r"[^A-Za-z0-9\u4e00-\u9fa5]+", "-", Path(resolved).name or "root").strip("-")
        digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:8]
        # 后缀是 .persona.jsonl：同一个目录里，经验是 .jsonl，使用者模型是 .persona.jsonl，
        # 一眼能分清"这是学世界的"还是"这是学我的"。
        return self.root / f"{slug or 'root'}-{digest}.persona.jsonl"

    def load(self, workspace: str | Path) -> list[Preference]:
        try:
            text = self.path_for(workspace).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            return []
        out: list[Preference] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("rule"):
                out.append(_from_dict(data))
        return out

    def save(self, workspace: str | Path, entries: Iterable[Preference]) -> None:
        path = self.path_for(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(json.dumps(item.as_dict(), ensure_ascii=False) for item in entries) + "\n"
        with _lock_for(path):
            path.write_text(body, encoding="utf-8")

    def record(self, workspace: str | Path, incoming: Preference) -> tuple[str, Preference]:
        path = self.path_for(workspace)
        with _lock_for(path):
            entries = self.load(workspace)
            entries, action = merge(entries, incoming)
            self.save(workspace, entries)
        for item in entries:
            if item.fingerprint == incoming.fingerprint:
                return action, item
        return action, incoming

    def learn_from_text(
        self,
        workspace: str | Path,
        text: str,
        *,
        task_id: str = "",
        source: str = "denial",
        scope_ref: str = "",
        origin: str = "explicit",
        task_specific: bool = False,
    ) -> list[tuple[str, Preference]]:
        """从一段用户原话里学。切不出要求就是空列表——那是正常结果。"""
        out: list[tuple[str, Preference]] = []
        for clause in extract_instructions(text, task_specific=task_specific):
            pref = preference_from_quote(
                clause,
                task_id=task_id,
                source=source,
                scope_ref=scope_ref,
                origin=origin,
            )
            if pref is not None:
                out.append(self.record(workspace, pref))
        return out

    def record_assertion(self, workspace: str | Path, text: str) -> list[Preference]:
        """他当面提的要求：记下来，**一次就生效**。

        为什么这一次不用"出现两次"：那个门槛是给**自动推断**用的——从行为里猜他
        想要什么，猜错代价高，所以要攒证据。他当面说明白话是另一回事，那是最强的
        证据本身。（消费级调研里的同一条：钉钉只认"帮我记住……"这类触发指令。）

        置信度给 0.8 但不给 1.0：连他本人都可能是这次这么想。
        """
        learned = self.learn_from_text(
            workspace, text, task_id="", source="tell", scope_ref=str(workspace)
        )
        if not learned:
            return []
        entries = self.load(workspace)
        touched: list[Preference] = []
        stamps = {pref.fingerprint for _action, pref in learned}
        for item in entries:
            if item.fingerprint not in stamps:
                continue
            item.status = "active"
            # 他说一次就等于满足门槛。不补这一步会**静默失效**：状态是 active，
            # 但 injectable 按 occurrences >= 2 过滤，条目看着生效了、其实一条都进不了提示。
            item.occurrences = max(int(item.occurrences), MIN_OCCURRENCES)
            item.confidence = max(float(item.confidence), 0.8)
            item.last_confirmed = _TIMESTAMP()
            touched.append(item)
        self.save(workspace, entries)
        return touched

    def persona_block(self, workspace: str | Path) -> str:
        """给提示用的那一块。取不到就返回空串，绝不挡任务。"""
        try:
            entries = self.load(workspace)
            parts = [
                render(injectable(entries, scope_ref=str(workspace))),
                render_standards(standards_of(entries)),
            ]
            return "\n\n".join(item for item in parts if item.strip())
        except Exception:  # noqa: BLE001 - 学人失败不该让任务起不来
            return ""


def _from_dict(data: dict) -> Preference:
    fields = {name for name in Preference.__dataclass_fields__}
    clean = {key: value for key, value in data.items() if key in fields}
    if not isinstance(clean.get("quotes"), list):
        clean["quotes"] = []
    try:
        clean["occurrences"] = max(1, int(clean.get("occurrences") or 1))
    except (TypeError, ValueError):
        clean["occurrences"] = 1
    try:
        clean["confidence"] = float(clean.get("confidence") or 0.5)
    except (TypeError, ValueError):
        clean["confidence"] = 0.5
    try:
        clean["expires_after_days"] = int(clean.get("expires_after_days") or 180)
    except (TypeError, ValueError):
        clean["expires_after_days"] = 180
    return Preference(**clean)


# --------------------------------------------------------------------------- #
# 体检：只报告
# --------------------------------------------------------------------------- #

def lint(entries: Iterable[Preference]) -> list[dict]:
    findings: list[dict] = []
    items = list(entries)

    for item in items:
        if item.status == "conflict":
            findings.append(
                {
                    "kind": "conflict",
                    "message": f"「{item.label}」上有一条方向相反的要求，两条都已停用；"
                    "你定留哪条（`scripts/persona.py --keep <指纹>`）",
                    "evidence": [item.rule[:80]],
                }
            )
        if is_expired(item):
            findings.append(
                {
                    "kind": "expired",
                    "message": f"超过 {item.expires_after_days} 天没被再次确认，已停止注入；"
                    "还成立就用 --promote 续期",
                    "evidence": [item.rule[:80]],
                }
            )
        if not item.quotes:
            # 没有原话的条目一律是"我们推断的"，不该存在。
            findings.append(
                {
                    "kind": "ungrounded",
                    "message": "这条没有用户原话，是推断出来的，应删除",
                    "evidence": [item.rule[:80]],
                }
            )
        if (
            item.status == "candidate"
            and item.origin == "explicit"
            and item.occurrences < MIN_OCCURRENCES
        ):
            findings.append(
                {
                    "kind": "unconfirmed",
                    "message": f"只出现过 {item.occurrences} 次（要 ≥{MIN_OCCURRENCES} 次才算一贯），"
                    "暂不注入",
                    "evidence": [item.rule[:80]],
                }
            )

    active = [item for item in items if item.status == "active"]
    by_dim: dict[str, list[Preference]] = {}
    for item in active:
        by_dim.setdefault(item.dimension, []).append(item)
    for dimension, group in by_dim.items():
        # 工序维度只要有多条 active 就报：注入层同维度只取一条，多出来的那条
        # 永远用不上，等于白学。其余维度允许并存到 3 条。
        limit = 1 if dimension == "order" else 2
        if len(group) > limit:
            findings.append(
                {
                    "kind": "crowded",
                    "message": f"「{DIMENSIONS.get(dimension, ('其它',))[0]}」上有 {len(group)} 条要求，"
                    "注入时只会用最常出现的那条，其余等于白学；考虑合并或退役",
                    "evidence": [item.rule[:60] for item in group[:4]],
                }
            )
    return findings


def render_lint(findings: list[dict]) -> str:
    if not findings:
        return "使用者模型体检通过：没有冲突、没有无原话条目、没有未确认项。"
    lines = [f"使用者模型体检发现 {len(findings)} 处问题："]
    for item in findings:
        lines.append(f"- [{item['kind']}] {item['message']}")
        for evidence in item.get("evidence") or []:
            lines.append(f"    · {evidence}")
    return "\n".join(lines)
