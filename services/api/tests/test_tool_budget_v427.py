"""工具说明的字符预算：每轮都发的东西，必须有个数。

量出来的事实（真机，任务 `692f5889` / `df717096`）：

| | 可见工具 | 工具说明 | 首调 prompt |
|---|---|---|---|
| 收敛前 | 44（实际 38） | 23,670 字符 | 19,631 token |
| 收敛后 | 27 | 16,382 字符 | 16,156 token |

首屏上下文里任务本身只有约 1,000 字符，剩下全是说明书——而它每一步都要重发。
一次 26 步的任务，光这一项就省下约 9 万 token。

所以这里钉两件事：

1. **预算**：说明总量不得超过 `SCHEMA_BUDGET_CHARS`。新增工具要能被看见，
   但每加一个都得先想清楚它配不配常驻。
2. **按需仍然可达**：被收起来的工具必须能从 tool_search 拉回来，一个都不能丢——
   收敛是省底噪，不是减功能。这条比第一条更重要：预算可以谈，功能不能悄悄消失。
"""

from __future__ import annotations

import json

from blank_api.services.tools import build_default_tools
from blank_api.services.tools.registry import SCHEMA_BUDGET_CHARS, ToolRegistry


def _cost(tool) -> int:
    return len(tool.description or "") + len(json.dumps(tool.input_schema, ensure_ascii=False))


def _registry() -> ToolRegistry:
    return ToolRegistry(build_default_tools())


def test_the_tool_manual_stays_within_budget():
    registry = _registry()
    total = sum(_cost(tool) for tool in build_default_tools() if tool.name in set(registry.visible_names()))

    assert total <= SCHEMA_BUDGET_CHARS, (
        f"工具说明 {total} 字符超出预算 {SCHEMA_BUDGET_CHARS}——"
        "常驻工具是要每步重发的，先把低频的改成按需加载，或者给出为什么要常驻的理由"
    )


def test_the_eye_is_always_visible():
    """browser_check 必须常驻：模型不会去搜一个它不知道存在的能力。"""
    assert "browser_check" in _registry().visible_names()


def test_deferred_families_are_still_reachable():
    """收起来 ≠ 删掉：每一个都能被 tool_search 拉回来。"""
    registry = _registry()
    visible = set(registry.visible_names())
    deferred = [tool.name for tool in build_default_tools() if tool.name not in visible]
    assert deferred, "所有工具都常驻了，预算一定已经爆了"

    for name in deferred:
        added = registry.activate([name])
        assert name in added, f"{name} 收起来之后拉不回来，等于删掉了"


def test_a_normal_coding_turn_does_not_carry_scheduled_task_tools():
    """定时任务与团队两族只在特定场景出现，普通编程的第一个回合不该背着它们。

    notebook / repl / report_findings / 任务台账**不在这条里**：它们各有测试钉着
    「常驻但不算核心」，那是有意的产品判断（report_findings 藏进 tool_search 之后，
    模型交付时就不会用它）。预算不够不能从这几样身上省。
    """
    visible = set(_registry().visible_names())

    for name in ("cron_create", "cron_list", "schedule_wakeup", "team_create_bot", "team_status"):
        assert name not in visible, f"{name} 又回到常驻了：它每步都要花一次 schema 钱"

    for name in ("notebook_edit", "repl", "report_findings", "task_create"):
        assert name in visible, f"{name} 被收起来了——这条是有测试守着的产品决定，别顺手改"
