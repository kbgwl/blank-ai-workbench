"""磁盘容量配额：容量卷 + 行为探针 + 执行层账本，一条链上的每一环都要能失败。

J2 之前这一项是冻结的：NTFS 没有目录级配额，按用户/卷配又会误伤同卷的其他进程。
本批把一个 VHDX 挂成独立卷、只把沙箱临时区（TEMP/TMP）放上去，写满由内核返回 ENOSPC。
判据刻意分成三层，缺一层就只剩"能建卷"这种能力推断：

1. `windows_volume.QuotaVolume` 的挂载/缓存/回收（用假 mount 钉调用次数与失败缓存，
   不碰 diskpart）；
2. `_probe_disk_quota` 的行为判据：没有 ENOSPC、写满后还能写、零写入都要判失败——
   "容量没生效"必须能红，否则它只是一个永远返回 True 的装饰；
3. 账本与搬运：`disk_enforced/disk_bytes/disk_error` 只按"探针证明 + 临时区真的在卷上"
   成立，网关白名单与治理快照同口径。

真实 64MB 卷写满、真实沙箱内 ENOSPC 需要 Windows + 管理员，缺条件时明确 skip（带原因），
不拿假读数充数。
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import pytest

from blank_api.services.agent import governance
from blank_api.services.sandbox import boundary
from blank_api.services.sandbox import windows_volume as wv
from blank_api.services.sandbox.boundary import Capability
from blank_api.services.sandbox.policy import SandboxPolicy, _parse_mb
from blank_api.services.sandbox.runner import LocalSandbox, _enforcement, limits_not_applied
from blank_api.services.tools.gateway import sandbox_backend_event_field

ADMIN = sys.platform == "win32" and wv.is_admin()
REAL_VOLUME = pytest.mark.skipif(
    not ADMIN, reason="容量卷需要 Windows + 管理员权限；非提权机器上只能验账本搬运"
)


# ------------------------------------------------------------------ 策略解析


def test_disk_mb_parsing_is_explicit():
    """只认数字 / m / g；认不出来按 0（不启用），不许拿默认值替用户做主。"""
    assert _parse_mb("64") == 64
    assert _parse_mb("512m") == 512
    assert _parse_mb("2g") == 2048
    assert _parse_mb(" 1G ") == 1024
    assert _parse_mb("abc") == 0
    assert _parse_mb("") == 0


def test_policy_reads_disk_mb_from_env(monkeypatch):
    monkeypatch.setenv("BLANK_SANDBOX_DISK_MB", "96")
    assert SandboxPolicy().disk_mb == 96
    monkeypatch.setenv("BLANK_SANDBOX_DISK_MB", "0")
    assert SandboxPolicy().disk_mb == 0


# ------------------------------------------------------------ 容量卷生命周期


def _fake_mount(monkeypatch, *, ok=True, error="", capacity=None):
    """把 diskpart 换成一张回执；只钉调用次数、缓存与清理，不测 diskpart 本身。"""
    calls: list[tuple[str, str, int]] = []

    def fake(vhdx, mount_dir, size_mb):
        calls.append((str(vhdx), str(mount_dir), int(size_mb)))
        report = wv.VolumeReport(
            attempted=True, vhdx=str(vhdx), mount=str(mount_dir), size_mb=int(size_mb)
        )
        if ok:
            os.makedirs(mount_dir, exist_ok=True)
            report.ok = True
            report.capacity_bytes = int(capacity if capacity is not None else size_mb * 1024 * 1024)
        else:
            report.error = error or "假挂载失败"
        return report

    monkeypatch.setattr(wv, "mount", fake)
    monkeypatch.setattr(wv, "_sweep_stale", lambda base: None)
    monkeypatch.setattr(wv, "_base_dir", lambda: _fake_base())
    return calls


_FAKE_BASE = None


def _fake_base():
    global _FAKE_BASE
    if _FAKE_BASE is None:
        _FAKE_BASE = pathlib.Path(tempfile.mkdtemp(prefix="blank-quota-test-"))
    return _FAKE_BASE


@pytest.fixture(autouse=True)
def _reset_fake_base():
    global _FAKE_BASE
    yield
    if _FAKE_BASE is not None:
        shutil.rmtree(_FAKE_BASE, ignore_errors=True)
        _FAKE_BASE = None


def test_ensure_caches_success_and_only_remounts_on_size_change(monkeypatch):
    calls = _fake_mount(monkeypatch)
    volume = wv.QuotaVolume()
    first = volume.ensure(64)
    assert first.ok and volume.ready()
    volume.ensure(64)
    assert len(calls) == 1, "同容量重复调用不许再跑 diskpart（每条命令都等几秒）"
    volume.ensure(128)
    assert len(calls) == 2, "换容量必须重新挂：旧卷的 maximum 改不了"
    volume.teardown()
    assert not volume.ready()


def test_failed_mount_is_not_retried_on_every_command(monkeypatch):
    """没有管理员权限的机器上，每条命令都重试一次注定失败的挂载＝给所有命令加延迟。"""
    calls = _fake_mount(monkeypatch, ok=False, error="没权限")
    volume = wv.QuotaVolume()
    assert volume.ensure(64).ok is False
    assert volume.ensure(64).error == "没权限"
    assert len(calls) == 1


def test_zero_means_disabled_and_never_touches_diskpart(monkeypatch):
    calls = _fake_mount(monkeypatch)
    volume = wv.QuotaVolume()
    report = volume.ensure(0)
    assert report.ok is False and "未启用" in report.error
    assert calls == []
    assert volume.new_dir("x-") == ""


def test_temp_dir_for_falls_back_with_a_reason(monkeypatch):
    _fake_mount(monkeypatch, ok=False, error="挂不上")
    directory, on_quota, capacity, error = wv.temp_dir_for("blank-test-", 64)
    try:
        assert on_quota is False and capacity == 0 and error == "挂不上"
        assert os.path.isdir(directory)
        assert "blank-test-" in os.path.basename(directory)
    finally:
        import shutil

        shutil.rmtree(directory, ignore_errors=True)


@pytest.mark.skipif(sys.platform != "win32", reason="目录联接是 Windows 语义")
def test_detach_removes_a_dangling_mount_junction(tmp_path):
    """卸卷后的挂载点是悬空联接：`exists()` 看不到它，必须用 lexists 收掉。

    本机实测旧实现每跑一个进程就在 `%ProgramData%\\blank-quota` 留一个空壳（一次 16 个）：
    detach 完成后再看 `Path.exists()` 跟随联接返回 False，`rmdir` 整段被跳过。
    """
    link, target = tmp_path / "mnt-dead", tmp_path / "gone"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True, encoding="gbk", errors="replace",
    )
    assert made.returncode == 0, made.stdout + made.stderr
    assert os.path.lexists(link) and not os.path.exists(link), "先造出悬空联接"
    wv.detach_and_remove(tmp_path / "nothing.vhdx", link)
    assert not os.path.lexists(link), "卸卷后的悬空联接必须删掉，否则每个进程留一个空壳"


def test_sweep_removes_orphan_mount_points(monkeypatch, tmp_path):
    """已死进程留下的挂载点由 `_sweep_stale` 收掉；活着的进程一个都不许动。"""
    monkeypatch.setattr(wv, "_pid_alive", lambda pid: pid == 12345)
    dead, live = tmp_path / "mnt-999999-aabbcc", tmp_path / "mnt-12345-aabbcc"
    dead.mkdir()
    live.mkdir()
    wv._sweep_stale(tmp_path)
    assert not dead.exists(), "死进程的孤儿挂载点要清掉"
    assert live.exists(), "活进程的挂载点不许碰（可能正有命令在里面跑）"


@REAL_VOLUME
def test_real_teardown_leaves_no_mount_point(tmp_path):
    """真挂一个卷再卸：VHDX 与挂载点都必须消失，不留悬空联接。"""
    vhdx, mount = tmp_path / "quota.vhdx", tmp_path / "mnt"
    report = wv.mount(vhdx, mount, wv.MIN_MB)
    assert report.ok is True, report.error
    branch = pathlib.Path(report.mount) / "blank-run-x"
    branch.mkdir()
    error = wv.detach_and_remove(report.vhdx, report.mount)
    assert error == "", error
    assert not os.path.exists(report.vhdx), "VHDX 必须删掉"
    assert not os.path.lexists(report.mount), "挂载点（含悬空联接）必须删掉"


def test_new_dir_lives_on_the_volume_and_drop_dir_removes_it(monkeypatch):
    _fake_mount(monkeypatch, capacity=64 * 1024 * 1024)
    volume = wv.QuotaVolume()
    report = volume.ensure(64)
    directory = volume.new_dir("blank-run-")
    try:
        assert directory.startswith(report.mount)
        assert os.path.isdir(directory)
    finally:
        volume.drop_dir(directory)
    assert not os.path.exists(directory), "临时目录用完必须清掉，别把卷塞满"
    volume.teardown()


# ------------------------------------------------------------------ 行为探针


def test_probe_refuses_to_claim_without_admin(monkeypatch):
    monkeypatch.setattr(wv, "is_admin", lambda: False)
    ok, error, details = boundary._probe_disk_quota()
    assert ok is False
    assert "管理员" in error
    assert details["admin"] is False


def test_probe_fails_when_mount_fails(monkeypatch):
    monkeypatch.setattr(wv, "is_admin", lambda: True)
    monkeypatch.setattr(
        wv, "mount", lambda *args, **kwargs: wv.VolumeReport(attempted=True, error="diskpart 不认这个参数")
    )
    ok, error, details = boundary._probe_disk_quota()
    assert ok is False and "diskpart" in error
    assert details["mount_error"]


@pytest.mark.skipif(sys.platform != "win32", reason="这条判的是 Windows 上 ENOSPC 的形状")
def test_probe_rejects_a_volume_that_is_not_really_bounded(monkeypatch, tmp_path):
    """把"卷"指到一块**普通**目录上、谎称容量 1MB：写完 20MB 也不报错，必须判失败。

    这条钉的是探针本身会不会被"挂载成功"骗过去——真容量的判据只能是写满后的 ENOSPC。
    """
    monkeypatch.setattr(wv, "is_admin", lambda: True)

    def fake_mount(vhdx, mount_dir, size_mb):
        os.makedirs(mount_dir, exist_ok=True)
        return wv.VolumeReport(
            attempted=True, ok=True, vhdx=str(vhdx), mount=str(mount_dir),
            size_mb=int(size_mb), capacity_bytes=1 * 1024 * 1024,
        )

    monkeypatch.setattr(wv, "mount", fake_mount)
    monkeypatch.setattr(wv, "detach_and_remove", lambda *args: "")
    ok, error, details = boundary._probe_disk_quota()
    assert ok is False
    assert "容量上限没有生效" in error
    assert details.get("enospc") is None
    assert details["written_mb"] > 1


@REAL_VOLUME
def test_probe_really_fills_a_64mb_volume_to_enospc():
    """真读数：64MB 卷写满必须拿到 ENOSPC，第二次写仍然失败。"""
    ok, error, details = boundary._probe_disk_quota()
    assert ok is True, error
    assert 32 <= details["capacity_mb"] <= 70
    assert details["enospc"] is True
    assert details["second_write_failed"] is True
    assert details["write_error"]["errno"] == 28 or details["write_error"]["winerror"] in (112, 39)


# -------------------------------------------------------------------- 账本


def _job(**enforced):
    default = {"memory_bytes": 1, "active_process_limit": 2, "kill_on_close": 1}
    default.update(enforced)
    return SimpleNamespace(ok=True, error="", io_error="", enforced=default)


def _cap(**overrides) -> Capability:
    fields = dict(
        platform=sys.platform,
        job=True,
        restricted_token=True,
        token_control=True,
        carrier_control=True,
        measured_at=time.time(),
    )
    fields.update(overrides)
    return Capability(**fields)


def test_ledger_requires_both_probe_and_scratch_on_quota(monkeypatch):
    """探针证明 + 临时区真的落在卷上，两条同真才算；缺一条就说清缺哪条。"""
    monkeypatch.setattr(boundary, "measure", lambda **_: _cap(disk_quota=True))
    ready = wv.VolumeReport(ok=True, capacity_bytes=64 * 1024 * 1024, mount="M:\\mnt")
    facts = _enforcement(_job(), True, "", ready, "M:\\mnt\\blank-run-1")
    assert facts["disk_enforced"] is True
    assert facts["disk_bytes"] == 64 * 1024 * 1024
    assert facts["disk_error"] == ""

    # 卷挂上了，但临时目录没建在卷上：命令写的是宿主 TEMP，容量上限管不到它。
    no_scratch = _enforcement(_job(), True, "", ready, "")
    assert no_scratch["disk_enforced"] is False
    assert "临时目录没建在卷上" in no_scratch["disk_error"]

    # 探针没证明 ENOSPC：卷再正常也不许点绿。
    monkeypatch.setattr(
        boundary, "measure", lambda **_: _cap(disk_quota=False, disk_quota_error="写满还能写")
    )
    unproven = _enforcement(_job(), True, "", ready, "M:\\mnt\\blank-run-1")
    assert unproven["disk_enforced"] is False
    assert unproven["disk_error"] == "写满还能写"


def test_ledger_reports_mount_failure_not_a_silent_false(monkeypatch, tmp_path):
    """挂载失败时临时区会退回宿主目录（scratch 非空）——账本仍须按卷的 ok 说 False。

    这里刻意传一个真实存在的宿主目录当 scratch：如果只断言"scratch 为空才 False"，
    这条用例会被 scratch 条件遮住，撤掉 `volume.ok` 也照样绿（实测变异抓到的死判据）。
    """
    monkeypatch.setattr(boundary, "measure", lambda **_: _cap(disk_quota=True))
    fallback = tmp_path / "blank-run-fallback"
    fallback.mkdir()
    failed = wv.VolumeReport(attempted=True, ok=False, error="需要管理员权限才能挂载容量受限卷")
    facts = _enforcement(_job(), True, "", failed, str(fallback))
    assert facts["disk_enforced"] is False, "卷没挂上，临时区在宿主目录，容量上限没人执行"
    assert facts["disk_bytes"] == 0
    assert facts["disk_error"] == "需要管理员权限才能挂载容量受限卷"


def test_off_path_has_disk_fields_too():
    off = limits_not_applied("沙箱开关为关")
    assert off["disk_enforced"] is False
    assert off["disk_bytes"] == 0
    assert off["disk_error"] == "沙箱开关为关"


def test_restricted_and_appcontainer_ledgers_use_the_same_disk_rule(monkeypatch):
    from blank_api.services.sandbox import runner

    monkeypatch.setattr(boundary, "measure", lambda **_: _cap(disk_quota=True, file_boundary=True))
    report = SimpleNamespace(
        job=SimpleNamespace(
            created=True,
            assigned=True,
            error="",
            io_error="",
            enforced={"memory_bytes": 1, "active_process_limit": 2, "kill_on_close": 1},
        ),
        disk_on_quota=True,
        disk_bytes=64 * 1024 * 1024,
        disk_error="",
    )
    for ledger in (runner._enforcement_restricted, runner._enforcement_appcontainer):
        facts = ledger(report)
        assert facts["disk_enforced"] is True
        assert facts["disk_bytes"] == 64 * 1024 * 1024

    monkeypatch.setattr(boundary, "measure", lambda **_: _cap(disk_quota=False))
    for ledger in (runner._enforcement_restricted, runner._enforcement_appcontainer):
        facts = ledger(report)
        assert facts["disk_enforced"] is False
        assert facts["disk_error"], "不许只给一个 False，必须写上为什么"


def test_gateway_carries_disk_fields_and_drops_everything_else():
    field = sandbox_backend_event_field(
        {
            "sandbox_backend": "local",
            "data": {
                "enforcement": {
                    "limits_applied": True,
                    "disk_enforced": True,
                    "disk_bytes": 67108864,
                    "disk_error": "",
                    "host_path": r"C:\Users\someone\secret",
                }
            },
        }
    )
    facts = field["sandbox_enforcement"]
    assert facts["disk_enforced"] is True
    assert facts["disk_bytes"] == 67108864
    assert "host_path" not in facts


def test_governance_snapshot_exposes_disk_mb(monkeypatch):
    monkeypatch.setenv("BLANK_SANDBOX_DISK_MB", "640")
    monkeypatch.setattr(boundary, "_CACHE", (time.time(), _cap(disk_quota=False)))
    block = governance.sandbox_boundary()
    assert block["disk_mb"] == 640
    assert block["enforced"]["disk"] is False, "探针没证明 / 卷没挂上时，配置值不许冒充生效值"


# ------------------------------------------------------- 真实执行：容量卷上的临时区


@REAL_VOLUME
def test_local_sandbox_points_temp_at_the_quota_volume_and_hits_enospc(monkeypatch, tmp_path):
    """真跑一条命令：TEMP 在容量卷上，写超容量由内核拒绝（errno 28）。"""
    monkeypatch.setenv("BLANK_SANDBOX_DISK_MB", "64")
    monkeypatch.setattr(boundary, "_CACHE", (time.time(), _cap(disk_quota=True)))
    volume = wv.QuotaVolume()
    monkeypatch.setattr(wv, "quota_volume", lambda: volume)
    try:
        result = asyncio.run(
            LocalSandbox(SandboxPolicy()).run("echo TEMP=%TEMP%", workspace=str(tmp_path))
        )
        assert result.exit_code == 0
        assert "blank-run-" in result.stdout
        facts = result.enforcement
        assert facts["disk_enforced"] is True
        assert facts["disk_bytes"] > 0
        assert any("容量上限" in line and "工作区" in line for line in result.notes), (
            "备注必须说清这条上限只管临时区，不许让读者以为工作区也被限了")

        code = (
            "import os; p=os.path.join(os.environ['TEMP'],'big.bin'); "
            "d=b'x'*(70*1024*1024); f=open(p,'wb'); f.write(d); f.close(); print('WROTE_ALL')"
        )
        over = asyncio.run(
            LocalSandbox(SandboxPolicy()).run(
                [sys.executable, "-c", code], workspace=str(tmp_path)
            )
        )
        assert over.exit_code not in (0, None), f"超容量写入居然成功了：{over.stdout!r}"
        assert "No space left on device" in over.stderr or "Errno 28" in over.stderr
    finally:
        volume.teardown()


@REAL_VOLUME
def test_restricted_child_can_write_its_quota_temp(monkeypatch, tmp_path):
    """受限令牌子进程必须能写 TEMP：卷根/临时目录没有它的 ACE 时，临时区等于摆设。

    先在卷上写一个小文件并读回；读回内容证明写的是同一个目录，不是被拒后静默降级。
    """
    from blank_api.services.sandbox import windows_restricted as wr

    if not wr.available():
        pytest.skip("本机受限令牌后端未通过准入自检（v160 的判据负责解释原因）")
    volume = wv.QuotaVolume()
    monkeypatch.setattr(wv, "quota_volume", lambda: volume)
    try:
        report = wr.run_command(
            'echo quota-ok>"%TEMP%\\w.txt" && type "%TEMP%\\w.txt"',
            workspace=str(tmp_path),
            env={},
            timeout=60,
            disk_mb=64,
        )
        assert report.disk_on_quota is True, report.disk_error
        assert b"quota-ok" in report.stdout, (report.stdout, report.stderr, report.error)
    finally:
        volume.teardown()
