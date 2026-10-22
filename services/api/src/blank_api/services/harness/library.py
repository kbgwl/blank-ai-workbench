"""Domain experience library: what earlier tasks in the same domain adopted.

The rule that makes this useful is *retrieval by construction*: entries are
stored under `<root>/<domain>/`, so a task can only ever see harnesses from its
own domain. Nothing is summarised — the full tree is kept, because a lesson
without the file that embodied it is just an opinion.

Layout::

    <root>/<domain>/INDEX.md
    <root>/<domain>/harnesses/<task>/harness/<rel>   full adopted tree
    <root>/<domain>/harnesses/<task>/EXPERIENCE.md   why + transferable lesson

An entry is only written when it carries a complete lesson (cue, lesson and
evidence all non-empty). Storing a winner without the reason it won would let
the next task copy the shape and miss the point.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from blank_api.services.harness import spec

INDEX_NAME = "INDEX.md"
EXPERIENCE_NAME = "EXPERIENCE.md"
HARNESS_NAME = "harness"
FAILURES_NAME = "failures"
FEEDBACK_NAME = "FEEDBACK.json"
DEFAULT_CONTEXT_LIMIT = 12_000

_UNSAFE = re.compile(r'[<>:"|?*\x00-\x1f]')


def slug(task_id: str) -> str:
    """A task id that is safe as one directory name on every platform."""
    text = str(task_id or "").strip().replace("\\", "/").replace("/", "__")
    text = _UNSAFE.sub("_", text).strip(" ._")
    return text[:120] or "task"


def domain_dir(domain: str) -> str:
    """One directory name for a domain, never a path.

    Separators collapse to ``__``; characters Windows rejects become ``_``.
    Anything empty falls back to ``unknown`` so an unlabelled task still lands
    somewhere deterministic.
    """
    text = re.sub(r"\s*[/\\]\s*", "__", str(domain or "")).strip()
    text = _UNSAFE.sub("_", text).strip(" .")
    return text or "unknown"


def root_for(
    base: str | Path, *, benchmark: str, k: int, variant: str = "", tag: str = ""
) -> Path:
    """Per-run library root; an explicit ``tag`` shares one library across runs."""
    if tag:
        return Path(base) / benchmark / slug(tag)
    parts = [f"k{int(k)}"] + ([domain_dir(variant)] if variant else [])
    return Path(base) / benchmark / "-".join(parts)


def _count(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class Experience:
    cue: str
    lesson: str
    evidence: str
    confidence: str = ""
    edits: str = ""
    # ACE 式条目元数据：适用范围、可用性计数、最后一次被验证的时间。
    scope: str = ""
    helpful: int = 0
    harmful: int = 0
    last_verified: str = ""

    def __post_init__(self) -> None:
        for name in ("cue", "lesson", "evidence", "confidence", "edits", "scope", "last_verified"):
            object.__setattr__(self, name, str(getattr(self, name) or "").strip())
        object.__setattr__(self, "helpful", _count(self.helpful))
        object.__setattr__(self, "harmful", _count(self.harmful))

    @property
    def complete(self) -> bool:
        return all((self.cue, self.lesson, self.evidence))

    @property
    def usefulness(self) -> int:
        return self.helpful - self.harmful

    def as_dict(self) -> dict:
        return {
            "cue": self.cue,
            "lesson": self.lesson,
            "evidence": self.evidence,
            "confidence": self.confidence,
            "edits": self.edits,
            "scope": self.scope,
            "helpful": self.helpful,
            "harmful": self.harmful,
            "last_verified": self.last_verified,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object] | None) -> "Experience":
        data = dict(payload or {})
        return cls(
            cue=str(data.get("cue") or ""),
            lesson=str(data.get("lesson") or ""),
            evidence=str(data.get("evidence") or ""),
            confidence=str(data.get("confidence") or ""),
            edits=str(data.get("edits") or ""),
            scope=str(data.get("scope") or ""),
            helpful=_count(data.get("helpful")),
            harmful=_count(data.get("harmful")),
            last_verified=str(data.get("last_verified") or ""),
        )


def has_experience(experience: Experience | Mapping[str, object] | None) -> bool:
    if isinstance(experience, Experience):
        return experience.complete
    return Experience.from_dict(experience).complete


@dataclass
class Library:
    """One (domain) library: read before a task, appended after it is adopted."""

    root: str | Path
    domain: str

    @property
    def dir(self) -> Path:
        return Path(self.root) / domain_dir(self.domain)

    @property
    def index(self) -> Path:
        return self.dir / INDEX_NAME

    def entry_dir(self, task_id: str) -> Path:
        return self.dir / "harnesses" / slug(task_id)

    def failure_dir(self, task_id: str) -> Path:
        return self.dir / FAILURES_NAME / slug(task_id)

    def exists(self) -> bool:
        return self.index.is_file()

    def entries(self) -> list[str]:
        harnesses = self.dir / "harnesses"
        if not harnesses.is_dir():
            return []
        return sorted(path.name for path in harnesses.iterdir() if path.is_dir())

    def failure_entries(self) -> list[str]:
        failures = self.dir / FAILURES_NAME
        if not failures.is_dir():
            return []
        return sorted(path.name for path in failures.iterdir() if path.is_dir())

    def tree_of(self, task_id: str) -> dict[str, str]:
        return spec.read_tree(self.entry_dir(task_id) / HARNESS_NAME)

    def forget(self, task_ids: Sequence[str]) -> None:
        """Drop the entries for these tasks; unknown tasks are ignored."""
        removed = False
        for task_id in task_ids:
            target = self.entry_dir(task_id)
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
                removed = True
        if removed:
            self.rebuild_index()

    def retain(self, task_ids: Sequence[str]) -> None:
        """Keep only the given entries (used to replay one stream's prefix)."""
        keep = {slug(task_id) for task_id in task_ids}
        self.forget([entry for entry in self.entries() if entry not in keep])

    def add(
        self,
        task_id: str,
        tree: Mapping[str, str],
        *,
        experience: Experience | Mapping[str, object] | None,
        why: str = "",
        label: str = "",
        run_id: str = "",
        replace: bool = True,
    ) -> Path | None:
        """Store one adopted harness plus why it was adopted.

        Returns ``None`` when the lesson is incomplete, so a caller that only
        has a score can still call this unconditionally. An invalid or leaky
        tree raises: that is a bug in the optimizer, not a missing lesson.
        """
        entry = experience if isinstance(experience, Experience) else Experience.from_dict(experience)
        if not entry.complete:
            return None
        spec.validate(tree)

        target = self.entry_dir(task_id)
        if target.exists() and not replace:
            return None
        if replace:
            shutil.rmtree(target, ignore_errors=True)

        harness_dir = target / HARNESS_NAME
        spec.write_tree(harness_dir, tree)

        lines = [
            f"# {task_id}",
            "",
            f"- adopted: `{label or task_id}`" + (f"  (run `{run_id}`)" if run_id else ""),
            f"- components: {_inventory(tree)}",
            "",
        ]
        if why.strip():
            lines += ["## Why this was chosen", why.strip(), ""]
        if entry.edits:
            lines += ["## What each edit did", entry.edits, ""]
        lines += [
            "## Transferable lesson",
            f"- **cue**: {entry.cue}",
            f"- **lesson**: {entry.lesson}",
            f"- **evidence**: {entry.evidence}",
        ]
        if entry.confidence:
            lines.append(f"- **confidence**: {entry.confidence}")
        if entry.scope:
            lines.append(f"- **scope**: {entry.scope}")
        if entry.helpful:
            lines.append(f"- **helpful**: {entry.helpful}")
        if entry.harmful:
            lines.append(f"- **harmful**: {entry.harmful}")
        if entry.last_verified:
            lines.append(f"- **last_verified**: {entry.last_verified}")
        lines.append("")
        (target / EXPERIENCE_NAME).write_text("\n".join(lines), encoding="utf-8")
        self.rebuild_index()
        return target

    def add_failure(
        self,
        task_id: str,
        *,
        experience: Experience | Mapping[str, object] | None,
        label: str = "",
        run_id: str = "",
        replace: bool = True,
    ) -> Path | None:
        """Record a measured failure as a negative example.

        Failures live outside ``harnesses/`` on purpose: they are not adopted
        trees, and a proposer must not mistake them for something that worked.
        """
        entry = experience if isinstance(experience, Experience) else Experience.from_dict(experience)
        if not entry.complete:
            return None

        target = self.failure_dir(task_id)
        if target.exists() and not replace:
            return None
        if replace:
            shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)

        lines = [
            f"# {task_id} — failure",
            "",
            f"- measured: `{label or task_id}`" + (f"  (run `{run_id}`)" if run_id else ""),
            "",
            "## Transferable lesson",
            f"- **cue**: {entry.cue}",
            f"- **lesson**: {entry.lesson}",
            f"- **evidence**: {entry.evidence}",
        ]
        if entry.confidence:
            lines.append(f"- **confidence**: {entry.confidence}")
        if entry.scope:
            lines.append(f"- **scope**: {entry.scope}")
        if entry.helpful:
            lines.append(f"- **helpful**: {entry.helpful}")
        if entry.harmful:
            lines.append(f"- **harmful**: {entry.harmful}")
        if entry.last_verified:
            lines.append(f"- **last_verified**: {entry.last_verified}")
        lines.append("")
        (target / EXPERIENCE_NAME).write_text("\n".join(lines), encoding="utf-8")
        self.rebuild_index()
        return target

    def record_feedback(
        self,
        task_id: str,
        *,
        helpful: int = 0,
        harmful: int = 0,
        verified_at: str = "",
    ) -> dict | None:
        """Attach observed usefulness to an adopted or failed entry.

        Counters are kept in a sidecar so the human-readable EXPERIENCE.md stays
        stable; retrieval ranks by ``helpful - harmful`` and never hides a
        negative record.
        """
        for directory in (self.entry_dir(task_id), self.failure_dir(task_id)):
            experience = directory / EXPERIENCE_NAME
            if not experience.is_file():
                continue
            path = directory / FEEDBACK_NAME
            data = _read_json(path)
            data.setdefault("helpful", _field_int(experience, "helpful"))
            data.setdefault("harmful", _field_int(experience, "harmful"))
            data["helpful"] = _count(data.get("helpful")) + max(0, int(helpful))
            data["harmful"] = _count(data.get("harmful")) + max(0, int(harmful))
            data["last_verified"] = verified_at or datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return data
        return None

    def rebuild_index(self) -> Path:
        """Regenerate INDEX.md from what is on disk."""
        names = self.entries()
        lines = [
            f"# Library — {self.domain}",
            "",
            "Each entry is an earlier task in THIS domain whose harness was chosen, stored "
            "with its full component tree and why it was picked. Read the entries that look "
            "relevant to the task in front of you; do not copy an entry wholesale.",
            "",
        ]
        if not names:
            lines.append("_(empty — this is the first task in this domain)_")
        for name in names:
            entry_dir = self.dir / "harnesses" / name
            tree = spec.read_tree(entry_dir / HARNESS_NAME)
            cue = _field(entry_dir / EXPERIENCE_NAME, "cue")
            lines += [
                f"## `harnesses/{name}/`",
                f"- harness: `harnesses/{name}/{HARNESS_NAME}/` — {_inventory(tree)}",
                f"- why:     `harnesses/{name}/{EXPERIENCE_NAME}`",
            ]
            if cue:
                lines.append(f"- cue:     {cue}")
            lines.append("")
        failures = self.failure_entries()
        if failures:
            lines += [
                "## Measured failures (negative examples, not adopted harnesses)",
                "",
            ]
            for name in failures:
                entry_dir = self.dir / FAILURES_NAME / name
                cue = _field(entry_dir / EXPERIENCE_NAME, "cue")
                lesson = _field(entry_dir / EXPERIENCE_NAME, "lesson")
                lines.append(f"- `{FAILURES_NAME}/{name}/{EXPERIENCE_NAME}`")
                if cue:
                    lines.append(f"  - cue: {cue}")
                if lesson:
                    lines.append(f"  - lesson: {lesson}")
            lines.append("")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index.write_text("\n".join(lines), encoding="utf-8")
        return self.index

    def context(self, *, limit: int = DEFAULT_CONTEXT_LIMIT) -> str:
        """The text handed to a proposer: index plus every lesson, bounded.

        Full trees are not inlined. The proposer can call :meth:`tree_of` for a
        specific entry when it wants to reuse a component.
        """
        if not self.exists():
            return ""
        parts = [self.index.read_text(encoding="utf-8", errors="replace").strip()]
        for name in self.entries():
            experience = self.dir / "harnesses" / name / EXPERIENCE_NAME
            if not experience.is_file():
                continue
            body = experience.read_text(encoding="utf-8", errors="replace").strip()
            if body:
                parts.append(body)
            if sum(len(part) for part in parts) > limit:
                break
        if len("\n\n".join(parts)) <= limit:
            for name in self.failure_entries():
                experience = self.dir / FAILURES_NAME / name / EXPERIENCE_NAME
                if not experience.is_file():
                    continue
                body = experience.read_text(encoding="utf-8", errors="replace").strip()
                if body:
                    parts.append(f"## 失败记录 `{FAILURES_NAME}/{name}/`\n\n{body}")
                if sum(len(part) for part in parts) > limit:
                    break
        text = "\n\n".join(parts)
        if len(text) > limit:
            text = text[:limit].rstrip() + "\n…（经验库超过预算，已截断）"
        return text

    def lessons(self) -> list[dict]:
        """Structured adopted lessons, oldest entry first, with feedback counters.

        The proposer uses these to seed long-term memory; keeping them as data
        (rather than re-parsing :meth:`context`) means a prompt change never
        silently drops the lesson.
        """
        out: list[dict] = []
        for name in self.entries():
            entry = _lesson(
                self.dir / "harnesses" / name,
                task=name,
                outcome="adopted",
            )
            if entry is not None:
                out.append(entry)
        return out

    def failure_lessons(self) -> list[dict]:
        """Measured failures, kept separate so they are never treated as rules."""
        out: list[dict] = []
        for name in self.failure_entries():
            entry = _lesson(
                self.dir / FAILURES_NAME / name,
                task=name,
                outcome="failed",
            )
            if entry is not None:
                out.append(entry)
        return out



