import shutil
from pathlib import Path
from uuid import uuid4

from blank_api.services.agent.artifacts import ArtifactStore


def test_artifact_store_round_trips_complete_output():
    root = Path("data") / f"_artifact_test_{uuid4().hex}"
    try:
        store = ArtifactStore(root)
        path = store.save("task-1", "call-1", {"output": "x" * 10000})
        assert path.endswith("task-1/call-1.json")
        assert store.load("task-1", "call-1")["output"] == "x" * 10000
    finally:
        shutil.rmtree(root, ignore_errors=True)
