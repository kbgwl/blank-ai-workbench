"""Windows Job Object：把 policy 里的 memory/pids/cpus 变成内核真的会拒的限额。

为什么只有 Job：M0 探针在这台机器（非管理员 / Windows 11 build 26300）实测，
AppContainer 入口对任何载荷都返回 575、`CreateRestrictedToken` 返回 998、
`CreateProcessWithTokenW` 报 1314（见 docs/SANDBOX-WINDOWS-DESIGN.md §2.0）。
唯一被内核强制的非管理员原语是 Job Object，而且限额都有阳性对照：
`PROCESS_MEMORY`（48MB 上限时 256MB 分配被拒）、`ACTIVE_PROCESS`（第二个进程 assign 被拒）、
`KILL_ON_JOB_CLOSE`（关句柄带走 600 秒睡眠的子进程）；CPU 限速在 build 26300 上
实测可设且真的压得住（`JobObjectCpuRateControlInformation`）；IO 速率上限同样实测生效
（`JobObjectIoRateControlInformation`，16/32/8 MB/s 三档实测误差 <1.5%）。

所以本模块承诺的是这五项。文件与网络边界在 local 后端不由系统强制，
谁在这里写"已隔离文件系统"谁就是在骗用户。
"""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass, field

JobObjectExtendedLimitInformation = 9
JobObjectCpuRateControlInformation = 15
JobObjectIoRateControlInformation = 31
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
# CpuRate 的单位是整机总算力的万分之一：16 核机器上 625 = 1 核，10000 = 全部。
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x00000001
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x00000004
JOB_OBJECT_IO_RATE_CONTROL_ENABLE = 0x00000001
PROCESS_SET_QUOTA = 0x00000100
PROCESS_TERMINATE = 0x00000001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
CREATE_SUSPENDED = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002
TH32CS_SNAPTHREAD = 0x00000004


@dataclass(slots=True)
class Job:
    """一枚 Job 句柄 + 内核**实际接受**的限额。

    `enforced` 只写设成功的东西：设不上就必须留 `error`/`cpu_error`/`io_error`，
    不许静默假装设上了。CPU/IO 限速设不上不毁掉整个 Job——内存/进程数仍然有效，
    但账上必须把对应项标成没强制。
    """

    handle: int = 0
    enforced: dict[str, int] = field(default_factory=dict)
    error: str = ""
    cpu_error: str = ""
    io_error: str = ""

    @property
    def ok(self) -> bool:
        return self.handle != 0 and not self.error


def supported() -> bool:
    return sys.platform == "win32"


def parse_size(text: str | int) -> int:
    """`"512m"` / `"128k"` / `"1g"` / 裸字节数 → 字节。认不出来返回 0（=不设这条限）。"""
    if isinstance(text, int):
        return max(0, text)
    s = str(text).strip().lower()
    if not s:
        return 0
    mult = 1
    if s[-1] == "k":
        mult, s = 1024, s[:-1]
    elif s[-1] == "m":
        mult, s = 1024 * 1024, s[:-1]
    elif s[-1] == "g":
        mult, s = 1024 ** 3, s[:-1]
    try:
        return max(0, int(float(s) * mult))
    except ValueError:
        return 0


