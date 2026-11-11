"""Q 批第 4 项：ReportFindings 对齐 Claude。

判据分三层：
- 工具面：默认可见、低风险、只读（计划模式放行）、可并发、中文别名能检索到；
- 回执：整批校验（必填、行号、长度、枚举、上限），统一形状，空数组是合法报告；
- 接线：事件 `review.findings` 只落一次，真实任务里模型能看见工具、调用后事件与
  artifact 都能还原那份结构化清单。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blank_api.db import get_session_factory
from blank_api.models.task import AgentEvent
from blank_api.services.agent.artifacts import ArtifactStore
from blank_api.services.agent.runtime import AgentRuntime, RunRequest
from blank_api.services.models.registry import build_adapter
from blank_api.services.permissions import READ_ONLY_TOOLS
from blank_api.services.subagent import READ_ONLY_TOOLS as SUBAGENT_READ_ONLY_TOOLS
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.base import ToolContext
from blank_api.services.tools.gateway import ToolGateway
from blank_api.services.tools.registry import LEAN_TOOLS, PARALLEL_SAFE_TOOLS, ToolRegistry
from blank_api.services.tools.review import (
    MAX_CATEGORY,
    MAX_FINDINGS,
    MAX_SHORT_SUMMARY,
    normalize_findings,
    review_tools,
)
from tests.test_agent_v04 import (  # noqa: F401  (env 是端到端夹具)
    Env,
    call,
    create_project,
    create_provider,
    create_task,
    env,
    provider_row,
)


def _context(**overrides) -> ToolContext:
    params = {
        "root": Path("."),
        "task_id": "task-review",
        "session_factory": None,
        "recorder": _Recorder(),
    }
    params.update(overrides)
    return ToolContext(**params)


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def add(self, event: str, phase: str, payload: dict) -> None:
        self.events.append((event, phase, payload))


def _tool():
    return {tool.name: tool for tool in review_tools()}["report_findings"]


def _finding(**overrides) -> dict:
    item = {
        "file": "src/app.py",
        "line": 42,
        "summary": "空值被当成 0 继续参与计算",
        "short_summary": "空值被当成 0",
        "failure_scenario": "配置缺字段时返回 None，调用方按数值相加直接 TypeError",
        "category": "correctness",
    }
    item.update(overrides)
    return item


# --------------------------------------------------------------------------- #
# 1. 工具面
# --------------------------------------------------------------------------- #
def test_report_findings_is_wired_into_the_default_surface() -> None:
    tools = {tool.name: tool for tool in build_default_tools()}
    assert "report_findings" in tools
    tool = tools["report_findings"]
    assert tool.risk_level == "low"
    assert tool.requires_approval is False
    assert tool.input_schema["required"] == ["findings"]
    findings = tool.input_schema["properties"]["findings"]
    assert findings["maxItems"] == MAX_FINDINGS
    assert findings["items"]["required"] == ["file", "summary", "failure_scenario"]
    assert findings["items"]["properties"]["line"]["type"] == "integer"
    assert findings["items"]["properties"]["short_summary"]["maxLength"] == MAX_SHORT_SUMMARY


def test_description_carries_the_three_claude_rules() -> None:
    description = _tool().description
    # 只在审查说明点名时才用；不要另发正文；修复后重报才带 outcome
    assert "只在当前生效的代码审查说明明确要求用本工具上报时使用" in description
    assert "不要" in description and "以正文形式打印" in description
    assert "严重程度从高到低" in description
    assert "outcome" in description and "verdict" in description


def test_report_findings_is_visible_by_default_but_not_in_lean() -> None:
    registry = ToolRegistry(build_default_tools())
    assert "report_findings" in registry.visible_names()
    assert "report_findings" not in LEAN_TOOLS
    registry.set_profile("lean")
    assert "report_findings" not in registry.visible_names()


def test_report_findings_is_allowed_in_plan_mode() -> None:
    """上报只是一份结构化回执，不碰工作区；只读调研时也能先把发现记下来。"""
    assert "report_findings" in READ_ONLY_TOOLS


def test_report_findings_is_parallel_safe() -> None:
    assert "report_findings" in PARALLEL_SAFE_TOOLS


def test_report_findings_is_searchable_by_its_chinese_aliases() -> None:
    registry = ToolRegistry(build_default_tools())
    names = [tool.name for tool in registry.search("代码审查")[0]]
    assert "report_findings" in names


def test_report_findings_stays_out_of_the_subagent_whitelist() -> None:
    """审查批次里子代理把发现交回主代理，由主代理一次性上报（Claude 的契约）。"""
    assert "report_findings" not in SUBAGENT_READ_ONLY_TOOLS


# --------------------------------------------------------------------------- #
# 2. 回执：整批校验与统一形状
# --------------------------------------------------------------------------- #
async def test_empty_array_is_a_valid_report() -> None:
    context = _context()
    outcome = await _tool().handler(context, {"findings": []})

    assert outcome.ok, outcome.message
    assert outcome.output == "没有需要上报的审查发现。"
    assert outcome.data == {"count": 0, "findings": []}
    assert context.recorder.events[0][2]["count"] == 0


async def test_a_complete_finding_round_trips_in_a_fixed_shape() -> None:
    context = _context()
    outcome = await _tool().handler(
        context,
        {"level": "high", "findings": [_finding(verdict="CONFIRMED", outcome="fixed")]},
    )

    assert outcome.ok, outcome.message
    assert outcome.output == "已上报 1 条审查发现。"
    assert outcome.data["count"] == 1
    assert outcome.data["level"] == "high"
    assert outcome.data["findings"] == [
        {
            "file": "src/app.py",
            "line": 42,
            "summary": "空值被当成 0 继续参与计算",
            "failure_scenario": "配置缺字段时返回 None，调用方按数值相加直接 TypeError",
            "short_summary": "空值被当成 0",
            "category": "correctness",
            "verdict": "CONFIRMED",
            "outcome": "fixed",
        }
    ]


async def test_unknown_fields_are_stripped_from_the_typed_list() -> None:
    outcome = await _tool().handler(
        _context(), {"findings": [_finding(severity="critical")]}
    )
    assert outcome.ok
    assert "severity" not in outcome.data["findings"][0]


@pytest.mark.parametrize("field", ["file", "summary", "failure_scenario"])
async def test_each_required_field_is_enforced(field: str) -> None:
    item = _finding()
    item.pop(field)
    outcome = await _tool().handler(_context(), {"findings": [item]})

    assert outcome.ok is False
    assert outcome.status == "error"
    assert "第 1 条" in outcome.message
    assert field in outcome.message


async def test_a_non_object_finding_names_its_position() -> None:
    outcome = await _tool().handler(_context(), {"findings": [_finding(), "坏的"]})
    assert outcome.ok is False
    assert "第 2 条" in outcome.message
    assert "对象" in outcome.message


async def test_findings_must_be_an_array() -> None:
    outcome = await _tool().handler(_context(), {"findings": {"file": "a.py"}})
    assert outcome.ok is False
    assert "必须是数组" in outcome.message


async def test_more_than_the_cap_is_rejected_as_a_whole() -> None:
    items = [_finding(file=f"src/{index}.py") for index in range(MAX_FINDINGS + 1)]
    outcome = await _tool().handler(_context(), {"findings": items})

    assert outcome.ok is False
    assert str(MAX_FINDINGS) in outcome.message
    assert "收到" in outcome.message


@pytest.mark.parametrize("line", [0, -1, True, "12", 1.5])
async def test_line_must_be_a_positive_integer(line) -> None:
    outcome = await _tool().handler(_context(), {"findings": [_finding(line=line)]})
    assert outcome.ok is False
    assert "line" in outcome.message


async def test_line_can_be_omitted() -> None:
    item = _finding()
    item.pop("line")
    outcome = await _tool().handler(_context(), {"findings": [item]})
    assert outcome.ok
    assert "line" not in outcome.data["findings"][0]


async def test_short_summary_accepts_exactly_the_limit() -> None:
    outcome = await _tool().handler(
        _context(), {"findings": [_finding(short_summary="短" * MAX_SHORT_SUMMARY)]}
    )
    assert outcome.ok


async def test_short_summary_over_the_limit_is_rejected() -> None:
    outcome = await _tool().handler(
        _context(), {"findings": [_finding(short_summary="短" * (MAX_SHORT_SUMMARY + 1))]}
    )
    assert outcome.ok is False
    assert "short_summary" in outcome.message


async def test_category_accepts_exactly_the_limit() -> None:
    outcome = await _tool().handler(
        _context(), {"findings": [_finding(category="c" * MAX_CATEGORY)]}
    )
    assert outcome.ok


async def test_category_over_the_limit_is_rejected() -> None:
    outcome = await _tool().handler(
        _context(), {"findings": [_finding(category="c" * (MAX_CATEGORY + 1))]}
    )
    assert outcome.ok is False
    assert "category" in outcome.message


async def test_verdict_enum_is_enforced() -> None:
    outcome = await _tool().handler(_context(), {"findings": [_finding(verdict="confirmed")]})
    assert outcome.ok is False
    assert "verdict" in outcome.message


async def test_outcome_enum_is_enforced() -> None:
    outcome = await _tool().handler(_context(), {"findings": [_finding(outcome="done")]})
    assert outcome.ok is False
    assert "outcome" in outcome.message


async def test_level_enum_is_enforced() -> None:
    outcome = await _tool().handler(_context(), {"level": "extreme", "findings": []})
    assert outcome.ok is False
    assert "level" in outcome.message


async def test_invalid_batch_records_no_event() -> None:
    context = _context()
    outcome = await _tool().handler(context, {"findings": [{"file": "a.py"}]})

    assert outcome.ok is False
    assert context.recorder.events == []


async def test_no_recorder_does_not_break_the_report() -> None:
    outcome = await _tool().handler(_context(recorder=None), {"findings": [_finding()]})
    assert outcome.ok
    assert outcome.data["count"] == 1


async def test_one_event_carries_the_whole_typed_list() -> None:
    context = _context()
    await _tool().handler(
        context, {"level": "medium", "findings": [_finding(), _finding(file="src/b.py")]}
    )

    events = [item for item in context.recorder.events if item[0] == "review.findings"]
    assert len(events) == 1
    assert events[0][1] == "review"
    assert events[0][2]["count"] == 2
    assert events[0][2]["level"] == "medium"
    assert [item["file"] for item in events[0][2]["findings"]] == ["src/app.py", "src/b.py"]


def test_normalize_keeps_the_models_ranking() -> None:
    """顺序就是严重程度判断：静默按文件或行号重排等于改写结论。"""
    findings, error = normalize_findings(
        [_finding(file="z.py", line=90), _finding(file="a.py", line=1)]
    )
    assert error is None
    assert [item["file"] for item in findings] == ["z.py", "a.py"]


# --------------------------------------------------------------------------- #
# 3. 网关与真实任务接线
# --------------------------------------------------------------------------- #
def _gateway_context(task_id: str, **overrides) -> ToolContext:
    params = {
        "root": Path("."),
        "task_id": task_id,
        "session_factory": get_session_factory(),
        "approval_policy": lambda *_: False,
    }
    params.update(overrides)
    return ToolContext(**params)


def _artifact(store: ArtifactStore, task_id: str, call_id: str) -> dict:
    return store.load(task_id, call_id)


async def test_gateway_records_the_call_and_redacts_secrets(env, tmp_path) -> None:
    gateway = ToolGateway(review_tools())
    secret = "sk-synthetic-review-secret"
    context = _gateway_context(
        "task-review-gateway", extra_secrets=(secret,), approval_policy=lambda *_: False
    )

    payload = await gateway.execute(
        "report_findings",
        {"findings": [_finding(summary=f"日志里带着 {secret}")]},
        context,
    )

    assert payload["ok"] is True, payload
    assert payload["status"] == "ok"
    assert payload["output"] == "已上报 1 条审查发现。"
    rows = await env.tool_calls("task-review-gateway")
    assert [row.name for row in rows] == ["report_findings"]
    assert rows[0].status == "ok"

    artifact = _artifact(ArtifactStore(), "task-review-gateway", rows[0].id)
    text = json.dumps(artifact, ensure_ascii=False)
    assert secret not in text
    assert "日志里带着" in text


async def test_gateway_enforces_the_required_top_level_field(env) -> None:
    gateway = ToolGateway(review_tools())
    payload = await gateway.execute("report_findings", {}, _gateway_context("task-review-missing"))

    assert payload["ok"] is False
    assert payload["status"] == "error"
    assert "findings" in payload["message"]


async def test_real_task_shows_the_tool_and_stores_the_typed_list(env: Env, workspace: Path) -> None:
    """真实任务：模型看得见工具、调用后事件与 artifact 都能还原那份清单。"""
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["审查改动并上报发现"])
    env.mock.reset(
        [
            {"content": "先读 diff，再按审查说明上报。"},
            {
                "content": "",
                "tool_calls": [
                    call(
                        "report_findings",
                        {
                            "level": "medium",
                            "findings": [
                                _finding(
                                    file="main.py",
                                    line=1,
                                    summary="守卫被删掉后空值直接进计算",
                                )
                            ],
                        },
                    )
                ],
            },
            {"content": "已按清单上报，不再重复打印。"},
        ]
    )

    evidence = await AgentRuntime(get_session_factory()).run(
        RunRequest(
            task_id=task_id,
            provider_id=provider_id,
            provider_name="mock-vendor",
            adapter=build_adapter(await provider_row(provider_id), env.mock.factory),
            root=workspace,
            goal="审查这次改动并上报发现",
            acceptance_criteria=["审查发现已上报"],
            max_steps=5,
        )
    )

    assert evidence["task"]["status"] == "succeeded", evidence["task"]
    tool_names = [
        (item.get("function") or {}).get("name") for item in env.mock.requests[0].get("tools") or []
    ]
    assert "report_findings" in tool_names

    review_events = [
        event for event in evidence["timeline"] if event["type"] == "review.findings"
    ]
    assert len(review_events) == 1
    payload = review_events[0]["payload"]
    assert payload["count"] == 1
    assert payload["level"] == "medium"
    assert payload["findings"][0]["file"] == "main.py"

    completed = [
        event
        for event in evidence["timeline"]
        if event["type"] == "tool.completed" and event["payload"].get("tool") == "report_findings"
    ]
    assert len(completed) == 1
    assert completed[0]["payload"]["summary"] == "已上报 1 条审查发现。"

    rows = await env.tool_calls(task_id)
    assert [row.name for row in rows] == ["report_findings"]
    artifact = ArtifactStore().load(task_id, rows[0].id)
    assert artifact["data"]["findings"][0]["summary"] == "守卫被删掉后空值直接进计算"

    # 事件只落一条：再写第二条会让时间线出现两份互相对不上的清单。
    async with get_session_factory()() as session:
        stored = (
            await session.execute(
                AgentEvent.__table__.select()
                .where(AgentEvent.task_id == task_id)
                .where(AgentEvent.type == "review.findings")
            )
        ).all()
    assert len(stored) == 1
