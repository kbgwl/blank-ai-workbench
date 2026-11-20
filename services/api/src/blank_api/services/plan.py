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

# "cancelled" 是给「这一步不再做了」准备的：用户中途改主意，或者模型发现这条路走不通。
# 没有它，模型只有两条烂路——留成 pending（永远显示"还没做完"，像卡住了），
# 或者标 completed（把没做的事写成做完了）。两者都会让进度条说谎。
#
# "failed" 同理，而且是更严重的那种：一个包试过了、没过，模型只能留在 in_progress
# （看起来还在做，实际已经放弃）或者谎标 completed。计划里看不出哪一步是坏的，
# 用户看到的进度条就和实际脱节。失败必须能写下来，才谈得上"失败可定位"。
PLAN_STATUSES = ("pending", "in_progress", "completed", "cancelled", "failed")
MILESTONE_STATUSES = ("pending", "in_progress", "completed", "failed")
MAX_STEPS = 12
MAX_MILESTONES = 6
MAX_STEP_CHARS = 200
MAX_TITLE_CHARS = 120
MAX_EVIDENCE_CHARS = 400

_MARKS = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]", "cancelled": "[-]", "failed": "[!]"}
_MILESTONE_MARKS = {"completed": "●", "in_progress": "◐", "pending": "○", "failed": "▲"}
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


# 可执行的判据类型。只认这四种，全部能由 verifier 只读事实核对：
#   path_exists   文件/目录在不在
#   file_contains 文件里有没有那段文字
#   command       本轮有没有跑过这条命令且退出码为 0
#   page          本轮 browser_check 打开过这张页面，且某个选择器命中了够个数
# 刻意不收 http / db 这类：verifier 是纯函数，不能发请求、不能连库。
# page 同理——它只读 browser_check 已经留下的回执，不自己起浏览器。
#
# 加 page 是因为前三类都验不出「用户打开时看见什么」：一个三列空框的看板
# 能过 path_exists，能过 command（构建成功），却过不了「首屏 .task-item ≥ 3」。
# 没有这一维，判据写得再全，最优解仍然是交一个空壳。
CHECK_TYPES = ("path_exists", "file_contains", "command", "page")


