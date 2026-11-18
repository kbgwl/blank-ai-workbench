"""web_search 的域名过滤（对齐 Claude WebSearchInput 的 allowed/blocked_domains）。

为什么要有这一项：模型经常需要"只信官方文档"或"避开内容农场"。没有域名参数时，
它只能在拿到一堆结果后自己筛，既费 token 又容易把不可信来源当真。过滤必须在
工具层做——顺带保证过滤后仍然凑得够条数。
"""

import pytest

from blank_api.services.tools import web
from blank_api.services.tools.base import ToolContext


def _ctx(tmp_path) -> ToolContext:
    return ToolContext(root=tmp_path, task_id="t-domain", session_factory=None)


def _item(title: str, url: str) -> dict:
    return {"title": title, "url": url, "snippet": f"{title} 摘要"}


# --------------------------------------------------------------------------- #
# 1. 域名匹配语义
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("host", "domain", "expected"),
    [
        ("python.org", "python.org", True),
        ("docs.python.org", "python.org", True),
        ("DOCS.Python.ORG", "python.org", True),
        # 前缀相似但不是子域：不能误判成命中
        ("notpython.org", "python.org", False),
        ("python.org.evil.com", "python.org", False),
        ("", "python.org", False),
        ("python.org", "", False),
    ],
)
def test_domain_matches(host: str, domain: str, expected: bool) -> None:
    assert web.domain_matches(host, domain) is expected


def test_keep_domain_allow_and_block() -> None:
    assert web.keep_domain("https://docs.python.org/3/", ["python.org"], []) is True
    assert web.keep_domain("https://example.com/x", ["python.org"], []) is False
    # 排除优先于允许
    assert web.keep_domain("https://bad.python.org/x", ["python.org"], ["bad.python.org"]) is False
    # 两个都不给 = 全放行
    assert web.keep_domain("https://anything.test/x", [], []) is True


# --------------------------------------------------------------------------- #
# 2. 工具层：过滤、补足条数、过滤后为空的说明
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_search_filters_to_allowed_domain(monkeypatch, tmp_path) -> None:
    async def fake_search(query, *, limit=5, timeout=20.0):
        # 混着给：只有前两条在 python.org
        return [
            _item("官方文档", "https://docs.python.org/3/library/asyncio.html"),
            _item("官方教程", "https://python.org/about/"),
            _item("某博客", "https://blog.example.com/python"),
        ]

    monkeypatch.setattr(web, "search", fake_search)
    outcome = await web._web_search(_ctx(tmp_path), {"query": "asyncio", "allowed_domains": ["python.org"]})

    assert outcome.status == "ok"
    assert outcome.data["count"] == 2
    assert outcome.data["filtered_out"] == 1
    urls = [row["url"] for row in outcome.data["results"]]
    assert all("python.org" in url for url in urls)


@pytest.mark.asyncio
async def test_search_blocked_domain_removes_matches(monkeypatch, tmp_path) -> None:
    async def fake_search(query, *, limit=5, timeout=20.0):
        return [
            _item("内容农场", "https://csdn.net/post/1"),
            _item("官网", "https://nodejs.org/api/"),
        ]

    monkeypatch.setattr(web, "search", fake_search)
    outcome = await web._web_search(_ctx(tmp_path), {"query": "node", "blocked_domains": ["csdn.net"]})

    assert outcome.data["count"] == 1
    assert outcome.data["results"][0]["url"] == "https://nodejs.org/api/"


@pytest.mark.asyncio
async def test_search_over_fetches_before_filtering(monkeypatch, tmp_path) -> None:
    """带过滤时要多要一些：否则"只允许某站"经常只剩一两条。"""
    seen = {}

    async def fake_search(query, *, limit=5, timeout=20.0):
        seen["limit"] = limit
        return [_item("命中", "https://python.org/")] * limit

    monkeypatch.setattr(web, "search", fake_search)
    await web._web_search(_ctx(tmp_path), {"query": "x", "max_results": 3, "allowed_domains": ["python.org"]})
    assert seen["limit"] > 3


@pytest.mark.asyncio
async def test_search_empty_after_filter_explains_why(monkeypatch, tmp_path) -> None:
    async def fake_search(query, *, limit=5, timeout=20.0):
        return [_item("不相干", "https://example.com/a")]

    monkeypatch.setattr(web, "search", fake_search)
    outcome = await web._web_search(_ctx(tmp_path), {"query": "x", "allowed_domains": ["python.org"]})

    assert outcome.data["results"] == []
    # 必须说清"不是没资料，是域名都不符合"，否则模型会以为网上没有
    assert "域名都不符合" in outcome.output
    assert outcome.data["filtered_out"] == 1


@pytest.mark.asyncio
async def test_search_without_filter_is_unchanged(monkeypatch, tmp_path) -> None:
    async def fake_search(query, *, limit=5, timeout=20.0):
        return [_item("A", "https://example.com/a"), _item("B", "https://example.org/b")]

    monkeypatch.setattr(web, "search", fake_search)
    outcome = await web._web_search(_ctx(tmp_path), {"query": "x"})

    assert outcome.data["count"] == 2
    assert outcome.data["filtered_out"] == 0