def _lesson(directory: Path, *, task: str, outcome: str) -> dict | None:
    experience = directory / EXPERIENCE_NAME
    if not experience.is_file():
        return None
    feedback = _read_json(directory / FEEDBACK_NAME)
    return {
        "task": task,
        "cue": _field(experience, "cue"),
        "lesson": _field(experience, "lesson"),
        "evidence": _field(experience, "evidence"),
        "confidence": _field(experience, "confidence"),
        "scope": _field(experience, "scope"),
        "helpful": _count(feedback.get("helpful", _field_int(experience, "helpful"))),
        "harmful": _count(feedback.get("harmful", _field_int(experience, "harmful"))),
        "last_verified": str(
            feedback.get("last_verified") or _field(experience, "last_verified")
        ),
        "outcome": outcome,
    }


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _field_int(path: Path, name: str) -> int:
    return _count(_field(path, name))


def _inventory(tree: Mapping[str, str]) -> str:
    counts: dict[str, int] = {"skills": 0, "sub_agents": 0, "scripts": 0}
    always_on = 0
    for rel, body in tree.items():
        component = spec.component_for(rel)
        if component is None:
            continue
        if component.always_on:
            always_on += len(str(body))
        if component.key == "c5" and rel.endswith("AGENT.md"):
            counts["sub_agents"] += 1
        elif component.key == "c6" and rel.endswith("SKILL.md"):
            counts["skills"] += 1
        elif rel.startswith("scripts/") or "/scripts/" in rel:
            counts["scripts"] += 1
    bits = [f"{value} {name}" for name, value in counts.items() if value] or ["always-on edits only"]
    return f"{', '.join(bits)}; {always_on:,} always-on chars"


def _field(path: Path, name: str) -> str:
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(f"- **{name}**:"):
            return line.split(":", 1)[1].strip()
    return ""
