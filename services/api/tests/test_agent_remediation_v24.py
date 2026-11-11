"""环境缺口补救：缺一条命令不是结论，是要做的工作。

真实读数（安装版 blank.db）：

- 任务 4378855e…（43 步）：`where node` / `node --version` 回执是
  `'node' 不是内部或外部命令`；`where /R C:\\ node.exe`（找现成安装）被空转断路器
  误拦；最后交付「需要你：装上 Node 后重跑 `npm test`」。
- 任务 6ce4cc16…：同一类诉求被判成 advise，只读模式下 run_shell 全被拒，
  它连「去装」这一步都调不出来。

这里的判据不是"提示词写了没有"，而是执行节点会不会把这种收尾推回一轮：
三条同时成立才算缺口——有真实缺失回执、答复是把活儿交回用户、没有任何一次
安装/下载尝试。上限两次，之后允许如实收尾（断网、没权限也是结论）。
"""

from __future__ import annotations

import json
import uuid

import pytest

from blank_api.db import get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent import remediation
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import (
    MAX_REMEDIATION_ROUNDS,
    AgentSession,
    execute_action_node,
    route_after_act,
)
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome
from blank_api.services.tools.gateway import ToolGateway

TASK_ID = "task-remediation-1"

# 真实交付原文（任务 4378855e…）：前半句说"等价脚本跑通了"，最后一句还是把
# 装 Node 这件事交回用户。前者不该被判成交回，后者必须。
HANDOFF = "完成：本机无 Node，`npm test` 未执行；等价脚本已跑通\n需要你：装上 Node 后重跑 `npm test`"

# 真实回执（中文 Windows；GBK 落库后中文已损坏，单引号里的命令名仍可认）。
NODE_MISSING = "'node' 不是内部或外部命令，也不是可运行的程序\n或批处理文件。"