if supported():
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _Io(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _ExtLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _Basic),
            ("IoInfo", _Io),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _CpuRateControl(ctypes.Structure):
        # DWORD ControlFlags + union { DWORD CpuRate; DWORD Weight; }，共 8 字节。
        _fields_ = [
            ("ControlFlags", ctypes.c_uint32),
            ("CpuRate", ctypes.c_uint32),
        ]

    class _IoRatePublic(ctypes.Structure):
        # 公共头文件（jobapi2.h）里的 40 字节版。build 26300 上 Set 会回
        # ERROR_BAD_LENGTH=24——这台机器要的是带 VolumeNameLength 的 NATIVE_V1。
        _fields_ = [
            ("MaxIops", ctypes.c_int64),
            ("MaxBandwidth", ctypes.c_int64),
            ("ReservationIops", ctypes.c_int64),
            ("VolumeName", ctypes.c_wchar_p),
            ("BaseIoSize", ctypes.c_uint32),
            ("ControlFlags", ctypes.c_uint32),
        ]

    class _IoRateV1(ctypes.Structure):
        # NATIVE_V1：在公共版末尾多一个 USHORT VolumeNameLength，按 8 字节对齐后共 48。
        # 上一轮把 MaxBandwidthStrict 插在 ReservationIops 与 VolumeName 之间，ControlFlags
        # 落到结构之外：Set 返回成功、限速却根本没开。字段顺序不许凭记忆改。
        _fields_ = [
            ("MaxIops", ctypes.c_int64),
            ("MaxBandwidth", ctypes.c_int64),
            ("ReservationIops", ctypes.c_int64),
            ("VolumeName", ctypes.c_wchar_p),
            ("BaseIoSize", ctypes.c_uint32),
            ("ControlFlags", ctypes.c_uint32),
            ("VolumeNameLength", ctypes.c_uint16),
        ]

    _k32.CreateJobObjectW.restype = ctypes.c_void_p
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    _k32.SetInformationJobObject.restype = ctypes.c_bool
    _k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    _k32.AssignProcessToJobObject.restype = ctypes.c_bool
    _k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _k32.IsProcessInJob.restype = ctypes.c_bool
    _k32.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_bool)]
    _k32.OpenProcess.restype = ctypes.c_void_p
    _k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    _k32.CloseHandle.restype = ctypes.c_bool
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_uint32),
            ("cntUsage", ctypes.c_uint32),
            ("th32ThreadID", ctypes.c_uint32),
            ("th32OwnerProcessID", ctypes.c_uint32),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", ctypes.c_uint32),
        ]

    _k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    _k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    _k32.Thread32First.restype = ctypes.c_bool
    _k32.Thread32First.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ThreadEntry32)]
    _k32.Thread32Next.restype = ctypes.c_bool
    _k32.Thread32Next.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ThreadEntry32)]
    _k32.OpenThread.restype = ctypes.c_void_p
    _k32.OpenThread.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    _k32.ResumeThread.restype = ctypes.c_uint32
    _k32.ResumeThread.argtypes = [ctypes.c_void_p]


def _err() -> str:
    code = ctypes.get_last_error()
    buf = ctypes.create_unicode_buffer(512)
    # FORMAT_MESSAGE_FROM_SYSTEM | IGNORE_INSERTS。不能带 ALLOCATE_BUFFER(0x100)：
    # 那个标志要求传"缓冲区指针的指针"，配上我们自己的 buf 就等于让 API 把地址写进内容里，
    # 取回来的是乱码——而这些字符串会进 job_error、进事件、进用户看到的说明。
    _k32.FormatMessageW(0x0200 | 0x1000, None, code, 0, buf, 512, None)
    return f"{code} {(buf.value or '').strip() or '未知错误'}"


def cpu_rate_for(cpus: float, logical_cpus: int | None = None) -> int:
    """把"允许几核"换成 Job 的 CpuRate（整机万分之一）。

    这不是拍脑袋的换算：2026-11-10 在本机（16 逻辑核、非管理员、build 26300）实测，
    16 个自旋子进程的基线合计 40.2 CPU 秒；CpuRate=625（1 核）压到 3.69 秒，
    1250（2 核）压到 7.13 秒。所以 10000 = 整机全部算力，不是"一个核的 100%"。
    `cpus` 不小于整机核数时返回 10000——那不是限制，调用方据此不设、也不许记成已强制。
    """
    cores = logical_cpus if logical_cpus is not None else (os.cpu_count() or 1)
    if cpus <= 0 or cores <= 0:
        return 0
    return max(1, min(10000, int(round(float(cpus) * 10000.0 / cores))))


