"""M3：文件工具、子代理、团队、MCP 进同一套执行边界。

M2 把命令类工具钉在了三档沙箱上，但"M3 退出条件"要的是**整条执行面**都能回答
两个问题：这次调用按哪一档跑、实际落在哪一层边界。这里逐条给可失败的判据：

* 档位排序：子级/续跑只能收紧，不许变宽（`off > workspace-net > workspace`）；
* 运行时入口：同一任务第二次运行换到更宽的档位必须 409，且不产生新的 `task.started`；
* 子代理：`run_subagent(parent_tier=...)` 收到更宽的档位直接失败；用量行带父级档位；
* MCP：桥接工具在宿主进程里执行，状态与证据包必须把这条边界说出来，不许混进"边界内"；
* 每个 `tool.completed` 都带 `sandbox_mode` 与 `boundary_kind`，证据包按种类计数；
* 团队群档位落库并传给每个成员的 Task。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.test_agent_v04 import (  # noqa: F401
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)
from tests.test_subagent_usage_v18 import ScriptedAdapter, _response, _tool_call

from blank_api.db import get_session_factory
from blank_api.models.model_provider import ModelProvider, ModelUsage
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.evidence import build_evidence
from blank_api.services.agent.usage import account_call
from blank_api.services.mcp.bridge import (
    STATE_FAILED,
    STATE_READY,
    STATE_SKIPPED,
    MCPBundle,
    MCPStatus,
)
from blank_api.services.models.base import Usage
from blank_api.services.sandbox.tiers import TierDowngrade, assert_not_looser
from blank_api.services.subagent import SubAgentRequest, run_subagent
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome, ok
from blank_api.services.tools.gateway import ToolGateway, sandbox_backend_event_field

TASK_ID = "task-m3-boundary"
PROVIDER_ID = "provider-m3"


async def _seed(with_provider: bool = False) -> None:
    async with get_session_factory()() as session:
        session.add(Project(id="project-m3", name="M3 边界"))
        if with_provider:
            session.add(
                ModelProvider(
                    id=PROVIDER_ID,
                    name="mock",
                    kind="openai_compatible",
                    base_url="https://example.invalid",
                    default_model="mock",
                    supports_tools=True,
                )
            )
        session.add(Task(id=TASK_ID, project_id="project-m3", title="边界", goal="边界", max_steps=8))
        await session.commit()


def _context(root: Path, tier: str = "workspace") -> ToolContext:
    return ToolContext(
        root=root, task_id=TASK_ID, session_factory=get_session_factory(), sandbox_tier=tier
    )


# --------------------------------------------------------------------------- #
# 档位排序
# --------------------------------------------------------------------------- #
def test_child_tier_may_only_tighten():
    assert assert_not_looser("workspace", "workspace") == "workspace"
    # 收紧：off（宿主）→ workspace（Job）→ workspace-net 都是合法的。
    assert assert_not_looser("off", "workspace") == "workspace"
    assert assert_not_looser("off", "workspace-net") == "workspace-net"
    assert assert_not_looser("workspace-net", "workspace") == "workspace"
    # 变宽：这是越权，不是配置错误。
    for parent, child in (
        ("workspace", "workspace-net"),
        ("workspace", "off"),
        ("workspace-net", "off"),
    ):
        with pytest.raises(TierDowngrade):
            assert_not_looser(parent, child)
    # 当时没记（空串）不冒充知道：父级空时放行并规范化子级，子级空时退回默认档。
    assert assert_not_looser("", "off") == "off"
    assert assert_not_looser("off", "") == "workspace"


async def test_second_run_cannot_widen_the_task_tier(env, workspace):  # noqa: F811
    """同一任务第二次运行换到更宽的档位：409，且不能留下第二次 task.started。"""
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读 README"])
    script = [
        {"content": "执行计划：\n1. 读文件"},
        {"content": "先看一眼文件", "tool_calls": [call("read_file", {"path": "README.md"})]},
        {"content": "已读到 README 第一行"},
    ]

    env.mock.reset(script)
    first = await env.client.post(
        f"{API}/tasks/{task_id}/run",
        json={"provider_id": provider_id, "sandbox_tier": "workspace"},
    )
    assert first.status_code == 200, first.text
    assert await _started_events(task_id) == 1

    widened = await env.client.post(
        f"{API}/tasks/{task_id}/run",
        json={"provider_id": provider_id, "sandbox_tier": "off"},
    )
    assert widened.status_code == 409, widened.text
    assert widened.json()["error"]["code"] == "sandbox_tier_downgrade"
    # 闸门在装配之前：被拒的那次一行事件都不该写。
    assert await _started_events(task_id) == 1

    # 阳性对照：沿用同一档位可以再跑，说明拒绝的是"变宽"而不是"重跑"。
    env.mock.reset(script)
    again = await env.client.post(
        f"{API}/tasks/{task_id}/run",
        json={"provider_id": provider_id, "sandbox_tier": "workspace"},
    )
    assert again.status_code == 200, again.text
    assert await _started_events(task_id) == 2


async def _started_events(task_id: str) -> int:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(AgentEvent).where(
                    AgentEvent.task_id == task_id, AgentEvent.type == "task.started"
                )
            )
        ).scalars().all()
    return len(rows)


# --------------------------------------------------------------------------- #
# 子代理
# --------------------------------------------------------------------------- #
async def test_subagent_refuses_a_wider_tier_before_spending_anything(env, tmp_path):  # noqa: F811
    await _seed()
    # 守卫在任何适配器调用之前：adapter=None 也能走到这里，证明确实是"先拦后跑"。
    result = await run_subagent(
        adapter=None,  # type: ignore[arg-type]
        tools=[],
        root=tmp_path,
        task_id=TASK_ID,
        session_factory=get_session_factory(),
        recorder=EventRecorder(get_session_factory(), TASK_ID),
        request=SubAgentRequest(goal="读 README"),
        agent_path="root/par-1",
        depth=1,
        parent_thread_id="root-thread",
        sandbox_tier="off",
        parent_tier="workspace",
    )
    assert result.ok is False
    assert "不得比父级更宽" in result.error


async def test_subagent_usage_rows_carry_the_inherited_tier(env, tmp_path):  # noqa: F811
    await _seed(with_provider=True)
    adapter = ScriptedAdapter(
        "mock",
        [
            _response("看一眼", Usage(prompt_tokens=10, completion_tokens=2), [_tool_call("read_file")]),
            _response("只有一行", Usage(prompt_tokens=20, completion_tokens=3)),
        ],
    )
    result = await run_subagent(
        adapter=adapter,
        tools=[],
        root=tmp_path,
        task_id=TASK_ID,
        session_factory=get_session_factory(),
        recorder=EventRecorder(get_session_factory(), TASK_ID),
        request=SubAgentRequest(goal="读 README", max_steps=4),
        agent_path="root/sub-1",
        depth=1,
        parent_thread_id="root-thread",
        provider_id=PROVIDER_ID,
        provider_name="mock",
        sandbox_tier="off",
        parent_tier="off",
    )
    assert result.ok, result.error
    async with get_session_factory()() as session:
        rows = (
            await session.execute(select(ModelUsage).where(ModelUsage.task_id == TASK_ID))
        ).scalars().all()
        events = (
            await session.execute(
                select(AgentEvent).where(
                    AgentEvent.task_id == TASK_ID, AgentEvent.type == "model.usage"
                )
            )
        ).scalars().all()
    assert [row.sandbox_mode for row in rows] == ["off", "off"]
    assert [event.payload["attribution"]["sandbox_mode"] for event in events] == ["off", "off"]
    evidence = await build_evidence(get_session_factory(), TASK_ID)
    assert evidence["usage"]["sandbox_modes"] == ["off"]
    assert evidence["usage"]["sandbox_mode_unattributed_calls"] == 0


# --------------------------------------------------------------------------- #
# 每个工具调用的边界
# --------------------------------------------------------------------------- #
def _boundary_tools() -> list[ToolDefinition]:
    async def ping(context, arguments):
        return ok("pong")

    async def blocked(context, arguments):
        return ToolOutcome(
            ok=False,
            status="denied",
            message="档位不可用",
            data={
                "tier_blocked": True,
                "enforcement": {"tier_notice": "workspace-net 不提供：本机无法强制网络边界"},
            },
        )

    return [
        ToolDefinition(
            name="mcp__demo__ping",
            description="外部 MCP 工具",
            input_schema={"type": "object"},
            risk_level="low",
            handler=ping,
        ),
        ToolDefinition(
            name="blocked_cmd",
            description="档位不可用时的命令工具",
            input_schema={"type": "object"},
            risk_level="low",
            handler=blocked,
        ),
    ]


async def test_every_tool_call_carries_mode_and_boundary_kind(env, workspace):  # noqa: F811
    await _seed()
    gateway = ToolGateway([*build_default_tools(), *_boundary_tools()])
    context = _context(workspace)
    cases = {
        "read_file": {"path": "README.md"},
        "run_shell": {"command": "echo m3"},
        "mcp__demo__ping": {},
        "blocked_cmd": {},
    }
    payloads = {name: await gateway.execute(name, arguments, context, "act") for name, arguments in cases.items()}

    assert payloads["read_file"]["boundary_kind"] == "in_process"
    assert payloads["run_shell"]["boundary_kind"] == "process"
    assert payloads["mcp__demo__ping"]["boundary_kind"] == "mcp_host_process"
    assert payloads["blocked_cmd"]["boundary_kind"] == "blocked"
    for payload in payloads.values():
        assert payload["sandbox_mode"] == "workspace"
        fields = sandbox_backend_event_field(payload)
        assert fields["boundary_kind"] == payload["boundary_kind"]
        assert fields["sandbox_mode"] == "workspace"
    # 被拒绝的那条：命令没起，但"为什么没起"必须进事件。
    assert payloads["blocked_cmd"]["sandbox_blocked"]
    assert "sandbox_backend" not in payloads["blocked_cmd"]


async def test_evidence_rolls_up_boundaries_and_mcp_host_process(env, workspace):  # noqa: F811
    await _seed()
    recorder = EventRecorder(get_session_factory(), TASK_ID)
    await recorder.add(
        "task.started",
        "receive_task",
        {
            "sandbox": {
                "requested": "workspace-net",
                "tier": "workspace",
                "backend": "local",
                "strength": "job_only",
                "degraded": True,
                "filesystem_enforced": False,
                "network_enforced": False,
                "notice": "文件与网络边界未由系统强制",
            },
            "mcp": MCPBundle(
                statuses=[
                    MCPStatus("demo", "stdio", STATE_READY, tools=1, server_info="demo-mcp"),
                ]
            ).as_dict(),
        },
    )
    gateway = ToolGateway([*build_default_tools(), *_boundary_tools()])
    context = _context(workspace)
    emitted = 0
    for name, arguments in (
        ("read_file", {"path": "README.md"}),
        ("mcp__demo__ping", {}),
        ("blocked_cmd", {}),
    ):
        payload = await gateway.execute(name, arguments, context, "act")
        await recorder.add(
            "tool.completed",
            "act",
            {
                "tool": name,
                "status": payload.get("status"),
                "ok": payload.get("ok"),
                "tool_call_id": payload.get("tool_call_id"),
                **sandbox_backend_event_field(payload),
            },
        )
        emitted += 1
    assert emitted == 3

    evidence = await build_evidence(get_session_factory(), TASK_ID)
    boundaries = evidence["boundaries"]
    assert boundaries["sandbox"]["tier"] == "workspace"
    assert boundaries["sandbox"]["requested"] == "workspace-net"
    assert boundaries["sandbox"]["filesystem_enforced"] is False
    assert boundaries["mcp"]["boundary"] == "host_process"
    assert boundaries["mcp"]["calls"] == 1
    assert "宿主进程" in boundaries["mcp"]["note"]
    assert boundaries["tool_calls"] == {
        "blocked": 1,
        "mcp_host_process": 1,
        "process": 0,
        "in_process": 1,
        "unattributed": 0,
    }
    assert boundaries["sandbox_modes"] == ["workspace"]
    by_name = {row["name"]: row for row in evidence["tools"]}
    assert by_name["mcp__demo__ping"]["boundary_kind"] == "mcp_host_process"
    assert by_name["blocked_cmd"]["boundary_kind"] == "blocked"
    assert by_name["read_file"]["sandbox_mode"] == "workspace"


# --------------------------------------------------------------------------- #
# MCP 边界事实
# --------------------------------------------------------------------------- #
def test_mcp_status_reports_where_the_server_runs():
    ready = MCPStatus("demo", "stdio", STATE_READY, tools=3, server_info="demo-mcp").as_dict()
    assert ready["boundary"] == "host_process"
    assert "宿主进程" in ready["boundary_note"]
    # 没起进程、起没起来不知道，是两种不同的事实，不许都写成 host_process。
    assert MCPStatus("off", "stdio", STATE_SKIPPED).as_dict()["boundary"] == "none"
    assert MCPStatus("bad", "stdio", STATE_FAILED).as_dict()["boundary"] == "unknown"

    bundle = MCPBundle(statuses=[MCPStatus("demo", "stdio", STATE_READY)]).as_dict()
    assert bundle["boundary"] == "host_process"
    assert bundle["servers"][0]["boundary"] == "host_process"
    empty = MCPBundle().as_dict()
    assert empty["boundary"] == "none"


# --------------------------------------------------------------------------- #
# 用量账
# --------------------------------------------------------------------------- #
async def test_usage_row_and_attribution_event_record_the_mode(env):  # noqa: F811
    await _seed()
    recorder = EventRecorder(get_session_factory(), TASK_ID)
    await account_call(
        session_factory=get_session_factory(),
        recorder=recorder,
        task_id=TASK_ID,
        trace_id="thread-m3",
        provider_id="",
        provider_name="mock",
        node="act",
        role="coder",
        depth=0,
        agent_path="root",
        model="mock",
        provider="mock",
        usage=Usage(prompt_tokens=7, completion_tokens=3),
        latency_ms=12,
        status="ok",
        turn={},
        sandbox_mode="off",
    )
    async with get_session_factory()() as session:
        row = (
            await session.execute(select(ModelUsage).where(ModelUsage.task_id == TASK_ID))
        ).scalar_one()
        event = (
            await session.execute(
                select(AgentEvent).where(
                    AgentEvent.task_id == TASK_ID, AgentEvent.type == "model.usage"
                )
            )
        ).scalar_one()
    assert row.sandbox_mode == "off"
    assert event.payload["attribution"]["sandbox_mode"] == "off"
    evidence = await build_evidence(get_session_factory(), TASK_ID)
    assert evidence["usage"]["sandbox_modes"] == ["off"]
    # 老账（没记档位）必须单独计数，不许被读成 workspace。
    assert evidence["usage"]["sandbox_mode_unattributed_calls"] == 0


# --------------------------------------------------------------------------- #
# 迁移：模型加列不等于真实库有列
# --------------------------------------------------------------------------- #
def _alembic(args: list[str], db_path: Path) -> None:
    cwd = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["BLANK_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    environment["PYTHONIOENCODING"] = "utf-8"
    done = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "alembic", *args],
        cwd=str(cwd),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=240,
    )
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]


def _column_ddl(db: sqlite3.Connection, table: str, column: str) -> str:
    sql = db.execute(
        "select sql from sqlite_master where type='table' and name=?", (table,)
    ).fetchone()[0]
    for part in sql.replace("\n", " ").split(","):
        if part.strip().startswith(f"{column} "):
            return part.strip()
    return ""


def test_migration_0025_adds_boundary_columns_without_backfilling(tmp_path):
    """改了模型忘了写迁移，真实库上就是 no such column；老行也不许被补默认档位。"""
    db_path = tmp_path / "mig-m3.db"
    _alembic(["upgrade", "0024_events_origin_run_id"], db_path)
    db = sqlite3.connect(db_path)
    assert _column_ddl(db, "model_usage", "sandbox_mode") == ""
    assert _column_ddl(db, "team_groups", "sandbox_tier") == ""
    db.execute("insert into model_usage (id, provider_id, model) values ('u-old','p-gone','m-old')")
    db.execute("insert into team_groups (id, project_id, name) values ('g-old','p-gone','老群')")
    db.commit()
    db.close()

    _alembic(["upgrade", "head"], db_path)
    db = sqlite3.connect(db_path)
    usage_ddl = _column_ddl(db, "model_usage", "sandbox_mode").upper()
    group_ddl = _column_ddl(db, "team_groups", "sandbox_tier").upper()
    assert "VARCHAR(20)" in usage_ddl and "NOT NULL" in usage_ddl, usage_ddl
    assert "VARCHAR(20)" in group_ddl and "NOT NULL" in group_ddl, group_ddl
    assert db.execute("select sandbox_mode from model_usage where id='u-old'").fetchone()[0] == ""
    assert db.execute("select sandbox_tier from team_groups where id='g-old'").fetchone()[0] == ""
    db.close()

    _alembic(["downgrade", "0024_events_origin_run_id"], db_path)
    db = sqlite3.connect(db_path)
    assert _column_ddl(db, "model_usage", "sandbox_mode") == ""
    assert _column_ddl(db, "team_groups", "sandbox_tier") == ""
    assert db.execute("select count(*) from model_usage").fetchone()[0] == 1, "往返不许丢行"
    assert db.execute("select count(*) from team_groups").fetchone()[0] == 1
    db.close()
