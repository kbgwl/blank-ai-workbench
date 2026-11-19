"""经验条目：自学习的存储形状。

## 为什么是这个形状

实测数据（`docs/self-improvement-methods.md` 引 2604.27003 / 2608.20274）：
**抽象后的子任务级经验能正迁移；原始轨迹（−9.5/−7.5）和"整任务总结"会负迁移**
——后者甚至低于"完全没有记忆"的基线。

我们原来的 `MemoryStore.consolidate()` 写的正是"整任务总结"：
「结果：结论 - 顶层共 31 项，前 5 个都是目录…」。57 条里 45 条是这个形状，
同一条事实重复 13 次，还有两条互相矛盾。那不是"效果不好"，那是**负收益**。

所以这里换个形状：一条经验 = 一个具体的坑或做法 + 它什么时候适用 + 凭什么信它。

## 四条硬约束（都有出处）

1. **准入**：不是所有任务都值得学。没有改动、没有验证、没有失败信号 → 不学。
2. **证据**：每条必须能指向具体文件、命令或报错。纯反思会污染经验
   （Reflexion 去掉测试只留反思：0.52 < 基线 0.60）。
3. **去重**：同一事实只留一条。新认识覆盖旧的，不是并排躺着。
4. **剂量**：检索最多注入 1 条（SWE-Exp 的 k 消融：1 条 42.0 > 2 条 39.8 > 4 条 39.0）。

## 关于"关掉记忆"

2610.04083 的实测：agent 自己写进持久存储的东西会跨会话继承；把 memory 工具拿掉后，
74% 的会话改用文件系统；审计器只能把传播率从 71% 降到 34%。**"关掉记忆"不是防御。**
防御只能靠证据门与回放验证，所以这两条必须写进代码，而不是写在文档里。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

# 同一份经验库的读改写要串行：两个并发任务各自读到旧文件再各自写回，
# 后写的会覆盖先写的（MemoryStore 当年在这里踩过）。
_LOCK_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _lock_for(path: Path) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(str(path)))
    with _LOCK_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock

# 条目的三种类型。区分它们是因为注入措辞不同：
# pitfall（坑）要提醒"这样会错"，procedure（做法）要提醒"这样能成"，
# fact（事实）只是客观信息。
KINDS = ("pitfall", "procedure", "fact")
STATUSES = ("candidate", "active", "retired")

# 一条经验最多活多久（到期未复验 → 降回 candidate，由回放决定去留）。
DEFAULT_TTL_DAYS = 90
# 低于这个置信度就不注入：0.3 以下等于"我猜的"。
MIN_INJECT_CONFIDENCE = 0.3


@dataclass
class Experience:
    statement: str
    kind: str = "pitfall"
    status: str = "candidate"
    # 什么时候该想起它。三个维度都不填 = 永远不会被检索到，等于没写。
    trigger_paths: list[str] = field(default_factory=list)
    task_terms: list[str] = field(default_factory=list)
    error_signature: str = ""
    why: str = ""
    # 证据：谁、什么信号、指向哪里。空证据的条目不会被晋级。
    evidence: list[dict] = field(default_factory=list)
    confidence: float = 0.5
    hits: int = 0
    misses: int = 0
    last_confirmed: str = ""
    expires_after_days: int = DEFAULT_TTL_DAYS
    task_id: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def fingerprint(self) -> str:
        """去重键：这条经验"说的是哪件事"。

        用规范化后的 statement，不用 trigger（路径 / 关键词集合）。

        第一版把 trigger 混进指纹，结果同一个坑只要关键词多写一个就成了两条——
        `["pagination"]` 与 `["pagination","cursor"]` 指纹不同，去重等于没做。
        trigger 描述的是"什么时候想起来"，不是"这件事是什么"，两件事不能混。
        """
        blob = _squash(self.statement)[:200]
        if not blob:
            blob = str(self.error_signature or "").strip().lower()[:200]
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _norm_paths(paths: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for item in paths or ():
        text = str(item or "").replace("\\", "/").strip().lstrip("./")
        if text and text not in out:
            out.append(text[:200])
    return out


# --------------------------------------------------------------------------- #
# 准入：什么样的任务值得学
# --------------------------------------------------------------------------- #

@dataclass
class Admission:
    learn: bool
    reason: str
    kind: str = ""
    strength: float = 0.0


def admission(
    *,
    changed_paths: Iterable[Any] = (),
    validation: dict | None = None,
    failures: Iterable[dict] = (),
    readonly: bool = False,
    succeeded: bool = True,
) -> Admission:
    """这次任务该不该产出经验。返回理由，便于解释"为什么没学"。

    四条判据，全部来自实测结论：

    - **没有改动也没有失败 → 不学**。冒烟测试、纯问答都属于这类。它们占了我们
      现有记忆的 79%，而且是"整任务总结"这种负迁移形状。
    - **失败 → 学，但必须有外部信号**（报错文本 / 退出码）。没有信号的失败
      只是"感觉不对"，据此写经验就是 Reflexion 那个 0.52 < 0.60 的坑。
    - **成功但没验证 → 不学**。我们不知道它到底对不对；把它写成"这样做能成"
      是最危险的假阳性。
    - **只读调研 → 不学**（除非有明确的失败信号）。
    """
    paths = _norm_paths(changed_paths)
    failure_list = [f for f in (failures or ()) if isinstance(f, dict)]
    status = str((validation or {}).get("status") or "")
    exit_code = (validation or {}).get("exit_code")
    verified = status in ("ok", "failed") or isinstance(exit_code, int)

    if failure_list and any(_has_signal(f) for f in failure_list):
        # 失败优先：一次踩坑的价值通常高于一次顺利。
        return Admission(True, "有失败且带外部信号", "pitfall", 0.7)
    if readonly:
        return Admission(False, "只读任务，没有可复用的做法")
    if not paths:
        return Admission(False, "没有改动任何文件——没有可复用的东西")
    if not verified:
        return Admission(False, "成功了但没有验证结论，不知道对不对")
    if not succeeded:
        return Admission(False, "失败但没有可定位的信号")
    return Admission(True, "有改动且验证过", "procedure", 0.5)


def _has_signal(failure: dict) -> bool:
    """失败是否可定位：有报错文本、退出码或明确的工具名 + 原因。"""
    if isinstance(failure.get("exit_code"), int):
        return True
    for key in ("error", "stderr", "message", "reason"):
        if len(str(failure.get(key) or "").strip()) >= 12:
            return True
    return bool(str(failure.get("tool") or "").strip() and str(failure.get("reason") or "").strip())


# --------------------------------------------------------------------------- #
# 去重与合并
# --------------------------------------------------------------------------- #

def merge(existing: list[Experience], incoming: Experience) -> tuple[list[Experience], str]:
    """把新条目并进已有列表。返回 (新列表, 动作)。

    动作用来解释账：`added` / `confirmed` / `replaced`。
    为什么不并排躺着：实测里两条互相矛盾的记忆同时被检索到，遵从率 63–72%，
    而遵从之后成功率塌到 17–31%（2607.10608）。同主题只能有一条现行版本。
    """
    key = incoming.fingerprint
    for index, item in enumerate(existing):
        if item.fingerprint == key and item.status != "retired":
            # 同一句话被再次证实：涨一点置信度，刷新复验时间。
            existing[index] = _bump(item, incoming)
            return existing, "confirmed"
        # 同一个报错签名、但结论不同：新认识覆盖旧的（旧条目退役，保留可追溯）。
        # 实证：两条互相矛盾的记忆同时被检索到，遵从率 63–72%，而遵从之后
        # 成功率塌到 17–31%——同主题只能有一条现行版本。
        if (
            incoming.error_signature
            and item.error_signature
            and _squash(item.error_signature) == _squash(incoming.error_signature)
            and item.status != "retired"
        ):
            existing[index] = Experience(**{**item.as_dict(), "status": "retired"})
            existing.append(incoming)
            return existing, "replaced"
    existing.append(incoming)
    return existing, "added"


def _same_claim(a: Experience, b: Experience) -> bool:
    return _squash(a.statement) == _squash(b.statement)


def _bump(old: Experience, new: Experience) -> Experience:
    data = old.as_dict()
    data["hits"] = int(data.get("hits") or 0) + 1
    # 置信度只小步上涨，且封顶 0.9：不写 1.0 是因为"从来没错过"这种状态
    # 在真实工程里不存在，写满了就再也降不下来。
    data["confidence"] = round(min(0.9, float(data.get("confidence") or 0.5) + 0.05), 2)
    data["last_confirmed"] = new.last_confirmed or old.last_confirmed
    if new.evidence:
        data["evidence"] = (list(data.get("evidence") or []) + list(new.evidence))[-5:]
    return Experience(**data)


# --------------------------------------------------------------------------- #
# 检索：最多 1 条
# --------------------------------------------------------------------------- #

def rank(
    entries: Iterable[Experience],
    *,
    goal: str = "",
    paths: Iterable[Any] = (),
    limit: int = 1,
) -> list[Experience]:
    """挑出最该被想起的一条。

    打分是**规则式**的，不用向量：条目自带 trigger（路径 / 关键词 / 报错签名），
    匹配这些显式条件比语义相似更可控，也更容易解释"为什么想起了这条"。

    剂量上限是 1（默认）。这个数字不是保守，是 SWE-Exp 消融出来的：
    1 条 42.0、2 条 39.8、4 条 39.0。多给只会互相干扰。
    """
    goal_text = _squash(goal)
    wanted = _norm_paths(paths)
    scored: list[tuple[float, Experience]] = []
    for entry in entries:
        if entry.status == "retired":
            continue
        if float(entry.confidence) < MIN_INJECT_CONFIDENCE:
            continue
        score = 0.0
        hard = False  # 路径或报错签名命中才叫"想起了这件事"，关键词只是加权
        for pattern in entry.trigger_paths:
            base = pattern.rstrip("*").rstrip("/").lower()
            if base and any(path.lower().startswith(base) for path in wanted):
                score += 2.0
                hard = True
                break
        # 关键词只加分，不单独构成命中：路径完全不同的任务也曾靠
        # `deliverables` / `html` 这类词想起了同一条经验，那是误命中。
        # 硬条件只有两个——路径前缀命中，或报错签名命中。
        terms = [t.lower() for t in entry.task_terms if t.strip()]
        if terms and goal_text:
            hit = sum(1 for term in terms if term in goal_text)
            if hit:
                score += min(1.0, hit / max(1, len(terms)))
        if entry.error_signature:
            signature = entry.error_signature.lower()[:60]
            if signature and signature in goal_text:
                score += 2.5
                hard = True
        # 关键词命中只加权，不构成命中理由：`deliverables` / `html` 这种词几乎每个
        # 任务里都有，让它单独触发就是"永远命中"。
        if not hard or score <= 0:
            continue
        # 置信度只做微调：一条很准但触发条件不匹配的经验仍然不该被想起。
        scored.append((score + float(entry.confidence), entry))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in scored[: max(1, limit)]]


_GOAL_PATH = re.compile(r"[A-Za-z0-9_\-./\\]+\.[A-Za-z0-9]{1,6}")


def paths_from_goal(goal: str) -> list[str]:
    """从任务描述里认出它要动哪些文件。

    任务刚开始时还没有 `changed_paths`，而"路径前缀命中"是检索最硬的信号。
    好在用户写目标时常常把路径直接写出来（"在 deliverables/x.html 写…"），
    从目标里认一遍就能让第一轮就命中。

    只认"带扩展名的像路径的东西"，不做模糊猜测——认错一个路径会让检索带偏。
    """
    out: list[str] = []
    for match in _GOAL_PATH.findall(str(goal or "")):
        text = match.replace("\\", "/").strip().lstrip("./")
        if not text or "/" not in text and "." not in text:
            continue
        if text.lower() in out:
            continue
        out.append(text[:200])
    return out[:8]


def render(entry: Experience) -> str:
    """注入给模型的措辞。

    三条来自实测的写法要求：
    - 说"历史经验，可能不适用"，不说"必须遵守"——后者会让模型放弃验证；
    - **不要求引用或复述**（SWE-Exp：强制引用导致过度依赖，改成隐性上下文最好）；
    - 带上证据指针，让模型能自己去核对。
    """
    label = {"pitfall": "踩过的坑", "procedure": "走通过的做法", "fact": "已知事实"}.get(
        entry.kind, "历史经验"
    )
    lines = [f"[{label}，来自本项目的历史任务，可能不适用，请自行验证] {entry.statement}"]
    if entry.why:
        lines.append(f"为什么：{entry.why}")
    for item in entry.evidence[:2]:
        signal = str(item.get("signal") or "").strip()
        pointer = str(item.get("pointer") or "").strip()
        if signal or pointer:
            lines.append(f"依据：{signal}{'（' + pointer + '）' if pointer else ''}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 抽取：0–2 条，确定性规则，不调模型
# --------------------------------------------------------------------------- #

MAX_EXTRACTED = 2


def extract(
    *,
    task_id: str = "",
    goal: str = "",
    changed_paths: Iterable[Any] = (),
    validation: dict | None = None,
    failures: Iterable[dict] = (),
    at: str = "",
) -> list[Experience]:
    """从一次任务里抽 0–2 条可复用经验。

    **刻意不用模型来抽**：ExpeL 把 reflection 加进 insight 提取，39 → 29；
    Reflexion 去掉测试只留反思，HumanEval Rust 0.52 < 基线 0.60。自反思会写出
    听起来对、实际是幻觉的经验，然后下一轮被当成事实照做。

    所以这里只做"把已有事实换个形状"：
    - 失败 + 有信号 → 一条 pitfall（错在哪个命令、错在什么形态）
    - 改动 + 验证通过 → 一条 procedure（哪些路径靠哪条命令验的）
    没有可抽的就返回空——**"这次学到了 0 条"是正常结果**，不是失败。
    """
    out: list[Experience] = []
    paths = _norm_paths(changed_paths)
    verdict = admission(
        changed_paths=paths,
        validation=validation,
        failures=failures,
        succeeded=bool(validation),
    )
    if not verdict.learn:
        return []

    if verdict.kind == "pitfall":
        for item in failures:
            if not isinstance(item, dict) or not _has_signal(item):
                continue
            signal = _signal_text(item)
            if not signal:
                continue
            tool = str(item.get("tool") or item.get("tool_name") or "命令").strip()
            out.append(
                Experience(
                    statement=f"{tool} 在这里会失败：{signal}",
                    kind="pitfall",
                    status="candidate",
                    trigger_paths=paths[:3],
                    task_terms=_terms(goal, paths),
                    error_signature=signal[:120],
                    why=f"任务 {task_id[:8]} 里实际踩到",
                    evidence=[
                        {
                            "task": task_id,
                            "signal": signal,
                            "pointer": str(item.get("path") or item.get("command") or "")[:200],
                        }
                    ],
                    confidence=verdict.strength,
                    last_confirmed=at,
                    task_id=task_id,
                )
            )
            break  # 一次任务最多记一条坑：多记会把同一次经历拆成好几条噪音

    if verdict.kind == "procedure" and paths:
        command = str((validation or {}).get("command") or "").strip()
        if command:
            out.append(
                Experience(
                    statement=f"改动 {', '.join(paths[:2])} 之后，用 `{command}` 验证能通过",
                    kind="procedure",
                    status="candidate",
                    trigger_paths=paths[:3],
                    task_terms=_terms(goal, paths),
                    why=f"任务 {task_id[:8]} 里实测通过",
                    evidence=[
                        {
                            "task": task_id,
                            "signal": f"{command} 退出码 0",
                            "pointer": paths[0],
                        }
                    ],
                    confidence=verdict.strength,
                    last_confirmed=at,
                    task_id=task_id,
                )
            )
    return out[:MAX_EXTRACTED]


def _signal_text(item: dict) -> str:
    for key in ("error", "stderr", "message", "reason"):
        text = str(item.get(key) or "").strip()
        if len(text) >= 12:
            return _squash(text)[:160]
    code = item.get("exit_code")
    if isinstance(code, int):
        return f"退出码 {code}"
    return ""


# 关键词里不该出现的词：它们在几乎每个任务里都会出现，命中它们等于没筛。
# `deliverables` / `html` / `index` 这类是路径与文件类型碎片，不是任务语义。
_TERM_STOPWORDS = frozenset(
    {
        "deliverables", "probe", "index", "html", "css", "js", "the", "and", "for",
        "with", "that", "this", "then", "from", "run", "file", "page",
    }
)


def _terms(goal: str, paths: Iterable[Any] = ()) -> list[str]:
    """从目标里取几个词做触发条件。

    两条过滤：
    - 只取 ASCII 词与数字：中文没有分词，粗切会产生一堆两字垃圾词；
    - 去掉停用词与**路径片段**：`deliverables` / `html` 在几乎每个任务里都有，
      留着它们会让"关键词命中"变成永远命中（实测：路径完全不同的任务也想起了同一条）。
    """
    path_blob = " ".join(_norm_paths(paths)).lower()
    words = re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", str(goal or ""))
    seen: list[str] = []
    for word in words:
        lowered = word.lower()
        if lowered in seen or lowered in _TERM_STOPWORDS:
            continue
        if lowered in path_blob:
            # 出现在路径里的词是"这次改哪儿"，不是"这件事是什么"。
            continue
        seen.append(lowered)
    return seen[:6]


# --------------------------------------------------------------------------- #
# 存储：按工作区一个 JSONL
# --------------------------------------------------------------------------- #

class ExperienceStore:
    """一个工作区一份 JSONL。一行一条，追加为主、改写时整文件重写。

    为什么不是 md：md 版本（`MemoryStore`）已经证明了后果——散文条目没法去重、
    没法标状态、没法算置信度，只能一路追加。57 条里 45 条是冒烟流水账，就是那样长出来的。
    JSONL 一行一条，坏一行不影响其余，人去读也不难。
    """

    def __init__(self, root: Path | str | None = None) -> None:
        override = str(os.environ.get("BLANK_EXPERIENCE_DIR") or "").strip()
        self.root = Path(root or override or "data/experience")

    def path_for(self, workspace: str | Path) -> Path:
        resolved = str(Path(workspace).resolve())
        slug = re.sub(r"[^A-Za-z0-9\u4e00-\u9fa5]+", "-", Path(resolved).name or "root").strip("-")
        digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:8]
        return self.root / f"{slug or 'root'}-{digest}.jsonl"

    def load(self, workspace: str | Path) -> list[Experience]:
        path = self.path_for(workspace)
        try:
            text = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            return []
        out: list[Experience] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                # 坏行跳过而不是整份放弃：一份经验库不该因为一行手改坏的 JSON 就全废。
                continue
            if isinstance(data, dict) and data.get("statement"):
                out.append(_from_dict(data))
        return out

    def save(self, workspace: str | Path, entries: Iterable[Experience]) -> None:
        path = self.path_for(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(json.dumps(item.as_dict(), ensure_ascii=False) for item in entries) + "\n"
        with _lock_for(path):
            path.write_text(body, encoding="utf-8")

    def record(self, workspace: str | Path, incoming: Experience) -> tuple[str, list[Experience]]:
        """写入一条，返回 (动作, 全量条目)。动作是 added / confirmed / replaced。"""
        path = self.path_for(workspace)
        with _lock_for(path):
            entries = self.load(workspace)
            entries, action = merge(entries, incoming)
            self.save(workspace, entries)
        return action, entries

    def record_outcome(self, workspace: str | Path, experience_id: str, *, success: bool) -> str:
        """这条经验被用过了，结果如何。返回它现在的状态。

        这是整个闭环里最容易被做成摆设的一环：只写不读、读了不回溯，条目就永远停在
        candidate，谁也不知道它有没有用（开源实现里普遍如此——OpenHands 的两级
        MEMORY.md 就完全没有这个回路）。

        两个方向的步长不对称，因为**归因本身不对称**：
        - 成功时 +0.05：任务成功很可能是别的因素，不能全记在经验头上；
        - 失败时 −0.12：也可能是经验带偏了，同样不能全怪它，但代价更该被记住。
        攒够两次成功才晋级，两次失败就退役——小样本上单次结论不可靠。
        """
        path = self.path_for(workspace)
        with _lock_for(path):
            entries = self.load(workspace)
            for item in entries:
                if item.fingerprint != experience_id or item.status == "retired":
                    continue
                if success:
                    item.hits += 1
                    item.confidence = round(min(0.9, float(item.confidence) + 0.05), 2)
                    if item.hits >= 2 and item.status == "candidate":
                        item.status = "active"
                else:
                    item.misses += 1
                    item.confidence = round(max(0.1, float(item.confidence) - 0.12), 2)
                    if item.misses >= 2:
                        # 两次都没帮上忙就退役。留着只会继续被检索到、继续干扰。
                        item.status = "retired"
                self.save(workspace, entries)
                return item.status
        return ""

    def recall(
        self, workspace: str | Path, *, goal: str = "", paths: Iterable[Any] = (), limit: int = 1
    ) -> list[Experience]:
        return rank(self.load(workspace), goal=goal, paths=paths, limit=limit)


def _from_dict(data: dict) -> Experience:
    """只取已知字段：手改过的文件里多出来的键不该让 Load 崩掉。"""
    fields = {name for name in Experience.__dataclass_fields__}
    clean = {key: value for key, value in data.items() if key in fields}
    for key in ("trigger_paths", "task_terms", "evidence"):
        if not isinstance(clean.get(key), list):
            clean[key] = []
    try:
        clean["confidence"] = float(clean.get("confidence") or 0.5)
    except (TypeError, ValueError):
        clean["confidence"] = 0.5
    for key in ("hits", "misses", "expires_after_days"):
        try:
            clean[key] = int(clean.get(key) or 0)
        except (TypeError, ValueError):
            clean[key] = 0
    return Experience(**clean)