def _call(name: str, arguments: dict) -> dict:
    return {
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def _shell_tool(output: str, ok: bool) -> ToolDefinition:
    async def handler(context: ToolContext, arguments: dict) -> ToolOutcome:
        return ToolOutcome(ok=ok, output=output)

    return ToolDefinition(
        name="run_shell",
        description="测试用命令执行",
        input_schema={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
        risk_level="low",
        handler=handler,
        timeout_seconds=5.0,
    )


async def _seed_task() -> None:
    async with get_session_factory()() as session:
        session.add(Project(id="project-remediation", name="补救测试"))
        session.add(
            Task(
                id=TASK_ID,
                project_id="project-remediation",
                title="装环境",
                goal="为什么你看到没有后你不想办法自己装呢",
                max_steps=20,
            )
        )
        await session.commit()


@pytest.fixture
async def seeded():
    from blank_api.db import Base, get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await _seed_task()


def _session(tmp_path, *, reply: str, tool: ToolDefinition | None = None) -> AgentSession:
    session = AgentSession(
        adapter=None,  # type: ignore[arg-type]  # 本用例不走到模型调用
        gateway=ToolGateway([tool] if tool else []),
        context=ToolContext(
            root=tmp_path,
            task_id=TASK_ID,
            session_factory=get_session_factory(),
            approval_policy=lambda *_: True,
        ),
        recorder=EventRecorder(get_session_factory(), TASK_ID),
        session_factory=get_session_factory(),
        provider_id="provider-1",
        provider_name="mock",
        goal="为什么你看到没有后你不想办法自己装呢",
        max_steps=20,
    )

    async def fake_ask(state, prompt, node, *, messages=None):
        return {
            "text": reply,
            "tool_calls": [],
            "usage": {},
            "messages": list(messages or []) + [{"role": "assistant", "content": reply}],
        }

    session.ask = fake_ask  # type: ignore[method-assign]
    return session


async def _events() -> list[tuple[str, dict]]:
    async with get_session_factory()() as session:
        rows = (await session.execute(AgentEvent.__table__.select())).fetchall()
    return [(row.type, row.payload or {}) for row in rows]


def _state(**extra) -> dict:
    state = {
        "messages": [{"role": "user", "content": "为什么你看到没有后你不想办法自己装呢"}],
        "steps": 0,
    }
    state.update(extra)
    return state


# ── 1. 判据本身：三条同时成立才算缺口 ──────────────────────────────


class TestGapVerdict:
    def test_real_delivery_is_a_handoff(self) -> None:
        assert remediation.is_handoff(HANDOFF) is True

    def test_a_completed_workaround_is_not_a_handoff(self) -> None:
        """「本机没有 node，所以改用等价脚本并跑通」是完成，不是交回。

        误判成收尾会把一次已经做完的交付再推一轮，代价比漏判大。
        """
        assert remediation.is_handoff("本机没有 node，改用 PowerShell 等价脚本，两项检查都跑通了") is False

    def test_install_attempts_are_recognized(self) -> None:
        for command in (
            "winget install OpenJS.NodeJS.LTS",
            "choco install nodejs -y",
            "Invoke-WebRequest https://nodejs.org/dist/v24.20.0/node-v24.20.0-win-x64.zip -OutFile n.zip",
            "msiexec /i node.msi /qn",
        ):
            assert remediation.is_obtain_attempt(command) is True, command

    def test_confirmation_is_not_an_install_attempt(self) -> None:
        # 确认不是补齐：把确认算成尝试，这条闸门永远不生效。
        for command in ("where node", "dir /s /b node.exe", 'if exist "%LOCALAPPDATA%\\Programs\\nodejs\\node.exe" echo FOUND'):
            assert remediation.is_obtain_attempt(command) is False, command

    def test_gap_needs_all_three(self) -> None:
        commands = ["where node", "node --version"]
        assert remediation.gap_reason(answer=HANDOFF, commands=commands, missing={"node"}) == "node"
        # 有过安装尝试：交付里说"装不上"是诚实结论，不再推。
        assert (
            remediation.gap_reason(
                answer=HANDOFF,
                commands=[*commands, "winget install OpenJS.NodeJS.LTS"],
                missing={"node"},
            )
            == ""
        )
        # 不是交回：任务确实做完了。
        assert remediation.gap_reason(answer="等价脚本跑通了", commands=commands, missing={"node"}) == ""
        # 没有真实缺失证据：模型自己说的「没装」不算。
        assert remediation.gap_reason(answer=HANDOFF, commands=commands, missing=set()) == ""


# ── 2. 验证通道：补齐后必须走 run_tests，run_shell 跑通不算 ────────


class TestEvidenceChannel:
    """真实模型两次跑出同一形状（2026-11-11，:8061）：

    补装 Node 后用 `run_shell` 跑通 `npm test`（退出码 0），交付写「测试通过」；
    但验证闸门只采信 run_tests，而最后一次 run_tests 失败/跳过，于是任务被判
    「交付文本声称测试通过，实际验证结果不支持」。三处上下文都要把通道写清楚，
    否则模型会一直拿 run_shell 当证据。
    """

    def test_prompt_names_run_tests_as_the_evidence_channel(self) -> None:
        from blank_api.services.agent.graph import SYSTEM_PROMPT

        assert "用 run_tests 重跑" in SYSTEM_PROMPT
        assert "同一条 run_tests 命令" in SYSTEM_PROMPT
        assert "run_shell 里跑通不算验证证据" in SYSTEM_PROMPT

    def test_remediation_hint_keeps_the_channel(self) -> None:
        text = remediation.hint("node", 1)
        assert "run_tests" in text
        assert "同一条命令" in text
        assert "run_shell 里跑通不算验证证据" in text


# ── 3. 执行节点：什么情况下推回、什么情况下不推 ────────────────────


async def test_missing_command_then_handoff_is_pushed_back(seeded, tmp_path):
    """真实链路：node 不存在 → 模型交付「需要你装」→ 推回并给出补齐顺序。"""
    session = _session(tmp_path, reply=HANDOFF, tool=_shell_tool(NODE_MISSING, ok=False))

    result = await execute_action_node(
        _state(pending=[_call("run_shell", {"command": "node --version"})]),
        session,
    )

    # 缺失证据来自工具回执，不是模型自述。
    assert "node" in session.stall.missing_commands
    # 这条命令行被记下来，供下一轮判断"有没有试过安装"。
    assert result["shell_commands"] == ["node --version"]
    assert result["gap_continue"] is True
    assert result["remediation_rounds"] == 1
    assert result["pending"] == []

    # 补齐顺序走系统侧上下文：只进下一次模型调用，不改用户原话。
    note = session.hook_context[-1]
    assert note["event"] == "remediation.required"
    assert "node" in note["text"] and "winget" in note["text"]
    # 三步都要在：现场确认、找现成、重跑。
    assert "where node" in note["text"]
    assert "PATH" in note["text"]

    types = [item[0] for item in await _events()]
    assert "remediation.required" in types


async def test_install_attempt_allows_an_honest_handoff(seeded, tmp_path):
    """已经试过装（哪怕失败）就不再推：那是真装不上，不是没去做。"""
    session = _session(tmp_path, reply=HANDOFF, tool=_shell_tool(NODE_MISSING, ok=False))

    result = await execute_action_node(
        _state(pending=[_call("run_shell", {"command": "winget install OpenJS.NodeJS.LTS"})]),
        session,
    )

    assert result["gap_continue"] is False
    assert result["remediation_rounds"] == 0


async def test_readonly_research_never_pushes(seeded, tmp_path):
    """只读调研里推回也没用：run_shell 本来就被拒，只会空转一轮。"""
    session = _session(tmp_path, reply=HANDOFF)
    session.readonly = True
    session.stall.record(
        "run_shell",
        {"command": "node -v"},
        {"ok": False, "status": "error", "output": NODE_MISSING},
    )

    result = await execute_action_node(_state(readonly=True, pending=[]), session)

    assert result["gap_continue"] is False
    assert result["remediation_rounds"] == 0


async def test_round_limit_leaves_an_honest_handoff(seeded, tmp_path):
    """推满两次就停：真装不上也是一种结论，不能推成死循环。"""
    session = _session(tmp_path, reply=HANDOFF)
    session.stall.record(
        "run_shell",
        {"command": "node -v"},
        {"ok": False, "status": "error", "output": NODE_MISSING},
    )

    result = await execute_action_node(
        _state(pending=[], remediation_rounds=MAX_REMEDIATION_ROUNDS),
        session,
    )

    assert result["gap_continue"] is False
    assert result["remediation_rounds"] == MAX_REMEDIATION_ROUNDS


async def test_exhausted_budget_does_not_push(seeded, tmp_path):
    """预算已经用完时不能再推：那一轮跑不动工具，推了也是空转。"""
    session = _session(tmp_path, reply=HANDOFF)
    session.max_steps = 1
    session.stall.record(
        "run_shell",
        {"command": "node -v"},
        {"ok": False, "status": "error", "output": NODE_MISSING},
    )

    result = await execute_action_node(_state(steps=1, pending=[]), session)

    assert result["budget_exhausted"] is True
    assert result["gap_continue"] is False


def test_route_sends_gap_back_to_execute() -> None:
    assert route_after_act({"gap_continue": True}, None) == "execute_action"  # type: ignore[arg-type]
    assert route_after_act({"gap_continue": False, "pending": []}, None) == "run_validation"  # type: ignore[arg-type]
