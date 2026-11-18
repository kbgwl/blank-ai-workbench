"""技能市场适配器：三种来源形状都要认，且只认这三种。

网络全部用假 client，测试不打外网——打外网的测试会在没网时变红，最后被迫删除。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from blank_api.services import skill_market as sm
from blank_api.services.skill_install import install_skill


class _Resp:
    def __init__(self, payload: bytes | str, status: int = 200) -> None:
        self.status_code = status
        self.content = payload.encode("utf-8") if isinstance(payload, str) else payload


class _FakeClient:
    """按 URL 关键字返回预置响应。"""

    def __init__(self, routes: dict[str, _Resp]) -> None:
        self.routes = routes
        self.seen: list[str] = []

    def get(self, url: str, headers: dict | None = None) -> _Resp:
        self.seen.append(url)
        for key, response in self.routes.items():
            if key in url:
                return response
        return _Resp(b"", status=404)

    def close(self) -> None:
        return None


TREE = json.dumps(
    {
        "tree": [
            {"path": "skills/pdf/SKILL.md"},
            {"path": "skills/pdf/scripts/extract.py"},
            {"path": "code-review/SKILL.md"},
            {"path": "README.md"},
        ]
    }
)

PDF_MD = "---\nname: PDF 处理\ndescription: 读写 PDF\n---\n正文\n"
REVIEW_MD = "---\nname: 代码审查\ndescription: 审查改动\n---\n正文\n"


def test_lists_github_skills_from_the_tree_api() -> None:
    client = _FakeClient(
        {
            "api.github.com": _Resp(TREE),
            "skills/pdf/SKILL.md": _Resp(PDF_MD),
            "code-review/SKILL.md": _Resp(REVIEW_MD),
        }
    )
    items = sm.list_github("acme/skills", ref="main", client=client)

    by_id = {item.id: item for item in items}
    assert set(by_id) == {"skills/pdf", "code-review"}
    assert by_id["skills/pdf"].name == "PDF 处理"
    assert by_id["skills/pdf"].description == "读写 PDF"
    # 安装参数要能直接喂给安装器：仓库 + ref + 仓库内目录
    assert by_id["skills/pdf"].install_ref["repo"] == "acme/skills"
    assert by_id["skills/pdf"].install_ref["path"] == "skills/pdf"


def test_frontmatter_parse_failure_degrades_to_directory_name() -> None:
    client = _FakeClient(
        {"api.github.com": _Resp(TREE), "SKILL.md": _Resp("<html>not markdown</html>")}
    )
    items = sm.list_github("acme/skills", client=client)
    assert {item.name for item in items} == {"pdf", "code-review"}


def test_lists_claude_marketplace_manifest() -> None:
    manifest = json.dumps(
        {
            "plugins": [
                {"name": "code-review", "description": "审查插件", "source": "./plugins/code-review"},
                {"name": "no-source"},
            ]
        }
    )
    client = _FakeClient({".claude-plugin/marketplace.json": _Resp(manifest)})
    items = sm.list_claude_marketplace("https://github.com/acme/market", client=client)

    assert len(items) == 1
    assert items[0].name == "code-review"
    assert items[0].install_ref["path"] == "plugins/code-review"
    assert items[0].install_ref["repo"] == "acme/market"


def test_query_filters_by_name_and_description() -> None:
    client = _FakeClient(
        {
            "api.github.com": _Resp(TREE),
            "skills/pdf/SKILL.md": _Resp(PDF_MD),
            "code-review/SKILL.md": _Resp(REVIEW_MD),
        }
    )
    items = asyncio_run(sm.list_market("acme/skills", query="pdf", client=client))
    assert [item.id for item in items] == ["skills/pdf"]


def test_unknown_hosts_are_blocked() -> None:
    with pytest.raises(sm.MarketError) as exc:
        sm._check_url("https://evil.example.com/skill.zip")
    assert exc.value.code == "market_host_blocked"


def test_bad_source_shapes_are_rejected() -> None:
    async def run():
        return await sm.list_market("不是仓库也不是地址")

    with pytest.raises(sm.MarketError) as exc:
        asyncio_run(run())
    assert exc.value.code == "market_source_invalid"


def test_zip_prefix_only_extracts_the_wanted_skill(tmp_path: Path, monkeypatch) -> None:
    """市场安装走"仓库 zip + 子目录"：只解目标那一支，别把整仓库落盘。"""
    monkeypatch.setenv("BLANK_USER_SKILLS_DIR", str(tmp_path / "user-skills"))
    repo_zip = tmp_path / "repo.zip"
    with zipfile.ZipFile(repo_zip, "w") as zf:
        zf.writestr("acme-skills-main/skills/pdf/SKILL.md", PDF_MD)
        zf.writestr("acme-skills-main/skills/pdf/scripts/extract.py", "print(1)\n")
        zf.writestr("acme-skills-main/skills/other/SKILL.md", REVIEW_MD)
        zf.writestr("acme-skills-main/README.md", "readme")

    result = install_skill(str(repo_zip), scope="user", name="pdf", prefix="skills/pdf")

    target = Path(result["path"])
    assert (target / "SKILL.md").is_file()
    assert (target / "scripts" / "extract.py").is_file()
    # 别的技能与仓库根文件不该被带进来
    assert not (target / "other").exists()
    assert not (target / "README.md").exists()


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
