"""`blank doctor --sandbox` 的判据：探针结论必须能失败，账本与行为必须同源。

这里分两层：
- 纯函数层：拒绝/超时/对照组失败各自归到哪个状态。装置自检失败时返回 UNKNOWN，
  不允许把"脚本写坏了"读成"边界拦住了"；
- 真执行层（Windows）：在本机 job_only 边界上跑完整八条，逐条跟这次执行的
  `enforcement` 对质。机器换成 os_enforced 时，期望值跟着能力探测走，不是写死。
"""

from __future__ import annotations

import shutil
import sys

import pytest

from blank_api.services.sandbox import boundary

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="边界探针依赖 Windows Job/AC")

from blank_api.doctor import (  # noqa: E402
    BOUNDARY_IDS,
    ENFORCED,
    NOT_ENFORCED,
    NOT_TESTED,
    UNKNOWN,
    VERIFIED,
    access_verdict,
    audit_evidence,
    collect,
    exit_code_for,
    inherit_verdict,
    pids_verdict,
    render,
)


def _evidence(**overrides):
    tools = [
        {"name": "list_files", "boundary_kind": "in_process", "sandbox_mode": "off"},
        {"name": "run_shell", "boundary_kind": "process", "sandbox_mode": "workspace"},
    ]
    payload = {
        "tools": tools,
        "boundaries": {
            "tool_calls": {
                "blocked": 0,
                "mcp_host_process": 0,
                "process": 1,
                "in_process": 1,
                "unattributed": 0,
            },
            "mcp": {"boundary": "none"},
            "sandbox_modes": ["off", "workspace"],
        },
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# 纯函数：结论的边界条件
# --------------------------------------------------------------------------- #
def test_access_verdict_only_calls_an_explicit_denial_enforced():
    assert access_verdict({"exit_code": 0, "stdout": "ok", "stderr": ""}, "读")[0] == NOT_ENFORCED
    denied = access_verdict(
        {"exit_code": 1, "stdout": "", "stderr": "PermissionError: [WinError 5] Access is denied"},
        "读",
    )
    assert denied[0] == ENFORCED and "被拒" in denied[1]
    # 脚本自己写错时是非零退出，但这不能算"系统拦住了"。
    assert access_verdict({"exit_code": 1, "stderr": "SyntaxError: invalid syntax"}, "读")[0] == UNKNOWN
    assert access_verdict({"exit_code": None, "timed_out": True}, "读")[0] == UNKNOWN


def test_pids_verdict_refuses_to_conclude_without_a_working_control():
    broken = pids_verdict(
        {"exit_code": 1, "stdout": ""},
        {"exit_code": 1, "stdout": ""},
    )
    assert broken[0] == UNKNOWN and "对照" in broken[1]
    assert pids_verdict({"exit_code": 0, "stdout": "pids-ok"}, {"exit_code": 0, "stdout": "pids-ok"})[0] == NOT_ENFORCED
    assert pids_verdict({"exit_code": 0, "stdout": "pids-ok"}, {"exit_code": 1, "stdout": ""})[0] == ENFORCED


def test_inherit_verdict_needs_the_control_and_a_real_write():
    assert inherit_verdict(False, True, True)[0] == UNKNOWN
    assert inherit_verdict(True, False, True)[0] == UNKNOWN
    assert inherit_verdict(True, True, True)[0] == ENFORCED
    assert inherit_verdict(True, True, False)[0] == NOT_ENFORCED


def test_audit_evidence_requires_the_report_and_per_call_facts():
    assert audit_evidence(_evidence())[0] == VERIFIED

    missing = _evidence()
    missing.pop("boundaries")
    status, detail = audit_evidence(missing)
    assert status == UNKNOWN and "boundaries" in detail

    no_kind = _evidence(tools=[{"name": "list_files", "sandbox_mode": "off"}])
    status, detail = audit_evidence(no_kind)
    assert status == UNKNOWN and "list_files 没有 boundary_kind" in detail

    no_mode = _evidence(tools=[{"name": "list_files", "boundary_kind": "in_process"}])
    status, detail = audit_evidence(no_mode)
    assert status == UNKNOWN and "list_files 没有 sandbox_mode" in detail

    short = _evidence()
    short["boundaries"]["tool_calls"]["in_process"] = 0
    status, detail = audit_evidence(short)
    assert status == UNKNOWN and "少于工具调用" in detail

    mcp = _evidence(
        tools=[{"name": "mcp__demo__ping", "boundary_kind": "mcp_host_process", "sandbox_mode": "off"}]
    )
    mcp["boundaries"]["tool_calls"] = {
        "blocked": 0, "mcp_host_process": 1, "process": 0, "in_process": 0, "unattributed": 0,
    }
    mcp["boundaries"]["mcp"] = {"boundary": ""}
    status, detail = audit_evidence(mcp)
    assert status == UNKNOWN and "MCP" in detail


def test_exit_code_fails_on_unknown_or_inconsistent_only():
    clean = {
        "summary": {"unknown": 0, "inconsistent": 0},
    }
    assert exit_code_for(clean) == 0
    assert exit_code_for({"summary": {"unknown": 1, "inconsistent": 0}}) == 1
    assert exit_code_for({"summary": {"unknown": 0, "inconsistent": 1}}) == 1


# --------------------------------------------------------------------------- #
# 真执行：八条边界都跑，结论跟着本机能力走
# --------------------------------------------------------------------------- #
async def test_every_boundary_is_attacked_and_matches_the_account(monkeypatch):
    """期望值不是写死的：本机是 job_only 就必须报"未强制"，是 os_enforced 就必须报"拦住"。"""
    # 测试夹具默认 BLANK_SANDBOX_ENABLED=0；doctor 不替部署开关做决定，所以这里显式打开，
    # 否则 execute_command 会把 workspace 折成 off，P-PROC 的限额根本不会挂 Job。
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("BLANK_SANDBOX_ALLOW_LOCAL", "1")
    cap = boundary.measure(force=True)
    assert cap.apparatus_ok, "探测装置自己不可信，这组断言不成立"
    report = await collect()
    rows = {item["id"]: item for item in report["boundaries"]}
    assert list(rows) == list(BOUNDARY_IDS), "八条边界缺一条就是少跑了一次攻击"

    file_status = ENFORCED if cap.strength == "os_enforced" else NOT_ENFORCED
    for probe_id in ("P-READ", "P-WRITE", "P-ESCAPE"):
        assert rows[probe_id]["status"] == file_status, rows[probe_id]
    assert rows["P-NET"]["status"] == file_status
    job_status = ENFORCED if cap.job else NOT_ENFORCED
    assert rows["P-PROC"]["status"] == job_status, rows["P-PROC"]
    assert rows["P-INHERIT"]["status"] == job_status, rows["P-INHERIT"]
    # P-ENV 只要给出了确定结论，就必须和文件边界的账一致；不一致时 doctor 退出 1。
    assert rows["P-ENV"]["consistent"] is True, rows["P-ENV"]
    assert rows["P-AUDIT"]["status"] == NOT_TESTED
    for item in rows.values():
        if item["account_key"]:
            assert item["consistent"] is True, item
    assert report["summary"]["inconsistent"] == 0
    assert report["exit_code"] == 0
    text = render(report)
    for probe_id in BOUNDARY_IDS:
        assert probe_id in text
    assert "沙箱已启用" not in text
    assert "安全沙箱" not in text


@pytest.mark.skipif(shutil.which("docker") is not None, reason="有 Docker 时 workspace 后端可用")
async def test_no_backend_is_reported_as_not_tested_not_as_a_fake_boundary(monkeypatch):
    """没有 Docker、又没允许本地回退时，七条探针不许硬跑，更不许报成"拦住"。"""
    monkeypatch.setenv("BLANK_SANDBOX_ALLOW_LOCAL", "0")
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "1")
    report = await collect()
    assert [item["status"] for item in report["boundaries"]] == [NOT_TESTED] * len(BOUNDARY_IDS)
    assert all("BLANK_SANDBOX_ALLOW_LOCAL=1" in item["reason"] for item in report["boundaries"][:7])
    assert report["exit_code"] == 0