def _set_io_rate(handle: int, bps: int) -> str:
    """把每秒字节上限设进 Job。空串=成功，否则是两种结构布局各自的错误。

    `VolumeName` 传 NULL 表示 Job 内所有卷合计受这条上限约束。2026-11-10 本机实测：
    对照（不设限）680.9 MB/s；请求 16/32/8 MB/s 时限速侧实测 16.1/32.5/8.0 MB/s。
    先试 NATIVE_V1（48 字节），失败再试公共 40 字节版——build 26300 上前者可用，
    后者回 ERROR_BAD_LENGTH=24；老系统上可能反过来。
    """
    native = _IoRateV1(0, bps, 0, None, 0, JOB_OBJECT_IO_RATE_CONTROL_ENABLE, 0)
    if _k32.SetInformationJobObject(
        handle, JobObjectIoRateControlInformation, ctypes.byref(native), ctypes.sizeof(native)
    ):
        return ""
    native_error = _err()
    public = _IoRatePublic(0, bps, 0, None, 0, JOB_OBJECT_IO_RATE_CONTROL_ENABLE)
    if _k32.SetInformationJobObject(
        handle, JobObjectIoRateControlInformation, ctypes.byref(public), ctypes.sizeof(public)
    ):
        return ""
    return f"NATIVE_V1(48B) -> {native_error}；public(40B) -> {_err()}"


def create_job(name: str, *, memory_bytes: int = 0, active_process_limit: int = 0,
               kill_on_close: bool = True, cpu_cpus: float = 0.0,
               io_bps: int = 0) -> Job:
    """建 Job 并设限。任何一步失败都返回带 `error` 的 Job——调用方据此把限额标成"未强制"。"""
    if not supported():
        return Job(error=f"本平台不是 Windows（sys.platform={sys.platform!r}），没有 Job Object")
    handle = _k32.CreateJobObjectW(None, name)
    if not handle:
        return Job(error="CreateJobObjectW -> " + _err())
    flags = 0
    info = _ExtLimits()
    if memory_bytes:
        flags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY
        info.ProcessMemoryLimit = memory_bytes
    if active_process_limit:
        flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        info.BasicLimitInformation.ActiveProcessLimit = active_process_limit
    if kill_on_close:
        flags |= JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    info.BasicLimitInformation.LimitFlags = flags
    enforced: dict[str, int] = {}
    if kill_on_close:
        enforced["kill_on_close"] = 1
    if flags and not _k32.SetInformationJobObject(
        handle, JobObjectExtendedLimitInformation, ctypes.byref(info),
            ctypes.sizeof(info)):
        code = _err()
        _k32.CloseHandle(handle)
        return Job(error=f"SetInformationJobObject(flags=0x{flags:X}) -> {code}")
    if memory_bytes:
        enforced["memory_bytes"] = memory_bytes
    if active_process_limit:
        enforced["active_process_limit"] = active_process_limit
    cpu_rate = cpu_rate_for(cpu_cpus) if cpu_cpus > 0 else 0
    cpu_error = ""
    if cpu_cpus > 0 and cpu_rate >= 10000:
        # 上限不小于整机核数，设了也是摆设。账上必须说清"没构成限制"，不许当成功。
        cpu_error = f"cpus={cpu_cpus} 不小于整机 {os.cpu_count() or 1} 个逻辑核，未设 CPU 限速"
    elif cpu_rate:
        rate_info = _CpuRateControl(
            JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP,
            cpu_rate,
        )
        if _k32.SetInformationJobObject(
            handle, JobObjectCpuRateControlInformation, ctypes.byref(rate_info),
                ctypes.sizeof(rate_info)):
            enforced["cpu_rate"] = cpu_rate
        else:
            # CPU 设不上不毁掉 Job：内存/进程数仍然有效，但 cpu 这一项必须留成没强制。
            cpu_error = "SetInformationJobObject(JobObjectCpuRateControlInformation) -> " + _err()
    io_error = ""
    if io_bps > 0:
        io_error = _set_io_rate(handle, io_bps)
        if not io_error:
            enforced["io_bps"] = io_bps
    return Job(handle=int(handle), enforced=enforced, cpu_error=cpu_error, io_error=io_error)


def assign_pid(job: Job, pid: int) -> tuple[bool, str]:
    """把已存在的进程挂进 Job。

    注意这里有个**真实且无法消除的竞态**：命令是 `create_subprocess_shell` 起来的，
    等我们能 OpenProcess 时，它可能已经派生了子进程。所以 active_process_limit 只能保证
    "从现在起不再放进来的超过上限"，不能保证"第一个孙子也算数"。要消掉这段窗口只能
    用 CREATE_SUSPENDED 起进程（M1 的 runner 化解决），这里先把话说清楚而不是假装严密。
    """
    if not job.ok:
        return False, job.error or "Job 句柄无效"
    ph = _k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
    if not ph:
        return False, "OpenProcess(pid=%d) -> %s" % (pid, _err())
    try:
        if not _k32.AssignProcessToJobObject(job.handle, ph):
            return False, "AssignProcessToJobObject(pid=%d) -> %s" % (pid, _err())
    finally:
        _k32.CloseHandle(ph)
    return True, ""


