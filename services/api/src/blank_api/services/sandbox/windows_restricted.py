"""受限令牌执行后端：非管理员 Windows 上真能把 `~/.ssh` 这类私有目录拒掉的那一层。

M0 的旧探针曾把 `CreateRestrictedToken` 记成"恒 998"，那是探针自己多传了一个参数
（10 个实参对 9 个形参），不是机器结论。2026-11-09 在本机（非管理员 / build 26300）
逐条重测后，可用的形状是：

- 取当前进程令牌 → `CreateRestrictedToken(DISABLE_MAX_PRIVILEGE)`，限制 SID 集合取
  **Users ∪ Everyone ∪ Authenticated Users ∪ 登录 SID ∪ 能力 SID**。只挂 Everyone 会让
  `cmd.exe` 以 0xC0000142 起不来；把 Users 放进去才够它加载系统 DLL。
- 能力 SID 默认是**当前登录会话 SID**（`S-1-5-5-X-Y`）。2026-11-09 在本机实测：`Guests`
  （`S-1-5-32-546`）、`DCOM Users`、`Distributed COM Users` 这类 SID 即使写进限制列表，
  icacls 授权后限制检查仍判拒绝；登录会话 SID 才能同时"被授权"与"通过限制检查"，且它只属于
  本次登录会话，不会把工作区扩给别的用户或别的会话。代价是每次登录会往授权目录多留一条
  （对后续会话惰性的）ACE，见 `prepare` 的说明。
- 受限令牌的访问判定是"普通令牌允许 **且** 限制 SID 之一允许"。所以私有目录（ACL 只有
  当前用户 + SYSTEM）会被拒，而工作区只要给能力 SID 加一条 `(OI)(CI)M` 就能读写。
- `DuplicateTokenEx(TokenPrimary)` + `CreateProcessAsUserW`；启动标志用 **0 + `SW_HIDE`**。
  2026-11-09 实测：`DETACHED_PROCESS` 下受限子进程里的 Python 会 `0xC0000142`（DLL 初始化失败），
  `CREATE_NO_WINDOW` 连 `cmd.exe` 都起不来；同一令牌换成 flags=0 后 `python -c "print(7)"` 正常。
  标准输入必须给 `NUL`，否则 uv 的 Python 蹦床会报错。
- **命令用 inline `cmd /d /s /c "<命令>"`，不写临时 `.cmd`**：受限令牌下 cmd 能读批处理
  （`type s.cmd` 正常）却拒绝执行（`call`/直接调用一律 exit 1、零输出，与启动标志、限制 SID
  集合无关）。inline 与宿主 `create_subprocess_shell` 逐字同形，多行命令只跑第一行也是宿主
  本来就有的语义，不是本后端引入的差异。
- Job Object 用进程句柄直接挂（`CREATE_SUSPENDED` → 挂 Job → `ResumeThread`），消掉
  "按 PID 再 OpenProcess"那段子进程已派生的竞态。
- `bInheritHandles=True` 是标准输入输出的前提，但它同时把宿主里**所有带继承标志的句柄**
  递给子进程（浏览器管道、打开的文件、父进程的套接字）。改用 `STARTUPINFOEXW` +
  `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`，继承范围只剩 NUL/stdout/stderr 三个；属性表建不出来
  就拒绝启动，不退回"继承全部"——那正是要消灭的泄漏形状。

本模块只承诺**文件边界**。网络没有 AppContainer 就仍由系统放行，账本必须写
`network_enforced=False`——拿受限令牌去冒充网络隔离，就是本立项要消灭的那类谎。

自检是这套东西的准入条件，不是装饰：合成一个"家目录"（`.ssh` 只授权当前用户），
在同一个子进程里同时验证「读 .ssh 被拒」与「工作区读写成功」，再跑三条对照
（普通令牌能读、把能力 SID 加进 ACL 后能读、去掉限制 SID 的同载体能读）。
任何一条对照红掉，整次判定作废，后端不许启用。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import msvcrt
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from blank_api.services.sandbox import rules as rules_module
from blank_api.services.sandbox import windows_job, windows_volume

DEFAULT_CAPABILITY_SID = rules_module.DEFAULT_SID
RESTRICTING_SIDS = ("S-1-5-32-545", "S-1-1-0", "S-1-5-11")  # Users / Everyone / Authenticated Users
SYSTEM_SID = "S-1-5-18"

TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_DEFAULT = 0x0080
TOKEN_ADJUST_SESSIONID = 0x0100
MAXIMUM_ALLOWED = 0x02000000
DISABLE_MAX_PRIVILEGE = 0x00000001
SECURITY_IMPERSONATION = 2
TOKEN_PRIMARY = 1

CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100
STARTF_USESHOWWINDOW = 0x00000001
SW_HIDE = 0
HANDLE_FLAG_INHERIT = 0x00000001
WAIT_TIMEOUT = 0x00000102
SE_GROUP_LOGON_ID = 0xC0000000
CREATE_NO_WINDOW = 0x08000000
# 句柄白名单：bInheritHandles=True 时，子进程只拿到属性表里列出的这几个。
# 没有它，宿主进程里每一个带继承标志的句柄（日志、数据库、用户的其它文件）
# 都会原样漏进受限子进程——边界收窄了文件访问，却把宿主打开的东西递了进去。
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
EXTENDED_STARTUPINFO_PRESENT = 0x00080000

_SELF_CACHE: "SelfCheck | None" = None
_SELF_LOCK = threading.Lock()
_ACL_LOCK = threading.Lock()
_GRANTED: set[str] = set()
_CAPABILITY_LOCK = threading.Lock()
_CAPABILITY: dict[str, str] = {}


class RestrictedUnavailable(RuntimeError):
    """受限令牌后端起不来，或工作区授权失败。调用方必须拒绝执行，不许静默退回宿主。"""


class TokenError(RuntimeError):
    """令牌创建失败。与"机器不支持"分开写：前者可能是本进程的权限形状问题。"""


@dataclass(slots=True)
class SelfCheck:
    """一次准入自检的全部事实。字段就是证据，别在界面里重新推导。"""

    attempted: bool = False
    secret_denied: bool = False
    workspace_read: bool = False
    workspace_write: bool = False
    positive_control: bool = False
    negative_control: bool = False
    mutation_control: bool = False
    error: str = ""
    details: dict[str, str] = field(default_factory=dict)

    @property
    def boundary_ok(self) -> bool:
        return self.secret_denied and self.workspace_read and self.workspace_write

    @property
    def controls_ok(self) -> bool:
        return self.positive_control and self.negative_control and self.mutation_control

    @property
    def ok(self) -> bool:
        return self.attempted and not self.error and self.boundary_ok and self.controls_ok

    def as_dict(self) -> dict[str, object]:
        return {
            "attempted": self.attempted,
            "secret_denied": self.secret_denied,
            "workspace_read": self.workspace_read,
            "workspace_write": self.workspace_write,
            "positive_control": self.positive_control,
            "negative_control": self.negative_control,
            "mutation_control": self.mutation_control,
            "boundary_ok": self.boundary_ok,
            "controls_ok": self.controls_ok,
            "ok": self.ok,
            "error": self.error,
            "details": dict(self.details),
        }


@dataclass(slots=True)
class JobReport:
    created: bool = False
    assigned: bool = False
    error: str = ""
    enforced: dict[str, int] = field(default_factory=dict)
    # IO 限速单独设、单独失败：设不上时 Job 仍然可用，账本要能说出为什么。
    io_error: str = ""


@dataclass(slots=True)
class CommandReport:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    job: JobReport
    error: str = ""
    notes: list[str] = field(default_factory=list)
    grants: dict[str, list[str]] = field(default_factory=dict)
    # 临时区是否真的落在容量卷上，以及卷容量；`disk_error` 说明为什么没落上。
    # 账本不按 policy 里的 disk_mb 推断，只认这三项。
    disk_on_quota: bool = False
    disk_bytes: int = 0
    disk_error: str = ""


if sys.platform == "win32":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", ctypes.c_uint32),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", ctypes.c_bool),
        ]

    class _StartupInfoExW(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_uint32), ("lpReserved", ctypes.c_wchar_p), ("lpDesktop", ctypes.c_wchar_p),
            ("lpTitle", ctypes.c_wchar_p), ("dwX", ctypes.c_int32), ("dwY", ctypes.c_int32),
            ("dwXSize", ctypes.c_int32), ("dwYSize", ctypes.c_int32), ("dwXCountChars", ctypes.c_int32),
            ("dwYCountChars", ctypes.c_int32), ("dwFillAttribute", ctypes.c_uint32),
            ("dwFlags", ctypes.c_uint32), ("wShowWindow", ctypes.c_uint16), ("cbReserved2", ctypes.c_uint16),
            ("lpReserved2", ctypes.c_void_p), ("hStdInput", ctypes.c_void_p),
            ("hStdOutput", ctypes.c_void_p), ("hStdError", ctypes.c_void_p),
            ("lpAttributeList", ctypes.c_void_p),
        ]

    class _ProcessInformation(ctypes.Structure):
        _fields_ = [
            ("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p),
            ("dwProcessId", ctypes.c_uint32), ("dwThreadId", ctypes.c_uint32),
        ]

    _k32.InitializeProcThreadAttributeList.restype = wt.BOOL
    _k32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.c_size_t),
    ]
    _k32.UpdateProcThreadAttribute.restype = wt.BOOL
    _k32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p,
    ]
    _k32.DeleteProcThreadAttributeList.restype = None
    _k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_uint32)]

    _k32.GetCurrentProcess.restype = ctypes.c_void_p
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _k32.CloseHandle.restype = ctypes.c_bool
    _k32.SetHandleInformation.argtypes = [ctypes.c_void_p, wt.DWORD, wt.DWORD]
    _k32.SetHandleInformation.restype = wt.BOOL
    _k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wt.DWORD]
    _k32.WaitForSingleObject.restype = wt.DWORD
    _k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]
    _k32.GetExitCodeProcess.restype = wt.BOOL
    _k32.TerminateProcess.argtypes = [ctypes.c_void_p, wt.UINT]
    _k32.TerminateProcess.restype = wt.BOOL
    _k32.ResumeThread.argtypes = [ctypes.c_void_p]
    _k32.ResumeThread.restype = wt.DWORD
    _k32.LocalFree.argtypes = [ctypes.c_void_p]
    _k32.LocalFree.restype = ctypes.c_void_p

    _adv.OpenProcessToken.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    _adv.OpenProcessToken.restype = wt.BOOL
    _adv.CreateRestrictedToken.argtypes = [
        ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p,
        wt.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    ]
    _adv.CreateRestrictedToken.restype = wt.BOOL
    _adv.DuplicateTokenEx.argtypes = [
        ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _adv.DuplicateTokenEx.restype = wt.BOOL
    _adv.GetTokenInformation.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
    ]
    _adv.GetTokenInformation.restype = wt.BOOL
    _adv.ConvertStringSidToSidW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
    _adv.ConvertStringSidToSidW.restype = wt.BOOL
    _adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    _adv.ConvertSidToStringSidW.restype = wt.BOOL
    _adv.CreateProcessAsUserW.argtypes = [
        ctypes.c_void_p, wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wt.BOOL, wt.DWORD, ctypes.c_void_p, wt.LPCWSTR,
        ctypes.c_void_p, ctypes.POINTER(_ProcessInformation),
    ]
    _adv.CreateProcessAsUserW.restype = wt.BOOL


def supported() -> bool:
    return sys.platform == "win32"


def _last_error() -> str:
    code = ctypes.get_last_error()
    if not supported():
        return str(code)
    buf = ctypes.create_unicode_buffer(512)
    _k32.FormatMessageW(0x0200 | 0x1000, None, code, 0, buf, 512, None)
    return f"{code} {(buf.value or '').strip() or '未知错误'}"


def _handle_list(handles: list[int]) -> tuple[Any, Any, str]:
    """构造只含 `handles` 的句柄白名单属性表。返回 (属性表缓冲, 保存数组, 错误)。

    两个返回值都必须活到 `CreateProcessAsUserW` 返回：属性表缓冲区里存的是数组地址，
    数组一被回收，内核读到的就是悬空指针。
    建不出来就**拒绝启动**，不退回"继承全部"——那正是要消灭的泄漏形状。
    """
    size = ctypes.c_size_t(0)
    # 第一次调用按文档就是失败，只回填所需字节数
    _k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    if not size.value:
        return None, None, "InitializeProcThreadAttributeList -> " + _last_error()
    buffer = ctypes.create_string_buffer(size.value)
    if not _k32.InitializeProcThreadAttributeList(buffer, 1, 0, ctypes.byref(size)):
        return None, None, "InitializeProcThreadAttributeList -> " + _last_error()
    array = (ctypes.c_void_p * len(handles))(*[int(item) for item in handles])
    if not _k32.UpdateProcThreadAttribute(
        ctypes.cast(buffer, ctypes.c_void_p), 0,
        ctypes.c_void_p(PROC_THREAD_ATTRIBUTE_HANDLE_LIST),
        ctypes.cast(array, ctypes.c_void_p), ctypes.c_size_t(ctypes.sizeof(array)),
        None, None,
    ):
        error = "UpdateProcThreadAttribute(0x20002) -> " + _last_error()
        _k32.DeleteProcThreadAttributeList(ctypes.cast(buffer, ctypes.c_void_p))
        return None, None, error
    return buffer, array, ""


# --------------------------------------------------------------------------- 令牌


def _open_current_token() -> int:
    token = ctypes.c_void_p()
    rights = TOKEN_ASSIGN_PRIMARY | TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ADJUST_DEFAULT | TOKEN_ADJUST_SESSIONID | MAXIMUM_ALLOWED
    if not _adv.OpenProcessToken(_k32.GetCurrentProcess(), rights, ctypes.byref(token)):
        raise TokenError("OpenProcessToken -> " + _last_error())
    return int(token.value)


def _token_buffer(token: int, info_class: int) -> ctypes.Array:
    """把 TokenUser/TokenGroups 读进**调用方持有**的缓冲区。

    不能返回 `.raw`：SID 是缓冲区内部地址，原始 bytes 一旦被临时对象回收，指针 1337。
    """
    need = wt.DWORD(0)
    # 第一次调用按文档就是失败（只回填所需字节数），不能当成错误
    _adv.GetTokenInformation(ctypes.c_void_p(token), info_class, None, 0, ctypes.byref(need))
    if not need.value:
        raise TokenError(f"GetTokenInformation({info_class}) -> " + _last_error())
    buf = ctypes.create_string_buffer(need.value)
    if not _adv.GetTokenInformation(ctypes.c_void_p(token), info_class, buf, need, ctypes.byref(need)):
        raise TokenError(f"GetTokenInformation({info_class}) -> " + _last_error())
    return buf


def _sid_string(sid: int) -> str:
    text = ctypes.c_wchar_p()
    if not _adv.ConvertSidToStringSidW(ctypes.c_void_p(sid), ctypes.byref(text)):
        raise TokenError("ConvertSidToStringSidW -> " + _last_error())
    try:
        return text.value or ""
    finally:
        assert text.value is not None
        _k32.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def _token_user_sid(token: int) -> str:
    buf = _token_buffer(token, 1)  # TokenUser
    sid = ctypes.cast(buf, ctypes.POINTER(_SidAndAttributes)).contents.Sid
    if not sid:
        raise TokenError("TokenUser 里没有 SID")
    return _sid_string(int(sid))


def current_user_sid() -> str:
    """当前进程所属用户的 SID。任务根收紧 ACL 时用它保留宿主自己的完全控制权。"""
    if sys.platform != "win32":
        raise TokenError("当前用户 SID 只在 Windows 上可取")
    token = _open_current_token()
    try:
        return _token_user_sid(token)
    finally:
        _k32.CloseHandle(ctypes.c_void_p(token))


def _token_logon_sid(token: int) -> str:
    buf = _token_buffer(token, 2)  # TokenGroups
    count = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint32)).contents.value
    offset = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 4
    array = ctypes.cast(
        ctypes.addressof(buf) + offset,
        ctypes.POINTER(_SidAndAttributes * max(1, count)),
    ).contents
    for item in array:
        if item.Sid and (item.Attributes & SE_GROUP_LOGON_ID) == SE_GROUP_LOGON_ID:
            return _sid_string(int(item.Sid))
    raise TokenError("令牌的组列表里没有登录 SID（SE_GROUP_LOGON_ID）")


def _restricted_token(base: int, sids: tuple[str, ...], flags: int) -> int:
    allocated: list[int] = []
    try:
        for text in sids:
            sid = ctypes.c_void_p()
            if not _adv.ConvertStringSidToSidW(text, ctypes.byref(sid)):
                raise TokenError(f"ConvertStringSidToSidW({text}) -> " + _last_error())
            allocated.append(int(sid.value))
        array = (_SidAndAttributes * len(allocated))(
            *[_SidAndAttributes(ctypes.c_void_p(sid), 0) for sid in allocated]
        )
        new_token = ctypes.c_void_p()
        ok = _adv.CreateRestrictedToken(
            ctypes.c_void_p(base), flags,
            0, None, 0, None,
            len(allocated), ctypes.cast(array, ctypes.c_void_p), ctypes.byref(new_token),
        )
        if not ok:
            raise TokenError("CreateRestrictedToken -> " + _last_error())
        return int(new_token.value)
    finally:
        for sid in allocated:
            _k32.LocalFree(ctypes.c_void_p(sid))


def resolve_capability(text: str) -> str:
    """规则里的 `capability_sid` → 可授权的真实 SID。`logon` 解析成当前登录会话 SID。

    不贪快缓存任意 SID 的**有效性**：解析出来的字符串会进 icacls 与限制列表，写错一个字符
    就会变成"什么都没授权"，所以只缓存进程内稳定的 logon 结果。
    """
    value = (text or "").strip()
    if value.lower() != "logon":
        if not value.upper().startswith("S-1-5-"):
            raise TokenError(f"capability_sid 只接受 logon 或 S-1-5-* 形式：{text!r}")
        return value
    with _CAPABILITY_LOCK:
        cached = _CAPABILITY.get("logon")
        if cached:
            return cached
        base = _open_current_token()
        try:
            sid = _token_logon_sid(base)
        finally:
            _k32.CloseHandle(ctypes.c_void_p(base))
        _CAPABILITY["logon"] = sid
        return sid


def create_token(capability_sid: str) -> int:
    """拿一枚可启动进程的主令牌：限制 SID 集合固定为 Users/Everyone/Authenticated/登录 SID/能力 SID。

    不提供"同载体但不受限"的变体：`CreateRestrictedToken` 即使不给限制 SID 也会把令牌标记成
    restricted，拿它当对照会得到一个"连该读的也读不了"的假红。变异对照改用宿主令牌直接起进程
    （见 `_run_probe_plain`），那才是真正的"去掉限制 SID"。
    """
    capability = resolve_capability(capability_sid)
    base = _open_current_token()
    try:
        logon = _token_logon_sid(base)
        wanted = [*RESTRICTING_SIDS, logon, capability]
        sids = tuple(dict.fromkeys(item for item in wanted if item))
        restricted = _restricted_token(base, sids, DISABLE_MAX_PRIVILEGE)
    finally:
        _k32.CloseHandle(ctypes.c_void_p(base))
    primary = ctypes.c_void_p()
    try:
        if not _adv.DuplicateTokenEx(
            ctypes.c_void_p(restricted), MAXIMUM_ALLOWED, None, SECURITY_IMPERSONATION,
            TOKEN_PRIMARY, ctypes.byref(primary),
        ):
            raise TokenError("DuplicateTokenEx -> " + _last_error())
        return int(primary.value)
    finally:
        _k32.CloseHandle(ctypes.c_void_p(restricted))


def token_probe() -> tuple[bool, bool, str]:
    """边界探针用的最小形状：能建受限令牌吗？`DuplicateTokenEx` 对照能过吗？

    对照失败说明本进程的令牌句柄不可信，此时 `CreateRestrictedToken` 的失败不能归因于机器。
    """
    if not supported():
        return False, True, "非 Windows：没有内核令牌对象"
    base = 0
    try:
        base = _open_current_token()
        duplicate = ctypes.c_void_p()
        control = bool(_adv.DuplicateTokenEx(
            ctypes.c_void_p(base), MAXIMUM_ALLOWED, None, SECURITY_IMPERSONATION,
            TOKEN_PRIMARY, ctypes.byref(duplicate),
        ))
        if duplicate.value:
            _k32.CloseHandle(duplicate)
        create_token(DEFAULT_CAPABILITY_SID)
        return True, control, ""
    except TokenError as error:
        return False, False, str(error)
    finally:
        if base:
            _k32.CloseHandle(ctypes.c_void_p(base))


# --------------------------------------------------------------------------- ACL


def _run_icacls(args: list[str], *, timeout: float = 60.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["icacls", *args],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            timeout=timeout,
            creationflags=0x08000000,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, f"{type(error).__name__}: {error}"
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def _grant(path: str, rights: str, sid: str, *, recursive: bool = False) -> tuple[bool, str]:
    """给能力 SID 加一条可继承 ACE。

    `(OI)(CI)` 只对"新建子项"和"继承链完好的已有子项"生效。DACL 被保护（继承标志被清）
    的文件拿不到这条 ACE：本机 `python312.dll` 就带着一条显式 `BUILTIN\\Guests:(RX)`，
    受限子进程加载它时第二道检查只能看到 Users/Everyone/登录 SID，于是 0xC0000142。

    `recursive=True` 分两段写，不能合成一条：icacls 把带 `(OI)(CI)` 的授权套 `/T` 时，
    **只在目录上写、对文件静默跳过**（还报 0 失败）。所以第一段给根目录写可继承 ACE，
    第二段用不带继承标志的授权 `/T` 把显式 ACE 落到每个现存文件上。
    """
    if not os.path.exists(path):
        return False, f"路径不存在：{path}"
    key = f"{os.path.normcase(os.path.abspath(path))}|{rights}|{sid}|{'T' if recursive else ''}"
    with _ACL_LOCK:
        if key in _GRANTED:
            return True, ""
        is_dir = os.path.isdir(path)
        permission = f"(OI)(CI){rights}" if is_dir else rights
        code, output = _run_icacls([path, "/grant", f"*{sid}:{permission}"])
        if code != 0:
            return False, f"icacls {rights} rc={code}: {output.splitlines()[-1] if output else ''}"
        if recursive and is_dir:
            code, output = _run_icacls([path, "/grant", f"*{sid}:{rights}", "/T", "/C", "/Q"])
            if code != 0:
                return False, f"icacls {rights} /T rc={code}: {output.splitlines()[-1] if output else ''}"
        _GRANTED.add(key)
        return True, ""


def _revoke(path: str, sid: str) -> None:
    with _ACL_LOCK:
        _run_icacls([path, "/remove:g", f"*{sid}"])
        for key in list(_GRANTED):
            if key.startswith(os.path.normcase(os.path.abspath(path)) + "|"):
                _GRANTED.discard(key)


def _set_private(path: str, owner_sid: str) -> None:
    """把合成目录的 DACL 收紧到"当前用户 + SYSTEM"，模拟 `~/.ssh` 的形状。"""
    code, output = _run_icacls([
        path, "/inheritance:r", "/grant:r",
        f"*{owner_sid}:(OI)(CI)F", f"*{SYSTEM_SID}:(OI)(CI)F",
    ])
    if code != 0:
        raise RestrictedUnavailable(f"icacls 收紧合成目录失败 rc={code}: {output}")


def _within(path: str, root: str) -> bool:
    path_key = os.path.normcase(os.path.abspath(path))
    root_key = os.path.normcase(os.path.abspath(root))
    if path_key == root_key:
        return True
    return path_key.startswith(root_key if root_key.endswith(os.sep) else root_key + os.sep)


def prepare(
    workspace: str,
    temp_dir: str,
    rule_set: rules_module.SandboxRules,
    temp_root: str = "",
) -> tuple[dict[str, list[str]], list[str]]:
    """按规则给工作区/临时目录/工具链根授权。工作区失败是致命的，工具链根失败只记账。

    `temp_root` 是临时目录所在的容量卷挂载点：临时区落在卷上时补一条 RX，否则受限令牌
    连卷根都进不去（祖先路径上没有它的 ACE），临时目录形同虚设。

    授权是一次性写进 DACL 的持久 ACE：解除沙箱不会回收它（收窄去删 ACE 反而会误伤别人加的
    权限）。默认能力 SID 是登录会话 SID，所以每多一次登录，目录上会多一条对旧会话惰性的 ACE；
    这条 ACE 不被任何现存进程匹配，既不是提权也没扩大可读范围，但会留痕。
    """
    capability = resolve_capability(rule_set.capability_sid)
    ctx = rule_set.context(workspace, temp_dir)
    write_roots = rule_set.write_roots(ctx)
    read_roots = rule_set.read_roots(ctx)
    rule_set.guard(tuple(write_roots) + tuple(read_roots), ctx)
    for root in write_roots:
        ok, detail = _grant(root, "M", capability)
        if not ok:
            raise RestrictedUnavailable(f"工作区授权失败，拒绝在无法写工作区的边界里执行：{root}：{detail}")
    ok, detail = _grant(temp_dir, "M", capability)
    if not ok:
        raise RestrictedUnavailable(f"临时目录授权失败：{temp_dir}：{detail}")
    granted_read: list[str] = []
    notes: list[str] = []
    if temp_root:
        for path in dict.fromkeys((temp_root, os.path.dirname(temp_root))):
            if not os.path.isdir(path):
                continue
            ok, detail = _grant(path, "RX", capability)
            if not ok:
                notes.append(f"容量卷根授权失败（临时区可能不可用）：{path}：{detail}")
    for root in read_roots:
        if not os.path.exists(root) or _within(root, workspace):
            continue
        ok, detail = _grant(root, "RX", capability, recursive=True)
        if ok:
            granted_read.append(root)
        else:
            notes.append(f"工具链根授权失败（命令可能读不到它）：{root}：{detail}")
    return {"write": list(write_roots), "read": granted_read, "capability": [capability]}, notes


# --------------------------------------------------------------------------- 启动进程


def _environment(env: dict[str, str]) -> ctypes.Array:
    body = "".join(f"{key}={value}\0" for key, value in sorted(env.items(), key=lambda item: item[0].upper()))
    return ctypes.create_unicode_buffer(body + "\0")


def _command_line(command: str) -> tuple[str, str]:
    """把一条命令包成 `cmd /d /s /c "<命令>"`，与宿主 `create_subprocess_shell` 逐字同形。

    不走临时 `.cmd`：受限令牌下 cmd **能读**批处理文件（`type s.cmd` 正常），却拒绝执行它
    （`call`/直接调用/原形状一律 exit 1、零输出，跟启动标志、限制 SID 集合都无关）。
    单行 inline 在同一个令牌下照常工作，所以这里不再经过批处理。代价是多行命令只执行
    第一行——宿主 `cmd /c` 本来就这个行为（实测同一台机器），不是本后端引入的差异。
    """
    comspec = os.environ.get("COMSPEC") or os.path.join(
        os.environ.get("SystemRoot") or r"C:\Windows", "System32", "cmd.exe"
    )
    # cmd /s 会去掉最外层一对引号；命令里的引号原样保留。
    return comspec, f'"{comspec}" /d /s /c "{command}"'


def _read_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read(4 * 1024 * 1024 + 1)
    except OSError:
        return b""


def _spawn(
    token: int,
    *,
    application: str,
    cmdline: str,
    cwd: str,
    env: dict[str, str],
    timeout: float,
    job: windows_job.Job | None,
    out_path: str,
    err_path: str,
) -> tuple[int | None, bytes, bytes, bool, bool, str, str]:
    """起进程、挂 Job、等退出。返回 (退出码, stdout, stderr, 超时, 已挂Job, 挂Job错误, 启动错误)。"""
    out_fd = err_fd = nul_fd = 0
    handles: list[int] = []
    # 属性表缓冲区里存的是句柄数组的地址。把 `_handle_list` 的三个返回值整体留在局部
    # 变量里，数组和缓冲区就都活到 CreateProcessAsUserW 返回（提前回收 = 悬空指针）。
    attr: tuple[Any, Any, str] | None = None
    pi = _ProcessInformation()
    try:
        for path in (out_path, err_path):
            out = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
            handle = msvcrt.get_osfhandle(out)
            _k32.SetHandleInformation(ctypes.c_void_p(handle), HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
            if path == out_path:
                out_fd = out
            else:
                err_fd = out
            handles.append(handle)
        nul_fd = os.open("NUL", os.O_RDONLY)
        nul_handle = msvcrt.get_osfhandle(nul_fd)
        _k32.SetHandleInformation(ctypes.c_void_p(nul_handle), HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)

        si = _StartupInfoExW()
        si.cb = ctypes.sizeof(si)
        si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW
        si.wShowWindow = SW_HIDE
        si.hStdInput = nul_handle
        si.hStdOutput = handles[0]
        si.hStdError = handles[1]
        # bInheritHandles=True 是标准输入输出的前提，但它同时会把宿主里所有带继承标志的
        # 句柄都递给子进程。属性表白名单把继承范围压到这三个；列出的句柄必须可继承
        # （上面已逐个 SetHandleInformation），否则 CreateProcess 直接失败。
        attr = _handle_list([nul_handle, handles[0], handles[1]])
        if attr[2]:
            return None, b"", b"", False, False, "", attr[2]
        si.lpAttributeList = ctypes.cast(attr[0], ctypes.c_void_p)
        cmd_buf = ctypes.create_unicode_buffer(cmdline)
        env_buf = _environment(env)
        # 不能用 DETACHED_PROCESS：本机实测它在受限令牌下让 `python312.dll` DLL 初始化失败
        # （0xC0000142），同一令牌换成 flags=0 就能跑 `python -c "print(7)"`。
        # 也不能用 CREATE_NO_WINDOW：受限令牌下 cmd 直接 0xC0000142。SW_HIDE 负责不弹窗。
        flags = CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT
        ok = _adv.CreateProcessAsUserW(
            ctypes.c_void_p(token),
            application,
            cmd_buf,
            None,
            None,
            True,
            flags,
            ctypes.cast(env_buf, ctypes.c_void_p),
            cwd,
            ctypes.byref(si),
            ctypes.byref(pi),
        )
        if not ok:
            return None, b"", b"", False, False, "", "CreateProcessAsUserW -> " + _last_error()
        if job is not None:
            assigned, assign_error = windows_job.assign_handle(job, int(pi.hProcess))
        else:
            assigned, assign_error = False, ""
        resumed = _k32.ResumeThread(pi.hThread)
        if resumed == 0xFFFFFFFF:
            _k32.TerminateProcess(pi.hProcess, 125)
            _k32.WaitForSingleObject(pi.hProcess, 5000)
            return None, b"", b"", False, assigned, assign_error, "ResumeThread -> " + _last_error()

        timed_out = False
        result = _k32.WaitForSingleObject(pi.hProcess, max(1, int(timeout * 1000)))
        if result == WAIT_TIMEOUT:
            timed_out = True
            _k32.TerminateProcess(pi.hProcess, 124)
            _k32.WaitForSingleObject(pi.hProcess, 5000)
        code = wt.DWORD(0)
        _k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        return code.value, b"", b"", timed_out, assigned, assign_error, ""
    finally:
        for fd in (out_fd, err_fd, nul_fd):
            if fd:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if attr is not None:
            _k32.DeleteProcThreadAttributeList(ctypes.cast(attr[0], ctypes.c_void_p))
        if pi.hThread:
            _k32.CloseHandle(pi.hThread)
        if pi.hProcess:
            _k32.CloseHandle(pi.hProcess)


def _job_report(job: windows_job.Job | None, assigned: bool, error: str) -> JobReport:
    if job is None:
        return JobReport()
    return JobReport(
        created=job.ok,
        assigned=bool(job.ok and assigned),
        error=job.error or error,
        enforced=dict(job.enforced),
        io_error=job.io_error,
    )


def run_command(
    script: str,
    *,
    workspace: str,
    env: dict[str, str],
    timeout: float,
    memory: str = "",
    pids: int = 0,
    cpus: float = 0.0,
    io_bps: int = 0,
    disk_mb: int = 0,
    rule_set: rules_module.SandboxRules | None = None,
) -> CommandReport:
    if not supported():
        raise RestrictedUnavailable("受限令牌后端只在 Windows 上存在")
    rules = rule_set or rules_module.load()
    temp_dir, disk_on_quota, disk_bytes, disk_error = windows_volume.temp_dir_for(
        "blank-restricted-", disk_mb
    )
    job = windows_job.create_job(
        "blank-restricted-" + uuid.uuid4().hex[:8],
        memory_bytes=windows_job.parse_size(memory),
        active_process_limit=pids or 0,
        cpu_cpus=cpus,
        io_bps=io_bps,
    )
    token = 0
    try:
        grants, notes = prepare(
            workspace,
            temp_dir,
            rules,
            temp_root=os.path.dirname(temp_dir) if disk_on_quota else "",
        )
        if disk_mb and not disk_on_quota:
            notes.append(f"临时区未放进容量卷，容量上限未生效：{disk_error}")
        child_env = dict(env)
        child_env["TEMP"] = temp_dir
        child_env["TMP"] = temp_dir
        for key in ("SystemRoot", "windir", "SystemDrive", "COMSPEC", "PATHEXT", "PATH"):
            if key not in child_env and os.environ.get(key):
                child_env[key] = os.environ[key]
        token = create_token(rules.capability_sid)
        application, cmdline = _command_line(script)
        code, _, _, timed_out, assigned, assign_error, spawn_error = _spawn(
            token,
            application=application,
            cmdline=cmdline,
            cwd=workspace,
            env=child_env,
            timeout=timeout,
            job=job,
            out_path=str(Path(temp_dir) / "stdout.txt"),
            err_path=str(Path(temp_dir) / "stderr.txt"),
        )
        # 在关 Job 之前把限额事实抄下来：关句柄会把 handle 置 0，之后再读就成了"没建成"。
        report = CommandReport(
            exit_code=code,
            stdout=_read_bytes(str(Path(temp_dir) / "stdout.txt")),
            stderr=_read_bytes(str(Path(temp_dir) / "stderr.txt")),
            timed_out=timed_out,
            job=_job_report(job, assigned, assign_error),
            error=spawn_error,
            notes=notes,
            grants=grants,
            disk_on_quota=disk_on_quota,
            disk_bytes=disk_bytes,
            disk_error=disk_error,
        )
        return report
    finally:
        windows_job.close_job(job)
        if token:
            _k32.CloseHandle(ctypes.c_void_p(token))
        shutil.rmtree(temp_dir, ignore_errors=True)


# --------------------------------------------------------------------------- 准入自检


# 单行 inline：`if ... (...) else (...)` 后面接 `&` 时 cmd 会把后续命令吞进 else 分支，
# 所以用 `&& / ||` 链；这条写法在受限令牌下实测输出三行（fix6）。
_PROBE_COMMAND = (
    'type "%USERPROFILE%\\.ssh\\id_probe" >nul 2>nul && echo SECRET_READ || echo SECRET_DENIED & '
    "echo ws-ok>ws.out && echo WS_WRITE_OK || echo WS_WRITE_FAIL & "
    "type ws.out >nul 2>nul && echo WS_READ_OK || echo WS_READ_FAIL"
)


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("mbcs")
        except UnicodeDecodeError:
            return raw.decode("utf-8", "replace")


def _self_env(home: str, temp_dir: str) -> dict[str, str]:
    env = {"USERPROFILE": home, "HOME": home, "TEMP": temp_dir, "TMP": temp_dir}
    for key in ("SystemRoot", "windir", "SystemDrive", "COMSPEC", "PATHEXT", "PATH"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def _run_probe(token: int, *, ws: str, temp_dir: str, env: dict[str, str], tag: str) -> tuple[str, str, str, int | None]:
    tag_dir = os.path.join(temp_dir, tag)
    os.makedirs(tag_dir, exist_ok=True)
    application, cmdline = _command_line(_PROBE_COMMAND)
    code, _, _, timed_out, _, _, error = _spawn(
        token,
        application=application,
        cmdline=cmdline,
        cwd=ws,
        env=env,
        timeout=20.0,
        job=None,
        out_path=os.path.join(tag_dir, "stdout.txt"),
        err_path=os.path.join(tag_dir, "stderr.txt"),
    )
    stdout = _decode(_read_bytes(os.path.join(tag_dir, "stdout.txt")))
    stderr = _decode(_read_bytes(os.path.join(tag_dir, "stderr.txt")))
    if error:
        return "", "", error, code
    if timed_out:
        return stdout, stderr, "自检子进程超时", code
    return stdout, stderr, "", code


def _run_probe_plain(*, ws: str, temp_dir: str, env: dict[str, str], tag: str) -> tuple[str, str, str]:
    """变异对照：同载体、宿主令牌（无限制 SID）起同一条探针。这里不经过 CreateProcessAsUserW，
    因为非管理员没有 SeAssignPrimaryTokenPrivilege，用宿主令牌直接起才是真正的"去掉限制"。

    命令行与 `_command_line` 逐字一致；CREATE_NO_WINDOW 只是不弹窗，不影响文件访问判定。
    """
    tag_dir = os.path.join(temp_dir, tag)
    os.makedirs(tag_dir, exist_ok=True)
    application, _ = _command_line(_PROBE_COMMAND)
    command = f'"{application}" /d /s /c "{_PROBE_COMMAND}"'
    try:
        proc = subprocess.run(
            command,
            cwd=ws,
            env=env,
            capture_output=True,
            timeout=20.0,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return "", "", f"{type(error).__name__}: {error}"
    return (
        _decode(proc.stdout or b""),
        _decode(proc.stderr or b""),
        "",
    )


def self_check(*, force: bool = False) -> SelfCheck:
    """进程内只跑一次；`force` 给测试用。准入失败的后端不会被选中。"""
    global _SELF_CACHE
    if _SELF_CACHE is not None and not force:
        return _SELF_CACHE
    with _SELF_LOCK:
        if _SELF_CACHE is not None and not force:
            return _SELF_CACHE
        _SELF_CACHE = _self_check_once()
        return _SELF_CACHE


def invalidate() -> None:
    global _SELF_CACHE
    _SELF_CACHE = None


def _self_check_once() -> SelfCheck:
    check = SelfCheck(attempted=True)
    if not supported():
        check.error = "非 Windows：没有受限令牌"
        return check
    if shutil.which("icacls") is None:
        check.error = "找不到 icacls，无法配置能力 SID 的 ACL"
        return check
    root = tempfile.mkdtemp(prefix="blank-restricted-selftest-")
    home = os.path.join(root, "home")
    ws = os.path.join(root, "ws")
    temp_dir = os.path.join(root, "tmp")
    tokens: list[int] = []
    try:
        os.makedirs(os.path.join(home, ".ssh"))
        os.makedirs(ws)
        os.makedirs(temp_dir)
        secret = os.path.join(home, ".ssh", "id_probe")
        Path(secret).write_text("SYNTHETIC-NOT-A-KEY", encoding="utf-8")
        Path(os.path.join(ws, "keep.txt")).write_text("workspace-read-ok", encoding="utf-8")

        base = _open_current_token()
        try:
            owner = _token_user_sid(base)
        finally:
            _k32.CloseHandle(ctypes.c_void_p(base))
        capability = resolve_capability(DEFAULT_CAPABILITY_SID)
        check.details["capability_sid"] = capability
        _set_private(home, owner)
        # 阴性对照：合成文件在普通令牌下必须读得到，否则"读不到"不能归因于受限令牌
        try:
            check.negative_control = Path(secret).read_text(encoding="utf-8") == "SYNTHETIC-NOT-A-KEY"
        except OSError as error:
            check.error = f"阴性对照读不了合成文件：{error}"
            return check

        for path in (ws, temp_dir):
            ok, detail = _grant(path, "M", capability)
            if not ok:
                check.error = f"自检目录授权失败：{detail}"
                return check

        env = _self_env(home, temp_dir)
        restricted = create_token(DEFAULT_CAPABILITY_SID)
        tokens.append(restricted)
        stdout, stderr, error, code = _run_probe(restricted, ws=ws, temp_dir=temp_dir, env=env, tag="restricted")
        check.details["restricted_stdout"] = stdout.strip()
        check.details["restricted_stderr"] = stderr.strip()
        check.details["restricted_exit"] = "None" if code is None else str(code)
        if error:
            check.error = f"受限令牌子进程没跑起来：{error}"
            return check
        check.secret_denied = "SECRET_DENIED" in stdout
        check.workspace_write = "WS_WRITE_OK" in stdout
        check.workspace_read = "WS_READ_OK" in stdout

        # 变异对照：同载体、宿主令牌、同样的 ACL，读命令必须成功。它必须在"给 .ssh 加能力
        # SID"之前跑——否则成功只能说明那条授权生效了，说明不了红是限制 SID 造成的。
        stdout, stderr, error = _run_probe_plain(ws=ws, temp_dir=temp_dir, env=env, tag="mutation")
        check.details["mutation_stdout"] = stdout.strip()
        check.details["mutation_stderr"] = stderr.strip()
        check.mutation_control = not error and "SECRET_READ" in stdout
        if error:
            check.details["mutation_error"] = error

        # 阳性对照：把能力 SID 加进 home 的 ACL，同一条受限读命令必须成功
        ok, detail = _grant(home, "RX", capability)
        if not ok:
            check.error = f"阳性对照授权失败：{detail}"
            return check
        stdout, stderr, error, code = _run_probe(restricted, ws=ws, temp_dir=temp_dir, env=env, tag="positive")
        check.details["positive_stdout"] = stdout.strip()
        check.details["positive_stderr"] = stderr.strip()
        check.details["positive_exit"] = "None" if code is None else str(code)
        check.positive_control = not error and "SECRET_READ" in stdout
        if error:
            check.details["positive_error"] = error
        return check
    except (OSError, RestrictedUnavailable, TokenError) as error:
        check.error = f"{type(error).__name__}: {error}"
        return check
    finally:
        for token in tokens:
            try:
                _k32.CloseHandle(ctypes.c_void_p(token))
            except Exception:  # noqa: BLE001 - 清理失败不影响判定
                pass
        shutil.rmtree(root, ignore_errors=True)


def available() -> bool:
    return self_check().ok


__all__ = [
    "CommandReport",
    "DEFAULT_CAPABILITY_SID",
    "JobReport",
    "RestrictedUnavailable",
    "SelfCheck",
    "TokenError",
    "available",
    "create_token",
    "invalidate",
    "prepare",
    "run_command",
    "self_check",
    "supported",
    "token_probe",
]
