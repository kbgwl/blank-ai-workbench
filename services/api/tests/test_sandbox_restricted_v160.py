"""受限令牌后端：准入自检就是判据，不是装饰。

这一层的承诺只有一条：**文件边界由 NTFS 真的拒**（读 `~/.ssh` 失败、工作区读写成功），
网络边界必须如实写 False。所以这里的用例分三层：

1. 规则解析：SID 只认 logon / S-1-5-*，拼错直接报错；
2. 真装置：同一台机器上 `self_check()` 必须同时给出「私钥读不到」+「工作区读写成功」，
   外加宿主令牌、授权后、去掉限制 SID 三条对照。装置自己起不来（造不出令牌）时跳过，
   因为那就是"这台机器不提供该后端"的正确结果；能造令牌而边界不成立，才是红。
3. 执行层接线：`get_sandbox` 只在自检全绿时选中它；`RestrictedSandbox` 的账本
   filesystem=True / network=False，绝不拿受限令牌冒充网络隔离。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from blank_api.services.sandbox import docker_probe, runner, windows_restricted as wr
from blank_api.services.sandbox.policy import SandboxPolicy

pytestmark = pytest.mark.skipif(not wr.supported(), reason="受限令牌后端只在 Windows 上存在")

CONTROLS = ("secret_denied", "workspace_read", "workspace_write",
            "positive_control", "negative_control", "mutation_control")


def _skip_if_token_creation_is_unsupported() -> None:
    ok, control, error = wr.token_probe()
    if not ok:
        pytest.skip(f"本机造不出受限令牌（{error}）：后端不提供是正确结果")
    assert control, "DuplicateTokenEx 对照失败：令牌句柄本身不可信，这次判定作废"


def _private(path: str) -> None:
    """把合成家目录收成"只有当前用户 + SYSTEM"，与 self_check 的装置同形。"""
    handle = wr._open_current_token()
    try:
        owner = wr._token_user_sid(handle)
    finally:
        wr._k32.CloseHandle(handle)
    wr._set_private(path, owner)


def test_capability_sid_resolves_to_the_logon_session() -> None:
    sid = wr.resolve_capability("logon")
    assert sid.startswith("S-1-5-5-"), f"logon 应解析成登录会话 SID，实际 {sid}"
    assert wr.resolve_capability(sid) == sid
    assert wr.resolve_capability("logon") == sid


@pytest.mark.parametrize("sid", ["Users", "Everyone", "builtin\\users"])
def test_capability_rejects_group_names_that_cannot_pass_the_restricted_check(sid: str) -> None:
    """组名不是 SID，写进限制列表只会变成"什么都没限制"；只收 logon 或 S-1-5-* 字面量。"""
    with pytest.raises(wr.TokenError, match="capability_sid"):
        wr.resolve_capability(sid)


def test_self_check_proves_both_sides_of_the_file_boundary() -> None:
    _skip_if_token_creation_is_unsupported()
    check = wr.self_check(force=True)
    assert check.attempted, check.as_dict()
    assert not check.error, check.error
    assert check.ok, check.as_dict()
    for name in CONTROLS:
        assert getattr(check, name) is True, (name, check.as_dict())
    assert check.details["capability_sid"].startswith("S-1-5-5-")
    # 判据必须来自真实子进程输出，不能是默认值。
    assert "SECRET_DENIED" in check.details["restricted_stdout"], check.details
    assert "WS_WRITE_OK" in check.details["restricted_stdout"], check.details
    assert "WS_READ_OK" in check.details["restricted_stdout"], check.details
    assert "SECRET_READ" in check.details["mutation_stdout"], check.details
    assert "SECRET_READ" in check.details["positive_stdout"], check.details


def test_recursive_grant_reaches_files_not_just_the_directory(tmp_path) -> None:
    """icacls `/T` + `(OI)(CI)` 会静默跳过文件（还报成功），所以授权必须两段式。

    这条用例钉的正是那个缺陷：只给根目录授权时，文件上查不到能力 SID。
    """
    sid = wr.resolve_capability("logon")
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "sub" / "b.txt").write_text("b", encoding="utf-8")
    ok, detail = wr._grant(str(root), "RX", sid, recursive=True)
    assert ok, detail
    for path in (root / "a.txt", root / "sub" / "b.txt"):
        listing = subprocess.run(
            ["icacls", str(path)], capture_output=True, text=True, encoding="mbcs", errors="replace"
        )
        assert listing.returncode == 0, listing.stdout + listing.stderr
        # icacls 会把 SID 显示成 NT AUTHORITY\LogonSessionId_0_N，所以钉"显式(RX) ACE"，
        # 而不是钉 SID 文本：继承来的 `(I)(RX)` 在只授权根目录时也会出现，不能算数。
        explicit = [line for line in listing.stdout.splitlines() if "(RX)" in line and "(I)" not in line]
        assert explicit, f"{path} 上没有显式 RX ACE（只有继承）：{listing.stdout}"


def test_inline_command_line_never_needs_a_batch_file() -> None:
    application, cmdline = wr._command_line("echo hi")
    assert application.lower().endswith("cmd.exe"), application
    assert cmdline.startswith(f'"{application}" /d /s /c "echo hi"'), cmdline


def test_run_command_denies_the_synthetic_ssh_and_keeps_the_workspace(tmp_path, monkeypatch) -> None:
    _skip_if_token_creation_is_unsupported()

    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    secret = home / ".ssh" / "id_rsa"
    secret.write_text("SYNTHETIC-NOT-A-KEY", encoding="utf-8")
    _private(str(home))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))

    from blank_api.services.sandbox.rules import SandboxRules

    rule = SandboxRules(
        version=1, capability_sid="logon", default_action="deny",
        write_allow=(), read_allow=(), protected=("{home}/.ssh",), source="test",
    )
    report = wr.run_command(
        'type "%USERPROFILE%\\.ssh\\id_rsa" >nul 2>nul && echo LEAK || echo DENIED & '
        "echo ws-ok>out.txt && echo WS_OK",
        workspace=str(workspace),
        env={"USERPROFILE": str(home), "HOME": str(home)},
        timeout=30.0,
        memory="256m",
        pids=32,
        rule_set=rule,
    )
    stdout = report.stdout.decode("utf-8", "replace")
    assert not report.error, report.error
    assert report.exit_code == 0, (report.exit_code, stdout)
    assert "DENIED" in stdout and "LEAK" not in stdout, (stdout, report.stderr)
    assert "WS_OK" in stdout, stdout
    assert (workspace / "out.txt").read_text(encoding="utf-8").strip() == "ws-ok"
    assert report.job.created and report.job.assigned, report.job
    assert "memory_bytes" in report.job.enforced and "active_process_limit" in report.job.enforced


def _stub_backend(monkeypatch, *, restricted: bool) -> None:
    monkeypatch.setattr(
        docker_probe, "probe_for_decision",
        lambda **_: SimpleNamespace(usable=False, state=docker_probe.STATE_NOT_INSTALLED, detail="stub"),
    )
    monkeypatch.setattr(wr, "available", lambda: restricted)


def test_get_sandbox_uses_restricted_only_when_the_gate_is_green(monkeypatch) -> None:
    _stub_backend(monkeypatch, restricted=True)
    policy = SandboxPolicy(enabled=True, allow_local=False, restricted_enabled=True)
    assert runner.get_sandbox(policy).name == "restricted"


def test_get_sandbox_refuses_when_neither_container_nor_restricted_is_available(monkeypatch) -> None:
    _stub_backend(monkeypatch, restricted=False)
    policy = SandboxPolicy(enabled=True, allow_local=False, restricted_enabled=True)
    with pytest.raises(runner.SandboxUnavailable):
        runner.get_sandbox(policy)


def test_get_sandbox_keeps_local_when_restricted_is_opt_in_and_off(monkeypatch) -> None:
    _stub_backend(monkeypatch, restricted=True)
    policy = SandboxPolicy(enabled=True, allow_local=True, restricted_enabled=False)
    assert runner.get_sandbox(policy).name == "local"


def test_docker_still_wins_when_it_is_usable(monkeypatch) -> None:
    monkeypatch.setattr(
        docker_probe, "probe_for_decision",
        lambda **_: SimpleNamespace(usable=True, state=docker_probe.STATE_READY, detail="stub"),
    )
    policy = SandboxPolicy(enabled=True, allow_local=True, restricted_enabled=True)
    assert runner.get_sandbox(policy).name == "docker"


def test_restricted_sandbox_run_claims_files_but_never_network(tmp_path) -> None:
    _skip_if_token_creation_is_unsupported()
    if not wr.available():
        pytest.skip("自检未通过：后端不会被选中，执行层用例不成立")

    rule_file = tmp_path / "rules.json"
    rule_file.write_text(json.dumps({
        "version": 1, "capability_sid": "logon", "default_action": "deny",
        "write_allow": [], "read_allow": [], "protected": [],
    }), encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    policy = SandboxPolicy(
        enabled=True, allow_local=False, restricted_enabled=True,
        rules_file=str(rule_file), memory="256m", pids=32, timeout_seconds=60.0,
    )
    started = time.perf_counter()
    result = asyncio.run(runner.RestrictedSandbox(policy).run(["echo", "hello"], workspace=str(workspace)))
    assert time.perf_counter() - started < 60
    assert result.backend == "restricted"
    assert result.exit_code == 0, (result.exit_code, result.stdout, result.stderr)
    assert "hello" in result.stdout, result.stdout
    assert result.enforcement["filesystem_enforced"] is True, result.enforcement
    assert result.enforcement["network_enforced"] is False, result.enforcement
    assert result.enforcement["memory_enforced"] is True, result.enforcement
