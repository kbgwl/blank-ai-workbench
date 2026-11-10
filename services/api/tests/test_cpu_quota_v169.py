"""CPU 限速（P2 解冻）的真值验收。

2026-11-10 在本机（16 逻辑核、非管理员、build 26300）实测：`CpuRate` 的单位是
**整机算力的万分之一**，16 个自旋子进程基线 40.2 CPU 秒，`625`（1 核）压到 3.69 秒、
`1250`（2 核）压到 7.13 秒。所以这里钉三件事，缺一件这条限额就只是文案：

1. 换算：`cpus` → `CpuRate` 按整机核数折算，不是"一个核的百分比"；不小于整机核数时
   不设限、也不许记成已强制。
2. 行为证据：`SetInformationJobObject` 返回 True 不代表压得住，探针必须跑一次
   「多进程自旋 vs 限速」的对照，限速侧落在阈值内才算数。
3. 账本：Job 真接受了限速 + 探针证明这台机器压得住，两个都真才写 `True`；
   写进 notes 的那句话必须与账本同源。
"""

from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace

import pytest

from blank_api.services.sandbox import boundary, windows_job
from blank_api.services.sandbox.policy import SandboxPolicy
from blank_api.services.sandbox.runner import (
    LocalSandbox,
    _enforcement,
    _enforcement_notes,
    _enforcement_restricted,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Job Object 是 Windows 原语")


def _cap(**overrides) -> boundary.Capability:
    """一封"装置可信"的探测结果；cpu_rate 由用例显式决定，默认 False＝没测出。"""
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


# ------------------------------------------------------------------ 换算：整机万分之一


def test_cpu_rate_is_a_share_of_the_whole_machine() -> None:
    """16 核机器：1 核 = 625，4 核 = 2500，整机 = 10000。

    第一版探针按"单核的百分比"理解，2000/5000 两档在单线程上毫无变化——那不是没生效，
    是把单位读错了。这条把单位钉死，换算一改就红。
    """
    assert windows_job.cpu_rate_for(1.0, 16) == 625
    assert windows_job.cpu_rate_for(4.0, 16) == 2500
    assert windows_job.cpu_rate_for(16.0, 16) == 10000
    assert windows_job.cpu_rate_for(0.5, 1) == 5000
    # 0 / 负数 = 不设限；不许悄悄回落到某个默认核数再声称"已强制"
    assert windows_job.cpu_rate_for(0, 16) == 0
    assert windows_job.cpu_rate_for(-1, 16) == 0
    # 超过整机只夹到 10000：调用方据此判成"没构成限制"
    assert windows_job.cpu_rate_for(64, 16) == 10000


def test_job_records_the_rate_the_kernel_accepted() -> None:
    job = windows_job.create_job("blank-cpu-rate", cpu_cpus=1.0)
    try:
        assert job.ok, job.error
        assert job.enforced.get("cpu_rate") == windows_job.cpu_rate_for(1.0)
        assert job.cpu_error == ""
    finally:
        windows_job.close_job(job)


def test_full_machine_cpus_is_not_a_limit_and_is_not_recorded() -> None:
    """cpus 不小于整机核数时，设了也是摆设：账上必须说清，不许当成功。"""
    cores = os.cpu_count() or 1
    job = windows_job.create_job("blank-cpu-rate-full", cpu_cpus=float(cores))
    try:
        assert job.ok, job.error
        assert "cpu_rate" not in job.enforced
        assert "未设 CPU 限速" in job.cpu_error
    finally:
        windows_job.close_job(job)


# ------------------------------------------------------------------ 行为证据


def test_probe_rejects_its_own_failed_control(monkeypatch) -> None:
    """对照没跑满＝装置不可信：限速侧再低也不能当证据。

    这条不跑真负载，直接喂一个"几乎没花 CPU"的对照——把 floor 自检删掉，
    探针就会把"什么都没跑起来"读成"限速非常有效"。
    """
    monkeypatch.setattr(boundary, "_run_spin", lambda *args, **kwargs: (0.01, ""))
    ok, error, details = boundary._probe_cpu_rate()
    assert ok is False
    assert "对照没跑满" in error, (error, details)


def test_probe_rejects_a_rate_that_did_not_throttle(monkeypatch) -> None:
    """限速侧没掉到阈值的 70% 以下 ⇒ 这台机器的 CpuRate 不算数。

    这是"设上了但不生效"的形状：Job 接受 CpuRate、两轮都跑完，只是 CPU 秒几乎没差。
    """
    calls = iter([(2.0, ""), (1.9, "")])
    monkeypatch.setattr(boundary, "_run_spin", lambda *args, **kwargs: next(calls))
    ok, error, details = boundary._probe_cpu_rate()
    assert ok is False
    assert "限速未生效" in error, (error, details)
    assert details["capped_cpu_seconds"] > details["pass_threshold_cpu_seconds"]


def test_measure_once_records_the_cpu_probe(monkeypatch) -> None:
    """`_measure_once` 必须把探针结论抄进 Capability——删掉那次调用，账本永远 False。"""
    monkeypatch.setattr(boundary, "_probe_job", lambda: (True, ""))
    monkeypatch.setattr(
        boundary, "_probe_cpu_rate", lambda: (True, "", {"control_cpu_seconds": 1.0}))
    measured = boundary._measure_once()
    assert measured.cpu_rate is True
    assert measured.cpu_rate_check == {"control_cpu_seconds": 1.0}

    monkeypatch.setattr(boundary, "_probe_cpu_rate", lambda: (False, "限速未生效", {}))
    measured = boundary._measure_once()
    assert measured.cpu_rate is False and measured.cpu_rate_error == "限速未生效"

    monkeypatch.setattr(boundary, "_probe_job", lambda: (False, "本平台无 Job Object"))
    measured = boundary._measure_once()
    assert measured.cpu_rate is False
    assert "Job 不可用" in measured.cpu_rate_error


def test_cpu_rate_probe_shows_real_throttling() -> None:
    """同一负载跑两轮：限速侧合计 CPU 秒必须明显低于不限速对照。

    对照自己先要跑满（设备自检），否则"限速侧更低"可能只是探针根本没跑起来；
    单核机器构造不出对照，如实跳过而不是猜成支持。
    """
    if (os.cpu_count() or 1) < 2:
        pytest.skip("单核机器构造不出「限速 vs 不限速」对照")
    ok, error, details = boundary._probe_cpu_rate()
    assert ok, (error, details)
    assert details["capped_cpu_seconds"] <= details["pass_threshold_cpu_seconds"], details
    assert details["control_cpu_seconds"] >= details["workers"] * details["spin_seconds"] * 0.4, (
        f"对照没跑满，限速侧再低也不能当证据：{details}")


# ------------------------------------------------------------------ 账本


def test_ledger_needs_both_accepted_rate_and_machine_proof(cached_cap) -> None:
    """Job 接受过 CpuRate 只是"设上了"；机器探针没证明压得住就不许点绿。"""
    job = windows_job.Job(
        handle=1,
        enforced={"memory_bytes": 1, "active_process_limit": 2, "kill_on_close": 1, "cpu_rate": 625},
    )

    cached_cap(_cap(cpu_rate=True))
    facts = _enforcement(job, True, "")
    assert facts["cpu_enforced"] is True
    assert any("CPU 上限" in line for line in _enforcement_notes(facts))

    # 探针说压不住：Job 里明明有 cpu_rate，账上也不能写强制。
    cached_cap(_cap(cpu_rate=False))
    facts = _enforcement(job, True, "")
    assert facts["cpu_enforced"] is False, "没有行为证据的 CpuRate 只是设了个参数"
    assert not any("CPU 上限" in line for line in _enforcement_notes(facts))

    # 本次 Job 没设限速（cpus 不小于整机）：探针再支持也不属于这次执行。
    without_rate = windows_job.Job(handle=1, enforced={"memory_bytes": 1, "active_process_limit": 2})
    cached_cap(_cap(cpu_rate=True))
    assert _enforcement(without_rate, True, "")["cpu_enforced"] is False


def test_restricted_ledger_uses_the_same_cpu_rule(cached_cap) -> None:
    report = SimpleNamespace(
        job=SimpleNamespace(
            created=True,
            assigned=True,
            error="",
            io_error="",
            enforced={"memory_bytes": 1, "active_process_limit": 2, "kill_on_close": 1, "cpu_rate": 625},
        ),
        disk_on_quota=False,
        disk_bytes=0,
        disk_error="用例没有容量卷",
    )
    cached_cap(_cap(cpu_rate=True))
    assert _enforcement_restricted(report)["cpu_enforced"] is True
    cached_cap(_cap(cpu_rate=False))
    assert _enforcement_restricted(report)["cpu_enforced"] is False


def test_boundary_ledger_follows_the_probe(cached_cap) -> None:
    """机器级账：CPU 也是行为证据，且载体是 Job——Job 不可用时不能单独点绿。"""
    cached_cap(_cap(job=True, cpu_rate=True))
    assert boundary.enforced_fields("local")["cpu"] is True
    assert boundary.enforced_fields("restricted")["cpu"] is True

    cached_cap(_cap(job=True, cpu_rate=False))
    assert boundary.enforced_fields("local")["cpu"] is False
    assert boundary.enforced_fields("restricted")["cpu"] is False

    cached_cap(_cap(job=False, cpu_rate=True))
    assert boundary.enforced_fields("restricted")["cpu"] is False


async def test_local_sandbox_passes_policy_cpus_into_the_job(tmp_path, cached_cap) -> None:
    """端到端：policy.cpus → create_job → 账本。

    探针结论用夹具钉住（机器是否真压得住由上面的行为测试证明），这条只钉接线：
    把 `cpu_cpus=self.policy.cpus` 删掉，Job 里没有 cpu_rate，账本立刻变红。
    """
    cached_cap(_cap(cpu_rate=True))
    policy = SandboxPolicy()
    policy.timeout_seconds = 30
    policy.cpus = 1.0
    result = await LocalSandbox(policy).run("echo cpu", workspace=str(tmp_path))
    assert result.enforcement["cpu_enforced"] is True
    assert any("CPU 上限" in line for line in result.notes)

    policy.cpus = 0.0
    result = await LocalSandbox(policy).run("echo cpu", workspace=str(tmp_path))
    assert result.enforcement["cpu_enforced"] is False, "cpus=0 表示不设限，不许记成已强制"
