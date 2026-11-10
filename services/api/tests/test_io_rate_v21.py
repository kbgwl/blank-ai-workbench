"""IO 速率上限（J1 解冻）的真值验收。

上一轮冻结论是"Job IO rate 接受设置但不节流"，根因在结构体布局：多插了一个
`MaxBandwidthStrict` 后 `ControlFlags` 落到结构外，调用返回 True、限速根本没开。
2026-11-10 在本机（build 26300、管理员）用真实执行形状复测：对照 680.9 MB/s；
请求 16/32/8 MB/s 时限速侧 16.1/32.5/8.0 MB/s（误差 <1.5%）。所以这里钉四件事：

1. 换算：`SANDBOX_IO_BPS` 的 `"128m"` 是每秒字节数，认不出来按 0（不设限）。
2. 行为证据：`SetInformationJobObject` 返回 True 不代表节流，探针必须写两轮盘，
   限速侧落在对照的 70% 以下才算数；对照本身不够快就如实说"构造不出对照"。
3. 账本：Job 真接受了 io_bps + 探针证明这台机器压得住，两个都真才写 `True`。
4. 搬运：docker 命令行没有带宽参数，那一档对 IO 只能说 False。
"""

from __future__ import annotations

import sys
import time
from types import SimpleNamespace

import pytest

