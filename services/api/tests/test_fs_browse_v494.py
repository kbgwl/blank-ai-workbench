"""目录选择：后端只列目录，给浏览器环境一个可用的选择入口。"""

from __future__ import annotations

from pathlib import Path


async def test_lists_only_directories_sorted_and_keeps_parent(client, tmp_path: Path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "Beta").mkdir()
    (tmp_path / "note.txt").write_text("not a directory", encoding="utf-8")

    response = await client.get("/api/v1/fs/directories", params={"path": str(tmp_path)})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["path"] == str(tmp_path.resolve())
    assert [item["name"] for item in body["entries"]] == ["alpha", "Beta"]
    assert all(Path(item["path"]).is_dir() for item in body["entries"])
    assert body["parent"] == str(tmp_path.resolve().parent)
    assert body["roots"], "根目录清单不能为空"
    assert body["truncated"] is False


async def test_empty_path_returns_roots(client):
    response = await client.get("/api/v1/fs/directories")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["path"] == ""
    assert body["entries"] == []
    assert body["roots"], "至少要给一个可用的根（盘符或 /）"


async def test_missing_file_and_relative_paths_are_rejected(client, tmp_path: Path):
    missing = await client.get(
        "/api/v1/fs/directories", params={"path": str(tmp_path / "nope")}
    )
    assert missing.status_code == 404

    file_path = tmp_path / "note.txt"
    file_path.write_text("x", encoding="utf-8")
    not_dir = await client.get("/api/v1/fs/directories", params={"path": str(file_path)})
    assert not_dir.status_code == 422

    relative = await client.get("/api/v1/fs/directories", params={"path": "relative/dir"})
    assert relative.status_code == 400
