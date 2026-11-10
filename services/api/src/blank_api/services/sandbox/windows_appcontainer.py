"""AppContainer 执行后端：内核同时拒掉文件与网络的那一层，需要管理员权限。

2026-11-10 在本机（管理员 / build 26300）实测可用的形状：

- 用 `userenv!CreateAppContainerProfile` 建一个有名字的包（固定名 `BlankSandbox`，重复创建
  返回 0x800700B7 时改用 `DeriveAppContainerSidFromAppContainerName`），得到包 SID。
- 起进程用 `CreateProcessW` + `STARTUPINFOEXW`，属性表挂两个：
  `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES`（包 SID，零能力）与
  `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`（stdin/stdout/stderr 三个句柄）。
  句柄白名单和受限令牌后端同一理由：`bInheritHandles=True` 会把宿主里所有可继承句柄
  一起递进去。
- 包 SID 在**工作区/临时目录**上拿 `(OI)(CI)M`，在工具链根（`python_base`/`python_prefix`
  的 realpath）上拿 `(OI)(CI)RX`。祖先路径**不授权**：AC 令牌保留并启用
  `SeChangeNotifyPrivilege`（绕过遍历检查），实测用全新包名、任何祖先上都没有它的 ACE
  时，AC 内 Python 照常启动（`E:/tmp/i-probe/no_ancestor.py`）。早先给祖先写 `(X)` 的
  做法会让每次自检多花 200 秒——对目录写 DACL 本身就触发子树继承传播。
- 网络判定是**行为证据**，不是"能进 AppContainer"这种能力推断。实测（`E:/tmp/i-probe/acpy.py`）：
  AC 内 `socket` 能 bind 回环端口并自连（同一个进程内），外连 1.1.1.1:443 抛
  `PermissionError:13`（WSAEACCES），读合成私钥同样 `PermissionError:13`，工作区读写正常；
  宿主令牌对照四项全通。只要有一条对照红掉，整次判定作废——离线机器上"连不上外网"
  不能算边界生效。
- `CheckNetIsolation LoopbackExempt` **不加**：它会把本机 LAN 地址也放行，等于为了
  自检方便打开一个洞；回环自连本来就不需要豁免。
- 交给 cmd 的命令里，**路径型首词必须带引号**（`quote_leading_path` 统一补）：未引号的
  绝对路径 exe 会让 cmd 走搜索解析，在缺目录访问权的 AC 令牌下报「拒绝访问。」；同一
  命令在受限令牌后端与宿主上正常。参数本身引不引号不影响（实测见
  `E:/tmp/i-probe/ac_quote_isolate.json`）。

本模块只承诺自检真的量到的两件事：文件边界与网络边界。Job 的内存/进程数/进程树/CpuRate
仍由 `windows_job` 提供，账本与 local/restricted 后端同一形状。
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
from blank_api.services.sandbox import windows_job
from blank_api.services.sandbox import windows_restricted as wr

PROFILE_NAME = "BlankSandbox"
SECRET_TEXT = "SYNTHETIC-NOT-A-KEY"
WORKSPACE_TEXT = "ws-ok"
ERROR_ALREADY_EXISTS = 0x800700B7
# 外连被策略拒绝的两种 errno：WSAEACCES(10013) 在 Python 里映射成 errno 13，
# 少数路径会原样给出 10013。只认这两个，超时/拒绝连接都不能算"策略拒绝"。
DENIED_ERRNOS = {"13", "10013"}

PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
CREATE_SUSPENDED = wr.CREATE_SUSPENDED
CREATE_UNICODE_ENVIRONMENT = wr.CREATE_UNICODE_ENVIRONMENT
EXTENDED_STARTUPINFO_PRESENT = wr.EXTENDED_STARTUPINFO_PRESENT
HANDLE_FLAG_INHERIT = wr.HANDLE_FLAG_INHERIT
WAIT_TIMEOUT = wr.WAIT_TIMEOUT

_PAYLOAD = r'''
import json, os, socket

out = {}
server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    server.bind(("127.0.0.1", 0))
    out["bind"] = "ok"
    server.listen(1)
    try:
        client = socket.create_connection(("127.0.0.1", server.getsockname()[1]), timeout=3)
        client.close()
        out["loopback"] = "ok"
    except OSError as exc:
        out["loopback"] = "%s:%s" % (type(exc).__name__, getattr(exc, "errno", ""))
finally:
    server.close()
try:
    client = socket.create_connection(("1.1.1.1", 443), timeout=5)
    client.close()
    out["external"] = "ok"
except OSError as exc:
    out["external"] = "%s:%s" % (type(exc).__name__, getattr(exc, "errno", ""))
try:
    path = os.path.join(os.environ["USERPROFILE"], ".ssh", "id_probe")
    out["secret"] = open(path, encoding="utf-8").read()
except OSError as exc:
    out["secret"] = "%s:%s" % (type(exc).__name__, getattr(exc, "errno", ""))
try:
    target = os.path.join(os.getcwd(), "ws.out")
    open(target, "w", encoding="utf-8").write("ws-ok")
    out["ws_write"] = open(target, encoding="utf-8").read()
except OSError as exc:
    out["ws_write"] = "%s:%s" % (type(exc).__name__, getattr(exc, "errno", ""))
print(json.dumps(out))
'''

_SELF_CACHE: "SelfCheck | None" = None
_SELF_LOCK = threading.Lock()
_PROFILE_LOCK = threading.Lock()
_PROFILE: tuple[str, int] | None = None

if sys.platform == "win32":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _userenv = ctypes.WinDLL("userenv", use_last_error=True)

    class _SecurityCapabilities(ctypes.Structure):
        _fields_ = [
            ("AppContainerSid", ctypes.c_void_p),
            ("Capabilities", ctypes.c_void_p),
            ("CapabilityCount", ctypes.c_uint32),
            ("Reserved", ctypes.c_uint32),
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
    _k32.CreateProcessW.restype = wt.BOOL
    _k32.CreateProcessW.argtypes = [
        wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL, wt.DWORD,
        ctypes.c_void_p, wt.LPCWSTR, ctypes.c_void_p,
        ctypes.POINTER(wr._ProcessInformation),
    ]
    _k32.SetHandleInformation.argtypes = [ctypes.c_void_p, wt.DWORD, wt.DWORD]
    _k32.SetHandleInformation.restype = wt.BOOL
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _k32.CloseHandle.restype = wt.BOOL
    _k32.ResumeThread.argtypes = [ctypes.c_void_p]
    _k32.ResumeThread.restype = wt.DWORD
    _k32.TerminateProcess.argtypes = [ctypes.c_void_p, wt.UINT]
    _k32.TerminateProcess.restype = wt.BOOL
    _k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wt.DWORD]
    _k32.WaitForSingleObject.restype = wt.DWORD
    _k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]
    _k32.GetExitCodeProcess.restype = wt.BOOL

    _userenv.CreateAppContainerProfile.restype = ctypes.c_long
    _userenv.CreateAppContainerProfile.argtypes = [
        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p,
        wt.DWORD, ctypes.POINTER(ctypes.c_void_p),
    ]
    _userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    _userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
        ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p),
    ]


class AppContainerUnavailable(RuntimeError):
    """包建不出来、AC 子进程起不来、或工作区授权失败。调用方必须拒绝执行，不许退回宿主。"""


@dataclass(slots=True)
class SelfCheck:
    """一次行为自检的全部事实。字段就是证据，界面别再重新推导。"""

    attempted: bool = False
    error: str = ""
    secret_denied: bool = False
    workspace_read: bool = False
    workspace_write: bool = False
    bind: bool = False
    loopback: bool = False
    network_denied: bool = False
    plain_secret_read: bool = False
    plain_network: bool = False
    details: dict[str, str] = field(default_factory=dict)

    @property
    def file_boundary_ok(self) -> bool:
        """AC 内私钥被拒 + 工作区读写成功 + 宿主对照读得到私钥。"""
        return (
            self.attempted and not self.error
            and self.secret_denied and self.workspace_read and self.workspace_write
            and self.plain_secret_read
        )

    @property
    def network_ok(self) -> bool:
        """AC 内能 bind/自连、外连被策略拒，宿主对照外连正常。"""
        return (
            self.attempted and not self.error
            and self.bind and self.loopback and self.network_denied and self.plain_network
        )

    @property
    def ok(self) -> bool:
        return self.file_boundary_ok and self.network_ok

    def as_dict(self) -> dict[str, object]:
        return {
            "attempted": self.attempted,
            "secret_denied": self.secret_denied,
            "workspace_read": self.workspace_read,
            "workspace_write": self.workspace_write,
            "bind": self.bind,
            "loopback": self.loopback,
            "network_denied": self.network_denied,
            "plain_secret_read": self.plain_secret_read,
            "plain_network": self.plain_network,
            "file_boundary_ok": self.file_boundary_ok,
            "network_ok": self.network_ok,
            "ok": self.ok,
            "error": self.error,
            "details": dict(self.details),
        }


def supported() -> bool:
    return sys.platform == "win32"


# --------------------------------------------------------------------------- 包 SID


def profile() -> tuple[str, int]:
    """固定的 AppContainer 包 SID。进程内只建一次；已存在时按名字反推。

    用固定名而不是每次随机：包与 ACL 都是持久对象，随机名会让每次启动都在工具链根上
    再写一条 ACE。代价是同一台机器共用一个包——它只被本工具的沙箱进程使用。
    """
    global _PROFILE
    if not supported():
        raise AppContainerUnavailable("AppContainer 只在 Windows 上存在")
    with _PROFILE_LOCK:
        if _PROFILE is not None:
            return _PROFILE
        sid = ctypes.c_void_p()
        hr = _userenv.CreateAppContainerProfile(
            PROFILE_NAME, PROFILE_NAME, "blank sandbox profile", None, 0, ctypes.byref(sid)
        )
        if hr < 0 and (hr & 0xFFFFFFFF) != ERROR_ALREADY_EXISTS:
            raise AppContainerUnavailable(
                f"CreateAppContainerProfile -> {hr & 0xFFFFFFFF:#x}（需要管理员权限）"
            )
        if not sid.value:
            hr = _userenv.DeriveAppContainerSidFromAppContainerName(PROFILE_NAME, ctypes.byref(sid))
            if hr < 0 or not sid.value:
                raise AppContainerUnavailable(
                    f"DeriveAppContainerSidFromAppContainerName -> {hr & 0xFFFFFFFF:#x}"
                )
        text = ctypes.c_wchar_p()
        if not _adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise AppContainerUnavailable("ConvertSidToStringSidW -> " + wr._last_error())
        _PROFILE = (text.value or "", int(sid.value))
        return _PROFILE


def prepare(
    workspace: str,
    temp_dir: str,
    rule_set: rules_module.SandboxRules,
) -> tuple[dict[str, list[str]], list[str]]:
    """按规则给包 SID 授权。工作区/临时目录可写，工具链根可读，其余默认拒绝。

    工具链根取 realpath：`%APPDATA%\\uv\\python\\...` 是重解析点，ACE 写在链接本身
    不会跟着生效。读根失败只记账，工作区失败是致命的。
    """
    sid, _ = profile()
    ctx = rule_set.context(workspace, temp_dir)
    write_roots = rule_set.write_roots(ctx)
    read_roots = rule_set.read_roots(ctx)
    rule_set.guard(tuple(write_roots) + tuple(read_roots), ctx)

    for root in write_roots:
        ok, detail = wr._grant(root, "M", sid)
        if not ok:
            raise AppContainerUnavailable(
                f"工作区授权失败，拒绝在无法写工作区的边界里执行：{root}：{detail}"
            )
    ok, detail = wr._grant(temp_dir, "M", sid)
    if not ok:
        raise AppContainerUnavailable(f"临时目录授权失败：{temp_dir}：{detail}")

    granted_read: list[str] = []
    notes: list[str] = []
    for root in read_roots:
        if not os.path.exists(root) or wr._within(root, workspace):
            continue
        real = os.path.realpath(root)
        ok, detail = wr._grant(real, "RX", sid, recursive=True)
        if ok:
            granted_read.append(real)
        else:
            notes.append(f"工具链根授权失败（命令可能读不到它）：{root}：{detail}")
    return {"write": list(write_roots) + [temp_dir], "read": granted_read, "capability": [sid]}, notes


# --------------------------------------------------------------------------- 启动进程


def _attribute_list(entries: list[tuple[int, int, int]]) -> tuple[Any, list[Any], str]:
    """建属性表并逐条挂上。`entries` 里的值都是**地址**，调用方负责让对象活到进程起来。"""
    size = ctypes.c_size_t(0)
    # 第一次调用按文档就是失败，只回填所需字节数
    _k32.InitializeProcThreadAttributeList(None, len(entries), 0, ctypes.byref(size))
    if not size.value:
        return None, [], "InitializeProcThreadAttributeList -> " + wr._last_error()
    buffer = ctypes.create_string_buffer(size.value)
    if not _k32.InitializeProcThreadAttributeList(buffer, len(entries), 0, ctypes.byref(size)):
        return None, [], "InitializeProcThreadAttributeList -> " + wr._last_error()
    keep: list[Any] = []
    for number, value, nbytes in entries:
        holder = ctypes.c_void_p(int(value))
        keep.append(holder)
        if not _k32.UpdateProcThreadAttribute(
            ctypes.cast(buffer, ctypes.c_void_p), 0, ctypes.c_void_p(number),
            holder, ctypes.c_size_t(nbytes), None, None,
        ):
            error = f"UpdateProcThreadAttribute({number:#x}) -> " + wr._last_error()
            _k32.DeleteProcThreadAttributeList(ctypes.cast(buffer, ctypes.c_void_p))
            return None, [], error
    return buffer, keep, ""


def _spawn(
    *,
    sid_ptr: int,
    application: str,
    cmdline: str,
    cwd: str,
    env: dict[str, str],
    timeout: float,
    job: windows_job.Job | None,
    out_path: str,
    err_path: str,
) -> tuple[int | None, bool, bool, str, str]:
    """起 AC 进程、挂 Job、等退出。返回 (退出码, 超时, 已挂Job, 挂Job错误, 启动错误)。"""
    out_fd = err_fd = nul_fd = 0
    handles: list[int] = []
    attributes: tuple[Any, list[Any], str] | None = None
    pi = wr._ProcessInformation()
    try:
        for path in (out_path, err_path):
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
            handle = msvcrt.get_osfhandle(fd)
            _k32.SetHandleInformation(ctypes.c_void_p(handle), HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
            if path == out_path:
                out_fd = fd
            else:
                err_fd = fd
            handles.append(handle)
        nul_fd = os.open("NUL", os.O_RDONLY)
        nul_handle = msvcrt.get_osfhandle(nul_fd)
        _k32.SetHandleInformation(ctypes.c_void_p(nul_handle), HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)

        si = wr._StartupInfoExW()
        si.cb = ctypes.sizeof(si)
        si.dwFlags = wr.STARTF_USESTDHANDLES | wr.STARTF_USESHOWWINDOW
        si.wShowWindow = wr.SW_HIDE
        si.hStdInput = nul_handle
        si.hStdOutput = handles[0]
        si.hStdError = handles[1]

        caps = _SecurityCapabilities(ctypes.c_void_p(sid_ptr), None, 0, 0)
        inherit = (ctypes.c_void_p * 3)(nul_handle, handles[0], handles[1])
        attributes = _attribute_list([
            (PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, ctypes.addressof(caps), ctypes.sizeof(caps)),
            (wr.PROC_THREAD_ATTRIBUTE_HANDLE_LIST, ctypes.addressof(inherit), ctypes.sizeof(inherit)),
        ])
        if attributes[2]:
            return None, False, False, "", attributes[2]
        si.lpAttributeList = ctypes.cast(attributes[0], ctypes.c_void_p)
        cmd_buf = ctypes.create_unicode_buffer(cmdline)
        env_buf = wr._environment(env)
        flags = CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT
        ok = _k32.CreateProcessW(
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
            return None, False, False, "", "CreateProcessW -> " + wr._last_error()
        if job is not None:
            assigned, assign_error = windows_job.assign_handle(job, int(pi.hProcess))
        else:
            assigned, assign_error = False, ""
        if _k32.ResumeThread(pi.hThread) == 0xFFFFFFFF:
            _k32.TerminateProcess(pi.hProcess, 125)
            _k32.WaitForSingleObject(pi.hProcess, 5000)
            return None, False, assigned, assign_error, "ResumeThread -> " + wr._last_error()

        timed_out = False
        if _k32.WaitForSingleObject(pi.hProcess, max(1, int(timeout * 1000))) == WAIT_TIMEOUT:
            timed_out = True
            _k32.TerminateProcess(pi.hProcess, 124)
            _k32.WaitForSingleObject(pi.hProcess, 5000)
        code = wt.DWORD(0)
        _k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        return code.value, timed_out, assigned, assign_error, ""
    finally:
        for fd in (out_fd, err_fd, nul_fd):
            if fd:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if attributes is not None and attributes[0] is not None:
            _k32.DeleteProcThreadAttributeList(ctypes.cast(attributes[0], ctypes.c_void_p))
        if pi.hThread:
            _k32.CloseHandle(pi.hThread)
        if pi.hProcess:
            _k32.CloseHandle(pi.hProcess)


def _child_env(env: dict[str, str], temp_dir: str) -> dict[str, str]:
    child = dict(env)
    child["TEMP"] = temp_dir
    child["TMP"] = temp_dir
    # 本机实测（E:/tmp/i-probe/acenv.py）：环境块里没有 LOCALAPPDATA 时
    # CreateProcessW 直接报 203（ERROR_ENVVAR_NOT_FOUND）——AC 启动要用它算包内的
    # 用户目录。它不是用户私货，是运行必需项，与 SystemRoot 同级。
    for key in ("SystemRoot", "windir", "SystemDrive", "COMSPEC", "PATHEXT", "PATH",
                "LOCALAPPDATA", "APPDATA"):
        if key not in child and os.environ.get(key):
            child[key] = os.environ[key]
    return child


def quote_leading_path(command: str) -> str:
    """给路径型的首词补引号：AC 令牌下 cmd 对未引号的路径会走搜索解析并被拒。

    实测（`E:/tmp/i-probe/ac_quote_isolate.json`，同一令牌、同一工作区）：

    - `"<venv python>" <脚本>` → 成功；`<venv python> <脚本>` → exit 1「拒绝访问。」；
    - 参数引不引号都无所谓（`"<python>" -c print(1)` 正常），只有**首词**决定成败；
    - 受限令牌后端同样的未引号命令成功，所以这不是 cmd 的普遍行为，而是 AC 缺目录
      访问权时暴露出来的：cmd 解析未引号的可执行文件会去搜索，撞上未授权祖先。

    首词不带路径分隔符（`echo`、`npm`）时保持原样，仍按 PATH 解析——给裸名加引号会
    让 cmd 按字面文件名处理，反而把 PATH 搜索关掉。
    """
    text = command.lstrip()
    leading = command[: len(command) - len(text)]
    if not text or text[0] == '"':
        return command
    head, sep, rest = text.partition(" ")
    if '"' in head or "=" in head:
        return command
    if not any(ch in head for ch in ("\\", "/")):
        return command
    return f'{leading}"{head}"{sep}{rest}'


def run_command(
    script: str,
    *,
    workspace: str,
    env: dict[str, str],
    timeout: float,
    memory: str = "",
    pids: int = 0,
    cpus: float = 0.0,
    rule_set: rules_module.SandboxRules | None = None,
) -> wr.CommandReport:
    """在 AppContainer 里跑一条命令。Job 限额与受限令牌后端同一套。"""
    if not supported():
        raise AppContainerUnavailable("AppContainer 后端只在 Windows 上存在")
    rules = rule_set or rules_module.load()
    sid, sid_ptr = profile()
    temp_dir = tempfile.mkdtemp(prefix="blank-ac-")
    job = windows_job.create_job(
        "blank-ac-" + uuid.uuid4().hex[:8],
        memory_bytes=windows_job.parse_size(memory),
        active_process_limit=pids or 0,
        cpu_cpus=cpus,
    )
    try:
        grants, notes = prepare(workspace, temp_dir, rules)
        application, cmdline = wr._command_line(quote_leading_path(script))
        out_path = str(Path(temp_dir) / "stdout.txt")
        err_path = str(Path(temp_dir) / "stderr.txt")
        code, timed_out, assigned, assign_error, spawn_error = _spawn(
            sid_ptr=sid_ptr,
            application=application,
            cmdline=cmdline,
            cwd=workspace,
            env=_child_env(env, temp_dir),
            timeout=timeout,
            job=job,
            out_path=out_path,
            err_path=err_path,
        )
        report = wr.CommandReport(
            exit_code=code,
            stdout=wr._read_bytes(out_path),
            stderr=wr._read_bytes(err_path),
            timed_out=timed_out,
            job=wr._job_report(job, assigned, assign_error),
            error=spawn_error,
            notes=notes,
            grants=grants,
        )
        return report
    finally:
        windows_job.close_job(job)
        shutil.rmtree(temp_dir, ignore_errors=True)


# --------------------------------------------------------------------------- 判据


def _permission_denied(value: object) -> bool:
    """只认"被策略拒绝"：`PermissionError:<13|10013>`。超时、连接重置都不算。"""
    text = str(value or "")
    if not text.startswith("PermissionError:"):
        return False
    return text.split(":", 1)[1] in DENIED_ERRNOS


def judge(ac: object, plain: object, *, details: dict[str, str] | None = None) -> SelfCheck:
    """把两份 JSON 判成一份自检结论。纯函数，测试直接喂合成字典。

    对照先于断言：宿主令牌读不到合成私钥、或连不上外网时，AC 侧的"读不到/连不上"
    不能归因于边界。宁可判装置不可信，也不许把它写成"网络边界已强制"。
    """
    ac_map = ac if isinstance(ac, dict) else {}
    plain_map = plain if isinstance(plain, dict) else {}
    check = SelfCheck(attempted=True, details=dict(details or {}))
    check.plain_secret_read = plain_map.get("secret") == SECRET_TEXT
    check.plain_network = plain_map.get("external") == "ok"
    check.bind = ac_map.get("bind") == "ok"
    check.loopback = ac_map.get("loopback") == "ok"
    check.workspace_write = ac_map.get("ws_write") == WORKSPACE_TEXT
    check.workspace_read = check.workspace_write
    check.secret_denied = _permission_denied(ac_map.get("secret"))
    check.network_denied = _permission_denied(ac_map.get("external"))

    if not check.plain_secret_read:
        check.error = f"宿主令牌对照读不到合成私钥（{plain_map.get('secret')!r}）：AC 侧的拒绝不能归因于边界"
    elif not check.plain_network:
        check.error = f"宿主令牌对照连不上外网（{plain_map.get('external')!r}）：无法区分边界与环境"
    elif not check.bind:
        check.error = f"AC 内无法监听回环端口（{ac_map.get('bind')!r}）：探针载体不可信"
    elif not check.loopback:
        check.error = f"AC 内回环自连失败（{ac_map.get('loopback')!r}）：探针载体不可信"
    elif not check.workspace_write:
        check.error = f"AC 内写不了工作区（{ac_map.get('ws_write')!r}）：文件授权不成立"
    elif not check.secret_denied:
        check.error = f"AC 内读到了合成私钥（{ac_map.get('secret')!r}）：文件边界未强制"
    elif not check.network_denied:
        check.error = (
            f"AC 内外连没有被策略拒绝（{ac_map.get('external')!r}）："
            "只有 PermissionError[13|10013] 才算网络边界生效"
        )
    return check


def _decode(raw: bytes) -> str:
    return wr._decode(raw)


def _loads(text: str) -> dict[str, Any]:
    import json

    try:
        value = json.loads(text.strip().splitlines()[-1] if text.strip() else "")
    except (ValueError, IndexError):
        return {}
    return value if isinstance(value, dict) else {}


def _self_env(home: str, temp_dir: str) -> dict[str, str]:
    env = {
        "USERPROFILE": home,
        "HOME": home,
        "TEMP": temp_dir,
        "TMP": temp_dir,
        "PYTHONIOENCODING": "utf-8",
    }
    for key in ("SystemRoot", "windir", "SystemDrive", "COMSPEC", "PATHEXT", "PATH",
                "LOCALAPPDATA", "APPDATA"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def _run_ac(script: str, *, sid_ptr: int, ws: str, temp_dir: str, env: dict[str, str],
            tag: str) -> tuple[dict[str, Any], str]:
    tag_dir = os.path.join(temp_dir, tag)
    os.makedirs(tag_dir, exist_ok=True)
    out_path = os.path.join(tag_dir, "out.json")
    err_path = os.path.join(tag_dir, "err.txt")
    command = f'"{sys.executable}" "{script}" > "{out_path}" 2> "{err_path}"'
    _, cmdline = wr._command_line(command)
    code, timed_out, _, _, error = _spawn(
        sid_ptr=sid_ptr,
        application=os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe",
        cmdline=cmdline,
        cwd=ws,
        env=env,
        timeout=60.0,
        job=None,
        out_path=os.path.join(tag_dir, "stdout.txt"),
        err_path=os.path.join(tag_dir, "stderr.txt"),
    )
    details = {
        f"{tag}_exit": "None" if code is None else str(code),
        f"{tag}_stdout": _decode(wr._read_bytes(os.path.join(tag_dir, "stdout.txt"))).strip(),
        f"{tag}_stderr": _decode(wr._read_bytes(os.path.join(tag_dir, "stderr.txt"))).strip(),
        f"{tag}_report": _decode(wr._read_bytes(err_path)).strip(),
    }
    if error:
        details[f"{tag}_error"] = error
        return {}, details[f"{tag}_error"]
    if timed_out:
        return {}, "AC 自检子进程超时"
    payload = _loads(_decode(wr._read_bytes(out_path)))
    if not payload:
        return {}, "AC 自检子进程没有输出可解析的 JSON（" + details[f"{tag}_report"][:200] + "）"
    return payload, ""


def _self_check_once() -> SelfCheck:
    check = SelfCheck(attempted=True)
    if not supported():
        check.error = "AppContainer 后端只在 Windows 上存在"
        return check
    if shutil.which("icacls") is None:
        check.error = "找不到 icacls，无法给包 SID 配置 ACL"
        return check
    root = tempfile.mkdtemp(prefix="blank-ac-selftest-")
    home = os.path.join(root, "home")
    ws = os.path.join(root, "ws")
    temp_dir = os.path.join(root, "tmp")
    try:
        os.makedirs(os.path.join(home, ".ssh"))
        os.makedirs(ws)
        os.makedirs(temp_dir)
        secret = os.path.join(home, ".ssh", "id_probe")
        Path(secret).write_text(SECRET_TEXT, encoding="utf-8")
        Path(os.path.join(ws, "keep.txt")).write_text("workspace-read-ok", encoding="utf-8")

        base = wr._open_current_token()
        try:
            owner = wr._token_user_sid(base)
        finally:
            _k32.CloseHandle(ctypes.c_void_p(base))
        wr._set_private(home, owner)

        rules = rules_module.load()
        try:
            _, notes = prepare(ws, temp_dir, rules)
        except (AppContainerUnavailable, rules_module.RulesError) as error:
            check.error = f"自检目录授权失败：{error}"
            return check
        check.details["notes"] = "；".join(notes)

        payload_path = os.path.join(temp_dir, "payload.py")
        Path(payload_path).write_text(_PAYLOAD, encoding="utf-8")
        env = _self_env(home, temp_dir)
        sid, sid_ptr = profile()
        check.details["capability_sid"] = sid
        ac_result, error = _run_ac(payload_path, sid_ptr=sid_ptr, ws=ws, temp_dir=temp_dir, env=env, tag="ac")
        if error:
            check.error = f"AC 子进程没跑起来：{error}"
            return check
        check.details["ac"] = str(ac_result)

        try:
            plain = subprocess.run(
                [sys.executable, payload_path],
                cwd=ws,
                env=env,
                capture_output=True,
                timeout=120.0,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            check.error = f"宿主对照没跑起来：{type(error).__name__}: {error}"
            return check
        plain_result = _loads(_decode(plain.stdout or b""))
        check.details["plain"] = str(plain_result)
        if not plain_result:
            check.error = "宿主对照没有输出可解析的 JSON：" + _decode(plain.stderr or b"")[-200:]
            return check

        return judge(ac_result, plain_result, details=check.details)
    except (OSError, AppContainerUnavailable, wr.TokenError) as error:
        check.error = f"{type(error).__name__}: {error}"
        return check
    finally:
        shutil.rmtree(root, ignore_errors=True)


def self_check(*, force: bool = False) -> SelfCheck:
    """进程内只跑一次；`force` 给 doctor/测试用。准入失败的后端不会被选中。"""
    global _SELF_CACHE
    if _SELF_CACHE is not None and not force:
        return _SELF_CACHE
    with _SELF_LOCK:
        if _SELF_CACHE is not None and not force:
            return _SELF_CACHE
        _SELF_CACHE = _self_check_once()
        return _SELF_CACHE


def cached() -> SelfCheck | None:
    """已有结论就读，没有也不现场探测——治理快照在事件循环里，不能被起进程冻住。"""
    return _SELF_CACHE


def invalidate() -> None:
    global _SELF_CACHE
    _SELF_CACHE = None


_WARM_LOCK = threading.Lock()
_WARM_THREAD: threading.Thread | None = None


def warm() -> None:
    """后台跑一次自检；已有结论或已在跑就什么都不做。"""
    global _WARM_THREAD
    with _WARM_LOCK:
        if _SELF_CACHE is not None:
            return
        if _WARM_THREAD is not None and _WARM_THREAD.is_alive():
            return
        _WARM_THREAD = threading.Thread(target=self_check, name="blank-ac-selfcheck", daemon=True)
        _WARM_THREAD.start()


def available() -> bool:
    """后端选择门：只有进程内**已有**全绿结论时才为真；没有就起后台探测、本拍返回 False。

    自检要起子进程（本机实测约 4 秒），而 `get_sandbox` 会在事件循环里被同步调用——
    在这里同步等会把同进程的团队轮次与 SSE 一起冻住。这一拍退回受限令牌/local 是
    收紧方向（少一层边界），不是放宽；下一拍自检完成即切过来。
    """
    check = _SELF_CACHE
    if check is None:
        warm()
        return False
    return check.ok


__all__ = [
    "AppContainerUnavailable",
    "DENIED_ERRNOS",
    "PROFILE_NAME",
    "SECRET_TEXT",
    "SelfCheck",
    "available",
    "cached",
    "invalidate",
    "judge",
    "prepare",
    "profile",
    "run_command",
    "self_check",
    "supported",
    "warm",
]
