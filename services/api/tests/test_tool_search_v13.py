"""工具按需加载（tool_search）的回归测试。

三件事必须同时成立，否则"隐藏工具"就变成了"减少能力"或"绕过审批"：

1. 没加载的工具确实不出现在模型可见的 schema 里；
2. 检索命中后同一会话里可以直接调用；
3. 可见性变化不改变权限判定——deferred 只是一种展示策略。
"""

from __future__ import annotations

import pytest

from blank_api.services.permissions import MODE_PLAN, PermissionPolicy
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway
from blank_api.services.tools.registry import CORE_TOOLS, DEFERRED_TOOLS, ToolRegistry


def gateway() -> ToolGateway:
    return ToolGateway(build_default_tools())


def test_deferred_tools_are_registered_but_invisible():
    gw = gateway()
    names = {tool.name for tool in gw.registry.visible()}
    # 注册表里有，但默认不下发给模型
    assert "git" in gw.tools and "web_search" in gw.tools
    assert "git" not in names and "web_search" not in names
    # 核心能力一个都不能少
    for core in ("tool_search", "read_file", "apply_patch", "run_shell", "run_tests", "update_plan"):
        assert core in names
    # 可见集合是总集合的真子集
    assert names < set(gw.tools)


def test_search_activates_for_the_session():
    gw = gateway()
    before = len(gw.registry.visible())
    matched, added = gw.registry.search("提交代码")
    assert [tool.name for tool in matched] == ["git"]
    assert added == ["git"]
    assert len(gw.registry.visible()) == before + 1
    assert "git" in gw.registry.visible_names()

    # 第二次再搜同一个词，不重复"新增"
    _, again = gw.registry.search("提交代码")
    assert again == []


def test_search_does_not_need_english():
    gw = gateway()
    assert [t.name for t in gw.registry.search("查资料")[0]] == ["web_search"]
    assert [t.name for t in gw.registry.search("并行")[0]] == ["spawn_agents", "spawn_agent"]
    # 「记忆」现在对应一对工具：查旧账与记新账。两条都该被拉出来，
    # 只给 search 会让模型看得见历史却没法把这次学到的东西留下来。
    assert [t.name for t in gw.registry.search("记忆")[0]] == ["memory_search", "memory_write"]


def test_noise_queries_return_nothing_instead_of_the_whole_catalog():
    gw = gateway()
    matched, added = gw.registry.search("量子纠缠洗衣机")
    assert matched == [] and added == []
    # 描述里蹭到同一个词的工具不算命中：不能因为共享一个字就占掉每轮的 schema
    matched, _ = gw.registry.search("提交")
    assert [tool.name for tool in matched] == ["git"]


def test_empty_query_lists_what_is_still_loadable():
    gw = gateway()
    pending, added = gw.registry.search("")
    assert {tool.name for tool in pending} and set(added) == {tool.name for tool in pending}
    # 已经加载过的不再出现在"还能加载"里
    rest, _ = gw.registry.search("")
    assert not ({tool.name for tool in rest} & set(added))


def test_activation_is_per_session():
    first = gateway()
    second = gateway()
    first.registry.search("提交")
    assert "git" in first.registry.visible_names()
    assert "git" not in second.registry.visible_names()


def test_defer_can_be_turned_off():
    gw = ToolGateway(build_default_tools(), defer=False)
    assert set(gw.registry.visible_names()) == set(gw.tools)


async def test_tool_search_returns_usable_schemas(client):  # noqa: F811
    from blank_api.db import get_session_factory

    gw = gateway()
    context = ToolContext(
        root=".", task_id="t", session_factory=get_session_factory(), tool_registry=gw.registry
    )
    payload = await gw.execute("tool_search", {"query": "提交"}, context, "execute_action")
    assert payload["ok"] is True, payload
    assert payload["data"]["activated"] == ["git"]
    schemas = {item["name"]: item for item in payload["data"]["tools"]}
    assert "input_schema" in schemas["git"]
    # 说明里要告诉模型"下一步可以直接调用"，否则它可能只会反复检索
    assert "直接调用" in payload["output"]


async def test_tool_search_without_registry_says_so(client):  # noqa: F811
    from blank_api.db import get_session_factory

    gw = gateway()
    context = ToolContext(root=".", task_id="t", session_factory=get_session_factory())
    payload = await gw.execute("tool_search", {"query": "提交"}, context, "execute_action")
    assert payload["status"] == "error"
    assert "没有启用工具检索" in payload["message"]


async def test_tool_search_reports_no_match_without_failing_the_task(client):  # noqa: F811
    from blank_api.db import get_session_factory

    gw = gateway()
    context = ToolContext(
        root=".", task_id="t", session_factory=get_session_factory(), tool_registry=gw.registry
    )
    payload = await gw.execute("tool_search", {"query": "量子纠缠"}, context, "execute_action")
    assert payload["status"] == "empty"
    assert payload["data"]["visible"]


def test_deferral_never_changes_permission_decisions():
    gw = gateway()
    context = ToolContext(
        root=".",
        task_id="t",
        session_factory=None,
        permissions=PermissionPolicy.build("default"),
        tool_registry=gw.registry,
    )
    tool = gw.get("web_fetch")
    before = gw.decide(tool, {"url": "https://example.com"}, context)
    gw.registry.search("抓取网页")
    after = gw.decide(tool, {"url": "https://example.com"}, context)
    # 加载与否只影响可见性；判定必须一模一样
    assert (before.outcome, before.rule, before.risk) == (after.outcome, after.rule, after.risk)


def test_plan_mode_can_still_load_read_only_capabilities():
    policy = PermissionPolicy.build(MODE_PLAN)
    gw = gateway()
    context = ToolContext(
        root=".", task_id="t", session_factory=None, permissions=policy, tool_registry=gw.registry
    )
    search = gw.decide(gw.get("tool_search"), {"query": "git"}, context)
    assert search.allowed, "计划模式必须允许检索，否则连只读取证都做不了"
    git = gw.decide(gw.get("git"), {"action": "status"}, context)
    assert git.allowed, "git 在计划模式属于只读工具"


@pytest.mark.parametrize("name", sorted(DEFERRED_TOOLS))
def test_every_deferred_tool_is_reachable_by_some_query(name):
    """防止"登记了 deferred 却没人搜得到"——那等于把它悄悄删掉。"""
    gw = gateway()
    queries = [
        "提交",
        "跑测试",
        "查资料",
        "抓网页",
        "记忆",
        "并行",
        "子代理",
        name,
    ]
    found: set[str] = set()
    for query in queries:
        matched, _ = gw.registry.search(query)
        found |= {tool.name for tool in matched}
    assert name in found, f"{name} 无法通过任何检索词找到"


def test_core_tools_are_not_deferred():
    gw = gateway()
    assert CORE_TOOLS & set(gw.registry.deferred_names()) == set()
    registry = ToolRegistry(build_default_tools())
    assert set(registry.visible_names()) >= {name for name in CORE_TOOLS if name in gw.tools}
