"""容量受限卷：把一个 VHDX 挂到目录上，给沙箱临时区一个内核强制的容量上限。

为什么不用目录配额：NTFS 没有目录级配额；按用户/按卷的配额会误伤同卷上的其他进程。
VHDX 挂载点是一个独立卷，`maximum` 就是它的容量，写满由内核返回 ENOSPC。
2026-11-10 在 build 26300 上用管理员权限实测：64MB 卷可用容量 62.93MB；写 48MB 后
剩 3.14MB，继续写得到 `OSError errno 28`；detach 后无残留。

它约束的是**沙箱临时区**（TEMP/TMP），不是用户工作区：工作区是用户自己挑的目录，
为了配额把它换成挂载点会改掉"文件落在哪"这件事。账本与面板文案都按这个范围说。
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MB = 2048
MIN_MB = 32
_PROBE_MARK = ".blank-write-check"


def is_admin() -> bool:
    """当前进程是否提权。VHD 挂载与格式化都需要管理员。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _mb(value: int) -> int:
    return int(value) * 1024 * 1024


def _tail(text: str, limit: int = 300) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return " | ".join(lines[-3:])[:limit]


def _diskpart(commands: list[str], timeout: float = 180.0) -> tuple[int, str]:
    """跑一段 diskpart 脚本。输出在本机是 GBK；解不动也要留住文本，别把证据丢成替换符。"""
    fd, path = tempfile.mkstemp(prefix="blank-diskpart-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\r\n") as handle:
            handle.write("\n".join(commands) + "\n")
        proc = subprocess.run(  # noqa: S603 - 命令由本模块构造，路径只来自临时目录
            ["diskpart", "/s", path],
            capture_output=True,
            encoding="gbk",
            errors="replace",
            timeout=timeout,
        )
        return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    """进程还在不在。不能用 os.kill(pid, 0)：Windows 上任何非控制信号都会 TerminateProcess。"""
    if pid <= 0 or sys.platform != "win32":
        return False
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # 问不出来时按活着处理，宁可留着也不能拆别人的卷
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError):
        return True


def detach_and_remove(vhdx: str | Path, mount: str | Path) -> str:
    """卸载并删掉 VHDX。返回错误文本（空 = 干净）。已经不在挂载表里不算错。"""
    vhdx_path, mount_path = Path(vhdx), Path(mount)
    error = ""
    if vhdx_path.exists():
        rc, output = _diskpart([f'select vdisk file="{vhdx_path}"', "detach vdisk"])
        if rc != 0:
            error = _tail(output) or f"diskpart rc={rc}"
        try:
            vhdx_path.unlink()
        except OSError as exc:
            error = error or f"删除 VHDX 失败：{exc}"
    try:
        # 卷卸下后挂载点会变成**悬空**目录联接：`Path.exists()` 跟随联接看到目标已消失，
        # 会返回 False 从而跳过删除，每跑一个进程就留一个空壳（本机实测残留 16 个）。
        # 用 lexists 才看得到联接本身。
        if os.path.lexists(os.fspath(mount_path)):
            mount_path.rmdir()
    except OSError as exc:
        error = error or f"删除挂载点失败：{exc}"
    return error


@dataclass(slots=True)
class VolumeReport:
    attempted: bool = False
    ok: bool = False
    vhdx: str = ""
    mount: str = ""
    size_mb: int = 0
    capacity_bytes: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "attempted": self.attempted,
            "ok": self.ok,
            "size_mb": self.size_mb,
            "capacity_bytes": self.capacity_bytes,
            "error": self.error,
        }


def mount(vhdx: str | Path, mount_dir: str | Path, size_mb: int) -> VolumeReport:
    """建一个 `maximum=size_mb` 的卷并挂到空目录上，然后验证容量与可写。

    diskpart 在单步失败时也可能返回 0，所以判据不是退出码，而是**卷本身**：
    容量必须落在请求值附近，且能真实写入读回。
    """
    report = VolumeReport(attempted=True, vhdx=str(vhdx), mount=str(mount_dir), size_mb=int(size_mb or 0))
    if sys.platform != "win32":
        report.error = "非 Windows：没有 VHDX 挂载这条路"
        return report
    if not is_admin():
        report.error = "需要管理员权限才能挂载容量受限卷"
        return report
    if report.size_mb < MIN_MB:
        report.error = f"容量上限太小：{report.size_mb}MB（至少 {MIN_MB}MB）"
        return report
    # 同路径的残留先清掉：上一次崩在半路时 VHDX 可能还挂在表里。
    detach_and_remove(vhdx, mount_dir)
    try:
        Path(vhdx).parent.mkdir(parents=True, exist_ok=True)
        Path(mount_dir).mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        report.error = f"准备挂载目录失败：{exc}"
        return report

    rc, output = _diskpart([
        f'create vdisk file="{vhdx}" maximum={report.size_mb} type=expandable',
        f'select vdisk file="{vhdx}"',
        "attach vdisk",
        "create partition primary",
        "format fs=ntfs quick label=BlankQuota",
        f'assign mount="{mount_dir}"',
    ])
    if not Path(vhdx).exists():
        report.error = f"diskpart 没建出 VHDX（rc={rc}）：{_tail(output)}"
        detach_and_remove(vhdx, mount_dir)
        return report
    try:
        usage = shutil.disk_usage(mount_dir)
    except OSError as exc:
        report.error = f"挂载后读不到卷容量（rc={rc}）：{exc}；{_tail(output)}"
        detach_and_remove(vhdx, mount_dir)
        return report
    report.capacity_bytes = int(usage.total)
    expected = _mb(report.size_mb)
    if usage.total < expected * 0.5 or usage.total > expected * 1.1:
        report.error = (
            f"卷容量 {usage.total / 1024 / 1024:.1f}MB 与请求 {report.size_mb}MB 对不上（rc={rc}）"
        )
        detach_and_remove(vhdx, mount_dir)
        return report
    probe = Path(mount_dir) / _PROBE_MARK
    try:
        probe.write_bytes(b"blank")
        if probe.read_bytes() != b"blank":
            raise OSError("读回内容不一致")
        probe.unlink()
    except OSError as exc:
        report.error = f"容量卷不可写：{exc}"
        detach_and_remove(vhdx, mount_dir)
        return report
    report.ok = True
    return report


