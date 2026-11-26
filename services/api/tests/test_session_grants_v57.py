"""会话级授权撤销：能列出、能按类撤销、能全部撤销，坏文件 fail-closed。"""

from __future__ import annotations

from pathlib import Path

from blank_api.services import session_grants


def _use_file(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "session-grants.json"
    monkeypatch.setenv("BLANK_SESSION_GRANTS_FILE", str(target))
    return target


def test_list_revoke_one_and_all(tmp_path: Path, monkeypatch):
    _use_file(tmp_path, monkeypatch)

    assert session_grants.grant("t1", "git_push", "task-1") is True
    assert session_grants.grant("t1", "delete_file", "task-1") is True
    assert session_grants.grant("t2", "deploy", "task-2") is True

    assert {item["category"] for item in session_grants.list_grants("t1")} == {
        "git_push",
        "delete_file",
    }

    assert session_grants.revoke("t1", "git_push") is True
    assert session_grants.revoke("t1", "git_push") is False
    assert [item["category"] for item in session_grants.list_grants("t1")] == ["delete_file"]

    assert session_grants.revoke_all("t1") == 1
    assert session_grants.list_grants("t1") == []
    assert [item["category"] for item in session_grants.list_grants("t2")] == ["deploy"]


def test_unknown_category_is_not_granted(tmp_path: Path, monkeypatch):
    _use_file(tmp_path, monkeypatch)
    assert session_grants.grant("t1", "unknown_category", "task") is False
    assert session_grants.list_grants("t1") == []


def test_broken_file_fails_closed(tmp_path: Path, monkeypatch):
    target = _use_file(tmp_path, monkeypatch)
    target.write_text("{not json", encoding="utf-8")

    assert session_grants.list_grants("t1") == []
    assert session_grants.revoke("t1", "git_push") is False
    assert session_grants.revoke_all("t1") == 0


def test_empty_thread_has_no_grants():
    assert session_grants.list_grants("") == []
    assert session_grants.revoke("", "git_push") is False
    assert session_grants.revoke_all("") == 0
