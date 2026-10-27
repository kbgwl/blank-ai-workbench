"""#115：单次越界宿主执行（`run_shell(sandbox="host")`）。

对标 REA 拆解里的那条「模型可以请求一次宿主执行，但默认关闭、必须逐条人工批准、
且审计里数得出来」。本文件钉四层：

1. **判定**：部署开关关闭时任何模式都拒绝（连卡都不出）；打开后任何模式——包括
   bypass / approve_high_risk——都只能 ASK，前缀、工具档、手写 allow、guardian 都不代批。
2. **入口**：直接调 handler 也守同一道闸；批准是单次的，用掉即销账。
3. **执行**：真在宿主上跑，`sandbox_bypass=True`、`sandbox_backend=null`，限额全 false；
   沙箱后端不可用时也不会因为没有可用沙箱而偷偷放行（必须已有批准）。
4. **端到端**：批准一次 → 执行一次 → 第二次同样调用重新挂卡；事件与证据包计数一致，
   `doctor` 的 P-AUDIT 念得出「单次越界宿主执行 1 次」。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from blank_api.db import Base, get_engine, get_session_factory
from blank_api.doctor import UNKNOWN, VERIFIED, audit_evidence
from blank_api.models.project import Project
from blank_api.models.task import AgentEvent, Task
from blank_api.services.agent.events import EventRecorder
from blank_api.services.agent.runtime import AgentRuntime
from blank_api.services.approvals import approval_key
from blank_api.services.permission_rules import parse_rules
from blank_api.services.permissions import (
    ALLOW,
    ASK,
    DENY,
    MODE_ACCEPT_EDITS,
    MODE_BYPASS,
    MODE_DEFAULT,
    MODE_PLAN,
    MODES,
    PermissionPolicy,
    prefix_rule,
)
from blank_api.services.sandbox import boundary, runner
from blank_api.services.sandbox.boundary import Capability
from blank_api.services.sandbox.policy import host_bypass_enabled
from blank_api.services.sandbox.tiers import (
    HOST_BYPASS,
    TIER_OFF,
    TIER_WORKSPACE,
    host_bypass_requested,
)
from blank_api.services.tools import build_default_tools
from blank_api.services.tools.shell import _run_shell, execute_command

from tests.test_agent_v04 import (  # noqa: F401  (env 是夹具，故意导入)
    API,
    call,
    create_project,
    create_provider,
    create_task,
    env,
)

HOST_CALL = {"command": "echo bypass-ok", "sandbox": HOST_BYPASS}


def _tool(name: str = "run_shell"):
    return next(tool for tool in build_default_tools() if tool.name == name)


def _decide(policy: PermissionPolicy, arguments: dict | None = None):
    return policy.decide(_tool(), arguments or HOST_CALL, "medium")


@pytest.fixture
def job_only(monkeypatch):
    """这台开发机的实测形状：Job 可用、系统不管文件与网络（job_only）。"""
    cap = Capability(
        platform=sys.platform,
        job=True,
        restricted_token=False,
        token_control=True,
        appcontainer=False,
        carrier_control=True,
        measured_at=time.time(),
    )
    monkeypatch.setattr(boundary, "_CACHE", (time.time(), cap))
    return cap


@pytest.fixture
def local_sandbox(monkeypatch, job_only):
    """沙箱开着、允许本机回退、docker 固定为没有：档位解析得到 local。"""
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("BLANK_SANDBOX_ALLOW_LOCAL", "1")
    monkeypatch.setattr(runner, "shutil_which", lambda *_: None)
    return job_only


def _open_bypass(monkeypatch) -> None:
    monkeypatch.setenv("BLANK_SANDBOX_ALLOW_HOST_BYPASS", "1")


def _close_bypass(monkeypatch) -> None:
    monkeypatch.delenv("BLANK_SANDBOX_ALLOW_HOST_BYPASS", raising=False)


# --------------------------------------------------------------------------- #
# 1. 判定
# --------------------------------------------------------------------------- #
def test_only_run_shell_with_the_exact_host_flag_requests_a_bypass():
    assert host_bypass_requested("run_shell", {"sandbox": "host"}) is True
    assert host_bypass_requested("run_shell", {"sandbox": " HOST "}) is True
    assert host_bypass_requested("run_shell", {"sandbox": "workspace"}) is False
    assert host_bypass_requested("run_shell", {}) is False
    # 别的工具传这个键不算：参数不是能力，只有 run_shell 有这个语义。
    assert host_bypass_requested("run_tests", {"sandbox": "host"}) is False
    assert host_bypass_requested("apply_patch", {"sandbox": "host"}) is False


def test_the_deployment_switch_defaults_to_off(monkeypatch):
    _close_bypass(monkeypatch)
    assert host_bypass_enabled() is False
    _open_bypass(monkeypatch)
    assert host_bypass_enabled() is True


@pytest.mark.parametrize("mode", MODES)
def test_switch_off_denies_every_mode_without_asking(mode, monkeypatch):
    """开关关闭时连审批卡都不该出现：否则用户点了允许，命令仍然不执行。"""
    _close_bypass(monkeypatch)
    verdict = _decide(PermissionPolicy(mode=mode))
    assert verdict.outcome == DENY, (mode, verdict)
    assert verdict.rule == "sandbox:host_bypass_disabled"
    assert "BLANK_SANDBOX_ALLOW_HOST_BYPASS" in verdict.reason


@pytest.mark.parametrize("mode", [MODE_DEFAULT, MODE_ACCEPT_EDITS, MODE_BYPASS])
def test_switch_on_still_asks_in_every_mode(mode, monkeypatch):
    """bypass 模式与 approve_high_risk 都不代批：沙箱是能力边界，不是审批节奏。

    计划模式不在这条参数里：它连问都不问，直接拒绝（见下一条）。
    """
    _open_bypass(monkeypatch)
    for policy in (
        PermissionPolicy(mode=mode),
        PermissionPolicy(mode=mode, approve_high_risk=True),
    ):
        verdict = _decide(policy)
        assert verdict.outcome == ASK, (mode, policy.approve_high_risk, verdict)
        assert verdict.rule == "sandbox:host_bypass"
        # 越界只能一条一条批：卡片上不给"总是允许"这一档。
        assert verdict.memo == ""


def test_plan_mode_still_refuses_instead_of_asking(monkeypatch):
    _open_bypass(monkeypatch)
    verdict = _decide(PermissionPolicy(mode=MODE_PLAN))
    assert verdict.outcome == DENY
    assert verdict.rule == "mode:plan"


def test_session_grants_rules_and_tool_scope_cannot_pre_approve_a_bypass(monkeypatch):
    _open_bypass(monkeypatch)
    policy = PermissionPolicy(mode=MODE_DEFAULT)
    policy.approved_tools.add((policy.sandbox_tier, "run_shell", ""))
    policy.approved_prefixes.add(prefix_rule("echo"))
    policy.persistent_prefixes.add(prefix_rule("echo"))
    ruleset = parse_rules(f"allow run_shell({HOST_CALL['command']})\n", source="test")
    assert ruleset.error == ""
    policy.ruleset = ruleset

    verdict = _decide(policy)
    assert verdict.outcome == ASK, "前缀/工具档/手写 allow 都不该为一次越界代批"
    assert verdict.rule == "sandbox:host_bypass"


def test_a_previously_applied_approval_is_exactly_one_shot(monkeypatch):
    _open_bypass(monkeypatch)
    policy = PermissionPolicy(mode=MODE_DEFAULT)
    policy.remember("run_shell", HOST_CALL, scope="once")
    assert _decide(policy).outcome == ALLOW

    # 用掉即销账：同一条命令再跑必须重新问。
    policy.approved_actions.discard(approval_key("run_shell", HOST_CALL))
    assert _decide(policy).outcome == ASK


def test_remember_refuses_to_widen_a_bypass_into_a_session_or_tool_grant(monkeypatch):
    _open_bypass(monkeypatch)
    for scope in ("prefix", "tool"):
        policy = PermissionPolicy(mode=MODE_DEFAULT)
        policy.remember("run_shell", HOST_CALL, scope=scope)
        assert policy.approved_prefixes == set()
        assert policy.approved_tools == set()
        # 只记了这一次动作本身：下一次带不同参数必须重新问。
        other = {"command": "echo other", "sandbox": HOST_BYPASS}
        assert _decide(policy, other).outcome == ASK


def test_denied_bypass_is_not_offered_again(monkeypatch):
    _open_bypass(monkeypatch)
    policy = PermissionPolicy(mode=MODE_DEFAULT)
    policy.forget("run_shell", HOST_CALL)
    verdict = _decide(policy)
    assert verdict.outcome == DENY and verdict.rule == "action:denied"


def test_irreversible_command_still_reports_the_bypass_reason(monkeypatch):
    """越界是用户要看见的那件事，不能被子命令的不可逆归因顶掉。"""
    _open_bypass(monkeypatch)
    verdict = _decide(
        PermissionPolicy(mode=MODE_BYPASS),
        {"command": "git push --force origin main", "sandbox": HOST_BYPASS},
    )
    assert verdict.outcome == ASK
    assert verdict.rule == "sandbox:host_bypass"


# --------------------------------------------------------------------------- #
# 2. 工具入口的第二道闸：没有批准就不执行
# --------------------------------------------------------------------------- #
def _context(
    root: Path,
    policy: PermissionPolicy,
    *,
    tier: str = TIER_WORKSPACE,
    session_factory=None,
    task_id: str = "task-v115",
):
    return SimpleNamespace(
        root=root,
        task_id=task_id,
        permissions=policy,
        sandbox_tier=tier,
        safety=None,
        shell=None,
        session_factory=session_factory,
        trace_id="thread-v115" if session_factory is not None else "",
    )


async def test_handler_refuses_when_the_switch_is_off(workspace: Path, monkeypatch) -> None:
    _close_bypass(monkeypatch)
    marker = workspace / "must-not-exist.txt"
    outcome = await _run_shell(
        _context(workspace, PermissionPolicy()),
        {"command": f'echo ran > "{marker}"', "sandbox": HOST_BYPASS},
    )
    assert outcome.ok is False and outcome.status == "denied"
    assert outcome.data["permission_rule"] == "sandbox:host_bypass_disabled"
    assert not marker.exists(), "开关关闭时命令仍然被执行了"


async def test_handler_refuses_without_an_approval(workspace: Path, monkeypatch) -> None:
    _open_bypass(monkeypatch)
    marker = workspace / "must-not-exist.txt"
    outcome = await _run_shell(
        _context(workspace, PermissionPolicy()),
        {"command": f'echo ran > "{marker}"', "sandbox": HOST_BYPASS},
    )
    assert outcome.ok is False and outcome.status == "denied"
    assert outcome.data["permission_rule"] == "sandbox:host_bypass"
    assert not marker.exists(), "没获批准的命令仍然被执行了"


async def test_handler_executes_once_and_consumes_the_approval(workspace: Path, local_sandbox, monkeypatch) -> None:
    _open_bypass(monkeypatch)
    policy = PermissionPolicy()
    policy.remember("run_shell", {"command": "echo bypass-ran", "sandbox": HOST_BYPASS}, scope="once")
    context = _context(workspace, policy)

    first = await _run_shell(context, {"command": "echo bypass-ran", "sandbox": HOST_BYPASS})
    assert first.ok is True, first
    assert first.data["sandbox_bypass"] is True
    assert first.data["bypass_reason"]
    assert approval_key("run_shell", {"command": "echo bypass-ran", "sandbox": HOST_BYPASS}) not in policy.approved_actions

    # 第二次没批准，必须拒绝且不执行。
    second = await _run_shell(context, {"command": "echo bypass-ran", "sandbox": HOST_BYPASS})
    assert second.ok is False and second.status == "denied"
    assert second.data["permission_rule"] == "sandbox:host_bypass"


# --------------------------------------------------------------------------- #
# 2b. 一次性批准的消费必须跨 resume 持久
# --------------------------------------------------------------------------- #
@pytest.fixture
async def seeded_task():
    """只够放事件与任务的库：回放判据要真读一次事件表，而不是看内存。"""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_session_factory()() as session:
        session.add(Project(id="project-v115", name="单次越界"))
        session.add(
            Task(
                id="task-v115-consumed",
                project_id="project-v115",
                title="消费",
                goal="消费",
                max_steps=3,
            )
        )
        await session.commit()
    return "task-v115-consumed"


async def test_rehydration_applies_the_consumption_after_the_grant(seeded_task, monkeypatch):
    """授权 + 消费同一条链：回放后这枚 key 不许复活，下一次必须重新问。"""
    _open_bypass(monkeypatch)
    key = approval_key("run_shell", HOST_CALL)
    recorder = EventRecorder(get_session_factory(), seeded_task)
    await recorder.add(
        "approval.granted",
        "execute_action",
        {"tool": "run_shell", "arguments": HOST_CALL, "scope": "once"},
    )
    await recorder.add(
        "approval.consumed",
        "execute_action",
        {"tool": "run_shell", "approval_key": key, "reason": "sandbox:host_bypass"},
    )

    policy = PermissionPolicy()
    await AgentRuntime(get_session_factory())._rehydrate(seeded_task, policy)
    assert key not in policy.approved_actions
    assert _decide(policy).outcome == ASK


async def test_rehydration_keeps_a_grant_that_was_never_consumed(seeded_task, monkeypatch):
    """对照格：批准后、执行前崩溃的任务，恢复后这一次仍然有效。"""
    _open_bypass(monkeypatch)
    await EventRecorder(get_session_factory(), seeded_task).add(
        "approval.granted",
        "execute_action",
        {"tool": "run_shell", "arguments": HOST_CALL, "scope": "once"},
    )

    policy = PermissionPolicy()
    await AgentRuntime(get_session_factory())._rehydrate(seeded_task, policy)
    assert _decide(policy).outcome == ALLOW


async def test_handler_records_the_consumption_and_it_survives_replay(
    seeded_task, workspace: Path, local_sandbox, monkeypatch
) -> None:
    """真实链路：批准事件先落、handler 执行时落消费，回放后这枚 key 不许复活。

    只断言 handler 写了事件还不够：授权事件也要在，否则「回放后不复活」在一条
    根本不回放授权的实现上同样成立，那条判据就是死的。
    """
    _open_bypass(monkeypatch)
    key = approval_key("run_shell", HOST_CALL)
    await EventRecorder(get_session_factory(), seeded_task).add(
        "approval.granted",
        "execute_action",
        {"tool": "run_shell", "arguments": HOST_CALL, "scope": "once"},
    )
    policy = PermissionPolicy()
    policy.remember("run_shell", HOST_CALL, scope="once")
    context = _context(
        workspace, policy, session_factory=get_session_factory(), task_id=seeded_task
    )

    outcome = await _run_shell(context, dict(HOST_CALL))
    assert outcome.ok is True, outcome
    assert key not in policy.approved_actions

    async with get_session_factory()() as session:
        rows = (
            (
                await session.execute(
                    select(AgentEvent).where(
                        AgentEvent.task_id == seeded_task,
                        AgentEvent.type == "approval.consumed",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, rows
    payload = rows[0].payload
    assert payload["tool"] == "run_shell"
    assert payload["approval_key"] == key

    # 换一个进程内重建的策略：授权 + 消费按 seq 回放，同一枚 key 不许复活。
    fresh = PermissionPolicy()
    await AgentRuntime(get_session_factory())._rehydrate(seeded_task, fresh)
    assert key not in fresh.approved_actions
    assert _decide(fresh).outcome == ASK


# --------------------------------------------------------------------------- #
# 3. 执行层：真在宿主上跑，且账本说清"越界"
# --------------------------------------------------------------------------- #
async def test_execute_command_makes_the_bypass_visible_in_the_account(local_sandbox, workspace: Path) -> None:
    result = await execute_command("echo bypass-ok", workspace, timeout=10, tier=TIER_WORKSPACE, host_bypass=True)
    assert result.get("tier_blocked") is None
    assert result["sandbox"] is None, "越界执行必须落在宿主上"
    assert result["sandbox_bypass"] is True
    assert "原档位 workspace" in result["bypass_reason"]
    assert "bypass-ok" in result["stdout"]
    # 越界执行不受 Job 约束：限额账一项都不许说自己生效了。
    enforcement = result["enforcement"]
    assert enforcement["limits_applied"] is False
    assert enforcement["memory_enforced"] is False
    assert enforcement["pids_enforced"] is False
    assert enforcement["tree_kill_enforced"] is False
    assert enforcement["sandbox_bypassed"] is True
    assert enforcement["host_execution"] is True


async def test_off_tier_is_not_misreported_as_a_bypass(local_sandbox, workspace: Path) -> None:
    """off 档本来就在宿主上跑：不能借参数造一次假越界。"""
    result = await execute_command("echo off-ok", workspace, timeout=10, tier=TIER_OFF, host_bypass=True)
    assert result["sandbox"] is None
    assert result["sandbox_bypass"] is False
    assert "本就在宿主上执行" in result["bypass_reason"]


async def test_bypass_still_runs_when_the_sandbox_backend_is_unavailable(
    local_sandbox, workspace: Path, monkeypatch
) -> None:
    """后端不可用时普通命令会被拒；已有批准的单次越界仍能执行，并如实记账。"""

    def _raise(policy):
        raise runner.SandboxUnavailable("模拟：本机探测不到 docker")

    monkeypatch.setattr(runner, "get_sandbox", _raise)
    result = await execute_command("echo rescue-ok", workspace, timeout=10, tier=TIER_WORKSPACE, host_bypass=True)
    assert result.get("tier_blocked") is None
    assert result["sandbox"] is None and result["sandbox_bypass"] is True
    assert "rescue-ok" in result["stdout"]

    # 对照：同一条命令没有批准时，后端不可用必须拒绝（不许退回宿主）。
    blocked = await execute_command("echo nope", workspace, timeout=10, tier=TIER_WORKSPACE)
    assert blocked["tier_blocked"] is True
    assert blocked["enforcement"]["limits_applied"] is False


# --------------------------------------------------------------------------- #
# 4. doctor 能数出来
# --------------------------------------------------------------------------- #
def _evidence(calls: int) -> dict:
    return {
        "tools": [
            {"name": "run_shell", "boundary_kind": "process", "sandbox_mode": "workspace"},
        ],
        "boundaries": {
            "tool_calls": {
                "blocked": 0,
                "mcp_host_process": 0,
                "process": 1,
                "in_process": 0,
                "unattributed": 0,
            },
            "mcp": {"boundary": "none"},
            "sandbox_modes": ["workspace"],
            "sandbox_bypass": {"calls": calls, "tool_call_ids": ["call-1"] if calls else []},
        },
    }


def test_doctor_counts_bypasses_and_refuses_broken_shapes():
    status, detail = audit_evidence(_evidence(1))
    assert status == VERIFIED and "单次越界宿主执行 1 次" in detail

    status, detail = audit_evidence(_evidence(0))
    assert status == VERIFIED and "单次越界宿主执行 0 次" in detail

    missing = _evidence(0)
    missing["boundaries"].pop("sandbox_bypass")
    status, detail = audit_evidence(missing)
    assert status == VERIFIED and "未记" in detail, "缺键是未记，不能读成 0"

    for broken in (
        None,
        {"calls": -1, "tool_call_ids": []},
        {"calls": True, "tool_call_ids": []},
        {"calls": 1, "tool_call_ids": "call-1"},
        {"calls": 1, "tool_call_ids": ["call-1", "call-1"]},
        {"calls": 1, "tool_call_ids": ["call-1", "call-2"]},
    ):
        payload = _evidence(0)
        payload["boundaries"]["sandbox_bypass"] = broken
        status, _detail = audit_evidence(payload)
        assert status == UNKNOWN, broken


# --------------------------------------------------------------------------- #
# 5. 端到端：批准一次 → 执行一次 → 第二次再挂卡
# --------------------------------------------------------------------------- #
def _payloads(events: list[dict], kind: str) -> list[dict]:
    return [event["payload"] for event in events if event["type"] == kind]


def _enable_runtime_sandbox(monkeypatch) -> None:
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("BLANK_SANDBOX_ALLOW_LOCAL", "1")
    _open_bypass(monkeypatch)


async def test_approved_bypass_runs_once_then_asks_again(env, workspace: Path, local_sandbox, monkeypatch) -> None:  # noqa: F811
    _enable_runtime_sandbox(monkeypatch)
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["跑一条宿主命令"])

    env.mock.reset(
        [
            {"content": "先确认环境。"},
            {"content": "请批准这次宿主执行", "tool_calls": [call("run_shell", dict(HOST_CALL))]},
            # 第一次批准后模型又请求同一条越界命令：单次授权已经销账，必须重新挂卡。
            {"content": "再跑一次", "tool_calls": [call("run_shell", dict(HOST_CALL))]},
            {"content": "收到，结束。"},
            {"content": "结束。"},
            {"content": "结束。"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "waiting_approval"

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    cards = _payloads(events, "approval.required")
    assert len(cards) == 1, events
    assert cards[0]["tool"] == "run_shell"
    assert cards[0]["permission_rule"] == "sandbox:host_bypass"
    assert cards[0]["arguments"]["sandbox"] == HOST_BYPASS
    assert [option["scope"] for option in cards[0]["approval_options"]] == ["once"]

    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={"approve": True, "provider_id": provider_id, "scope": "once"},
    )
    assert resp.status_code == 200, resp.text
    # 第一次执行完后模型又发起同一条调用，第二次必须重新挂卡。
    assert resp.json()["task"]["status"] == "waiting_approval", resp.json()["task"]

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    completed = [
        payload for payload in _payloads(events, "tool.completed") if payload.get("tool") == "run_shell"
    ]
    assert len(completed) == 1, completed
    executed = completed[0]
    assert executed["ok"] is True, executed
    assert executed["sandbox_bypass"] is True
    assert executed["sandbox_backend"] is None
    assert "原档位 workspace" in executed["sandbox_bypass_reason"]
    assert executed["permission"]["rule"] == "sandbox:host_bypass"

    cards = _payloads(events, "approval.required")
    assert len(cards) == 2, "第二次相同调用没有重新挂卡"

    # 驳回第二次，让任务收尾，证据包才完整（待审批的调用没有 tool.completed）。
    resp = await env.client.post(
        f"{API}/tasks/{task_id}/approval",
        json={"approve": False, "provider_id": provider_id, "scope": "once"},
    )
    assert resp.status_code == 200, resp.text

    evidence = (await env.client.get(f"{API}/tasks/{task_id}/evidence")).json()
    bypass = evidence["boundaries"]["sandbox_bypass"]
    assert bypass["calls"] == 1, (
        bypass,
        [(item.get("call_id"), item.get("tool_call_id"), item.get("sandbox_bypass")) for item in completed],
    )
    assert len(bypass["tool_call_ids"]) == 1, bypass
    executed_tool = next(item for item in evidence["tools"] if item["id"] == executed["tool_call_id"])
    assert executed_tool["sandbox_bypass"] is True
    assert executed_tool["sandbox_backend"] is None

    status, detail = audit_evidence(evidence)
    assert status == VERIFIED, detail
    assert "单次越界宿主执行 1 次" in detail


async def test_guardian_cannot_auto_approve_a_bypass(env, workspace: Path, local_sandbox, monkeypatch) -> None:  # noqa: F811
    """审查员说 allow 也不能替用户批一次越界：那绕的是沙箱能力边界。"""
    _enable_runtime_sandbox(monkeypatch)
    monkeypatch.setenv("BLANK_GUARDIAN", "auto")
    project_id = await create_project(env, workspace)
    provider_id = await create_provider(env)
    task_id = await create_task(env, project_id, ["审查员别替我越界"])

    env.mock.reset(
        [
            {"content": "先看环境。"},
            {"content": "请求宿主执行", "tool_calls": [call("run_shell", dict(HOST_CALL))]},
            {"content": '{"verdict": "allow", "reasons": ["只是一条 echo"]}'},
            {"content": "结束。"},
            {"content": "结束。"},
        ]
    )
    resp = await env.client.post(f"{API}/tasks/{task_id}/run", json={"provider_id": provider_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "waiting_approval", resp.json()["task"]

    events = (await env.client.get(f"{API}/tasks/{task_id}/events")).json()
    reviewed = _payloads(events, "guardian.reviewed")
    assert reviewed and reviewed[-1]["outcome"] == "asked", reviewed
    assert _payloads(events, "guardian.auto_approved") == []
    card = _payloads(events, "approval.required")[0]
    assert card["tool"] == "run_shell"
    assert card["permission_rule"] == "sandbox:host_bypass"
    assert card["guardian"]["verdict"] == "allow"