def _clean_check(check: object) -> dict:
    """把一条 check 洗成已知形状；认不出就返回空字典（= 这条没有可执行判据）。"""
    if not isinstance(check, dict):
        return {}
    kind = str(check.get("type") or "").strip().lower()
    if kind not in CHECK_TYPES:
        return {}
    if kind == "command":
        cmd = str(check.get("cmd") or check.get("command") or "").strip()
        return {"type": "command", "cmd": cmd[:MAX_STEP_CHARS]} if cmd else {}
    if kind == "page":
        # 形状：哪张页面、看哪个选择器、至少几个。
        # 选择器与个数合写在 text 里（`".task-item:3"`），和 browser_check 的 expect 同一写法——
        # 工具手册按字符算预算且每步重发，不为这一种判据新增字段名。
        path = str(check.get("path") or check.get("url") or "").strip()
        spec = str(check.get("text") or check.get("selector") or "").strip()
        if not path or not spec:
            return {}
        selector, _, count = spec.rpartition(":")
        if not selector or not count.isdigit():
            selector, minimum = spec, 1
        else:
            minimum = int(count)
        return {
            "type": "page",
            "path": path[:MAX_STEP_CHARS],
            "selector": selector[:MAX_STEP_CHARS],
            "min": max(0, minimum),
        }
    path = str(check.get("path") or "").strip()
    if not path:
        return {}
    body = {"type": kind, "path": path[:MAX_STEP_CHARS]}
    if kind == "file_contains":
        text = str(check.get("text") or "").strip()
        if not text:
            return {}
        body["text"] = text[:MAX_STEP_CHARS]
    return body


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

    def _done(*keys: str) -> tuple[list[str], list[dict]]:
        """解析完成判据：每一项可以是字符串，也可以是 `{text, check}`。

        为什么要 check：`done_when` 现在只是给人看的一句话，verifier 只用它判
        "有没有交付文本"。写成可执行的检查（文件在不在、内容有没有、命令退出码）
        才能逐条回答"哪条判据没过"——这是 opencode / DSH 都没做到的一件事。

        兼容：字符串照旧进 done_when；带 check 的额外进 done_checks（同索引）。
        认不出的 check 类型**保留文本、丢掉 check**，不报错——一条判据写错不该
        让整个目标起不来。
        """
        for key in keys:
            value = raw.get(key)
            if not value:
                continue
            items = [value] if isinstance(value, str) else value
            if not isinstance(items, list):
                continue
            texts: list[str] = []
            checks: list[dict] = []
            for item in items[:10]:
                if isinstance(item, dict):
                    text = str(item.get("text") or item.get("criterion") or "").strip()
                    check = item.get("check")
                    if not text and not check:
                        continue
                    texts.append(_clip(text, MAX_STEP_CHARS))
                    checks.append(_clean_check(check))
                else:
                    texts.append(_clip(item, MAX_STEP_CHARS))
                    checks.append({})
            return [t for t in texts if t], checks
        return [], []

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
    done_when, done_checks = _done("done_when", "doneWhen", "acceptance", "definition_of_done")
    return {
        "objective": objective or _clip(fallback, 1000),
        "done_when": done_when,
        "done_checks": done_checks,
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
            explicit_id, output, check = "", "", {}
        elif isinstance(item, dict):
            text = str(item.get("step") or item.get("text") or item.get("title") or "")
            status = str(item.get("status") or "pending")
            milestone_id = _clip(item.get("milestone_id") or item.get("milestone"), 40)
            evidence = _clip(
                item.get("evidence") or item.get("result") or item.get("note"),
                MAX_EVIDENCE_CHARS,
            )
            explicit_id = _clip(item.get("id"), 40)
            # 工作包的产出物与判据。没有它，一条「实现整个功能」和一条
            # 「写 index.html 的三列骨架，check = 文件里含 task-list」在数据上长得一样。
            output = _clip(item.get("output") or item.get("deliverable"), MAX_STEP_CHARS)
            check = _clean_check(item.get("check"))
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

        # 产出物与判据没给就继承上一版：模型常常只改措辞并重发整份列表，
        # 每改一次就把判据清空，等于逼它反复重写同一件事。
        carried_check = (carried or {}).get("check")
        steps.append(
            {
                "id": step_id,
                "step": text,
                "status": status,
                "milestone_id": milestone_id or _clip((carried or {}).get("milestone_id"), 40),
                "evidence": evidence or _clip((carried or {}).get("evidence"), MAX_EVIDENCE_CHARS),
                "output": output or _clip((carried or {}).get("output"), MAX_STEP_CHARS),
                "check": check or (carried_check if isinstance(carried_check, dict) else {}),
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
        # 失败优先：一个阶段里有一步坏了，这个阶段整体就不能报"进行中"或"已完成"。
        # 顺序上放在 completed 之后、pending 之前——全做完且其中一步失败，仍然算失败。
        if any(step["status"] == "failed" for step in members):
            milestone["status"] = "failed"
        elif all(step["status"] == "completed" for step in members):
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
    cancelled = sum(1 for step in steps if step["status"] == "cancelled")
    failed = sum(1 for step in steps if step["status"] == "failed")
    # 已取消的、失败的不算待办，也不算完成：它们都是"不在这条路上"。
    # 混进 pending 会让进度条永远差一截；混进 completed 就是撒谎。
    pending = total - completed - in_progress - cancelled - failed

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
        # 单独一档给前端画"3 步已取消"，而不是让它去减 total - 其它字段。
        "cancelled": cancelled,
        # 同理：失败要能被数出来，否则前端只能靠"有 evidence 提到失败"去猜。
        "failed": failed,
        "percent": round(completed / total * 100) if total else 0,
        "milestones": milestone_views,
    }


# --------------------------------------------------------------------------- 渲染


def _check_key(check: dict) -> str:
    """一条判据验的是哪个对象。用来发现"两个包验同一件事"。"""
    kind = str(check.get("type") or "")
    if kind == "command":
        return "cmd:" + " ".join(str(check.get("cmd") or "").split())
    if kind == "page":
        return f"page:{check.get('path')}#{check.get('selector')}"
    return f"{kind}:{check.get('path')}#{check.get('text') or ''}"


def work_package_problems(steps: list[dict]) -> list[dict]:
    """每个工作包能不能独立验证。返回问题列表，空列表=都合格。

    判据来自「最小可验证」，不是「最细」——「最细」没有上界，会一路拆到"写一个 div"。

    - WP_NO_OUTPUT  写不出"这一步交出什么"，说明它还是一团愿望；
    - WP_NO_CHECK   做完没法用事实回答"做成了吗"，只能靠模型自述；
    - WP_DUP_CHECK  两个包验同一个对象 → 它们其实是一个包，拆碎了；
    - WP_NO_STEP    步骤文本为空（归一化一般会挡掉，这里兜底）。

    刻意不判的两件事：
    1. **不卡预计工时**。我们没有工时账本，编一个区间只会让模型去凑最安全的数字。
       粒度大小靠 WP_DUP_CHECK 和 MAX_STEPS 约束，比伪造估算诚实。
    2. **不判"产出物是否单一"**。那要靠"和/以及"这类词做文本规则，误报率高，
       第一版不做——宁可漏判，不要逼模型写病句。
    """
    problems: list[dict] = []
    seen: dict[str, int] = {}
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        position = index + 1
        sid = str(step.get("id") or f"#{position}")
        label = _clip(step.get("step"), 40) or "（无文本）"
        if not str(step.get("step") or "").strip():
            problems.append({"id": sid, "code": "WP_NO_STEP", "message": f"第 {position} 步没有内容"})
            continue
        if not str(step.get("output") or "").strip():
            problems.append(
                {
                    "id": sid,
                    "code": "WP_NO_OUTPUT",
                    "message": f"第 {position} 步「{label}」没写产出物（输出什么文件/命令结果/结论）",
                }
            )
        check = step.get("check") if isinstance(step.get("check"), dict) else {}
        if not check:
            problems.append(
                {
                    "id": sid,
                    "code": "WP_NO_CHECK",
                    "message": f"第 {position} 步「{label}」没有可执行的验收判据",
                }
            )
            continue
        key = _check_key(check)
        if key and key in seen:
            problems.append(
                {
                    "id": sid,
                    "code": "WP_DUP_CHECK",
                    "message": f"第 {position} 步与第 {seen[key]} 步验的是同一个对象，应该合并成一步",
                }
            )
        elif key:
            seen[key] = position
    return problems


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


def rewind_to_step(steps: list[dict], step_id: str) -> tuple[list[dict], int]:
    """把计划打回到指定步骤：该步及其之后全部重开成 pending。

    返回 ``(新步骤列表, 被打回的下标)``；找不到该 step_id 时下标为 -1、列表原样返回。

    为什么不是"只把那一步改回 pending"：后续步骤建立在那一步的产物上，只改一步会
    留下"第 3 步还是 completed、但它依赖的第 2 步被推翻了"这种自相矛盾的计划，
    进度条会继续显示已完成。打回是从那里重做，不是补一个勾。

    清掉被打回步骤的 evidence：那条证据对应的是旧产物，留着会让下一步误以为
    已经有了可用的输入。
    """
    target = str(step_id or "").strip()
    if not target:
        return list(steps or []), -1
    items = [dict(step) for step in (steps or []) if isinstance(step, dict)]
    index = next((i for i, step in enumerate(items) if str(step.get("id") or "") == target), -1)
    if index < 0:
        return items, -1
    for step in items[index:]:
        step["status"] = "pending"
        step["evidence"] = ""
    return items, index

