"""AppContainer 后端的账本、选择门与命令形状（I 批）。

判据分三层，每层都能单独翻红：

1. `judge` 的纯函数真值表：任何一条证据缺席都不能得出"边界已强制"，而且宿主对照
   失败必须先于 AC 侧证据判定——否则离线机器上的"连不上"会被写成"网络被拦"。
2. 选择门与档位：只有进程内已有全绿自检时才选中 AC；`workspace-net` 在 AC 下必须
   拒绝（零能力包的方向与请求相反），不许降级成 off。
3. 命令形状：`quote_leading_path` 只给路径型首词补引号——未引号的绝对路径在 AC 里
   会被 cmd 的搜索解析撞成「拒绝访问。」（本机实测，`E:/tmp/i-probe/ac_quote_isolate.json`），
   而给裸名加引号反而会关掉 PATH 搜索。
"""

from __future__ import annotations

import sys
import time
from types import SimpleNamespace

import pytest

from blank_api.services.agent import governance
from blank_api.services.safety import backend_info
from blank_api.services.sandbox import boundary, docker_probe, runner
from blank_api.services.sandbox import windows_appcontainer as wac
from blank_api.services.sandbox import windows_restricted as wr
from blank_api.services.sandbox.boundary import Capability
from blank_api.services.sandbox.policy import SandboxPolicy
from blank_api.services.sandbox.tiers import (
    REASON_NET_UNAVAILABLE,
    TIER_WORKSPACE_NET,
    resolve_tier,
)

GREEN_AC = {
    "bind": "ok", "loopback": "ok", "external": "PermissionError:13",
    "secret": "PermissionError:13", "ws_write": "ws-ok",
}
GREEN_PLAIN = {
    "bind": "ok", "loopback": "ok", "external": "ok",
    "secret": wac.SECRET_TEXT, "ws_write": "ws-ok",
}


def _check(**overrides) -> wac.SelfCheck:
    fields = dict(
        attempted=True,
        secret_denied=True, workspace_read=True, workspace_write=True,
        bind=True, loopback=True, network_denied=True,
        plain_secret_read=True, plain_network=True,
    )
    fields.update(overrides)
    return wac.SelfCheck(**fields)


def _green_check() -> wac.SelfCheck:
    check = _check()
    assert check.ok
    return check


def _cap(**overrides) -> Capability:
    fields = dict(
        platform=sys.platform,
        job=True,
        restricted_token=True,
        token_control=True,
        appcontainer=True,
        carrier_control=True,
        cpu_rate=True,
        io_rate=True,
        measured_at=time.time(),
    )
    fields.update(overrides)
    return Capability(**fields)


@pytest.fixture
def green(monkeypatch):
    """把 AC 行为自检与边界探测都钉成"全绿"，被测的是记账，不是这台机器。"""
    monkeypatch.setattr(boundary, "_CACHE", (time.time(), _cap()))
    monkeypatch.setattr(wac, "_SELF_CACHE", _green_check())
    return _green_check()


# --------------------------------------------------------------------- judge


@pytest.mark.parametrize(
    "field, value, expected",
    [
        ("secret", "ok", "读到了合成私钥"),                     # AC 没拦住文件
        ("external", "TimeoutError:timed out", "外连没有被策略拒绝"),  # 超时不算边界
        ("external", "PermissionError:10061", "外连没有被策略拒绝"),    # 连接被拒也不算
        ("ws_write", "PermissionError:13", "写不了工作区"),
        ("bind", "PermissionError:13", "无法监听回环端口"),
        ("loopback", "ConnectionRefusedError:10061", "回环自连失败"),
    ],
)
def test_judge_rejects_each_missing_ac_evidence(field, value, expected):
    ac = dict(GREEN_AC)
    ac[field] = value
    check = wac.judge(ac, GREEN_PLAIN)
    assert check.ok is False
    assert expected in check.error


def test_judge_only_counts_wsa_eacces_as_a_network_denial():
    for denied in ("PermissionError:13", "PermissionError:10013"):
        ac = dict(GREEN_AC, external=denied)
        assert wac.judge(ac, GREEN_PLAIN).network_ok is True
    for soft in ("TimeoutError:timed out", "ConnectionResetError:10054", "OSError:10061"):
        ac = dict(GREEN_AC, external=soft)
        assert wac.judge(ac, GREEN_PLAIN).network_ok is False, soft


