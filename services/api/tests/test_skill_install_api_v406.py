"""安装/卸载的接口层：接线错误（漏 import、路由顺序）只有走到 HTTP 才会现形。

单元测试只证明 service 写得对；上一版就是因为路由里少 import 了
`PROJECT_SKILLS_DIR`，service 测试全绿而接口 500。
"""

from __future__ import annotations

from pathlib import Path

import pytest

GOOD = """---
name: 接口安装
description: 验证安装接口
when_to_use: 只在接口测试里用
---
# 正文
照着做。
"""


@pytest.fixture
def skill_source(tmp_path: Path) -> Path:
    directory = tmp_path / "src" / "api-demo"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(GOOD, encoding="utf-8")
    return directory


@pytest.fixture(autouse=True)
def _user_root(monkeypatch, tmp_path):
    root = tmp_path / "user-skills"
    monkeypatch.setenv("BLANK_USER_SKILLS_DIR", str(root))
    return root


async def test_install_then_uninstall_round_trip(client, skill_source: Path, _user_root) -> None:
    created = await client.post(
        "/api/v1/skills/install",
        json={"source": str(skill_source), "scope": "user"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "api-demo"
    assert Path(body["path"]).is_dir()

    library = await client.get("/api/v1/skills/library")
    names = {item["name"]: item for item in library.json()["items"]}
    assert names["api-demo"]["source"] == "user"
    assert names["api-demo"]["installed_from"] == str(skill_source)

    removed = await client.post(
        "/api/v1/skills/uninstall",
        json={"name": "api-demo", "scope": "user", "path": body["path"]},
    )
    assert removed.status_code == 200, removed.text
    assert not Path(body["path"]).exists()


async def test_install_twice_returns_409_with_a_readable_code(client, skill_source: Path) -> None:
    await client.post("/api/v1/skills/install", json={"source": str(skill_source), "scope": "user"})
    again = await client.post(
        "/api/v1/skills/install", json={"source": str(skill_source), "scope": "user"}
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "skill_exists"


async def test_uninstall_refuses_a_path_outside_skill_roots(client) -> None:
    response = await client.post(
        "/api/v1/skills/uninstall",
        json={"name": "System32", "scope": "user", "path": "C:/Windows/System32"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "skill_path_outside_root"


async def test_install_rejects_a_directly_invalid_name(client, skill_source: Path) -> None:
    response = await client.post(
        "/api/v1/skills/install",
        json={"source": str(skill_source), "scope": "user", "name": "Not Valid"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "skill_name_invalid"
