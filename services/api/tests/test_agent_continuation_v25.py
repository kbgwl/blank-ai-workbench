"""任务延续契约：借鉴 Claude Code / Codex 的真实系统提示词，把「发现问题后继续干」钉住。

证据来源（均为抓取到的原文，不是二手描述）：

- Codex 0.154.0 系统提示词：*"keep going until the query is completely resolved, before
  ending your turn… Only terminate your turn when you are sure that the problem is solved.
  … Do NOT guess"*；*"Install dependencies into that environment with its package manager.
  In uv-managed environments, `uv pip install ...` remains the preferred path."*；
  *"If installation is not possible in this environment, tell the user which dependency is
  missing and how to install it into their active environment."*
- Claude Code 2.1.271 系统块：*"Report outcomes faithfully: if tests fail, say so with the
  output; if a step was skipped, say that"*；*"a denied call means the user declined it —
  adjust, don't retry verbatim"*。

这里只测三件能失败的事：

1. **缺口的证据不只来自文字**：run_tests 用 data.missing_test_runner 结构化写明缺哪个
   工具链，它不属于任何一条失败正则；不把它收进账本，「本机没有 go」之后交付
   「需要你装 Go」就永远不会被推回。
2. **项目依赖安装也算补齐尝试**：`pip install pytest` / `npm install` 真实发生过，
   却被当成"没试过"，等于把做对了的模型推回一轮。
3. **提示词契约存在且排在做事规则之前**：收尾条件、如实报告、拒绝语义、包管理器路径。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from blank_api.db import get_session_factory
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent import remediation
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.graph import AgentSession, SYSTEM_PROMPT, execute_action_node
from blank_api.services.agent.stall import StallBreaker
from blank_api.services.tools import testing as testing_mod
from blank_api.services.tools.base import ToolContext, ToolDefinition, ToolOutcome
from blank_api.services.tools.gateway import ToolGateway

TASK_ID = "task-continuation-1"

# 真实交付形状：先说了"做了什么"，最后还是把装工具链交给用户。
HANDOFF = "已完成代码检查。需要你：装上 Go 后重跑 `go test ./...`"

# 真实回执形状（testing.py 的 skipped 分支）：文字不是 "command not found"，
# 缺哪个工具链只写在 data.missing_test_runner 里。
GO_SKIPPED = {
    "ok": False,
    "status": "skipped",
    "message": "本机没有 go 工具链，测试没有执行（不是代码没通过）",
    "data": {"command": "go test ./... -count=1", "missing_test_runner": "go"},
}


def _call(name: str, arguments: dict) -> dict:
    return {
        "id": f"call_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def _run_tests_tool(payload: dict) -> ToolDefinition:
    async def handler(context: ToolContext, arguments: dict) -> ToolOutcome:
        return ToolOutcome(
            ok=bool(payload.get("ok")),
            status=str(payload.get("status") or ""),
            message=str(payload.get("message") or ""),
            output=str(payload.get("output") or ""),
            data=dict(payload.get("data") or {}),
        )

    return ToolDefinition(
        name="run_tests",
        description="测试用测试执行",
        input_schema={"type": "object", "properties": {}},
        risk_level="low",
        handler=handler,
        timeout_seconds=5.0,
    )


async def _seed_task() -> None:
    async with get_session_factory()() as session:
        session.add(Project(id="project-continuation", name="延续测试"))
        session.add(
            Task(
                id=TASK_ID,
                project_id="project-continuation",
                title="跑测试",
                goal="修好这个项目并确认测试能通过",
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


def _session(tmp_path, *, reply: str, tool: ToolDefinition) -> AgentSession:
    session = AgentSession(
        adapter=None,  # type: ignore[arg-type]  # 本用例不走到模型调用
        gateway=ToolGateway([tool]),
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
        goal="修好这个项目并确认测试能通过",
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


def _state(**extra) -> dict:
    state = {
        "messages": [{"role": "user", "content": "修好这个项目并确认测试能通过"}],
        "steps": 0,
    }
    state.update(extra)
    return state


async def _event_types() -> list[str]:
    async with get_session_factory()() as session:
        rows = (await session.execute(AgentEvent.__table__.select())).fetchall()
    return [row.type for row in rows]


# ── 1. 结构化缺口：文字正则认不出，data 里有 ──────────────────────


class TestStructuredEvidence:
    def test_run_tests_skip_is_recorded_as_missing_tool(self) -> None:
        breaker = StallBreaker()
        breaker.record("run_tests", {}, GO_SKIPPED)

        assert breaker.missing_commands == {"go"}
        # 与文字判据分开记：测试跑通之后要能整批撤掉。
        assert breaker.structured_missing == {"go"}

    def test_missing_evidence_is_only_from_data(self) -> None:
        """模型自己说的"本机没有 go"不算证据；工具回执里的名字才算。"""
        breaker = StallBreaker()
        breaker.record(
            "run_tests",
            {},
            {
                "ok": False,
                "status": "skipped",
                "message": "本机没有 go 工具链，测试没有执行（不是代码没通过）",
                "data": {"skipped_reason": "本机没有 go 工具链"},
            },
        )

        assert breaker.missing_commands == set()

    def test_successful_run_clears_structured_evidence(self) -> None:
        breaker = StallBreaker()
        breaker.record("run_tests", {}, GO_SKIPPED)
        breaker.record("run_tests", {}, {"ok": True, "status": "ok", "output": "ok"})

        assert breaker.missing_commands == set()
        assert breaker.structured_missing == set()


# ── 2. 安装项目依赖也算补齐尝试（Codex 的 uv/pip 路径） ──────────────


class TestObtainAttempts:
    def test_project_package_managers_count_as_attempts(self) -> None:
        for command in (
            "python -m pip install pytest",
            "pip install -r requirements.txt",
            "uv pip install -r requirements.txt",
            "poetry add pytest",
            "npm install",
            "pnpm install --frozen-lockfile",
            "yarn add axios",
            "go install golang.org/x/tools/cmd/goimports@latest",
            "dotnet tool install --global dotnet-ef",
        ):
            assert remediation.is_obtain_attempt(command) is True, command

    def test_confirmation_is_still_not_an_attempt(self) -> None:
        for command in ("where go", "go version", "npm --version", "Get-Command node"):
            assert remediation.is_obtain_attempt(command) is False, command

    def test_handoff_is_not_pushed_after_pip_install(self) -> None:
        assert (
            remediation.gap_reason(
                answer=HANDOFF,
                commands=["go version", "python -m pip install pytest"],
                missing={"pytest"},
            )
            == ""
        )


# ── 3. 真实链路：缺工具链 → 交付「需要你装」→ 推回执行 ────────────────


async def test_go_skip_then_handoff_is_pushed_back(seeded, tmp_path):
    session = _session(tmp_path, reply=HANDOFF, tool=_run_tests_tool(GO_SKIPPED))

    result = await execute_action_node(_state(pending=[_call("run_tests", {})]), session)

    # 缺口来自工具回执的结构化字段，不是模型转述。
    assert session.stall.missing_commands == {"go"}
    assert result["gap_continue"] is True
    assert result["remediation_rounds"] == 1
    # 系统侧补齐指令进下一次模型调用，且带上结构化缺口。
    note = session.hook_context[-1]
    assert note["event"] == "remediation.required"
    assert "go" in note["text"]
    assert "winget" in note["text"] and "pip install" in note["text"]
    assert "remediation.required" in await _event_types()


async def test_successful_tests_do_not_trigger_the_gate(seeded, tmp_path):
    """测试真跑通了就不该再推：缺口证据必须能随成功撤回。

    交付文本仍故意用交回句式——判据是证据，不是措辞，所以不该因为一句话又推一轮。
    """
    tool = _run_tests_tool({"ok": True, "status": "ok", "output": "ok"})
    session = _session(tmp_path, reply=HANDOFF, tool=tool)

    result = await execute_action_node(_state(pending=[_call("run_tests", {})]), session)

    assert result["gap_continue"] is False
    assert session.stall.missing_commands == set()


# ── 4. 工具回执自己带补齐路径（Codex 的"tell the user how"） ─────────


async def test_missing_toolchain_receipt_carries_the_way_out(tmp_path: Path, monkeypatch):
    root = tmp_path / "goproj"
    root.mkdir()
    (root / "go.mod").write_text("module example.com/strutil\n\ngo 1.23\n", encoding="utf-8")
    monkeypatch.setattr(testing_mod, "policy_gate", lambda *a, **k: None)
    monkeypatch.setattr(testing_mod.shutil, "which", lambda name: None)

    async def _never(argv, *args, **kwargs):
        raise AssertionError("缺少工具链时不该起进程")

    monkeypatch.setattr(testing_mod, "execute_command", _never)
    context = ToolContext(root=root, task_id="t-go-receipt", session_factory=None)

    outcome = await testing_mod._run_tests(context, {})

    assert outcome.status == "skipped"
    assert outcome.data["missing_test_runner"] == "go"
    # 结论（skipped_reason）保持原样：它是证据，不改口径。
    assert "本机没有" in outcome.data["skipped_reason"]
    # 回执本身给出补齐顺序，而不是只报"缺"。
    assert "winget" in outcome.message
    assert "PATH" in outcome.message
    assert "告诉用户" in outcome.message


# ── 5. 提示词契约：抄自两家原文，且排在做事规则之前 ──────────────────


class TestPromptContract:
    def test_contract_precedes_task_rules(self) -> None:
        assert SYSTEM_PROMPT.index("收尾条件") < SYSTEM_PROMPT.index("做事规则")

    def test_keep_going_and_faithful_reporting(self) -> None:
        assert "只有问题真正解决" in SYSTEM_PROMPT
        assert "跳过的步骤就说跳过" in SYSTEM_PROMPT
        assert "不要把没做的写成做完了" in SYSTEM_PROMPT

    def test_denial_is_not_a_retry_signal(self) -> None:
        assert "拒绝是结论" in SYSTEM_PROMPT

    def test_project_dependency_managers_are_named(self) -> None:
        assert "npm install" in SYSTEM_PROMPT
        assert "uv pip install" in SYSTEM_PROMPT
        assert "pip install -r requirements.txt" in SYSTEM_PROMPT


# ── 6. 只看不动手：计划当回答交回来时推回执行 ────────────────────────

EDIT_PLAN = {
    "steps": [
        {"id": "step-1", "title": "创建 整理/文本 目录", "kind": "edit"},
        {"id": "step-2", "title": "复制文本文件并写整理说明", "kind": "edit"},
    ]
}


async def test_advice_only_reply_is_pushed_back_to_action(seeded, tmp_path):
    """真实失败样本：读了三个文件就交回「可以做。需要新建……」。

    计划里明明有 edit 步骤，模型却一个都没执行；这种回复不能当交付，
    要推回一轮并给出「直接调用工具」的系统指令。
    """
    session = _session(tmp_path, reply="可以做。需要新建 整理/文本/ 与 整理说明.md。", tool=_run_tests_tool({}))

    result = await execute_action_node(_state(plan_manifest=EDIT_PLAN), session)

    assert result["act_continue"] is True
    assert result["act_nudges"] == 1
    assert session.hook_context[-1]["event"] == "plan.incomplete"
    assert "直接调用工具" in session.hook_context[-1]["text"]
    assert "plan.incomplete" in await _event_types()


async def test_real_action_does_not_trigger_the_push(seeded, tmp_path):
    """已经写过文件/跑过命令就不再催：那是做了一半，交给校验回路。"""
    session = _session(tmp_path, reply="已创建。", tool=_run_tests_tool({}))
    state = _state(
        plan_manifest=EDIT_PLAN,
        tool_results=[{"tool": "write_file", "ok": True}],
    )

    result = await execute_action_node(state, session)

    assert result["act_continue"] is False
    assert result["act_nudges"] == 0


async def test_push_is_capped_and_skipped_for_readonly(seeded, tmp_path):
    """催两次仍不动手就允许收尾；只读任务永远不催。"""
    session = _session(tmp_path, reply="可以做。", tool=_run_tests_tool({}))

    capped = await execute_action_node(_state(plan_manifest=EDIT_PLAN, act_nudges=2), session)
    readonly = await execute_action_node(_state(plan_manifest=EDIT_PLAN, readonly=True), session)

    assert capped["act_continue"] is False
    assert readonly["act_continue"] is False
