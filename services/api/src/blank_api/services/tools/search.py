"""tool_search: pull a capability into this session's tool list.

The agent's tool list is not fixed. Version control, the network, memory search
and sub-agents are registered but hidden until asked for, which keeps every
model call's schema payload small without taking capability away.

The tool is deliberately dumb and deterministic — it scores names, tags and
descriptions, then *activates* what matched so the next turn can call it. No
model call, no fuzzy embedding, no surprise: the same query always yields the
same list.
"""

from __future__ import annotations

from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, fail, ok
from blank_api.services.tools.registry import MAX_SEARCH_RESULTS, describe


async def _tool_search(context: ToolContext, arguments: dict) -> ToolOutcome:
    registry = getattr(context, "tool_registry", None)
    if registry is None:
        return fail("error", "当前运行时没有启用工具检索：所有工具已直接加载")
    query = str(arguments.get("query") or "").strip()
    try:
        limit = int(arguments.get("limit") or MAX_SEARCH_RESULTS)
    except (TypeError, ValueError):
        limit = MAX_SEARCH_RESULTS

    matched, added = registry.search(query, limit)
    if not matched:
        return ToolOutcome(
            ok=False,
            status="empty",
            message=(
                f"没有匹配「{query}」的工具。"
                "换个说法再试，或直接用已加载的工具完成任务；"
                "空查询会列出还能加载的能力。"
            ),
            data={"query": query, "visible": registry.visible_names()},
        )

    lines = [
        f"已加载 {len(matched)} 个工具（本次会话可继续调用）："
        + "、".join(tool.name for tool in matched)
    ]
    for tool in matched:
        lines.append(f"- {tool.name}（{tool.risk_level}）：{tool.description}")
    if added:
        lines.append("下一步直接调用这些工具即可，不需要重复检索。")
    else:
        lines.append("这些工具本来就已经可用。")
    return ok(
        "\n".join(lines),
        query=query,
        activated=added,
        tools=[describe(tool) for tool in matched],
        registry=registry.as_dict(),
    )


def search_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="tool_search",
            description=(
                "按需检索并加载工具。git（版本控制）、web_search / web_fetch（联网）、"
                "memory_search（历史记忆）、spawn_agent / spawn_agents（子代理）默认不在可用列表里，"
                "需要时先用这个工具检索；命中后这些工具在本次会话余下步骤中可直接调用。"
                "query 传能力描述（如「跑测试」「提交代码」「查资料」），传空串则列出还能加载的工具。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "想要的能力，自然语言或工具名都可以"},
                    "limit": {"type": "integer", "description": "最多返回几个，默认 6"},
                },
            },
            risk_level="low",
            handler=_tool_search,
            timeout_seconds=10.0,
        )
    ]
