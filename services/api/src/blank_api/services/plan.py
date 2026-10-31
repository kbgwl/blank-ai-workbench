"""计划 / 目标 / 里程碑的统一数据模型。

这里是**唯一真源**：工具写什么形状、图状态怎么带回、任务行怎么落库、前端读到什么，
全部经过这里的归一化函数。散在各处各写一份的话，迟早出现"工具认这个字段、
前端读那个字段"的静默不一致——而计划是用户唯一能看见的执行依据，错不起。

## 三层结构

::

    goal_spec        目标：为什么做、算不算做完、不做什么
      └─ milestones 里程碑：把若干步骤归到一个阶段性目标下
           └─ steps 步骤：最小可勾选单位，带状态与证据

## 兼容

历史数据里 ``acceptance_criteria`` 是 ``["验收点"]``、计划步骤是纯字符串。
所有归一化函数都同时吃新旧两种形状，读旧数据不需要迁移脚本。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable

PLAN_STATUSES = ("pending", "in_progress", "completed")
MILESTONE_STATUSES = ("pending", "in_progress", "completed")
MAX_STEPS = 12
MAX_MILESTONES = 6
MAX_STEP_CHARS = 200
MAX_TITLE_CHARS = 120
MAX_EVIDENCE_CHARS = 400

_MARKS = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}
_MILESTONE_MARKS = {"completed": "●", "in_progress": "◐", "pending": "○"}
_WS = re.compile(r"\s+")


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _stable_id(prefix: str, seed: str) -> str:
    """内容寻址的短 id。

    不用随机 uuid：同一段步骤文本在两次提交里算出的 id 必须一样，否则
    「重新提交计划」会把验收条件和里程碑的引用全部指空。
    """
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}{digest}"


def _norm_text(value: str) -> str:
    """比对用的规范化：只保留字与数字。标点、空格变了不算换了一步。"""
    return _WS.sub("", value).lower()


# --------------------------------------------------------------------------- 目标


def normalize_goal(raw: object, *, fallback: str = "") -> dict:
    """把目标归一化成 ``{objective, done_when, constraints, out_of_scope}``。

    接受三种输入：

    - 字符串：当作 objective，其余为空列表（模型只给了句话）；
    - 字典：逐键容错解析，缺的补空；
    - None：退回 ``fallback``（通常是用户原话 ``task.goal``）。
    """
    if isinstance(raw, str):
        text = raw.strip()
        return {
            "objective": _clip(text, 1000) or _clip(fallback, 1000),
            "done_when": [],
            "constraints": [],
            "out_of_scope": [],
        }

    if not isinstance(raw, dict):
        return {
            "objective": _clip(fallback, 1000),
            "done_when": [],
            "constraints": [],
            "out_of_scope": [],
        }

    def _list(*keys: str) -> list[str]:
        for key in keys:
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return [_clip(value, MAX_STEP_CHARS)]
            if isinstance(value, list):
                items = [_clip(item, MAX_STEP_CHARS) for item in value]
                return [item for item in items if item][:10]
        return []

    objective = _clip(
        raw.get("objective") or raw.get("goal") or raw.get("summary"), 1000
    )
    return {
        "objective": objective or _clip(fallback, 1000),
        "done_when": _list("done_when", "doneWhen", "acceptance", "definition_of_done"),
        "constraints": _list("constraints", "limits", "rules"),
        "out_of_scope": _list("out_of_scope", "outOfScope", "non_goals", "excluded"),
    }


# ------------------------------------------------------------------------- 里程碑


def normalize_milestones(raw: object) -> list[dict]:
    """归一化里程碑。

    接受 ``["阶段一", "阶段二"]`` 或 ``[{id, title, status, step_ids}]``。
    ``step_ids`` 允许留空——步骤自己也可以反过来带 ``milestone_id``，
    两边的指向在 :func:`link_milestones` 里合并。
    """
    milestones: list[dict] = []
    seen: set[str] = set()

    for index, item in enumerate(raw if isinstance(raw, list) else []):
        if isinstance(item, str):
            title, status, step_ids = item, "pending", []
        elif isinstance(item, dict):
            title = str(item.get("title") or item.get("name") or item.get("milestone") or "")
            status = str(item.get("status") or "pending")
            step_ids = [
                str(sid) for sid in (item.get("step_ids") or item.get("steps") or []) if sid
            ]
        else:
            continue

        title = _clip(title, MAX_TITLE_CHARS)
        if not title:
            continue

        milestone_id = _clip(item.get("id"), 40) if isinstance(item, dict) else ""
        milestone_id = milestone_id or _stable_id("m", f"{index}:{title}")
        if milestone_id in seen:
            milestone_id = f"{milestone_id}-{index}"
        seen.add(milestone_id)

        milestones.append(
            {
                "id": milestone_id,
                "title": title,
                "status": status if status in MILESTONE_STATUSES else "pending",
                "step_ids": step_ids,
            }
        )

    return milestones[:MAX_MILESTONES]


# --------------------------------------------------------------------------- 步骤


def normalize_steps(raw: object, previous: Iterable[dict] | None = None) -> list[dict]:
    """归一化步骤，并尽量沿用上一版的身份。

    ``previous`` 是现存的步骤列表。沿用 id 的目的不是漂亮，是**保住引用**：
    里程碑的 ``step_ids``、验收条件的 ``step_ids`` 都按 id 指过来，
    每次提交换一批随机 id 就等于把它们全指空。
    """
    old_items = [item for item in (previous or []) if isinstance(item, dict)]
    unclaimed = {id(item): item for item in old_items}
    by_text: dict[str, dict] = {}
    by_loose: dict[str, dict] = {}
    for item in old_items:
        text = _clip(item.get("step"), MAX_STEP_CHARS)
        if text:
            by_text.setdefault(text, item)
            by_loose.setdefault(_norm_text(text), item)

    def _claim(match: dict | None) -> dict | None:
        if match is None:
            return None
        return unclaimed.pop(id(match), None)

    steps: list[dict] = []
    used_ids: set[str] = set()

    for index, item in enumerate(raw if isinstance(raw, list) else []):
        if isinstance(item, str):
            text, status, milestone_id, evidence = item, "pending", "", ""
            explicit_id = ""
        elif isinstance(item, dict):
            text = str(item.get("step") or item.get("text") or item.get("title") or "")
            status = str(item.get("status") or "pending")
            milestone_id = _clip(item.get("milestone_id") or item.get("milestone"), 40)
            evidence = _clip(
                item.get("evidence") or item.get("result") or item.get("note"),
                MAX_EVIDENCE_CHARS,
            )
            explicit_id = _clip(item.get("id"), 40)
        else:
            continue

        text = _clip(text, MAX_STEP_CHARS)
        if not text:
            continue

        carried: dict | None = None
        # 优先：模型显式给了 id 且确实存在于上一版
        if explicit_id:
            for old in list(unclaimed.values()):
                if _clip(old.get("id"), 40) == explicit_id:
                    carried = _claim(old)
                    break
        # 其次：步骤文本完全一致
        if carried is None:
            carried = _claim(by_text.get(text))
        # 再次：只差标点/空格
        if carried is None:
            carried = _claim(by_loose.get(_norm_text(text)))

        step_id = explicit_id or (_clip(carried.get("id"), 40) if carried else "")
        step_id = step_id or _stable_id("s", f"{index}:{text}")
        if step_id in used_ids:
            step_id = _stable_id("s", f"{index}:{text}:{len(used_ids)}")
        used_ids.add(step_id)

        # 状态优先用这次提交的；没给就继承上一版（模型常常只改文本、不改状态）
        if status not in PLAN_STATUSES:
            status = str((carried or {}).get("status") or "pending")
            if status not in PLAN_STATUSES:
                status = "pending"

        steps.append(
            {
                "id": step_id,
                "step": text,
                "status": status,
                "milestone_id": milestone_id or _clip((carried or {}).get("milestone_id"), 40),
                "evidence": evidence or _clip((carried or {}).get("evidence"), MAX_EVIDENCE_CHARS),
            }
        )

    return steps[:MAX_STEPS]


def link_milestones(steps: list[dict], milestones: list[dict]) -> list[dict]:
    """把步骤与里程碑的双向指向合并成单向真源（``milestone_id`` 在步骤上）。

    模型可能只写一边：要么步骤带 ``milestone_id``，要么里程碑带 ``step_ids``。
    合并规则：

    - 步骤已经指向某个里程碑 → 以步骤为准，把它补进该里程碑的 ``step_ids``；
    - 步骤没指、但某个里程碑的 ``step_ids`` 点了它 → 反过来补上步骤的 ``milestone_id``；
    - 都没提 → 留在"未分组"。
    """
    known = {item["id"] for item in milestones}
    explicit: dict[str, set[str]] = {item["id"]: set(item.get("step_ids") or []) for item in milestones}
    step_ids = {step["id"] for step in steps}

    for step in steps:
        target = step.get("milestone_id") or ""
        if target and target in known:
            explicit[target].add(step["id"])
        elif target and target not in known:
            # 指向了不存在的里程碑：不猜、不造，按未分组处理。
            step["milestone_id"] = ""

    for milestone in milestones:
        claimed = {sid for sid in explicit[milestone["id"]] if sid in step_ids}
        milestone["step_ids"] = sorted(claimed)
        for step in steps:
            if step["id"] in claimed and not step.get("milestone_id"):
                step["milestone_id"] = milestone["id"]

    return milestones


def resolve_milestone_status(milestones: list[dict], steps: list[dict]) -> list[dict]:
    """里程碑状态由它的步骤推出来——不采信模型自己报的那个。

    理由：模型很容易把"这一步我打算做完"写成 completed。步骤状态至少还挂着
    证据和工具调用记录，里程碑只是它的父节点，没有独立的真相来源。
    空里程碑（一个步骤都没归进来）保留模型给的状态，没有别的依据可用。
    """
    by_id = {step["id"]: step for step in steps}
    for milestone in milestones:
        members = [by_id[sid] for sid in milestone.get("step_ids") or [] if sid in by_id]
        if not members:
            continue
        if all(step["status"] == "completed" for step in members):
            milestone["status"] = "completed"
        elif all(step["status"] == "pending" for step in members):
            milestone["status"] = "pending"
        else:
            milestone["status"] = "in_progress"
    return milestones


# ------------------------------------------------------------------------- 验收条件


def normalize_criteria(raw: object) -> list[dict]:
    """验收条件归一化。

    接受 ``["服务器能起来"]`` 或 ``[{criterion, milestone_id, step_ids}]``。
    把计划和验收打通的关键就是 ``step_ids``：一个验收点能指出"哪几步做完了它就算过"，
    验收循环据此可以精确到步骤，而不是只知道"整个任务失败了"。
    """
    criteria: list[dict] = []
    used: set[str] = set()

    for index, item in enumerate(raw if isinstance(raw, list) else []):
        if isinstance(item, str):
            text, milestone_id, step_ids = item, "", []
        elif isinstance(item, dict):
            text = str(
                item.get("criterion") or item.get("text") or item.get("item") or ""
            )
            milestone_id = _clip(item.get("milestone_id"), 40)
            step_ids = [str(sid) for sid in (item.get("step_ids") or []) if sid]
        else:
            continue

        text = _clip(text, MAX_STEP_CHARS)
        if not text:
            continue

        criterion_id = (
            _clip(item.get("id"), 40) if isinstance(item, dict) else ""
        ) or _stable_id("c", f"{index}:{text}")
        if criterion_id in used:
            criterion_id = f"{criterion_id}-{index}"
        used.add(criterion_id)

        criteria.append(
            {
                "id": criterion_id,
                "criterion": text,
                "milestone_id": milestone_id,
                "step_ids": step_ids,
            }
        )

    return criteria


def criteria_text(raw: object) -> list[str]:
    """只取验收条件的文字。给只认字符串的老调用方（会话主体、事件载荷）用。"""
    return [item["criterion"] for item in normalize_criteria(raw)]


# --------------------------------------------------------------------------- 进度


def plan_progress(steps: list[dict], milestones: list[dict]) -> dict:
    """汇总进度。前端拿到它就能画"完成 3/7"和进度条，不必自己数。"""
    total = len(steps)
    completed = sum(1 for step in steps if step["status"] == "completed")
    in_progress = sum(1 for step in steps if step["status"] == "in_progress")
    pending = total - completed - in_progress

    by_id = {step["id"]: step for step in steps}
    milestone_views = []
    for milestone in milestones:
        members = [by_id[sid] for sid in milestone.get("step_ids") or [] if sid in by_id]
        done = sum(1 for step in members if step["status"] == "completed")
        milestone_views.append(
            {
                "id": milestone["id"],
                "title": milestone["title"],
                "status": milestone.get("status") or "pending",
                "total": len(members),
                "completed": done,
                "percent": round(done / len(members) * 100) if members else 0,
            }
        )

    return {
        "total": total,
        "completed": completed,
        "in_progress": in_progress,
        "pending": pending,
        "percent": round(completed / total * 100) if total else 0,
        "milestones": milestone_views,
    }


# --------------------------------------------------------------------------- 渲染


def render_plan(steps: list[dict], milestones: list[dict] | None = None) -> str:
    """给模型看的纯文本计划。带里程碑分组，让它一眼看见自己在哪个阶段。"""
    milestones = milestones or []
    grouped: dict[str, list[dict]] = {item["id"]: [] for item in milestones}
    loose: list[dict] = []
    for step in steps:
        key = step.get("milestone_id") or ""
        if key and key in grouped:
            grouped[key].append(step)
        else:
            loose.append(step)

    lines: list[str] = []
    index = 0
    for milestone in milestones:
        members = grouped.get(milestone["id"]) or []
        if not members:
            continue
        mark = _MILESTONE_MARKS.get(milestone.get("status") or "pending", "○")
        done = sum(1 for step in members if step["status"] == "completed")
        lines.append(f"{mark} {milestone['title']}（{done}/{len(members)}）")
        for step in members:
            index += 1
            lines.append(f"  {_MARKS[step['status']]} {index}. {step['step']}")

    for step in loose:
        index += 1
        lines.append(f"{_MARKS[step['status']]} {index}. {step['step']}")

    return "\n".join(lines)


def ready_to_finish(steps: list[dict]) -> bool:
    """所有步骤都勾完了吗。收尾节点用它决定要不要再多转一轮。"""
    return bool(steps) and all(step["status"] == "completed" for step in steps)


def plan_fingerprint(steps: list[dict] | None) -> tuple:
    """计划的"有没有变过"指纹：只看步骤文本与状态。

    刻意不算 evidence：补一句"跑了什么命令"是记账，不是改计划。
    把它算进去会让每次收尾都认为计划变了，版本号随之变成"跑了几次"。
    """
    return tuple(
        (str(step.get("step") or ""), str(step.get("status") or ""))
        for step in (steps or [])
        if isinstance(step, dict)
    )
