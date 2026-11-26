"""自学习可见闭环：快照、退役、提升。

内核早就写入了经验与偏好，但只有 CLI 能看。这里钉住三件事：
快照能读出来、退役写到正确的一层、冲突中的偏好不能被提升。
"""

from __future__ import annotations

from pathlib import Path

from blank_api.services import learning
from blank_api.services.agent.experience import Experience, ExperienceStore
from blank_api.services.agent.persona import PersonaStore, Preference


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace


def test_snapshot_exposes_experience_and_preference(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BLANK_EXPERIENCE_DIR", str(tmp_path / "exp"))
    monkeypatch.setenv("BLANK_PERSONA_DIR", str(tmp_path / "persona"))
    workspace = _workspace(tmp_path)

    ExperienceStore().record(
        workspace,
        Experience(
            statement="npm test 会超时",
            kind="pitfall",
            trigger_paths=["deliverables/**"],
            evidence=[{"task": "t1", "signal": "timeout"}],
            confidence=0.6,
        ),
    )
    PersonaStore().record(
        workspace,
        Preference(
            rule="先跑测试再改代码",
            dimension="process",
            origin="harvested",
            status="candidate",
            quotes=[{"text": "先跑测试"}],
            scope_ref=str(workspace),
        ),
    )

    snap = learning.snapshot(workspace)

    assert snap["workspace"] == "<workspace:ws>"
    assert snap["experience"]["stats"] == {"candidate": 1}
    assert snap["experience"]["entries"][0]["statement"] == "npm test 会超时"
    assert snap["experience"]["entries"][0]["fingerprint"]
    assert snap["persona"]["stats"] == {"candidate": 1}
    assert snap["persona"]["entries"][0]["rule"] == "先跑测试再改代码"
    assert snap["persona"]["entries"][0]["origin"] == "harvested"


def test_retire_and_promote_write_to_the_same_layer(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BLANK_EXPERIENCE_DIR", str(tmp_path / "exp"))
    monkeypatch.setenv("BLANK_PERSONA_DIR", str(tmp_path / "persona"))
    workspace = _workspace(tmp_path)
    ExperienceStore().record(workspace, Experience(statement="旧坑", trigger_paths=["src/**"]))
    PersonaStore().record(
        workspace,
        Preference(
            rule="结论必须带命令",
            dimension="verification",
            origin="harvested",
            status="candidate",
            quotes=[{"text": "结论必须带命令"}],
            scope_ref=str(workspace),
        ),
    )

    snap = learning.snapshot(workspace)
    exp_id = snap["experience"]["entries"][0]["fingerprint"]
    pref_id = snap["persona"]["entries"][0]["fingerprint"]

    assert learning.retire_experience(workspace, exp_id) == ["旧坑"]
    assert learning.snapshot(workspace)["experience"]["stats"] == {"retired": 1}

    assert learning.promote_preference(workspace, pref_id) is True
    promoted = learning.snapshot(workspace)["persona"]
    assert promoted["stats"] == {"active": 1}
    assert promoted["entries"][0]["occurrences"] >= 2

    assert learning.retire_preference(workspace, pref_id) is True
    assert learning.snapshot(workspace)["persona"]["stats"] == {"retired": 1}


def test_global_preference_is_visible_and_retirable(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BLANK_EXPERIENCE_DIR", str(tmp_path / "exp"))
    monkeypatch.setenv("BLANK_PERSONA_DIR", str(tmp_path / "persona"))
    workspace = _workspace(tmp_path)
    store = PersonaStore()
    store.save_global(
        [
            Preference(
                rule="他从不用 tab 缩进",
                dimension="style",
                scope="global",
                status="active",
                occurrences=2,
                quotes=[{"text": "别用 tab"}],
            )
        ]
    )

    snap = learning.snapshot(workspace)
    assert [item["scope"] for item in snap["persona"]["entries"]] == ["global"]

    fingerprint = snap["persona"]["entries"][0]["fingerprint"]
    assert learning.retire_preference(workspace, fingerprint) is True
    assert store.load_global()[0].status == "retired"


def test_conflict_cannot_be_promoted(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BLANK_PERSONA_DIR", str(tmp_path / "persona"))
    workspace = _workspace(tmp_path)
    PersonaStore().record(
        workspace,
        Preference(
            rule="先实现再补测试",
            dimension="process",
            status="conflict",
            occurrences=2,
            quotes=[{"text": "先实现再补测试"}],
            scope_ref=str(workspace),
        ),
    )

    fingerprint = learning.snapshot(workspace)["persona"]["entries"][0]["fingerprint"]
    assert learning.promote_preference(workspace, fingerprint) is False


def test_empty_snapshot_has_no_fake_entries():
    snap = learning.empty_snapshot()
    assert snap["workspace"] == ""
    assert snap["experience"]["entries"] == []
    assert snap["persona"]["entries"] == []
    assert snap["persona"]["global_path"] == ""