from blank_api.services.sandbox import boundary, windows_job
from blank_api.services.sandbox.policy import SandboxPolicy, _parse_bps
from blank_api.services.sandbox.runner import (
    LocalSandbox,
    _enforcement,
    _enforcement_appcontainer,
    _enforcement_notes,
    _enforcement_restricted,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Job Object 是 Windows 原语")

ONE_HUNDRED_TWENTY_EIGHT_MBPS = 128 * 1024 * 1024


def _cap(**overrides) -> boundary.Capability:
    """一封"装置可信"的探测结果；io_rate 由用例显式决定，默认 False＝没测出。"""
    fields = dict(
        platform=sys.platform,
        job=True,
        token_control=True,
        carrier_control=True,
        measured_at=time.time(),
    )
    fields.update(overrides)
    return boundary.Capability(**fields)


@pytest.fixture
def cached_cap(monkeypatch):
    def _install(cap: boundary.Capability) -> boundary.Capability:
        monkeypatch.setattr(boundary, "_CACHE", (time.time(), cap))
        return cap

    return _install


def _job(**enforced) -> windows_job.Job:
    return windows_job.Job(handle=1, enforced=dict(enforced))


# ------------------------------------------------------------------ 换算：字节/秒


def test_io_budget_parses_to_bytes_per_second(monkeypatch) -> None:
    """`"128m"` 是 134217728 B/s；空串/认不出来按 0＝不设限，不许回落成某个默认值。"""
    assert _parse_bps("128m") == ONE_HUNDRED_TWENTY_EIGHT_MBPS
    assert _parse_bps("1g") == 1024 ** 3
    assert _parse_bps("16777216") == 16777216
    assert _parse_bps("0") == 0
    assert _parse_bps("") == 0
    assert _parse_bps("nonsense") == 0

    monkeypatch.delenv("SANDBOX_IO_BPS", raising=False)
    assert SandboxPolicy().io_bps == ONE_HUNDRED_TWENTY_EIGHT_MBPS


def test_job_records_the_rate_the_kernel_accepted() -> None:
    """账本只写内核收下的值；设不上时留 io_error、enforced 里不许出现 io_bps。"""
    job = windows_job.create_job("blank-io-rate", io_bps=ONE_HUNDRED_TWENTY_EIGHT_MBPS)
    try:
        assert job.ok, job.error
        assert ("io_bps" in job.enforced) == (job.io_error == ""), (
            f"enforced={job.enforced} io_error={job.io_error!r}：成功与错误必须互斥")
        if job.io_error == "":
            assert job.enforced["io_bps"] == ONE_HUNDRED_TWENTY_EIGHT_MBPS
    finally:
        windows_job.close_job(job)


def test_zero_io_budget_sets_nothing() -> None:
    job = windows_job.create_job("blank-io-rate-zero", io_bps=0)
    try:
        assert job.ok, job.error
        assert "io_bps" not in job.enforced
        assert job.io_error == ""
    finally:
        windows_job.close_job(job)


# ------------------------------------------------------------------ 行为证据


def test_probe_rejects_its_own_failed_control(monkeypatch) -> None:
    """对照写速不到限速档的 2 倍＝构造不出对照：限速侧再低也不能当证据。"""
    monkeypatch.setattr(boundary, "_run_io_write", lambda *args, **kwargs: (10.0, ""))
    ok, error, details = boundary._probe_io_rate()
    assert ok is False
    assert "构造不出对照" in error, (error, details)
    assert details["control_mb_per_s"] == 10.0


def test_probe_rejects_a_write_that_did_not_throttle(monkeypatch) -> None:
    """限速侧没掉到阈值的 70% 以下 ⇒ 这台机器的 IO rate 不算数。

    这是"设上了但不生效"的形状：Job 接受 io_bps、两轮都写完，只是 MB/s 几乎没差。
    先把本机不接受 IO 限速的情况跳过——那不是代码缺陷，代码对它的处理是记 io_error。
    """
    calls = iter([(500.0, ""), (480.0, "")])
    monkeypatch.setattr(boundary, "_run_io_write", lambda *args, **kwargs: next(calls))
    ok, error, details = boundary._probe_io_rate()
    if "建 Job 失败" in error or "没接受 IO 限速" in error:
        pytest.skip(f"本机内核不接受 IO 限速：{error}")
    assert ok is False
    assert "限速未生效" in error, (error, details)
    assert details["limited_mb_per_s"] > details["pass_threshold_mb_per_s"]


def test_measure_once_records_the_io_probe(monkeypatch) -> None:
    """`_measure_once` 必须把探针结论抄进 Capability——删掉那次调用，账本永远 False。"""
    monkeypatch.setattr(boundary, "_probe_job", lambda: (True, ""))
    monkeypatch.setattr(boundary, "_probe_cpu_rate", lambda: (False, "未测", {}))
    monkeypatch.setattr(
        boundary, "_probe_io_rate", lambda: (True, "", {"control_mb_per_s": 500.0}))
    measured = boundary._measure_once()
    assert measured.io_rate is True
    assert measured.io_rate_check == {"control_mb_per_s": 500.0}

    monkeypatch.setattr(boundary, "_probe_io_rate", lambda: (False, "限速未生效", {}))
    measured = boundary._measure_once()
    assert measured.io_rate is False and measured.io_rate_error == "限速未生效"

    monkeypatch.setattr(boundary, "_probe_job", lambda: (False, "本平台无 Job Object"))
    measured = boundary._measure_once()
    assert measured.io_rate is False
    assert "Job 不可用" in measured.io_rate_error


def test_io_rate_probe_shows_real_throttling() -> None:
    """同一负载写两轮：限速侧必须明显慢于不限速对照。

    对照自己先要够快（探针内已检查 ≥ 2× 限速档），否则"限速侧更低"可能只是磁盘本来就慢。
    本机写速不足 32MB/s 时如实跳过，不猜成支持。本机实测：对照 507.9 MB/s、限速侧 18.8 MB/s。
    """
    ok, error, details = boundary._probe_io_rate()
    if not ok and "构造不出对照" in error:
        pytest.skip(error)
    if not ok and ("建 Job 失败" in error or "没接受 IO 限速" in error):
        pytest.skip(f"本机内核不接受 IO 限速：{error}")
    assert ok, (error, details)
    assert details["limited_mb_per_s"] <= details["pass_threshold_mb_per_s"], details
    assert details["control_mb_per_s"] >= 2 * details["limit_mb_per_s"], (
        f"对照没跑满，限速侧再低也不能当证据：{details}")


# ------------------------------------------------------------------ 账本


def test_ledger_needs_both_accepted_rate_and_machine_proof(cached_cap) -> None:
    """Job 接受过 io_bps 只是"设上了"；机器探针没证明节流就不许点绿。"""
    job = _job(memory_bytes=1, active_process_limit=2, kill_on_close=1,
               io_bps=ONE_HUNDRED_TWENTY_EIGHT_MBPS)

    cached_cap(_cap(io_rate=True))
    facts = _enforcement(job, True, "")
    assert facts["io_enforced"] is True
    assert facts["io_bps"] == ONE_HUNDRED_TWENTY_EIGHT_MBPS
    assert any("读写带宽上限" in line for line in _enforcement_notes(facts))

    # 探针说没节流：Job 里明明有 io_bps，账上也不能写强制，notes 要说出为什么。
    cached_cap(_cap(io_rate=False))
    facts = _enforcement(job, True, "")
    assert facts["io_enforced"] is False, "没有行为证据的 IO rate 只是设了个参数"
    notes = _enforcement_notes(facts)
    assert not any("读写带宽上限" in line for line in notes)
    assert any("没能现场证明节流" in line for line in notes)

    # 本次 Job 没设限速（io_bps=0）：探针再支持也不属于这次执行。
    without_rate = _job(memory_bytes=1, active_process_limit=2)
    cached_cap(_cap(io_rate=True))
    assert _enforcement(without_rate, True, "")["io_enforced"] is False


def test_restricted_and_appcontainer_ledgers_use_the_same_io_rule(cached_cap, monkeypatch) -> None:
    report = SimpleNamespace(
        job=SimpleNamespace(
            created=True,
            assigned=True,
            error="",
            io_error="",
            enforced={"memory_bytes": 1, "active_process_limit": 2,
                      "kill_on_close": 1, "io_bps": ONE_HUNDRED_TWENTY_EIGHT_MBPS},
        ),
        disk_on_quota=False,
        disk_bytes=0,
        disk_error="用例没有容量卷",
    )
    cached_cap(_cap(io_rate=True))
    assert _enforcement_restricted(report)["io_enforced"] is True
    assert _enforcement_appcontainer(report)["io_enforced"] is True
    cached_cap(_cap(io_rate=False))
    assert _enforcement_restricted(report)["io_enforced"] is False
    assert _enforcement_appcontainer(report)["io_enforced"] is False


def test_boundary_ledger_follows_the_probe(cached_cap) -> None:
    """机器级账：IO 也是行为证据，且载体是 Job——Job 不可用时不能单独点绿。"""
    cached_cap(_cap(job=True, io_rate=True))
    assert boundary.enforced_fields("local")["io"] is True
    assert boundary.enforced_fields("restricted")["io"] is True

    cached_cap(_cap(job=True, io_rate=False))
    assert boundary.enforced_fields("local")["io"] is False
    assert boundary.enforced_fields("restricted")["io"] is False

    cached_cap(_cap(job=False, io_rate=True))
    assert boundary.enforced_fields("restricted")["io"] is False


def test_docker_never_claims_io(cached_cap) -> None:
    """docker 命令行没有带宽参数：探针再支持，容器这一档也不能跟着整排点绿。"""
    cached_cap(_cap(job=True, io_rate=True))
    assert boundary.enforced_fields("docker")["io"] is False


async def test_local_sandbox_passes_policy_io_bps_into_the_job(tmp_path, cached_cap) -> None:
    """端到端：policy.io_bps → create_job → 账本。

    探针结论用夹具钉住（机器是否真节流由上面的行为测试证明），这条只钉接线：
    把 `io_bps=self.policy.io_bps` 删掉，Job 里没有 io_bps，账本立刻变红。
    """
    cached_cap(_cap(io_rate=True))
    policy = SandboxPolicy()
    policy.timeout_seconds = 30
    policy.io_bps = ONE_HUNDRED_TWENTY_EIGHT_MBPS
    result = await LocalSandbox(policy).run("echo io", workspace=str(tmp_path))
    assert result.enforcement["io_enforced"] is True
    assert result.enforcement["io_bps"] == ONE_HUNDRED_TWENTY_EIGHT_MBPS
    assert any("读写带宽上限" in line for line in result.notes)

    policy.io_bps = 0
    result = await LocalSandbox(policy).run("echo io", workspace=str(tmp_path))
    assert result.enforcement["io_enforced"] is False, "io_bps=0 表示不设限，不许记成已强制"
    assert result.enforcement["io_bps"] == 0
