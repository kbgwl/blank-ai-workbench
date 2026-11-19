"""进展哨兵：这一步到底有没有带来新信息。

真机轨迹（任务 `36d86ed2`「3D 智慧工厂」）里，26 步中真正干活的只有前 6 步，后面：

- `js/gl.js` 被**成功写入 6 次**，每次内容都不同、都被覆盖；
- 同一条 `dir web\\src\\app & … three` 跑了 **9 次**（换个写法再试）；
- `js/scene.js` 上同一处 `/* __APPEND__ */` 锚点被改了 3 次；
- 期间**一次验证也没有**，最后一次 `run_tests` 还撞了 180 秒上限。

现有的空转断路器（`stall.py`）判的是"同一签名失败两次"，这四类它一条都拦不住：
参数每次都变，签名就每次都不同。但事实层面看得很清楚——**没有新证据**。

所以这里不判"意图"，只判"事实"，只用三种可核对的事件当证据：

* 文件被改（成功的写入类工具，取回执里的 path）；
* 验证通过（run_tests / browser_check 成功 —— 这是唯一的"结论型证据"）；
* 读到了没读过的内容（内容指纹第一次出现）。

连续 `IDLE_LIMIT` 步都没有其中任何一种，就提醒一次；同一份文件被重写 `REWRITE_LIMIT`
次而中间没有任何验证，也提醒一次。**只提醒，不拦**：拦错一次就是把省 token 做成了减功能，
而提醒最坏的情况只是多一句话。提醒走 `hook_context`，只进下一次模型调用，不写回历史。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

# 同一份文件被成功重写几次算"在打转"。3 次是"改-看-再改"的正常节奏之后那一格：
# 第 1 次是初版，第 2 次可能是修，第 3 次还没验证就说明靠猜。
REWRITE_LIMIT = 3
# 连续多少步没有新证据才提醒。4 步大约是"改一次 → 跑一次 → 再改一次"的完整来回，
# 超过它还在原地，基本可以确定它在换写法试同一件事。
IDLE_LIMIT = 4
# 两类提醒各自的最小间隔（步），避免每一步都念一遍。
NAG_COOLDOWN = 6

MUTATING_TOOLS = frozenset(
    {"apply_patch", "edit_file", "write_file", "notebook_edit", "create_file", "delete_file", "move_file"}
)
# 能给出"结论"的验证工具：它们成功 = 这一轮确实有东西被证明过。
VERIFY_TOOLS = frozenset({"run_tests", "browser_check"})
# 读到新内容也算证据，否则"读代码定位问题"这一段会被误判成空转。
READ_TOOLS = frozenset({"read_file", "search_code", "glob", "list_files", "git"})


def _digest(text: object) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest()[:16]


@dataclass
class ProgressSentinel:
    """一次任务里的进展账本。只活在内存，不跨轮也不落库。"""

    # 成功写入过的路径 → 次数
    writes: dict[str, int] = field(default_factory=dict)
    # 写过之后**还没有**被验证覆盖的路径
    unverified: set[str] = field(default_factory=set)
    # 最近一次"有新证据"的步号
    last_evidence: int = 0
    # 读过的内容指纹
    seen_reads: set[str] = field(default_factory=set)
    # 两类提醒各自上次提醒的步号
    nagged: dict[str, int] = field(default_factory=dict)

    # ── 记账 ────────────────────────────────────────────────────────────────

    def record(self, *, step: int, tool: str, payload: dict) -> None:
        """吃一条工具回执。失败的调用不算证据——它带来的信息是"这条路不通"，
        而"不通"已经由断路器负责，不该在这里重复计一次进展。"""
        if not isinstance(payload, dict) or not payload.get("ok"):
            return
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if tool in MUTATING_TOOLS:
            path = str(data.get("path") or "").strip()
            if path:
                # 写了但没验证：先记在"未验证"里，等一次成功验证来清账。
                self.writes[path] = self.writes.get(path, 0) + 1
                self.unverified.add(path)
            self.last_evidence = step
            return
        if tool in VERIFY_TOOLS:
            # 一次成功验证覆盖此前所有改动：它们"已经被证明过了"。
            self.unverified.clear()
            self.last_evidence = step
            return
        if tool in READ_TOOLS:
            fingerprint = _digest(payload.get("output"))
            if fingerprint not in self.seen_reads:
                self.seen_reads.add(fingerprint)
                self.last_evidence = step

    # ── 提醒 ────────────────────────────────────────────────────────────────

    def advise(self, step: int) -> list[str]:
        """要注入的提醒（可能为空）。每种提醒按 `NAG_COOLDOWN` 步节流。"""
        if step <= 0:
            return []
        notes: list[str] = []
        rewrite = self._rewrite_note(step)
        if rewrite:
            notes.append(rewrite)
        idle = self._idle_note(step)
        if idle:
            notes.append(idle)
        return notes

    def _rewrite_note(self, step: int) -> str:
        if not self.writes:
            return ""
        path, count = max(self.writes.items(), key=lambda item: item[1])
        if count < REWRITE_LIMIT or path not in self.unverified:
            return ""
        last = self.nagged.get("rewrite", -NAG_COOLDOWN)
        if step - last < NAG_COOLDOWN:
            return ""
        self.nagged["rewrite"] = step
        return (
            f"【系统：进展提醒】`{path}` 在本次任务里已经被成功写入 {count} 次，"
            "中间没有一次验证通过。重写同一份文件不会让它变对——"
            "先用 browser_check 看一眼页面（或 run_tests 拿到真实报错），再按结果改。"
        )

    def _idle_note(self, step: int) -> str:
        idle = step - max(self.last_evidence, 0)
        if idle < IDLE_LIMIT:
            return ""
        last = self.nagged.get("idle", -NAG_COOLDOWN)
        if step - last < NAG_COOLDOWN:
            return ""
        self.nagged["idle"] = step
        return (
            f"【系统：进展提醒】最近 {idle} 步没有产生任何新证据：没有文件被改动，"
            "也没有一次验证通过。先读一次相关文件、或跑一次能给出结论的验证，再决定下一步；"
            "不要继续换一种写法试同一件事。"
        )