def test_judge_puts_the_host_control_before_ac_evidence():
    """宿主读不到私钥时，AC 侧"读不到"不能归因于边界——宁可判装置不可信。"""
    plain = dict(GREEN_PLAIN, secret="PermissionError:13")
    ac = dict(GREEN_AC, external="ok")  # 两条 AC 证据都是坏的
    check = wac.judge(ac, plain)
    assert check.ok is False
    assert "宿主令牌对照读不到合成私钥" in check.error


def test_judge_refuses_to_infer_network_from_a_dead_host_control():
    plain = dict(GREEN_PLAIN, external="PermissionError:13")
    check = wac.judge(GREEN_AC, plain)
    assert check.ok is False
    assert "宿主令牌对照连不上外网" in check.error


# --------------------------------------------------------------------- 命令形状


@pytest.mark.parametrize(
    "command, expected",
    [
        ('"C:\\py\\python.exe" "a.py"', '"C:\\py\\python.exe" "a.py"'),
        ("echo hi", "echo hi"),
        ("npm test", "npm test"),
        ("python -m pytest", "python -m pytest"),
        ("C:\\py\\python.exe -m pytest", '"C:\\py\\python.exe" -m pytest'),
        (".\\venv\\Scripts\\python.exe x.py", '".\\venv\\Scripts\\python.exe" x.py'),
        ("  D:/tools/py.exe -c \"print(1)\"", '  "D:/tools/py.exe" -c "print(1)"'),
        ("%USERPROFILE%\\py.exe -V", '"%USERPROFILE%\\py.exe" -V'),
        # 已经带引号、或者首词不是路径的，一律不动。
        ('"C:\\py\\python.exe"', '"C:\\py\\python.exe"'),
        ("cmd /c echo hi", "cmd /c echo hi"),
    ],
)
def test_quote_leading_path_only_touches_a_path_like_first_word(command, expected):
    assert wac.quote_leading_path(command) == expected


# --------------------------------------------------------------------- 账本


def test_enforced_fields_appcontainer_reads_the_behavioral_check(green):
    enforced = boundary.enforced_fields("appcontainer")
    assert enforced["filesystem"] is True
    assert enforced["network"] is True              # 只因为自检真的量到 PermissionError:13
    assert enforced["memory"] is True and enforced["pids"] is True and enforced["tree_kill"] is True
    assert enforced["cpu"] is True and enforced["io"] is True and enforced["disk"] is False
    assert boundary.enforcement_source("appcontainer") == "measured_appcontainer_boundary"


def test_enforced_fields_appcontainer_never_borrows_a_capability_claim(monkeypatch, green):
    """AC 能被创建（capability=True）不等于这一次执行有边界：只认行为自检。"""
    red = _check(network_denied=False, secret_denied=False)
    monkeypatch.setattr(wac, "_SELF_CACHE", red)
    enforced = boundary.enforced_fields("appcontainer")
    assert enforced["filesystem"] is False
    assert enforced["network"] is False
    assert boundary.enforcement_source("appcontainer") == "appcontainer_unverified"

    monkeypatch.setattr(wac, "_SELF_CACHE", None)
    assert boundary.enforced_fields("appcontainer")["network"] is False
    assert boundary.enforcement_source("appcontainer") == "appcontainer_unverified"


def test_tier_facts_read_each_boundary_separately(monkeypatch, green):
    """文件边界过了、网络没过时，两条账必须分开写，不能一条绿带一条绿。"""
    _stub_docker(monkeypatch, usable=False)
    monkeypatch.setattr(wac, "available", lambda: True)
    monkeypatch.setattr(wac, "_SELF_CACHE", _check(network_denied=False))
    policy = SandboxPolicy(enabled=True, allow_local=False, appcontainer_enabled=True)
    resolution = resolve_tier("workspace", policy=policy)
    assert resolution.filesystem_enforced is True
    assert resolution.network_enforced is False
    assert resolution.strength == "unknown"


def test_enforcement_appcontainer_maps_job_facts_and_boundary_together(green):
    report = wr.CommandReport(
        exit_code=0, stdout=b"ok", stderr=b"", timed_out=False,
        job=wr.JobReport(
            created=True, assigned=True, error="",
            enforced={"memory_bytes": 536870912, "active_process_limit": 128,
                      "kill_on_close": 1, "cpu_rate": 1, "io_bps": 134217728},
        ),
    )
    ledger = runner._enforcement_appcontainer(report)
    assert ledger["boundary_strength"] == "os_enforced"
    assert ledger["filesystem_enforced"] is True and ledger["network_enforced"] is True
    assert ledger["memory_enforced"] is True and ledger["memory_bytes"] == 536870912
    assert ledger["pids_enforced"] is True and ledger["pids_limit"] == 128
    assert ledger["tree_kill_enforced"] is True and ledger["cpu_enforced"] is True
    # IO 速率上限与 CPU 同一条规则：Job 接受 + 探针证明节流，账上才是 True。
    assert ledger["io_enforced"] is True and ledger["io_bps"] == 134217728
    assert ledger["disk_enforced"] is False, "磁盘容量配额仍需要管理员，谁敢写 True 谁就是在编"

    failed_job = runner._enforcement_appcontainer(
        wr.CommandReport(
            exit_code=0, stdout=b"", stderr=b"", timed_out=False,
            job=wr.JobReport(created=True, assigned=False, error="assign failed"),
        )
    )
    assert failed_job["memory_enforced"] is False
    assert failed_job["pids_enforced"] is False
    assert failed_job["tree_kill_enforced"] is False


