"""本地市场镜像：目录与安装包都先落到本机，之后列表与安装不依赖网络。

判据来自用户的取舍："先去 GitHub 拉到我们的服务器，本地服务器再提供输出，
这样就不用担心网络问题"。所以核心是三条：
1. 目录同步是**一次性**动作，失败不破坏已有目录；
2. 有镜像包时安装走本地缓存，不再联网；
3. 没同步过时明确说"没有目录"，而不是装作搜不到。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from blank_api.services import market_cache as mc
from blank_api.services.skill_install import install_skill


@pytest.fixture(autouse=True)
def _market_home(monkeypatch, tmp_path):
    root = tmp_path / "market"
    monkeypatch.setenv("BLANK_MARKET_DIR", str(root))
    return root


CATALOG = {
    "entries": [
        {
            "name": "PDF 处理",
            "repo": "acme/skills",
            "path": "skills/pdf",
            "stars": 4200,
            "summary": "读写 PDF、提取表格",
            "tags": ["pdf", "文档"],
            "package": "pdf.zip",
            "checked_at": "2026-11-18",
        },
        {
            "name": "Playwright 测试",
            "repo": "acme/skills",
            "path": "skills/pw",
            "stars": 900,
            "summary": "写浏览器端到端测试",
            "tags": ["测试", "浏览器"],
            "checked_at": "2026-11-18",
        },
    ]
}


def test_sync_writes_catalog_and_stamps_it() -> None:
    catalog = mc.write_catalog(CATALOG, source="https://example.com/catalog.json")
    assert len(catalog["entries"]) == 2
    assert catalog["synced_at"], "必须留下同步时间，否则没人知道这份目录有多旧"
    assert catalog["source"] == "https://example.com/catalog.json"

    again = mc.read_catalog()
    assert [row.name for row in again["entries"]] == ["PDF 处理", "Playwright 测试"]


def test_bad_entries_are_dropped_not_fatal() -> None:
    catalog = mc.write_catalog({"entries": [{"name": "没有仓库"}, CATALOG["entries"][0]]})
    assert len(catalog["entries"]) == 1


def test_empty_catalog_is_rejected() -> None:
    with pytest.raises(mc.MarketError) as exc:
        mc.write_catalog({"entries": []})
    assert exc.value.code == "market_catalog_empty"


def test_search_matches_name_summary_and_tags() -> None:
    mc.write_catalog(CATALOG)
    assert [row.name for row in mc.search_catalog("表格")] == ["PDF 处理"]
    assert [row.name for row in mc.search_catalog("测试")] == ["Playwright 测试"]
    # 空查询按 stars 降序，等于"推荐列表"
    assert [row.name for row in mc.search_catalog("")] == ["PDF 处理", "Playwright 测试"]


def test_missing_catalog_reads_as_empty_not_an_error() -> None:
    data = mc.read_catalog()
    assert data["entries"] == []
    assert data["synced_at"] == ""


def test_local_package_is_used_without_network(_market_home) -> None:
    mc.write_catalog(CATALOG)
    entry = mc.read_catalog()["entries"][0]
    archive = _market_home / "packages" / "pdf.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("skills/pdf/SKILL.md", "---\nname: PDF\ndescription: 读写 PDF\n---\n正文\n")

    resolved = mc.ensure_package(entry)
    assert resolved == archive
    assert resolved.is_file()


def test_package_path_escape_is_rejected() -> None:
    entry = mc.CatalogEntry(key="a/b:x", name="x", repo="a/b", path="x", package="../../etc/passwd")
    with pytest.raises(mc.MarketError) as exc:
        mc.ensure_package(entry)
    assert exc.value.code == "market_package_escape"


def test_missing_package_falls_back_to_repo_flag() -> None:
    entry = mc.CatalogEntry(key="a/b:x", name="x", repo="a/b", path="x", package="")
    with pytest.raises(mc.MarketError) as exc:
        mc.ensure_package(entry)
    assert exc.value.code == "market_package_missing"


def test_cached_zip_installs_without_touching_github(tmp_path: Path, monkeypatch) -> None:
    """整条链：目录里有包 → 从缓存装 → 目录结构与来源都对。"""
    monkeypatch.setenv("BLANK_USER_SKILLS_DIR", str(tmp_path / "user-skills"))
    mc.write_catalog(CATALOG)
    entry = mc.read_catalog()["entries"][0]
    archive = mc.market_home() / "packages" / "pdf.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("skills/pdf/SKILL.md", "---\nname: PDF\ndescription: 读写 PDF\n---\n正文\n")
        zf.writestr("skills/pdf/scripts/x.py", "print(1)\n")

    result = install_skill(
        str(mc.ensure_package(entry)), scope="user", name="pdf", prefix="skills/pdf"
    )
    target = Path(result["path"])
    assert (target / "SKILL.md").is_file()
    assert (target / "scripts" / "x.py").is_file()