def _base_dir() -> Path:
    """卷与挂载点放哪。默认 ProgramData：受限令牌/AC 子进程要能**穿过**上级目录才能
    用挂载点里的临时目录，而 `%LOCALAPPDATA%\\Temp` 的 ACL 不给 Users 遍历权；
    ProgramData 默认就带 `Users:(OI)(CI)(RX)`，新建子目录自动继承。"""
    raw = str(os.environ.get("BLANK_SANDBOX_DISK_ROOT") or "").strip()
    if raw:
        root = Path(raw)
    elif is_admin():
        root = Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "blank-quota"
    else:
        root = Path(tempfile.gettempdir()) / "blank-quota"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sweep_stale(base: Path) -> None:
    """清掉已死进程留下的卷与挂载点。只动本模块命名的 `quota-<pid>-*`/`mnt-<pid>-*`。"""
    for vhdx in base.glob("quota-*.vhdx"):
        match = re.match(r"quota-(\d+)-", vhdx.name)
        if not match or _pid_alive(int(match.group(1))):
            continue
        try:
            if time.time() - vhdx.stat().st_mtime < 60:
                continue
        except OSError:
            continue
        detach_and_remove(vhdx, base / ("mnt-" + vhdx.stem[len("quota-"):]))
    # 旧版清理只删 VHDX，卸卷后的悬空联接会留在基目录里；这里按 pid 收掉孤儿挂载点。
    for mount in base.glob("mnt-*"):
        match = re.match(r"mnt-(\d+)-", mount.name)
        if not match or _pid_alive(int(match.group(1))):
            continue
        try:
            if os.path.lexists(os.fspath(mount)):
                mount.rmdir()
        except OSError:
            continue


class QuotaVolume:
    """进程内一个容量卷。命令级临时目录建在它上面，生命周期跟着进程走。

    `ensure()` 失败后按同一容量重复调用不会再跑 diskpart：没有管理员权限的机器上，
    每条命令都等一次注定失败的挂载，等于给所有命令加一笔无谓的延迟。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._report: VolumeReport | None = None
        self._size_mb = -1

    def ensure(self, size_mb: int) -> VolumeReport:
        size = int(size_mb or 0)
        with self._lock:
            if self._report is not None and self._size_mb == size:
                return self._report
            self._teardown_locked()
            self._size_mb = size
            if size <= 0:
                self._report = VolumeReport(error="未启用（容量上限为 0）")
                return self._report
            try:
                base = _base_dir()
                _sweep_stale(base)
            except OSError as exc:
                self._report = VolumeReport(attempted=True, size_mb=size, error=f"准备卷目录失败：{exc}")
                return self._report
            token = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
            self._report = mount(base / f"quota-{token}.vhdx", base / f"mnt-{token}", size)
            return self._report

    def ready(self) -> bool:
        report = self._report
        return bool(report and report.ok)

    def report(self) -> VolumeReport:
        return self._report or VolumeReport()

    def new_dir(self, prefix: str) -> str:
        with self._lock:
            report = self._report
            if not (report and report.ok):
                return ""
            path = Path(report.mount) / f"{prefix}{uuid.uuid4().hex[:8]}"
            try:
                path.mkdir()
            except OSError:
                return ""
            return str(path)

    def drop_dir(self, path: str) -> None:
        if path:
            shutil.rmtree(path, ignore_errors=True)

    def teardown(self) -> None:
        with self._lock:
            self._teardown_locked()

    def reset(self) -> None:
        self.teardown()

    def _teardown_locked(self) -> None:
        report, self._report = self._report, None
        self._size_mb = -1
        if report is not None and report.attempted and report.vhdx:
            detach_and_remove(report.vhdx, report.mount)


_SINGLETON: QuotaVolume | None = None


def quota_volume() -> QuotaVolume:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = QuotaVolume()
    return _SINGLETON


def temp_dir_for(prefix: str, size_mb: int) -> tuple[str, bool, int, str]:
    """给一次执行准备临时目录：优先容量卷，挂不上就退回宿主临时目录并带上原因。

    返回 `(目录, 是否在容量卷上, 卷容量字节, 失败原因)`。
    """
    volume = quota_volume()
    report = volume.ensure(size_mb)
    path = volume.new_dir(prefix)
    if path:
        return path, True, report.capacity_bytes, ""
    fallback = tempfile.mkdtemp(prefix=prefix)
    return fallback, False, 0, report.error or "容量卷不可用，临时区退回宿主目录"


def _cleanup_at_exit() -> None:
    try:
        quota_volume().teardown()
    except Exception:  # noqa: BLE001 - 退出清理不能再抛
        pass


atexit.register(_cleanup_at_exit)