# --------------------------------------------------------------------- 选择门


def _stub_docker(monkeypatch, *, usable: bool) -> None:
    state = docker_probe.STATE_READY if usable else docker_probe.STATE_NOT_INSTALLED
    monkeypatch.setattr(
        docker_probe, "probe_for_decision",
        lambda **_: SimpleNamespace(
            usable=usable, state=state, detail="stub", label="stub", measured_at=time.time()
        ),
    )


def test_get_sandbox_prefers_appcontainer_when_the_gate_is_green(monkeypatch):
    _stub_docker(monkeypatch, usable=False)
    monkeypatch.setattr(wac, "available", lambda: True)
    policy = SandboxPolicy(enabled=True, allow_local=False, appcontainer_enabled=True,
                           restricted_enabled=True)
    assert runner.get_sandbox(policy).name == "appcontainer"


def test_get_sandbox_falls_through_when_the_appcontainer_gate_is_not_green(monkeypatch):
    _stub_docker(monkeypatch, usable=False)
    monkeypatch.setattr(wac, "available", lambda: False)
    monkeypatch.setattr(wr, "available", lambda: False)
    policy = SandboxPolicy(enabled=True, allow_local=True, appcontainer_enabled=True)
    assert runner.get_sandbox(policy).name == "local"

    strict = SandboxPolicy(enabled=True, allow_local=False, appcontainer_enabled=True)
    with pytest.raises(runner.SandboxUnavailable):
        runner.get_sandbox(strict)


def test_docker_still_wins_over_appcontainer(monkeypatch):
    _stub_docker(monkeypatch, usable=True)
    monkeypatch.setattr(wac, "available", lambda: True)
    policy = SandboxPolicy(enabled=True, allow_local=True, appcontainer_enabled=True)
    assert runner.get_sandbox(policy).name == "docker"


def test_appcontainer_refuses_workspace_net_instead_of_degrading_to_off(monkeypatch, green):
    _stub_docker(monkeypatch, usable=False)
    monkeypatch.setattr(wac, "available", lambda: True)
    policy = SandboxPolicy(enabled=True, allow_local=False, appcontainer_enabled=True)
    resolution = resolve_tier(TIER_WORKSPACE_NET, policy=policy)
    assert resolution.available is False
    assert resolution.reason == REASON_NET_UNAVAILABLE
    assert "AppContainer" in resolution.notice
    assert resolution.network_enforced is False
    assert "off" in resolution.notice


def test_backend_info_names_appcontainer_as_isolation(monkeypatch, green):
    _stub_docker(monkeypatch, usable=False)
    monkeypatch.setenv("BLANK_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("BLANK_SANDBOX_ALLOW_LOCAL", "0")
    monkeypatch.setenv("BLANK_SANDBOX_APPCONTAINER", "1")
    monkeypatch.setattr(wac, "available", lambda: True)
    info = backend_info()
    assert info.backend == "appcontainer"
    assert info.isolation == "appcontainer"
    assert "AppContainer" in info.label


def test_governance_reports_appcontainer_as_its_own_isolation(monkeypatch, green):
    resolution = SimpleNamespace(
        policy=SandboxPolicy(enabled=True),
        tier="workspace", requested="workspace", available=True,
        degraded=False, reason="", notice="", strength="os_enforced",
        limits_enforced=True,
    )
    monkeypatch.setattr(governance, "resolve_tier", lambda *a, **k: resolution)
    monkeypatch.setattr(
        governance, "get_sandbox", lambda *a, **k: SimpleNamespace(name="appcontainer")
    )
    facts = governance.sandbox_boundary()
    assert facts["backend"] == "appcontainer"
    assert facts["isolation"] == "appcontainer"
    assert facts["filesystem_enforced"] is True
    assert facts["network_enforced"] is True
