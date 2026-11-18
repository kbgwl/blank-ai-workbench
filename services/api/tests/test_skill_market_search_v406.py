"""GitHub 当市场：搜索要带评分，推荐要留证据。

用户的诉求是"内置不符合就去 GitHub 找，但要能说清拉的是什么、多少分"。
所以这里钉三件事：
1. 搜索结果的 stars 必须来自 GitHub，不能我们自己编；
2. 技能与工具（仓库）要分开标，装错地方比不装更糟；
3. 精选清单是快照，必须带核对日期。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blank_api.services import skill_market as sm
from blank_api.services.tools import build_default_tools


class _Resp:
    def __init__(self, payload: bytes | str, status: int = 200) -> None:
        self.status_code = status
        self.content = payload.encode("utf-8") if isinstance(payload, str) else payload


class _FakeClient:
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


REPOS = json.dumps(
    {
        "items": [
            {
                "full_name": "acme/skills",
                "description": "一堆实用技能",
                "stargazers_count": 4200,
                "html_url": "https://github.com/acme/skills",
                "pushed_at": "2026-11-01T00:00:00Z",
                "default_branch": "main",
            },
            {
                "full_name": "acme/tool",
                "description": "一个命令行工具",
                "stargazers_count": 900,
                "html_url": "https://github.com/acme/tool",
                "pushed_at": "2026-10-20T00:00:00Z",
                "default_branch": "main",
            },
        ]
    }
)


def test_search_marks_skills_and_repos_and_keeps_stars() -> None:
    client = _FakeClient(
        {
            "search/repositories": _Resp(REPOS),
            "git/trees": _Resp(json.dumps({"tree": [{"path": "pdf/SKILL.md"}]})),
            "raw.githubusercontent.com": _Resp("---\nname: PDF 处理\ndescription: 读写 PDF\n---\n正文\n"),
        }
    )
    hits = sm.search_market("处理 pdf", client=client)

    # acme/tool 的 tree 也返回了同一个响应，所以它也会被当成有技能——
    # 这里只断言"评分与来源正确"，不依赖假数据的巧合。
    assert hits, "至少要有结果"
    assert hits[0].stars >= hits[-1].stars, "必须按 stars 降序"
    skill_hits = [hit for hit in hits if hit.kind == "skill"]
    assert skill_hits and skill_hits[0].repo == "acme/skills"
    assert skill_hits[0].name == "PDF 处理"
    assert skill_hits[0].path == "pdf"
    assert skill_hits[0].url == "https://github.com/acme/skills"


def test_repo_without_skill_md_is_reported_as_tool() -> None:
    repos = json.dumps(
        {
            "items": [
                {
                    "full_name": "acme/tool",
                    "description": "纯工具",
                    "stargazers_count": 77,
                    "html_url": "https://github.com/acme/tool",
                    "default_branch": "main",
                }
            ]
        }
    )
    client = _FakeClient({"search/repositories": _Resp(repos)})
    hits = sm.search_market("tool", client=client)
    assert len(hits) == 1
    assert hits[0].kind == "repo"
    assert hits[0].stars == 77


def test_one_broken_repo_does_not_kill_the_whole_search() -> None:
    """单个仓库读不到（空库/超限）不该让整次搜索失败——那是常见情况。"""
    client = _FakeClient({"search/repositories": _Resp(REPOS)})  # tree 一律 404
    hits = sm.search_market("anything", client=client)
    assert {hit.kind for hit in hits} == {"repo"}
    assert len(hits) == 2


def test_empty_query_is_rejected() -> None:
    with pytest.raises(sm.MarketError) as exc:
        sm.search_market("   ", client=_FakeClient({}))
    assert exc.value.code == "market_query_empty"


def test_curated_registry_parses_and_carries_evidence(tmp_path: Path) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "external": [
                    {
                        "repo": "acme/skills",
                        "path": "pdf",
                        "name": "PDF 处理",
                        "why": "读写 PDF 唯一像样的做法",
                        "stars": 4200,
                        "checked_at": "2026-11-18",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    items = sm.list_curated(registry)
    assert len(items) == 1
    assert items[0].name == "PDF 处理"
    assert items[0].install_ref["path"] == "pdf"
    assert items[0].source_label == "我们的市场"


def test_missing_registry_is_empty_not_an_error(tmp_path: Path) -> None:
    assert sm.list_curated(tmp_path / "nope.json") == []


def test_market_tools_are_registered_with_the_right_risk() -> None:
    tools = {tool.name: tool for tool in build_default_tools()}
    assert "find_skills" in tools
    assert "install_market_skill" in tools
    assert "list_curated_skills" in tools
    # 搜索是只读的，不该进审批；安装会下载并写盘，必须进审批。
    assert tools["find_skills"].risk_level == "low"
    assert tools["install_market_skill"].risk_level == "high"
