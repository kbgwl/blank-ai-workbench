"""Durable, redacted tool artifacts kept outside the model context."""

from __future__ import annotations

import json
from pathlib import Path


DEFAULT_ARTIFACT_ROOT = Path("data/artifacts")


class ArtifactStore:
    def __init__(self, root: Path | str = DEFAULT_ARTIFACT_ROOT) -> None:
        self.root = Path(root)

    def save(self, task_id: str, artifact_id: str, payload: dict) -> str:
        target = self.root / task_id / f"{artifact_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return target.as_posix()

    def load(self, task_id: str, artifact_id: str) -> dict:
        target = self.root / task_id / f"{artifact_id}.json"
        if not target.exists():
            raise FileNotFoundError(artifact_id)
        return json.loads(target.read_text(encoding="utf-8"))
