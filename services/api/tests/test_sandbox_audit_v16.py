"""执行边界必须进证据：这条命令跑在哪、事后能不能按任务还原。

对标 Codex rollout —— 每次调用都带着当时的沙箱口径，而不只在开场写一次。
四条各自能失败的事：

1. 沙箱开关关着时命令真的跑在宿主上，事件与证据包都得把这件事说出来；
   只读工具则不带这个字段。两种状态必须长得不一样，否则「宿主直连执行」
   —— 最该露出来的那一档 —— 就藏在一片缺字段里跟没发生过一样。
2. 后端名由工具写、网关只搬运：搬运错了或读成了 `isolation`（宿主会被写成
   "local"，看起来像"有一种沙箱叫 local"）这条就红。
3. 每一次过网关的调用都落一条审计账，被拒绝的也要落，按任务能还原调用链。
   这一条以前等于没测：AuditLog 唯一的用例挂在 test_sandbox_v07 的 docker
   skip 上，本机没 docker 就整组跳过，而运行时也没有任何地方往里写。
4. 主机绝对路径与运行时密钥在落盘前都要被换掉。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.evidence import build_evidence
from blank_api.services.sandbox import AuditLog
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.base import ToolContext, ToolDefinition, ok
from blank_api.services.tools.gateway import ToolGateway, sandbox_backend_event_field

from tests.test_agent_v04 import (  # noqa: F401  (复用阶段3 的夹具)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)

TASK_ID = "task-sandbox-audit-1"
SECRET = "sk-audit-secret-0123456789"
# 长得不像密钥的密钥：只有调用方把那份额外清单交给审计通道才挡得住，
# 靠 redact() 内置的 sk- 形态匹配抓不到它。
PLAIN_SECRET = "AuditTokenValue-42"


async def _seed() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="project-sa", name="执行边界"))
        session.add(
            Task(id=TASK_ID, project_id="project-sa", title="边界", goal="边界", max_steps=10)
        )
        await session.commit()


@pytest.fixture
async def seeded():
    await _seed()


@pytest.fixture
def audit_dir(tmp_path, monkeypatch):
    """审计账指向本用例自己的目录：跑测试不该往仓库里的 data/audit 堆记录。"""
    directory = tmp_path / "audit"
    monkeypatch.setenv("BLANK_AUDIT_DIR", str(directory))
    return directory


def _context(root: Path, secrets: tuple[str, ...] = ()) -> ToolContext:
    return ToolContext(
        root=root, task_id=TASK_ID, session_factory=get_session_factory(), extra_secrets=secrets
    )


def _entries(audit_dir: Path) -> list[dict]:
    return AuditLog(audit_dir).read(TASK_ID)


async def _commands_run_on_host(workspace: Path) -> dict:
    """conftest 关掉了沙箱，run_shell 的进程真的落在宿主上。"""
    return await ToolGateway(build_default_tools()).execute(
        "run_shell", {"command": "echo boundary"}, _context(workspace), "act"
    )


async def test_host_execution_is_stated_not_omitted(seeded, workspace, audit_dir):
    payload = await _commands_run_on_host(workspace)
    # 键必须在，值为 null：缺席＝没起进程，null＝起了但跑在宿主上。
    assert "sandbox_backend" in payload
    assert payload["sandbox_backend"] is None
    entry = _entries(audit_dir)[0]
    assert entry["tool"] == "run_shell"
    assert entry["detail"]["ran_command"] is True
    assert entry["detail"]["host_execution"] is True
    assert entry["backend"] is None
    assert entry["exit_code"] == 0


async def test_readonly_call_carries_no_boundary(seeded, workspace, audit_dir):
    payload = await ToolGateway(build_default_tools()).execute(
        "read_file", {"path": "README.md"}, _context(workspace), "act"
    )
    assert "sandbox_backend" not in payload
    assert sandbox_backend_event_field(payload) == {}
    entry = _entries(audit_dir)[0]
    assert entry["detail"]["ran_command"] is False
    assert entry["detail"]["host_execution"] is False


async def test_backend_name_is_carried_through_untouched(seeded, tmp_path, audit_dir):
    """后端名由命令工具写进 data["sandbox"]，网关只搬运。

    真后端的验收在 test_sandbox_v07（本机无 docker 时跳过）；这里钉的是搬运契约，
    所以用假工具，不假装本机有沙箱。
    """

    async def handler(context, arguments):
        return ok("done", exit_code=0, sandbox="win-native", isolation="win-native")

    tool = ToolDefinition(
        name="run_shell",
        description="d",
        input_schema={"type": "object"},
        risk_level="low",
        handler=handler,
    )
    payload = await ToolGateway([tool]).execute("run_shell", {"command": "whoami"}, _context(tmp_path), "act")
    assert payload["sandbox_backend"] == "win-native"
    entry = _entries(audit_dir)[0]
    assert entry["backend"] == "win-native"
    assert entry["detail"]["host_execution"] is False


async def test_denied_call_is_audited_and_chain_is_readable(seeded, workspace, audit_dir):
    gateway = ToolGateway(build_default_tools())
    context = _context(workspace)
    await gateway.execute("read_file", {"path": "README.md"}, context, "act")
    await gateway.refuse("run_shell", {"command": "del /q scratch.txt"}, context, "act", reason="用户驳回")
    log = AuditLog(audit_dir)
    entries = _entries(audit_dir)
    assert [entry["tool"] for entry in entries] == ["read_file", "run_shell"]
    assert entries[1]["status"] == "denied"
    # 被拒的调用没起进程：审计里它和"宿主上跑过"必须区分得开，否则看不出拦截生效了。
    assert entries[1]["detail"]["ran_command"] is False
    trace = log.trace(TASK_ID)
    assert len(trace) == 2
    assert "run_shell" in trace[1] and "del /q scratch.txt" in trace[1]


async def test_audit_lines_leave_out_host_paths_and_keys(seeded, workspace, audit_dir):
    context = _context(workspace, secrets=(SECRET, PLAIN_SECRET))
    await ToolGateway(build_default_tools()).execute(
        "run_shell",
        {"command": f'echo {workspace / "README.md"} {SECRET} {PLAIN_SECRET}'},
        context,
        "act",
    )
    raw = (audit_dir / "tool-calls.jsonl").read_text(encoding="utf-8")
    entry = _entries(audit_dir)[0]
    # 断言读的是解析出来的字段，不是文件里的原始字符串：路径序列化成 JSON 后
    # 反斜杠成对（C:\\Users\\…），拿真实路径去比对原始文本会永远"通过"。
    fields = [entry["action"], json.dumps(entry["detail"], ensure_ascii=False)]
    assert entry["workspace"].startswith("<workspace:")
    assert all("<workspace:" in text for text in fields)
    for text in fields + [raw]:
        assert str(workspace) not in text
        assert SECRET not in text
        assert PLAIN_SECRET not in text


async def test_evidence_says_where_each_command_ran(seeded, workspace, audit_dir):
    """证据包的工具清单里贴回执行边界：宿主跑的 run_shell 有 null，read_file 没有这一项。

    不给 ToolCall 表加列，靠 tool.completed 事件里的 tool_call_id 绑定 ——
    所以这条同时证明了"事件里确实带上了 id 和边界"。
    """
    gateway = ToolGateway(build_default_tools())
    recorder = EventRecorder(get_session_factory(), TASK_ID)
    for name, arguments in (("run_shell", {"command": "echo hi"}), ("read_file", {"path": "README.md"})):
        payload = await gateway.execute(name, arguments, _context(workspace), "act")
        await recorder.add(
            "tool.completed",
            "act",
            {
                "tool": name,
                "status": payload.get("status"),
                "ok": payload.get("ok"),
                "tool_call_id": payload["tool_call_id"],
                **sandbox_backend_event_field(payload),
            },
        )
    evidence = await build_evidence(get_session_factory(), TASK_ID)
    by_name = {row["name"]: row for row in evidence["tools"]}
    assert by_name["run_shell"]["sandbox_backend"] is None
    assert "sandbox_backend" not in by_name["read_file"]


async def test_the_graph_itself_puts_the_boundary_in_the_event(env, workspace):  # noqa: F811
    """整条图跑一遍：字段是 graph.py 那两个 emit 点写进事件的，不是单测里手抄的。

    少写一处 `**sandbox_backend_event_field(payload)`，这条就红。
    """
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["读 README 第一行"])
    env.mock.reset(
        [
            {"content": "执行计划：\n1. 读文件"},
            {"content": "先看一眼文件", "tool_calls": [call("read_file", {"path": "README.md"})]},
            {"content": "再确认行尾", "tool_calls": [call("run_shell", {"command": "echo done"})]},
            {"content": "已读到 README 第一行"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(AgentEvent)
                .where(AgentEvent.task_id == task_id, AgentEvent.type == "tool.completed")
                .order_by(AgentEvent.seq)
            )
        ).scalars().all()
    events = {row.payload.get("tool"): row.payload for row in rows if isinstance(row.payload, dict)}
    assert "sandbox_backend" not in events["read_file"]
    assert events["run_shell"]["sandbox_backend"] is None
    # 运行时真的在往审计账上写（不是只有单测手工构造记录）：一条只读、一条宿主命令。
    entries = AuditLog(Path(os.environ["BLANK_AUDIT_DIR"])).read(task_id)
    assert [entry["tool"] for entry in entries] == ["read_file", "run_shell"]
    assert entries[1]["detail"]["host_execution"] is True