def assign_handle(job: Job, process_handle: int) -> tuple[bool, str]:
    """按**进程句柄**挂进 Job——CREATE_SUSPENDED 起进程后用它，没有 pid 竞态。

    runner 化之后不再先起再查 pid：进程起来时是挂起的，句柄一直在我们手里，
    `AssignProcessToJobObject` 成功后才会 `ResumeThread`，所以第一个子进程也一定在 Job 里。
    """
    if not job.ok:
        return False, job.error or "Job 句柄无效"
    if not process_handle:
        return False, "进程句柄为空"
    if not _k32.AssignProcessToJobObject(job.handle, ctypes.c_void_p(process_handle)):
        return False, "AssignProcessToJobObject(handle=%#x) -> %s" % (process_handle, _err())
    return True, ""


def resume_process(pid: int) -> tuple[bool, str]:
    """恢复一个用 ``CREATE_SUSPENDED`` 起出来的进程。

    ``asyncio.create_subprocess_*`` 不暴露主线程句柄，拿不到 ``ResumeThread`` 的入参；
    这里枚举该进程的线程并逐个恢复。只对我们刚创建、尚未 resume 的进程调用，此时
    通常只有主线程一条。``assign_pid`` 必须在 resume 之前完成——进程还挂起时不会
    派生子进程，这正是消除「先起进程再挂 Job」竞态窗口的关键。
    """
    if not supported():
        return False, f"本平台不是 Windows（sys.platform={sys.platform!r}）"
    if pid <= 0:
        return False, "进程 pid 无效"
    snapshot = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return False, "CreateToolhelp32Snapshot -> " + _err()
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(_ThreadEntry32)
        if not _k32.Thread32First(snapshot, ctypes.byref(entry)):
            return False, "Thread32First -> " + _err()
        resumed = 0
        while True:
            if entry.th32OwnerProcessID == pid:
                thread = _k32.OpenThread(THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if not thread:
                    return False, "OpenThread(%d) -> %s" % (entry.th32ThreadID, _err())
                try:
                    if _k32.ResumeThread(thread) == 0xFFFFFFFF:
                        return False, "ResumeThread(%d) -> %s" % (entry.th32ThreadID, _err())
                    resumed += 1
                finally:
                    _k32.CloseHandle(thread)
            if not _k32.Thread32Next(snapshot, ctypes.byref(entry)):
                break
        if not resumed:
            return False, "没有找到 pid=%d 的线程" % pid
        return True, ""
    finally:
        _k32.CloseHandle(snapshot)


def close_job(job: Job) -> None:
    """关句柄 = 沙箱生命周期结束：挂了 KILL_ON_JOB_CLOSE 时整棵树被带走（P6.7 实测生效）。"""
    if job.handle:
        _k32.CloseHandle(job.handle)
        job.handle = 0


def pid_in_job(job: Job, pid: int) -> tuple[bool | None, str]:
    """这个 pid 此刻是否真的挂在该 Job 上。

    为什么单开一条查询：``assign_pid`` 的返回值只说明"系统接受了这次赋值"，
    证明不了随后的进程身份。验证装置必须能直接问内核，否则"已纳入边界"仍然只是
    我们自己的说法。查询失败返回 ``None``，不猜成 False——查不到与不在里面是两件事。
    """
    if not supported():
        return None, f"本平台不是 Windows（sys.platform={sys.platform!r}），没有 Job Object"
    if not job.ok:
        return None, job.error or "Job 句柄无效"
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None, "OpenProcess(pid=%d) -> %s" % (pid, _err())
    try:
        inside = ctypes.c_bool(False)
        if not _k32.IsProcessInJob(handle, job.handle, ctypes.byref(inside)):
            return None, "IsProcessInJob(pid=%d) -> %s" % (pid, _err())
        return bool(inside.value), ""
    finally:
        _k32.CloseHandle(handle)
