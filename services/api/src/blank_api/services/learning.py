"""自学习快照：经验（学世界）+ 使用者模型（学人）。

两个库本来就有完整的写入、召回、回写与退役逻辑，但只有 CLI 能看。
用户界面上看不到"它到底学了什么、为什么想起这条、要不要撤掉"，于是自学习
只能靠信任。这里只做**展示与可撤销**，不改变自动生效策略：

* 经验仍按路径/报错签名自动召回 1 条；
* 偏好仍按作用域注入，且必须有用户原话才准入；
* 技能提案仍要人采用，不在这里自动提升。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from blank_api.services.agent.experience import (
    ExperienceStore,
    is_expired as experience_expired,
    lint as experience_lint,
)
from blank_api.services.agent.persona import (
    PersonaStore,
    is_expired as persona_expired,
    lint as persona_lint,
)
from blank_api.services.workspace import mask_path


def _stats(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _empty_bucket(path: str = "") -> dict:
    return {"path": path, "entries": [], "stats": {}, "lint": []}


def empty_snapshot() -> dict:
    return {
        "workspace": "",
        "experience": _empty_bucket(),
        "persona": {**_empty_bucket(), "global_path": "", "origins": {}},
    }


def snapshot(workspace: str | Path) -> dict:
    """当前工作区学到的全部内容，路径一律脱敏。"""
    now = datetime.now(timezone.utc)
    experience_store = ExperienceStore()
    experiences = experience_store.load(workspace)
    persona_store = PersonaStore()
    preferences = persona_store.load_all(workspace)
    return {
        "workspace": mask_path(str(workspace)),
        "experience": {
            "path": mask_path(str(experience_store.path_for(workspace))),
            "entries": [
                {
                    **item.as_dict(),
                    "fingerprint": item.fingerprint,
                    "expired": experience_expired(item, now),
                }
                for item in experiences
            ],
            "stats": _stats(item.status for item in experiences),
            "lint": experience_lint(experiences),
        },
        "persona": {
            "path": mask_path(str(persona_store.path_for(workspace))),
            "global_path": mask_path(str(persona_store.global_path())),
            "entries": [
                {
                    **item.as_dict(),
                    "fingerprint": item.fingerprint,
                    "label": item.label,
                    "expired": persona_expired(item, now),
                }
                for item in preferences
            ],
            "stats": _stats(item.status for item in preferences),
            "origins": _stats(item.origin for item in preferences),
            "lint": persona_lint(preferences),
        },
    }


def retire_experience(workspace: str | Path, fingerprint: str) -> list[str]:
    return ExperienceStore().retire(workspace, experience_id=fingerprint)


def retire_preference(workspace: str | Path, fingerprint: str) -> bool:
    return PersonaStore().retire(workspace, fingerprint)


def promote_preference(workspace: str | Path, fingerprint: str) -> bool:
    return PersonaStore().promote(workspace, fingerprint)
