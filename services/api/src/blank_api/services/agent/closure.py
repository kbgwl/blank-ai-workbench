"""任务闭合记录：改了什么、校验结论、谁验收——三样绑在同一个 revision 上。

为什么单独一个模块：现状里这三样散在三个地方——改动在交付文本（甚至只在模型总结
里），校验在读库的 ``validation`` 字段，验收结论是模型自己在 JSON 里填的 ``covered``。
三者没有共同锚点，于是"改完并且被验收"这件事在证据上从未发生过：能查到"改过"，
查不到"这一版被谁按什么结论收下"。

这里的做法只有一条硬约束：**记录里的 revision 必须是当前 HEAD**。任务收尾时写一次
（``decision=pending``），验收决定另走 ``decide()``，并且拒绝在 revision 已经变了之后
把旧决定挂到新提交上——那正是"验收记录过期"最常见的形态。

为什么默认 ``pending``：模型说"已完成"不是验收，本地测试通过也不是验收（模型文档把
这两样都归到 Change Validation）。验收必须是项目真实边界上的一次决定，由人或项目
策略做出，所以这条记录先空着等人来签。
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# 闭合记录落在项目目录里（不进主仓库索引：``.blank/`` 是运行期目录）。
CLOSURE_DIR = ".blank/closures"
LEDGER_PATH = "acceptance/ledger.jsonl"
DECISION_PENDING = "pending"
DECISION_ACCEPTED = "accepted"
DECISION_REJECTED = "rejected"
_DECISIONS = (DECISION_ACCEPTED, DECISION_REJECTED)
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class TaskClosure:
    """一次任务的闭合事实。字段名与前端/台账保持同一套，不再各起别名。"""

    task_id: str
    revision: str
    branch: str
    root: str
    changed_paths: tuple[str, ...] = ()
    verify_command: str = ""
    verify_exit: int | None = None
    verify_covers: tuple[str, ...] = ()
    # 验收结论：默认 pending。模型的自评不进这个字段。
    decision: str = DECISION_PENDING
    decided_by: str = ""
    decided_at: str = ""
    note: str = ""
    at: str = ""

    @property
    def closed(self) -> bool:
        return self.decision in _DECISIONS and bool(self.revision)

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "revision": self.revision,
            "branch": self.branch,
            "root": self.root,
            "changed_paths": list(self.changed_paths),
            "verify": {
                "command": self.verify_command,
                "exit": self.verify_exit,
                "covers": list(self.verify_covers),
            },
            "decision": self.decision,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "note": self.note,
            "at": self.at,
            "closed": self.closed,
        }


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git(root: Path, *args: str) -> str:
    try:
        done = subprocess.run(  # noqa: S603 - 参数由本模块构造
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def revision_of(root: Path | str) -> str:
    """当前 HEAD。不在 git 仓库里就是空串——空 revision 的闭合记录不能算闭合。"""
    return _git(Path(root), "rev-parse", "HEAD")


def branch_of(root: Path | str) -> str:
    name = _git(Path(root), "rev-parse", "--abbrev-ref", "HEAD")
    return "" if name == "HEAD" else name


def build(
    *,
    task_id: str,
    root: Path | str,
    changed_paths: tuple[str, ...] | list[str] = (),
    verify_command: str = "",
    verify_exit: int | None = None,
    verify_covers: tuple[str, ...] | list[str] = (),
) -> TaskClosure:
    base = Path(root)
    return TaskClosure(
        task_id=str(task_id or ""),
        revision=revision_of(base),
        branch=branch_of(base),
        root=str(base),
        changed_paths=tuple(str(item) for item in changed_paths if str(item).strip()),
        verify_command=str(verify_command or "").strip(),
        verify_exit=verify_exit if isinstance(verify_exit, int) else None,
        verify_covers=tuple(str(item) for item in verify_covers if str(item).strip()),
        at=_stamp(),
    )


def record_path(root: Path | str, task_id: str) -> Path:
    safe = _SAFE_ID.sub("_", str(task_id or ""))[:80] or "task"
    return Path(root) / CLOSURE_DIR / f"{safe}.json"


def write(root: Path | str, record: TaskClosure) -> Path:
    """落盘。写不进去就抛——闭合记录悄悄丢掉，等于假装闭过了。"""
    target = record_path(root, record.task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def read(root: Path | str, task_id: str) -> dict:
    target = record_path(root, task_id)
    if not target.is_file():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def decide(
    root: Path | str,
    task_id: str,
    *,
    decision: str,
    decided_by: str,
    note: str = "",
) -> dict:
    """记一次验收决定，绑当前 revision。revision 变了就拒绝。

    拒绝而不是"顺手更新到新版本"：验收针对的是一份具体的结果。把旧决定挂到
    新提交上，台账会显示"这一版被验收过"，而实际被看的是上一版。
    """
    decision = str(decision or "").strip().lower()
    if decision not in _DECISIONS:
        raise ValueError("decision 只能是 accepted 或 rejected")
    who = str(decided_by or "").strip()
    if not who:
        raise ValueError("必须写清谁做的决定（decided_by）；匿名验收等于没有验收")
    payload = read(root, task_id)
    if not payload:
        raise ValueError(f"任务 {task_id} 没有闭合记录，先让任务跑完收尾再验收")
    recorded = str(payload.get("revision") or "")
    current = revision_of(root)
    if not current:
        raise ValueError("当前工作区不是 git 仓库（没有 revision），无法绑定验收决定")
    if recorded and recorded != current:
        raise ValueError(
            f"revision 已经变了（记录 {recorded[:8]}，当前 {current[:8]}）："
            "验收决定只能绑它看过的那一版，请重新确认当前结果"
        )
    payload["decision"] = decision
    payload["decided_by"] = who
    payload["decided_at"] = _stamp()
    payload["note"] = str(note or "").strip()
    payload["revision"] = current
    payload["closed"] = True
    target = record_path(root, task_id)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _append_ledger(root, payload)
    return payload


def _append_ledger(root: Path | str, payload: dict) -> Path | None:
    """台账追加一行：只增不改，跟 CI 的状态记录同一个用法。"""
    path = Path(root) / LEDGER_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "task_id": payload.get("task_id"),
            "revision": payload.get("revision"),
            "branch": payload.get("branch"),
            "decision": payload.get("decision"),
            "decided_by": payload.get("decided_by"),
            "decided_at": payload.get("decided_at"),
            "changed_files": len(payload.get("changed_paths") or []),
            "verify": (payload.get("verify") or {}).get("command", ""),
            "verify_exit": (payload.get("verify") or {}).get("exit"),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        return None
    return path


def ledger(root: Path | str) -> list[dict]:
    path = Path(root) / LEDGER_PATH
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            rows.append(json.loads(text))
        except ValueError:
            continue
    return rows


__all__ = [
    "CLOSURE_DIR",
    "DECISION_ACCEPTED",
    "DECISION_PENDING",
    "DECISION_REJECTED",
    "LEDGER_PATH",
    "TaskClosure",
    "branch_of",
    "build",
    "decide",
    "ledger",
    "read",
    "record_path",
    "revision_of",
    "write",
]
