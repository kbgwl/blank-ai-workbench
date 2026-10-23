"""Windows Job Object：把 policy 里的 memory/pids 变成内核真的会拒的限额。

为什么只有 Job：M0 探针在这台机器（非管理员 / Windows 11 build 26300）实测，
AppContainer 入口对任何载荷都返回 575、`CreateRestrictedToken` 返回 998、
`CreateProcessWithTokenW` 报 1314（见 docs/SANDBOX-WINDOWS-DESIGN.md §2.0）。
唯一被内核强制的非管理员原语是 Job Object，而且三条限额都有阳性对照：
`PROCESS_MEMORY`（48MB 上限时 256MB 分配被拒）、`ACTIVE_PROCESS`（第二个进程 assign 被拒）、
`KILL_ON_JOB_CLOSE`（关句柄带走 600 秒睡眠的子进程）。

所以本模块**只承诺这三件事**。文件与网络边界在 local 后端不由系统强制，
谁在这里写"已隔离文件系统"谁就是在骗用户。
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass, field

JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
PROCESS_SET_QUOTA = 0x00000100
PROCESS_TERMINATE = 0x00000001


@dataclass(slots=True)
class Job:
    """一枚 Job 句柄 + 内核**实际接受**的限额。

    `enforced` 只写设成功的东西：设不上就必须留 `error`，不许静默假装设上了。
    """

    handle: int = 0
    enforced: dict[str, int] = field(default_factory=dict)
    error: str = ""

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

    _k32.CreateJobObjectW.restype = ctypes.c_void_p
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    _k32.SetInformationJobObject.restype = ctypes.c_bool
    _k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    _k32.AssignProcessToJobObject.restype = ctypes.c_bool
    _k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _k32.OpenProcess.restype = ctypes.c_void_p
    _k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    _k32.CloseHandle.restype = ctypes.c_bool
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]


def _err() -> str:
    code = ctypes.get_last_error()
    buf = ctypes.create_unicode_buffer(512)
    # FORMAT_MESSAGE_FROM_SYSTEM | IGNORE_INSERTS。不能带 ALLOCATE_BUFFER(0x100)：
    # 那个标志要求传"缓冲区指针的指针"，配上我们自己的 buf 就等于让 API 把地址写进内容里，
    # 取回来的是乱码——而这些字符串会进 job_error、进事件、进用户看到的说明。
    _k32.FormatMessageW(0x0200 | 0x1000, None, code, 0, buf, 512, None)
    return f"{code} {(buf.value or '').strip() or '未知错误'}"


def create_job(name: str, *, memory_bytes: int = 0, active_process_limit: int = 0,
               kill_on_close: bool = True) -> Job:
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
    return Job(handle=int(handle), enforced=enforced)


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


def close_job(job: Job) -> None:
    """关句柄 = 沙箱生命周期结束：挂了 KILL_ON_JOB_CLOSE 时整棵树被带走（P6.7 实测生效）。"""
    if job.handle:
        _k32.CloseHandle(job.handle)
        job.handle = 0
