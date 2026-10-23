#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M0 —— 非管理员 Windows 上「哪些内核隔离原语真可用」的实测探针。

对应 `docs/SANDBOX-WINDOWS-DESIGN.md` §7 的 **M0 退出工件**：只勘测，不改产品代码。
它回答的不是"文档说能不能"，而是"这台机器（Win11 家庭版 / 无管理员 / 无 Docker）上
真跑起来返回什么"。这张可行性表决定 M1–M4 的走法，所以它必须先于任何实现存在。

三条硬规矩（沿用本仓库验收纪律）：
1. **常量不靠记忆**。这台机器没有 Windows SDK 头文件（2026-10-22 全盘查过：没有 `winnt.h`），
   所以 `PROC_THREAD_ATTRIBUTE_CAPABILITY_INFO` 这类值先用候选扫描问出"API 认账的那几个"，
   再用**行为**确认（子进程里真的出现我们的 AC SID）；两关都过才记 OK。
2. **每个判定必须"能失败"**：凡期望"被拦"的检查都配一条宿主侧阳性对照（同一个可执行文件、
   同一条命令，在边界外必须成功）。否则分不清"拦住了"和"这个工具本来就跑不起来"。
3. **没有 UNKNOWN 才算过**：退出码 0 == 每条检查都得出了确定结论
   （OK / DENIED+Win32 码 / UNAVAILABLE / NOT_SUPPORTED）；只要有一条停在 UNKNOWN 就返回 1。

用法：
    services/api/.venv/Scripts/python.exe -X utf8 evals/win_isolation_probe.py
    ... win_isolation_probe.py --with-python-copy   # 追加"把解释器复制进授权目录再进边界"（慢，M1 才需要）
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from ctypes import (
    POINTER,
    WinDLL,
    byref,
    c_bool,
    c_byte,
    c_ushort,
    c_void_p,
    c_wchar_p,
    create_string_buffer,
    sizeof,
)
from pathlib import Path

PROBE_DIR = Path(os.environ.get("BLANK_AC_PROBE_DIR", "E:/tmp/acprobe"))
WINDIR = Path(os.environ.get("WINDIR", r"C:\Windows"))
SYSTEM32 = WINDIR / "System32"
AC_NAME = "kbgai-m0-probe"

kernel32 = WinDLL("kernel32", use_last_error=True)
advapi32 = WinDLL("advapi32", use_last_error=True)
try:
    userenv = WinDLL("userenv", use_last_error=True)
except OSError as exc:  # DLL 不存在本身就是要记下来的事实
    userenv = None
    print(f"[warn] userenv.dll 加载失败：{exc}", file=sys.stderr)
ntdll = WinDLL("ntdll", use_last_error=False)

HANDLE = c_void_p
BOOL = c_bool
DWORD = ctypes.c_uint32
LPDWORD = POINTER(DWORD)
SIZE_T = ctypes.c_size_t
PSZ = c_wchar_p


class WinCall(Exception):
    pass


def last_error() -> int:
    return ctypes.get_last_error()


_FMT_ALLOCATE = 0x00000100
_FMT_IGNORE_INSERTS = 0x00000200
_FMT_FROM_SYSTEM = 0x00001000


def win_error(code: int) -> str:
    kernel32.FormatMessageW.restype = DWORD
    buf = c_wchar_p()
    n = kernel32.FormatMessageW(
        _FMT_ALLOCATE | _FMT_IGNORE_INSERTS | _FMT_FROM_SYSTEM,
        None,
        DWORD(code),
        DWORD(0),
        ctypes.cast(byref(buf), c_void_p),
        DWORD(0),
        None,
        None,
    )
    text = (buf.value or "").strip() if n else ""
    if n:
        kernel32.LocalFree(buf)
    return text or f"os error {code}"


# ------------------------------------------------------------------ 记录器

VERDICTS = ("OK", "DENIED", "UNAVAILABLE", "NOT_SUPPORTED", "UNKNOWN")


class Ledger:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, cid, title, verdict, *, expect, evidence, control="", note=""):
        assert verdict in VERDICTS, verdict
        assert cid and title and expect
        self.rows.append(
            {
                "id": cid,
                "title": title,
                "verdict": verdict,
                "expect": expect,
                "evidence": str(evidence)[:900],
                "control": str(control)[:500],
                "note": note,
            }
        )

    def fail_open(self, cid, title, why):
        """探针自身跑不完时也必须给出确定结论，不留 UNKNOWN。"""
        self.add(cid, title, "UNKNOWN", expect="—", evidence=why)

    def counts(self):
        out = {v: 0 for v in VERDICTS}
        for r in self.rows:
            out[r["verdict"]] += 1
        return out


L = Ledger()

# ------------------------------------------------------------------ Win32 结构


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", DWORD),
        ("lpSecurityDescriptor", c_void_p),
        ("bInheritHandle", BOOL),
    ]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", DWORD),
        ("lpReserved", c_wchar_p),
        ("lpDesktop", c_wchar_p),
        ("lpTitle", c_wchar_p),
        ("dwX", DWORD),
        ("dwY", DWORD),
        ("dwXSize", DWORD),
        ("dwYSize", DWORD),
        ("dwXCountChars", DWORD),
        ("dwYCountChars", DWORD),
        ("dwFillAttribute", DWORD),
        ("dwFlags", DWORD),
        ("wShowWindow", c_ushort),
        ("cbReserved2", c_ushort),
        ("lpReserved2", c_void_p),
        ("hStdInput", HANDLE),
        ("hStdOutput", HANDLE),
        ("hStdError", HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", HANDLE),
        ("hThread", HANDLE),
        ("dwProcessId", DWORD),
        ("dwThreadId", DWORD),
    ]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", c_void_p),
        ("Capabilities", POINTER(c_void_p)),
        ("CapabilityCount", DWORD),
        ("Reserved", DWORD),
    ]


class TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", DWORD)]


kernel32.CreateProcessW.restype = BOOL
kernel32.CreateProcessW.argtypes = [
    c_wchar_p,
    c_wchar_p,
    c_void_p,
    c_void_p,
    BOOL,
    DWORD,
    c_void_p,
    c_wchar_p,
    POINTER(STARTUPINFOEXW),
    POINTER(PROCESS_INFORMATION),
]
kernel32.InitializeProcThreadAttributeList.restype = BOOL
kernel32.InitializeProcThreadAttributeList.argtypes = [c_void_p, DWORD, DWORD, POINTER(SIZE_T)]
kernel32.UpdateProcThreadAttribute.restype = BOOL
kernel32.UpdateProcThreadAttribute.argtypes = [
    c_void_p,
    DWORD,
    SIZE_T,  # dwAttribute 是 DWORD_PTR，不是 DWORD
    c_void_p,
    SIZE_T,
    c_void_p,
    POINTER(SIZE_T),
]
kernel32.CreatePipe.restype = BOOL
kernel32.CreatePipe.argtypes = [POINTER(HANDLE), POINTER(HANDLE), POINTER(SECURITY_ATTRIBUTES), DWORD]
kernel32.SetHandleInformation.restype = BOOL
kernel32.ReadFile.restype = BOOL
kernel32.ReadFile.argtypes = [HANDLE, c_void_p, DWORD, POINTER(DWORD), c_void_p]
kernel32.CloseHandle.restype = BOOL
kernel32.WaitForSingleObject.restype = DWORD
kernel32.GetExitCodeProcess.restype = BOOL
advapi32.ConvertSidToStringSidW.restype = BOOL
advapi32.ConvertSidToStringSidW.argtypes = [c_void_p, POINTER(c_wchar_p)]
advapi32.ConvertStringSidToSidW.restype = BOOL
advapi32.ConvertStringSidToSidW.argtypes = [c_wchar_p, POINTER(c_void_p)]
advapi32.CreateProcessWithTokenW.restype = BOOL
advapi32.OpenProcessToken.restype = BOOL
advapi32.GetTokenInformation.restype = BOOL

CREATE_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100
WAIT_OBJECT_0 = 0x00000000
INFINITE_WAIT_MS = 0xFFFFFFFF
# GetCurrentProcess() 返回的是伪句柄 -1。第一版我按 ctypes 默认 restype(c_int) 取它，
# 64 位下被截断成无效句柄（OpenProcessToken 直接报"句柄无效"）——所以这里显式造 64 位伪句柄。
CURRENT_PROCESS = HANDLE(-1)


def q(path) -> str:
    """命令行里带空格/反斜杠的路径口径。"""
    return '"{}"'.format(str(path))


def sid_to_string(psid) -> str:
    out = c_wchar_p()
    if not advapi32.ConvertSidToStringSidW(c_void_p(psid), byref(out)):
        raise WinCall(f"ConvertSidToStringSidW -> {win_error(last_error())}")
    value = out.value or ""
    kernel32.LocalFree(out)
    return value


def sid_from_string(text: str):
    psid = c_void_p()
    if not advapi32.ConvertStringSidToSidW(c_wchar_p(text), byref(psid)):
        raise WinCall(f"ConvertStringSidToSidW({text}) -> {win_error(last_error())}")
    return psid


# ------------------------------------------------------------------ 起进程（可带任意属性）


def _pipe_pair():
    sa = SECURITY_ATTRIBUTES()
    sa.nLength = sizeof(sa)
    sa.bInheritHandle = True
    read_h = HANDLE()
    write_h = HANDLE()
    if not kernel32.CreatePipe(byref(read_h), byref(write_h), byref(sa), DWORD(0)):
        raise WinCall(f"CreatePipe -> {win_error(last_error())}")
    kernel32.SetHandleInformation(read_h, DWORD(1), DWORD(0))  # 读端不给子进程继承
    return read_h, write_h


def _read_all(handle) -> str:
    chunks = bytearray()
    buf = create_string_buffer(8192)
    got = DWORD()
    while True:
        ok = kernel32.ReadFile(handle, buf, DWORD(8192), byref(got), None)
        if not ok or got.value == 0:
            break
        chunks += buf.raw[: got.value]
    return chunks.decode("utf-8", "replace")


def spawn(command_line: str, *, attributes=(), timeout_ms: int = 25_000) -> dict:
    """attributes: [(attr_value, ctypes 结构实例 或 (buffer, size)), ...]；空则为普通宿主起进程。"""
    attr_list = None
    kept = []
    si = STARTUPINFOEXW()
    si.StartupInfo.cb = sizeof(STARTUPINFOEXW)
    if attributes:
        size = SIZE_T(0)
        kernel32.InitializeProcThreadAttributeList(None, DWORD(len(attributes)), DWORD(0), byref(size))
        if not size.value:
            return {"ok": False, "error": f"InitializeProcThreadAttributeList size=0 ({win_error(last_error())})"}
        attr_list = create_string_buffer(size.value)
        if not kernel32.InitializeProcThreadAttributeList(
            ctypes.cast(attr_list, c_void_p), DWORD(len(attributes)), DWORD(0), byref(size)
        ):
            return {"ok": False, "error": f"Initialize failed: {win_error(last_error())}"}
        kept.append(attr_list)
        # 这一行是整个 P3 的命门：属性表只是"填好了"没用，必须挂到 STARTUPINFOEXW 上交给
        # CreateProcessW。第一版漏了挂载，CreateProcess 把整张表当空表忽略（lpAttributeList=NULL
        # 是合法输入，不会报错），于是"每个属性号都能起进程、但没一个进 AC"——看起来像是
        # 这台非管理员机器不支持 AC，实际上是探针自己没把参数传进去。
        si.lpAttributeList = ctypes.cast(attr_list, c_void_p)
        for value, payload in attributes:
            ptr, nbytes = payload
            if not kernel32.UpdateProcThreadAttribute(
                ctypes.cast(attr_list, c_void_p),
                0,
                value,
                ptr,
                nbytes,
                None,
                None,
            ):
                code = last_error()
                kernel32.DeleteProcThreadAttributeList(ctypes.cast(attr_list, c_void_p))
                return {
                    "ok": False,
                    "attribute_rejected": value,
                    "win32": code,
                    "error": f"{code} {win_error(code)}",
                }
    si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
    rd_out, wr_out = _pipe_pair()
    rd_err, wr_err = _pipe_pair()
    kept += [rd_out, wr_out, rd_err, wr_err]
    si.StartupInfo.hStdInput = rd_out
    si.StartupInfo.hStdOutput = wr_out
    si.StartupInfo.hStdError = wr_err
    if not attributes:
        si.StartupInfo.cb = sizeof(STARTUPINFOW)
    pi = PROCESS_INFORMATION()
    flags = DWORD(CREATE_NO_WINDOW | (CREATE_EXTENDED_STARTUPINFO_PRESENT if attributes else 0))
    ok = kernel32.CreateProcessW(
        None,
        c_wchar_p(command_line),
        None,
        None,
        BOOL(True),
        flags,
        None,
        None,
        byref(si),
        byref(pi),
    )
    res = {
        "ok": bool(ok),
        "command": command_line,
        "attr_mounted": bool(si.lpAttributeList) if attributes else False,
    }
    if not ok:
        code = last_error()
        res.update({"win32": code, "error": f"{code} {win_error(code)}"})
    else:
        kernel32.CloseHandle(wr_out)
        kernel32.CloseHandle(wr_err)
        t0 = time.time()
        # 先等退出再读：子进程挂住时立即终止，避免管道读把整条判定钉死在这台机器上
        wait = kernel32.WaitForSingleObject(pi.hProcess, DWORD(timeout_ms))
        if wait != WAIT_OBJECT_0:
            kernel32.TerminateProcess(pi.hProcess, DWORD(0x7F))
        res["stdout"] = _read_all(rd_out)
        res["stderr"] = _read_all(rd_err)
        ec = DWORD()
        kernel32.GetExitCodeProcess(pi.hProcess, byref(ec))
        res["wait"] = "signaled" if wait == WAIT_OBJECT_0 else "timeout"
        res["exit_code"] = ec.value
        res["pid"] = pi.dwProcessId
        res["hProcess"] = pi.hProcess
        res["seconds"] = round(time.time() - t0, 3)
        kernel32.CloseHandle(pi.hThread)
    kernel32.CloseHandle(rd_out)
    kernel32.CloseHandle(rd_err)
    if attributes and attr_list is not None:
        kernel32.DeleteProcThreadAttributeList(ctypes.cast(attr_list, c_void_p))
    return res


class SID_AND_ATTRIBUTES(ctypes.Structure):
    """SECURITY_CAPABILITIES.Capabilities 指向的就是这个数组（x64 下每个 16 字节）。
    按裸指针数组（8 字节）去填，count 与步长对不上 —— 这条也是本机踩出来的。"""

    _fields_ = [("Sid", c_void_p), ("Attributes", DWORD)]


def sec_caps_for(ac_sid_ptr, capability_ptrs=()):
    sc = SECURITY_CAPABILITIES()
    sc.AppContainerSid = ac_sid_ptr if isinstance(ac_sid_ptr, int) else None
    if capability_ptrs:
        arr = (SID_AND_ATTRIBUTES * len(capability_ptrs))(
            *[SID_AND_ATTRIBUTES(int(p), DWORD(0)) for p in capability_ptrs]
        )
        sc.Capabilities = ctypes.cast(arr, POINTER(c_void_p))
        sc.CapabilityCount = DWORD(len(capability_ptrs))
        kept.append(arr)
    sc.Reserved = DWORD(0)
    return sc


kept: list = []


def cap_attr(ac_sid_ptr, capability_ptrs=()) -> tuple:
    sc = sec_caps_for(ac_sid_ptr, capability_ptrs)
    kept.append(sc)
    return ctypes.cast(byref(sc), c_void_p), sizeof(sc)


# ------------------------------------------------------------------ PowerShell 载体

POWERSHELL = SYSTEM32 / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def ps(cmd: str) -> str:
    """把一条 PowerShell 语句编成命令行。

    不能用 `-Command '<脚本>'`：单引号会被 PowerShell 当成**字符串字面量**，
    于是它把脚本原文打印出来、一条也没执行（本机实测踩到，第一版所有 AC 判定因此全是假的）。
    `-EncodedCommand`（UTF-16LE + base64）绕开所有引号层次。
    """
    import base64

    enc = base64.b64encode(cmd.encode("utf-16-le")).decode("ascii")
    return "{} -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {}".format(
        q(POWERSHELL), enc
    )


# ------------------------------------------------------------------ P0 环境事实


def p0_environment():
    L.add(
        "P0.1",
        "系统版本与解释器",
        "OK",
        expect="记录事实",
        evidence="{} build {} / python {} / {}".format(
            sys.getwindowsversion().major and "Windows",
            sys.getwindowsversion().build,
            ".".join(map(str, sys.version_info[:3])),
            sys.executable,
        ),
    )
    tok = HANDLE()
    if not advapi32.OpenProcessToken(CURRENT_PROCESS, DWORD(0x0008), byref(tok)):
        L.add("P0.2", "当前进程是否提升（TokenElevation）", "UNKNOWN",
              expect="OK/DENIED", evidence=f"OpenProcessToken -> {win_error(last_error())}")
        return
    elev = TOKEN_ELEVATION()
    cb = DWORD()
    # TokenElevation = 20；这台机器没有头文件，用"取回来的值只能是 0 或 1"来反证类号没记错
    if not advapi32.GetTokenInformation(tok, 20, byref(elev), DWORD(sizeof(elev)), byref(cb)):
        L.add("P0.2", "当前进程是否提升（TokenElevation=20）", "UNKNOWN",
              expect="取值成功", evidence=f"GetTokenInformation -> {win_error(last_error())}")
    else:
        L.add(
            "P0.2",
            "当前进程是否提升（TokenElevation）",
            "OK" if elev.TokenIsElevated == 0 else "DENIED",
            expect="非提升（0）——这正是本立项的前提",
            evidence=f"TokenIsElevated={elev.TokenIsElevated} cb={cb.value}",
            note="非 0/1 的值会立刻暴露类号取错，这里做了自证",
        )
    # 特权清单：SeImpersonate 决定"以另一身份起进程"能不能走，必须实测而不是引用文档
    rc_priv, privs, enc_priv = run_native([str(SYSTEM32 / "whoami.exe"), "/priv"])
    lines = [ln.strip() for ln in privs.splitlines() if "Se" in ln]
    interesting = [ln for ln in lines if any(
        k in ln for k in ("SeDebug", "SeImpersonate", "SeIncreaseQuota", "SeCreateToken", "SeCreatePermanent"))]
    L.add(
        "P0.3",
        "当前令牌特权（whoami /priv，取关键几项；按 {} 解码）".format(enc_priv),
        "OK" if rc_priv == 0 and lines else "UNKNOWN",
        expect="拿到特权清单",
        evidence="关键项={}".format(interesting or "（一条都没有：说明本机令牌里没有这些特权）"),
        control="清单总行数={}".format(len(lines)),
        note="SeImpersonate/SeIncreaseQuota 缺席就意味着'专用账号 + CreateProcessAsUser'这条 Codex 路线走不通，"
             "最终以 P5 的返回码为准而不是这条清单",
    )
    kernel32.IsProcessInJob.restype = BOOL
    in_job = c_bool(False)
    r = kernel32.IsProcessInJob(HANDLE(-1), None, byref(in_job))
    L.add(
        "P0.4",
        "本进程是否已在某个 Job Object 里（影响 P6 归因）",
        "OK",
        expect="记录事实",
        evidence=f"IsProcessInJob ok={r} in_job={in_job.value}",
    )


# ------------------------------------------------------------------ P1 AppContainer 身份


def find_export(name: str):
    """不靠记忆猜 API 在哪个 DLL：逐个候选 DLL 试取符号，把"到底在谁身上"写成工件。
    （第一版我按记忆写在 advapi32 上，跑起来直接 AttributeError —— 这条函数就是那次教训的产物。）"""
    tried = []
    for dll_name, dll in (("userenv", userenv), ("advapi32", advapi32), ("kernel32", kernel32)):
        try:
            fn = getattr(dll, name)
            return dll_name, fn, tried + [dll_name]
        except AttributeError:
            tried.append(dll_name + ":no")
    return None, None, tried


def p1_identity():
    out = {}
    where, cacp, tried = find_export("CreateAppContainerProfile")
    if where is None:
        L.add("P1.0", "CreateAppContainerProfile 在哪个 DLL", "NOT_SUPPORTED", expect="找到符号",
              evidence="tried=" + ",".join(tried))
    else:
        L.add("P1.0", "CreateAppContainerProfile 在哪个 DLL（本机实测）", "OK", expect="找到符号",
              evidence=f"found in {where}; tried={tried}")
    psid = c_void_p()
    hr = -1
    if cacp is not None:
        cacp.restype = ctypes.c_int
        cacp.argtypes = [c_wchar_p, c_wchar_p, c_wchar_p, c_void_p, POINTER(c_void_p), POINTER(c_void_p)]
        hr = cacp(
            c_wchar_p(AC_NAME), c_wchar_p(AC_NAME), c_wchar_p("kbgai M0 probe"),
            None, byref(psid), None,
        )
    if hr == 0 and psid.value:
        out["created_sid"] = sid_to_string(psid.value)
        out["created_ptr"] = psid.value
        L.add("P1.1", "CreateAppContainerProfile（非管理员建 AC 档案）", "OK",
              expect="返回 S_OK 并给出 SID", evidence=f"hr=0x{hr & 0xFFFFFFFF:08X} sid={out['created_sid']}")
    else:
        out["created_sid"] = None
        out["created_ptr"] = None
        L.add("P1.2", "CreateAppContainerProfile 失败原因", "UNAVAILABLE",
              expect="S_OK", evidence=f"hr=0x{hr & 0xFFFFFFFF:08X} {win_error(hr if hr > 0 else -hr)}")
    # P1.2b：上面那次传的是 NULL SD。给它一个"转换成功、IsValidSecurityDescriptor 通过"的真实 SD
    # 再试一次，才能区分"E_INVALIDARG 是因为我没给 SD"和"这条路本身在本机走不通"。
    # 必须在子进程里跑：实测这个调用会踩进未映射内存（ctypes 与 .NET P/Invoke 两套栈都 AV），
    # 放在本进程里探针自己就没了。
    child = (
        "import ctypes,sys\n"
        "from ctypes import *\n"
        "a=WinDLL('advapi32'); k=WinDLL('kernel32'); u=WinDLL('userenv')\n"
        "a.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype=c_bool\n"
        "a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes=[c_wchar_p,c_uint32,POINTER(c_void_p),POINTER(c_uint32)]\n"
        "sd=c_void_p(); sz=c_uint32()\n"
        "if not a.ConvertStringSecurityDescriptorToSecurityDescriptorW('D:(A;;GA;;;WD)(A;;GR;;;S-1-15-2-1)',1,byref(sd),byref(sz)):\n"
        "    print('CONV_FAIL',k.GetLastError()); sys.exit(2)\n"
        "ivk=getattr(k,'IsValidSecurityDescriptor',None) or getattr(a,'IsValidSecurityDescriptor',None)\n"
        "valid=None\n"
        "if ivk is not None:\n"
        "    ivk.restype=c_bool; valid=bool(ivk(sd))\n"
        "u.CreateAppContainerProfile.restype=c_long\n"
        "u.CreateAppContainerProfile.argtypes=[c_wchar_p,c_wchar_p,c_wchar_p,c_void_p,POINTER(c_void_p)]\n"
        "o=c_void_p()\n"
        "try:\n"
        "    r=u.CreateAppContainerProfile('kbgai-m0-sd','kbgai-m0-sd','probe',sd,byref(o))\n"
        "    print('RC',hex(r&0xFFFFFFFF),'HAS_SID',bool(o.value),'VALID_SD',valid)\n"
        "except OSError as e:\n"
        "    print('CRASHED',type(e).__name__,e,'VALID_SD',valid)\n"
    )
    p12 = run([sys.executable, "-X", "utf8", "-c", child], timeout=90)
    child_out = ((p12.stdout or "") + (p12.stderr or "")).strip()
    worked = child_out.startswith("RC 0x0") and "HAS_SID True" in child_out
    L.add(
        "P1.2b", "CreateAppContainerProfile 换成'真实且通过校验的 SD'后再试一次（子进程里跑，AV 不带走探针）",
        "OK" if worked else "UNAVAILABLE" if ("CRASHED" in child_out or child_out.startswith("RC 0x8"))
        else "UNKNOWN",
        expect="要么返回 S_OK 并给出 SID，要么给出可归因的失败",
        evidence="rc={} out={}".format(p12.returncode, child_out[:260] or "-"),
        note="'VALID_SD True' 且崩溃/失败 => 拒绝与 SD 内容无关：这台机器上非管理员建不出 AC 档案。"
             "UNKNOWN 时整条判据作废（多半是子进程环境问题），不能据此下'不可用'结论",
    )
    # 派生 SID：不需要建档案，任何机器都能算出来（这是"零权限"路线的底）
    where2, derive, tried2 = find_export("DeriveAppContainerSidFromAppContainerName")
    dsid = c_void_p()
    dh = -1
    if derive is not None:
        derive.restype = ctypes.c_int
        derive.argtypes = [c_wchar_p, POINTER(c_void_p)]
        dh = derive(c_wchar_p(AC_NAME), byref(dsid))
    else:
        L.add("P1.3a", "DeriveAppContainerSidFromAppContainerName 在哪个 DLL", "NOT_SUPPORTED",
              expect="找到符号", evidence="tried=" + ",".join(tried2))
    if dh == 0 and dsid.value:
        s = sid_to_string(dsid.value)
        out["derived_sid"] = s
        out["derived_ptr"] = dsid.value
        same = out["created_sid"] == s if out["created_sid"] else None
        L.add("P1.3", "DeriveAppContainerSidFromAppContainerName（零权限拿到 AC SID）", "OK",
              expect="返回 SID",
              evidence=f"sid={s} 与档案 SID 相同={same}",
              note="相同则说明本机档案就是按名字派生的，M1 可以直接用派生 SID，不需要任何特权")
    else:
        out["derived_sid"] = None
        out["derived_ptr"] = None
        L.add("P1.4", "DeriveAppContainerSidFromAppContainerName", "UNAVAILABLE",
              expect="S_OK", evidence=f"hr=0x{dh & 0xFFFFFFFF:08X} {win_error(dh if dh > 0 else -dh)}")
    aps = sid_from_string("S-1-15-2-1")  # ALL APPLICATION PACKAGES
    L.add("P1.5", "把 'S-1-15-2-1'（ALL APPLICATION PACKAGES）解析成 SID", "OK",
          expect="解析成功", evidence=sid_to_string(aps.value))
    out["sid"] = out["created_sid"] or out["derived_sid"]
    out["sid_ptr"] = out["created_ptr"] or out["derived_ptr"]
    if out["sid"] is None:
        L.fail_open("P1.6", "拿不到任何 AC SID", "两条取 SID 的路都失败了")
    return out


# ------------------------------------------------------------------ P2 授权目录（ACL）


def p2_setup_files():
    """造出"授权子树 / 未授权的同级文件"，全部落在我们自己拥有的目录里。"""
    root = PROBE_DIR
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    granted = root / "granted"
    outside = root / "outside"
    granted.mkdir(parents=True)
    outside.mkdir(parents=True)
    (granted / "read.txt").write_text("GRANTED-READ-MARKER-7731\n", encoding="utf-8")
    (granted / "sub").mkdir()
    (granted / "sub" / "deep.txt").write_text("DEEP-INHERIT-MARKER-4419\n", encoding="utf-8")
    (outside / "canary.txt").write_text("CANARY-MUST-NOT-BE-READ-1188\n", encoding="utf-8")
    (outside / "writable.txt").write_text("OUTSIDE-WRITE-MARKER-2277\n", encoding="utf-8")
    return root, granted, outside


def run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def run_native(cmd: list[str], timeout: int = 120) -> tuple:
    """系统工具（icacls / whoami / CheckNetIsolation）按 ANSI 代码页输出中文，
    硬按 UTF-8 解会得到乱码——判据不能建在乱码上（本仓库踩过一次'计数为 0 其实是解码错'）。"""
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    raw = (p.stdout or b"") + (p.stderr or b"")
    for enc in ("utf-8", "gbk"):
        try:
            return p.returncode, raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return p.returncode, raw.decode("utf-8", "replace"), "replace"


def icacls_try(target, sid_text, perm):
    """icacls 的 SID 参数有两种写法：直接给 S-1-15-2-… 会被判 1332（无效 SID），
    必须加前导 `*`。两种都试，把"哪种能用"记成工件，而不是靠我背语法。"""
    results = {}
    for label, arg in (("裸 SID", sid_text), ("*前缀 SID", "*" + sid_text)):
        rc, text, enc = run_native(
            [str(SYSTEM32 / "icacls.exe"), str(target), "/grant", "{}:{}".format(arg, perm)])
        results[label] = (rc, text.strip()[:120])
        if rc == 0:
            MARKER["grant_form"] = arg
            return label, results
    return None, results


def p2_grant(sid: str, granted: Path, outside: Path):
    """非管理员能否在自有文件上给 AC SID 写 ACE；用 /T 的深度决定生产要不要 helper copy。"""
    label, results = icacls_try(granted / "read.txt", sid, "R")
    L.add(
        "P2.1",
        "icacls 给 AC SID 授读权限的可用写法（本机实测语法）",
        "OK" if label else "UNAVAILABLE",
        expect="至少一种写法 rc=0",
        evidence="可用写法={} 各尝试={}".format(label, {k: (v[0], v[1][:60]) for k, v in results.items()}),
        note="裸 SID 报 1332(ERROR_INVALID_SID)：生产代码里拼 icacls 参数必须带 `*`，这是一条会静默失效的坑",
    )
    if label is None:
        return
    for f in (granted / "sub" / "deep.txt",):
        rc, text, enc = run_native([str(SYSTEM32 / "icacls.exe"), str(f), "/grant",
                                    "{}:R".format(MARKER["grant_form"])])
        L.add("P2.1b", f"给递归子文件单独授权（{f.name}）", "OK" if rc == 0 else "UNAVAILABLE",
              expect="rc=0", evidence=f"rc={rc} out={text.strip()[:160]}")
    t0 = time.time()
    rc, text, enc = run_native([str(SYSTEM32 / "icacls.exe"), str(granted), "/grant",
                                "{}:(OI)(CI)(RX)".format(MARKER["grant_form"]), "/T"])
    rcv, vertext, _ = run_native([str(SYSTEM32 / "icacls.exe"), str(granted / "sub" / "deep.txt"), "/verify"])
    L.add(
        "P2.2",
        "icacls 对目录 /grant (OI)(CI)(RX) /T 递归授权",
        "OK" if rc == 0 and rcv == 0 else "UNAVAILABLE",
        expect="rc=0 且子文件真的拿到了继承 ACE",
        evidence="rc={} 用时={:.2f}s（2 个文件）verify rc={} {}".format(
            rc, time.time() - t0, rcv, vertext.strip()[:200]
        ),
        note="这条的'用时'是 M1 决策依据：继承 ACE 不会追溯已有子项，生产必须 /T 或 helper copy",
    )
    t1 = time.time()
    rc2, text2, _ = run_native([str(SYSTEM32 / "icacls.exe"), str(outside), "/deny",
                                "{}:R".format(MARKER["grant_form"])])
    L.add(
        "P2.3",
        "icacls /deny 显式拒绝读（Codex 的 deny_read 机制在本机能否非管理员复刻）",
        "OK" if rc2 == 0 else "UNAVAILABLE",
        expect="rc=0",
        evidence=f"rc={rc2} 用时={time.time() - t1:.2f}s out={text2.strip()[:200]}",
    )


# ------------------------------------------------------------------ P3 把进程放进 AC


def close_proc(res):
    if res.get("hProcess"):
        kernel32.CloseHandle(res["hProcess"])
        res["hProcess"] = None


def p3_find_attribute(sid_ptr, granted: Path, outside: Path) -> dict:
    """候选扫描 + 行为确认：找出 capability 属性到底是哪个数，以及本机能否用。

    判据不认 whoami 的输出格式（本机中文系统），只认效果：
    同一个 powershell，在边界内"读得到授权文件、读不到未授权文件"才算真进了 AC。
    """
    our_sid = sid_to_string_safe(sid_ptr)
    MARKER["sid"] = our_sid
    whoami_cmd = q(SYSTEM32 / "whoami.exe") + " /all"
    probe_cmd = ps(
        "try{{$a=[IO.File]::ReadAllText('{}')}}catch{{$a='DENIED_A'}};"
        "try{{$b=[IO.File]::ReadAllText('{}')}}catch{{$b='DENIED_B'}};"
        "'GRANT='+$a.Substring(0,[Math]::Min(12,$a.Length))+'|CANARY='+$b.Substring(0,[Math]::Min(12,$b.Length))".format(
            granted / "read.txt", outside / "canary.txt")
    )
    host = spawn(whoami_cmd, timeout_ms=10_000)
    host_out = host.get("stdout") or ""
    close_proc(host)
    host_probe = spawn(probe_cmd, timeout_ms=20_000)
    close_proc(host_probe)
    # 先证明"这个标记与这条命令本身有区分度"：宿主必须能读两处、且 whoami 里没有我们的 AC SID。
    # 装置自检 —— 否则后面所有 DENIED 都可能是"工具本来就跑不起来"。
    host_text = _text(host_probe)
    carrier_ok = "GRANT=GRANTED-READ" in host_text and "CANARY=CANARY-MUST" in host_text
    L.add(
        "P3.0",
        "装置自检：宿主基线（whoami 不含我们的 AC SID + 载体真的执行了脚本、两个文件都读得到）",
        "OK" if (our_sid not in host_out and carrier_ok) else "UNKNOWN",
        expect="边界外：whoami 无 " + our_sid + " 且输出精确等于 GRANT=GRANTED-READ / CANARY=CANARY-MUST",
        evidence="host exit={} 含我们AC SID={} 含任一 S-1-15-2-*={} probe={}".format(
            host.get("exit_code"), our_sid in host_out, "S-1-15-2-" in host_out, _shape(host_probe)),
        note="输出必须带**文件内容片段**，不能只判关键字：第一版用 -Command '脚本' 时 PowerShell 把脚本原文"
             "打印出来（单引号=字符串字面量），关键字当然'命中'，整组 AC 判定全是假的。这条就是那次教训的闸门",
    )
    accepted = []
    rejected_codes = {}
    # P3.0b 变异自检：这张属性表到底有没有交给 CreateProcessW？
    # 用「只有挂载了才可能失败」的属性做探针 —— PARENT_PROCESS 塞一个假句柄：
    # UpdateProcThreadAttribute 只校验缓冲区大小，所以它一定通过；CreateProcessW 才会去
    # 校验句柄本身。于是"这条能失败"正好等价于"属性表真被消费"。
    # （本机实测教训：漏挂 lpAttributeList 时，32 个属性号全部"起得来、没效果"，
    #   看起来像"非管理员不能进 AC"，其实是探针自己没传参数。这条闸门就是为了让那种
    #   自欺在 P4 之前就现形。）
    fake_parent = (ctypes.cast(ctypes.c_void_p(0xDEAD), c_void_p), sizeof(HANDLE))
    ctrl = spawn(probe_cmd, attributes=[(0x00020002, fake_parent)], timeout_ms=12_000)
    close_proc(ctrl)
    consumed = (not ctrl.get("ok")) or ctrl.get("win32") not in (None, 0)
    L.add(
        "P3.0b",
        "变异自检：给 CreateProcessW 塞一个非法属性值，它必须拒绝起进程（=属性表真被消费）",
        "OK" if consumed else "UNKNOWN",
        expect="PARENT_PROCESS=0xDEAD 时 CreateProcessW 失败（多半 win32=6 ERROR_INVALID_HANDLE）",
        evidence="ok={} win32={} attr_mounted={} error={}".format(
            ctrl.get("ok"), ctrl.get("win32"), ctrl.get("attr_mounted"), ctrl.get("error")),
        note="判 UNKNOWN 时 P3.1/P3.2/P4 全部作废：属性被 CreateProcess 静默忽略 = 探针根本没进边界，"
             "而不是这台机器不支持 AC",
    )
    # 载体或挂载不成立就别扫了（32 次 spawn 只会产出 32 条假证据）
    for n in range(0x00, 0x20) if (carrier_ok and consumed) else []:
        value = 0x00020000 | n
        res = spawn(probe_cmd, attributes=[(value, cap_attr(sid_ptr))], timeout_ms=15_000)
        close_proc(res)
        if res.get("attribute_rejected") == value:
            rejected_codes[n] = res["win32"]
            continue
        if not res.get("ok"):
            rejected_codes[n] = res.get("win32", "?")
            continue
        t = _text(res)
        accepted.append(
            {
                "value": hex(value),
                "granted_readable": "GRANT=GRANTED-READ" in t,
                "canary_blocked": "CANARY=DENIED_B" in t,
                "exit": res.get("exit_code"),
                "out": t.strip()[:150],
            }
        )
    L.add(
        "P3.1",
        "UpdateProcThreadAttribute 认账的候选属性号 + 逐个的行为效果",
        "OK" if accepted else ("NOT_SUPPORTED" if (carrier_ok and consumed) else "UNKNOWN"),
        expect="至少一个属性号能起进程",
        evidence="accepted={} rejected~={}".format(
            json.dumps(accepted, ensure_ascii=False)[:820],
            {k: v for k, v in list(rejected_codes.items())[:10]},
        ) if (carrier_ok and consumed) else "自检（P3.0 载体 / P3.0b 挂载）未通过，扫描已跳过：32 次 spawn 只会产出 32 条假证据",
        note="capability 属性的号码只能这样问出来：这台机器没有 Windows SDK 头文件（全盘查过没有 winnt.h）",
    )
    working = [a for a in accepted if a["granted_readable"] and a["canary_blocked"]]
    partial = [a for a in accepted if a["canary_blocked"]]

    # --- 决定性判别：把"属性号被消费但 AC 入口被拒"和"我们的结构填错了"分开。
    #     同一个载体、同一份 payload，只换三样东西：正确号+真 AC SID / 正确号+NULL SID /
    #     一个不存在的号+真 AC SID。判据（三条都能失败）：
    #       1) 不传属性必须起得来（P3.0 已证）；
    #       2) 正确号的错误码 ≠ 不存在号的错误码（50 ERROR_NOT_SUPPORTED）→ 号真的被解释；
    #       3) 正确号下'真 SID'和'NULL SID'同码 → OS 根本没看 payload，是整体拒绝；
    #          若两者不同码 → 说明它在逐字段校验，那就是我们填错了，不能算这台机器不支持。
    #     （本机实测：0x20008 对 where/csrss/notepad/cmd 四个载体、对内置 S-1-15-2-1、
    #       派生未注册 SID、非 AC 的 Users SID、NULL、4~32 字节缓冲区全是同一个码，
    #       而 csrss 不带属性时是另一个码 —— 属性被消费、入口被拒。）
    AC_ATTR = 0x00020008
    GHOST_ATTR = 0x0BADC0DE
    a_real = spawn(probe_cmd, attributes=[(AC_ATTR, cap_attr(sid_ptr))], timeout_ms=15_000)
    a_null = spawn(probe_cmd, attributes=[(AC_ATTR, cap_attr(None))], timeout_ms=15_000)
    a_ghost = spawn(probe_cmd, attributes=[(GHOST_ATTR, cap_attr(sid_ptr))], timeout_ms=15_000)
    for _r in (a_real, a_null, a_ghost):
        close_proc(_r)

    def code_of(r):
        return 0 if r.get("ok") else r.get("win32")

    interpreted = code_of(a_real) != code_of(a_ghost)
    payload_blind = code_of(a_real) == code_of(a_null)
    refused = not a_real.get("ok")
    ac_refused = bool(carrier_ok and consumed and interpreted and refused and payload_blind)
    L.add(
        "P3.1c",
        "AC 属性（0x20008）是否被 CreateProcessW 真正解释；拒绝是否与载荷无关",
        "NOT_SUPPORTED" if ac_refused else "OK" if a_real.get("ok") else "UNKNOWN",
        expect="若本机不支持 AC：真 SID 与 NULL SID 应得到同一个错误码，且与'不存在的属性号'的码不同",
        evidence="真AC SID={} NULL AC SID={} 不存在号={} 不传属性=P3.0(exit 0)；号被解释={} 与载荷无关={}".format(
            code_of(a_real), code_of(a_null), code_of(a_ghost), interpreted, payload_blind),
        note="interpreted=假 → 属性号根本没被消费，P3 全组作废；payload_blind=假 → 是我们填错结构，"
             "不是这台机器的限制（两种情况都必须继续查，不能写成'不支持'）。"
             "跨进程树对照已做：由任务计划程序（父进程 Tasksvc，不在本探针的树/包装层里）跑同一份载荷，"
             "仍是 575 —— 工件 E:/tmp/realmodel/detached-in-tree.txt 与 detached-detached-task.txt",
    )
    if working:
        chosen = working[0]["value"]
        who_in = spawn(whoami_cmd, attributes=[(int(chosen, 16), cap_attr(sid_ptr))], timeout_ms=12_000)
        who_out = _text(who_in)
        close_proc(who_in)
        L.add(
            "P3.2",
            "行为确认：找到能让'授权可读、未授权被拦'的属性号（=真的进了 AC）",
            "OK",
            expect="同一载体在边界内一读一拦",
            evidence="属性号 {} 生效；whoami /all 在边界内的原文={}".format(chosen, who_out[:400] or "-"),
            note="这条过了，M1 才有'把命令放进 AC'的执行原语；whoami 原文一并留档，判据不依赖它",
        )
        return {"attribute": int(chosen, 16), "vehicle": str(POWERSHELL), "ok": True}
    L.add(
        "P3.2",
        "行为确认：找到能把进程放进 AC 的属性号",
        "UNKNOWN" if partial or not (carrier_ok and consumed) or (refused and not payload_blind)
        else "NOT_SUPPORTED",
        expect="同一载体在边界内一读一拦",
        evidence="起得来的属性号={} 只拦不放的属性号={} AC 属性(0x20008)被解释且拒绝与载荷无关={}".format(
            json.dumps(accepted, ensure_ascii=False)[:400],
            json.dumps(partial, ensure_ascii=False)[:400], ac_refused),
        note="UNKNOWN=自检没过/属性只拦不放（多半是 ACL 没授上）/0x20008 的码随载荷变化（=我们结构填错，"
             "不是机器限制）；NOT_SUPPORTED=属性被消费但非管理员进不去 AC",
    )
    return {"attribute": None, "vehicle": None, "ok": False}


def sid_to_string_safe(psid) -> str:
    try:
        return sid_to_string(psid)
    except Exception:
        return "?"


# ------------------------------------------------------------------ P4 行为 A/B（同一载体，边界内外各跑一遍）


def _shape(res) -> str:
    if not res.get("ok"):
        return "LAUNCH_FAIL " + str(res.get("error"))
    body = (res.get("stdout") or "").strip().replace("\r\n", " | ")[:200]
    se = (res.get("stderr") or "").strip().replace("\r\n", " | ")[:300]
    return "exit={} out={} err={}".format(res.get("exit_code"), body or "-", se or "-")


def _launched(res) -> bool:
    return res.get("ok", False)


def _text(res) -> str:
    return ((res.get("stdout") or "") + (res.get("stderr") or "")).replace("\r\n", " ")


DENY_MARKS = ("unauthorizedaccess", "access to the path", "is denied", "拒绝", "access denied")


def _denied(text: str) -> bool:
    """Access Denied 的几种真实形态：.NET 异常、本地化文案（中文系统是"对路径的访问被拒绝"）、Win32 err 5。"""
    low = text.lower()
    return any(m in low for m in DENY_MARKS) or "error 5" in low


def _net_blocked(text: str) -> bool:
    low = text.lower()
    return any(
        m in low
        for m in ("10013", "socketexception", "refused", "timed out", "timeout", "an attempt was made")
    )


# 每条 A/B 各自"在边界内什么算被拦住 / 什么算放行"，显式写死，不做模糊字符串嗅探。
# 返回 UNKNOWN 一律视为探针缺陷（会拖累退出码），不是"结果不好看"。
MARKER = {"sid": "", "grant_form": ""}  # P3 拿到本机 AC SID 后写入；P4.8 用它做精确匹配，避免"空串永远命中"

CASE_RULES = {
    "读授权文件": lambda t, res: "OK" if "READ_OK" in t else ("UNKNOWN" if not _launched(res) else "DENIED"),
    "读未授权的相邻文件": lambda t, res: (
        "OK" if "LEAKED" in t
        else "DENIED" if _launched(res) and _denied(t)
        else "UNKNOWN"
    ),
    "往授权目录写": lambda t, res: "OK" if "WROTE" in t else ("UNKNOWN" if not _launched(res) else "DENIED"),
    "往未授权的同级目录写": lambda t, res: (
        "OK" if "WROTE" in t else "DENIED" if _denied(t) else "UNKNOWN"
    ),
    "读递归子目录里的文件": lambda t, res: "OK" if "DEEP_OK" in t else ("UNKNOWN" if not _launched(res) else "DENIED"),
    "连本机监听端口（环回出网）": lambda t, res: (
        "OK" if "CONNECTED" in t
        else "DENIED" if _launched(res) and t.strip()
        else "UNKNOWN"
    ),
    "令牌环境变量仍在（AC 不隔离 env）": lambda t, res: (
        "OK" if "ENV-LEAK-9931" in t else "DENIED" if "ENV_GONE" in t else "UNKNOWN"
    ),
    "从边界内再生一个子进程（继承性）": lambda t, res: (
        "OK" if MARKER["sid"] in t else ("UNKNOWN" if not _launched(res) else "DENIED")
    ),
}

CASE_NOTES = {
    "读未授权的相邻文件": "不是 DENIED 就等于 ACL 授权口径写漏了 —— 这条是整份设计最要害的一条",
    "往未授权的同级目录写": "同上：写侧漏拦意味着 Agent 能改用户任何文件",
    "连本机监听端口（环回出网）": "环回是 AC 的默认拒绝项；真出网还要看 WFP，M2 另立探针",
    "令牌环境变量仍在（AC 不隔离 env）": "这里 OK 代表'env 看得见'：AppContainer 不管环境变量，"
                                    "所以 P-ENV 越狱探针必须落在应用层而不是内核层",
    "从边界内再生一个子进程（继承性）": "孙进程 whoami 里仍出现 AC SID = 边界随进程树继承；"
                                "不出现 = spawn 逃逸（堵它要靠 CHILD_PROCESS_POLICY 属性，号码同样只能实测）",
}

CASE_EXPECTS = {
    "读授权文件": "授权后可读",
    "读未授权的相邻文件": "Access Denied（Win32 5）",
    "往授权目录写": "授权后可写",
    "往未授权的同级目录写": "Access Denied（Win32 5）",
    "读递归子目录里的文件": "/T 递归授权后可读",
    "连本机监听端口（环回出网）": "连不上（10013 / 超时）",
    "令牌环境变量仍在（AC 不隔离 env）": "看得见 —— 这是设计事实，不是缺陷",
    "从边界内再生一个子进程（继承性）": "孙进程仍在同一个 AC 里",
}


def p4_cases(granted: Path, outside: Path, listener_port: int) -> dict:
    return {
        "读授权文件": ps("$t=[IO.File]::ReadAllText('{}'); if($t -match 'GRANTED-READ') {{'READ_OK'}} else {{'READ_WRONG'}}".format(
            granted / "read.txt")),
        "读未授权的相邻文件": ps("$t=[IO.File]::ReadAllText('{}'); if($t -match 'CANARY') {{'LEAKED'}} else {{'READ_WRONG'}}".format(
            outside / "canary.txt")),
        "往授权目录写": ps("[IO.File]::WriteAllText('{}','WROTE-IN-AC');'WROTE'".format(granted / "in-ac.txt")),
        "往未授权的同级目录写": ps("[IO.File]::WriteAllText('{}','SHOULD-FAIL');'WROTE'".format(outside / "in-ac.txt")),
        "读递归子目录里的文件": ps("$t=[IO.File]::ReadAllText('{}'); if($t -match 'DEEP-INHERIT') {{'DEEP_OK'}} else {{'DEEP_WRONG'}}".format(
            granted / "sub" / "deep.txt")),
        "连本机监听端口（环回出网）": ps("$c=New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1',{}); 'CONNECTED'".format(listener_port)),
        "令牌环境变量仍在（AC 不隔离 env）": ps("if($env:BLANK_AC_CANARY_ENV){{'ENV_VISIBLE:'+($env:BLANK_AC_CANARY_ENV)}}else{{'ENV_GONE'}}"),
        "从边界内再生一个子进程（继承性）": q(SYSTEM32 / "whoami.exe") + " /all",
    }


def p4_behaviour(ctx: dict, granted: Path, outside: Path, listener_port: int):
    attribute = ctx["attribute"]
    os.environ["BLANK_AC_CANARY_ENV"] = "ENV-LEAK-9931"
    cmds = p4_cases(granted, outside, listener_port)
    for i, (name, cmdline) in enumerate(cmds.items(), start=1):
        inside = spawn(cmdline, attributes=[(attribute, cap_attr(ctx["sid_ptr"]))], timeout_ms=20_000)
        host = spawn(cmdline, timeout_ms=20_000)
        verdict = CASE_RULES[name](_text(inside), inside)
        host_verdict = CASE_RULES[name](_text(host), host)
        L.add(
            f"P4.{i}",
            f"AC 内 {name}",
            verdict,
            expect=CASE_EXPECTS[name],
            evidence=_shape(inside),
            control="同一条命令在宿主上 exit/输出={} → 宿主判定={}".format(_shape(host), host_verdict),
            note=CASE_NOTES.get(name, "宿主对照必须判成'放行'，否则这条 DENIED 不可信"),
        )
    del os.environ["BLANK_AC_CANARY_ENV"]


# ------------------------------------------------------------------ P5 受限令牌 / 另一身份


def p5_restricted_token():
    tok = HANDLE()
    # TOKEN_QUERY|TOKEN_DUPLICATE|TOKEN_ASSIGN_PRIMARY —— 只给前两个时 CreateRestrictedToken 报 998
    if not advapi32.OpenProcessToken(CURRENT_PROCESS, DWORD(0x0001 | 0x0002 | 0x0008), byref(tok)):
        L.add("P5.1", "OpenProcessToken(QUERY|DUPLICATE)", "UNKNOWN", expect="成功",
              evidence=win_error(last_error()))
        return
    restricted = HANDLE()
    # 阳性对照：同一个令牌句柄，DuplicateTokenEx 必须成功。少了这条，'CreateRestrictedToken 报 998'
    # 完全可以被解释成"我自己的句柄是坏的"，说明不了这台机器的限制。
    dup = HANDLE()
    advapi32.DuplicateTokenEx.restype = BOOL
    advapi32.DuplicateTokenEx.argtypes = [HANDLE, DWORD, c_void_p, ctypes.c_int, ctypes.c_int,
                                          POINTER(HANDLE)]
    dup_ok = advapi32.DuplicateTokenEx(tok, DWORD(0x000F00FF | 0x0002), None, 2, 1, byref(dup))
    dup_code = 0 if dup_ok else last_error()
    advapi32.CreateRestrictedToken.restype = BOOL
    advapi32.CreateRestrictedToken.argtypes = [HANDLE, DWORD, DWORD, c_void_p, DWORD, c_void_p,
                                                DWORD, c_void_p, c_void_p, POINTER(HANDLE)]
    # 两种 flags 都试：只试'降权组合'时，998 有可能是某个 flag 在这台机器上的特例，
    # 而 flags=0（不删任何东西）是任何 Windows 都该成功的最小形状。
    shapes = {}
    ok = False
    for tag, flags in (("flags=0", 0), ("DISABLE_MAX_PRIVILEGE|WRITE_RESTRICTED", 0x00000001 | 0x00000004)):
        r = HANDLE()
        got = advapi32.CreateRestrictedToken(tok, DWORD(flags), DWORD(0), None, DWORD(0), None,
                                             DWORD(0), None, None, byref(r))
        shapes[tag] = 0 if got else last_error()
        if got:
            ok = True
            restricted = r
    L.add("P5.1b", "阳性对照：DuplicateTokenEx（证明上一个句柄真的可用）",
          "OK" if dup_ok else "UNKNOWN", expect="DuplicateTokenEx 成功",
          evidence="dup={} code={} {}；CreateRestrictedToken 各形状={}".format(
              bool(dup_ok), dup_code, win_error(dup_code) if not dup_ok else "句柄可用",
              json.dumps(shapes, ensure_ascii=False)),
          note="dup 失败时 P5.2 的 UNAVAILABLE 全部作废（那是探针自己的问题，不是机器的）")
    if not ok:
        code = next((v for v in shapes.values() if v), 0)
        L.add("P5.2", "CreateRestrictedToken（非管理员自我降权令牌）",
              "UNAVAILABLE" if dup_ok else "UNKNOWN",
              expect="成功",
              evidence="{} {}；形状={}；对照 DuplicateTokenEx={}（两套栈 ctypes/.NET 同码）".format(
                  code, win_error(code), json.dumps(shapes, ensure_ascii=False), bool(dup_ok)))
        # 早退会把两条与 CreateRestrictedToken 无关的事实一起吞掉（上一版就是这样）：
        # CreateProcessWithTokenW 能不能用、CheckNetIsolation 要不要管理员。
        if dup_ok:
            p5_launch_with_token(dup)
        else:
            L.add("P5.3", "CreateProcessWithTokenW（以另一枚令牌起进程）", "UNKNOWN",
                  expect="先要有一枚拿得到的令牌",
                  evidence="连 DuplicateTokenEx 的对照件都没有，无法问这个 API",
                  note="这条是装置缺陷（探针自己的），不能当成机器的结论")
        p5_net_isolation()
        return
    sid_str = ""
    # 新令牌确实不一样：用 GetTokenInformation(TokenGroups=2) 里 SID 数比对不现实，
    # 直接用"能不能拿它起进程"这条唯一要紧的判定
    p5_launch_with_token(restricted)
    kernel32.CloseHandle(restricted)
    kernel32.CloseHandle(tok)
    if dup_ok:
        kernel32.CloseHandle(dup)
    p5_net_isolation()


def p5_launch_with_token(token):
    """CreateProcessWithTokenW：Codex 的"换身份执行"那一半在本机到底能不能用。
    给它任何一枚我们拿得到的 primary 令牌（降权令牌，或对照里 DuplicateTokenEx 的复制件）都行，
    因为这条要问的是"这个 API 在非管理员下要不要 SeImpersonatePrivilege"，与令牌内容无关。

    上一版只试一种参数形状就写了 UNAVAILABLE——错：87/参数错误 完全可能是我把
    lpApplicationName/lpCommandLine 的分工填错了，那是探针的问题，不是机器的。
    所以这里把几种合法形状都跑一遍，只有拿到 1314（特权缺失）才允许写成"这台机器不让走"。
    """
    advapi32.CreateProcessWithTokenW.argtypes = [HANDLE, DWORD, c_wchar_p, c_wchar_p, DWORD,
                                                  c_void_p, c_wchar_p, c_void_p, POINTER(PROCESS_INFORMATION)]
    where = str(SYSTEM32 / "where.exe")
    shapes = [
        ("lpApplicationName=exe", where, None, DWORD(0)),
        ("lpCommandLine=exe", None, q(where), DWORD(0)),
        ("lpCommandLine+UNICODE_ENV", None, q(where), DWORD(CREATE_UNICODE_ENVIRONMENT)),
    ]
    codes = {}
    for tag, app, cmd, flags in shapes:
        si = STARTUPINFOW()
        si.cb = sizeof(si)
        pi = PROCESS_INFORMATION()
        ok2 = advapi32.CreateProcessWithTokenW(token, flags, app, cmd, DWORD(CREATE_NO_WINDOW),
                                               None, None, byref(si), byref(pi))
        if ok2:
            kernel32.WaitForSingleObject(pi.hProcess, DWORD(5000))
            kernel32.CloseHandle(pi.hThread)
            kernel32.CloseHandle(pi.hProcess)
            codes[tag] = 0
        else:
            codes[tag] = last_error()
    launched = [t for t, c in codes.items() if c == 0]
    privilege_denied = any(c == 1314 for c in codes.values())
    verdict = "OK" if launched else "UNAVAILABLE" if privilege_denied else "NOT_SUPPORTED"
    L.add(
        "P5.3",
        "CreateProcessWithTokenW（以另一枚令牌起进程 = Codex 的换身份路线）",
        verdict,
        expect="成功；或给出可归因的拒绝（1314 特权缺失 / 参数形状无关的固定错误码）",
        evidence="形状→码={}；成功形状={}".format(codes, launched or "无"),
        note="全部形状都是 87/参数错误 → 不是权限被拒，而是这个 API 在本机对我们这种令牌根本不认账，"
             "结论只能是 NOT_SUPPORTED；只有 1314 才说明'缺 SeImpersonate'这一条被证实。"
             "无论哪种，双账号路线在这里都不成立：本机没有第二枚可用的令牌（P5.2 已定）",
    )


def p5_net_isolation():
    # CheckNetIsolation：AC 环回豁免要不要管理员
    cni = SYSTEM32 / "CheckNetIsolation.exe"
    if cni.exists():
        rc, text, enc = run_native([str(cni), "LoopbackExempt", "-s"], timeout=30)
        need_admin = "administrator" in text.lower() or "管理员" in text
        L.add(
            "P5.4",
            "CheckNetIsolation LoopbackExempt -s（给 AC 放行环回的口子需不需要管理员）",
            "OK" if rc == 0 and not need_admin else "UNAVAILABLE" if need_admin else "UNKNOWN",
            expect="记录事实",
            evidence="rc={} 解码={} out={}".format(rc, enc, text.strip()[:300]),
            note="若这里就需要管理员，那'联网档'只能靠默认拒绝或另建机制，不能靠豁免列表。"
                 "本条只测了 -s（列出），没有测 -a（新增豁免）——后者按文档需要管理员，"
                 "但在 AC 本身进不去的机器上这条已经无意义，不值得为它改系统状态",
        )
    else:
        L.add("P5.4", "CheckNetIsolation.exe 是否存在", "NOT_SUPPORTED", expect="存在",
              evidence=str(cni))


# ------------------------------------------------------------------ P6 Job Object


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", DWORD),
        ("Affinity", SIZE_T),
        ("PriorityClass", DWORD),
        ("SchedulingClass", DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    ]


JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9
JobObjectBasicProcessIdList = 3


class JOBOBJECT_BASIC_PROCESS_ID_LIST(ctypes.Structure):
    _fields_ = [
        ("NumberOfAssignedProcesses", DWORD),
        ("NumberOfProcessIdsInList", DWORD),
        ("ListSize", SIZE_T),
    ]


def pids_in_job(job) -> list:
    """JobObjectBasicProcessIdList(=3)：证明'子进程真的挂在这个 Job 上'，
    否则 DENIED 有可能只是 assign 没成功。"""
    kernel32.QueryInformationJobObject.restype = BOOL
    kernel32.QueryInformationJobObject.argtypes = [HANDLE, ctypes.c_int, c_void_p, DWORD, LPDWORD]
    needed = DWORD()
    kernel32.QueryInformationJobObject(job, JobObjectBasicProcessIdList, None, DWORD(0), byref(needed))
    buf = create_string_buffer(max(needed.value, 64))
    got = DWORD()
    if not kernel32.QueryInformationJobObject(job, JobObjectBasicProcessIdList, buf,
                                              DWORD(len(buf)), byref(got)):
        return ["query_fail:" + win_error(last_error())]
    header = JOBOBJECT_BASIC_PROCESS_ID_LIST.from_buffer_copy(
        buf.raw[: sizeof(JOBOBJECT_BASIC_PROCESS_ID_LIST)])
    sz = ctypes.sizeof(ctypes.c_size_t)
    # ProcessIdList 的起点不是 sizeof(结构体)：结构体尾部那个 ULONG_PTR[1] 本身就占 8 字节，
    # 按 sizeof 起步会把第一个 PID 跳过去、读到后面的填充（本机实测拿回 [0]，
    # 于是"挂进去了"这条判定永远 UNKNOWN）。正确起点 = sizeof - 一个元素。
    off = sizeof(JOBOBJECT_BASIC_PROCESS_ID_LIST) - sz
    pids = []
    for _ in range(header.NumberOfProcessIdsInList):
        pids.append(int.from_bytes(buf.raw[off : off + sz], sys.byteorder))
        off += sz
    return pids


def job_child(code: str, sleep_first: float = 1.2):
    """用 Popen 起一个可控子进程：先睡 sleep_first 秒（留出 assign 窗口），再执行 code。"""
    body = "import time,sys;time.sleep({});{}\n".format(sleep_first, code)
    return subprocess.Popen([sys.executable, "-X", "utf8", "-c", body],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")


def assign(job, proc) -> tuple:
    ok = bool(kernel32.AssignProcessToJobObject(job, HANDLE(int(proc._handle))))
    return ok, (0 if ok else last_error())


def wait_out(proc, timeout=25) -> tuple:
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, (out or "").strip()
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        return -999, (out or "").strip() + " [TIMEOUT] "


def p6_jobobject():
    job = kernel32.CreateJobObjectW(None, c_wchar_p("kbgai-m0-job"))
    if not job:
        code = last_error()
        L.add("P6.1", "CreateJobObjectW（非管理员建 Job）", "UNAVAILABLE", expect="成功",
              evidence=f"{code} {win_error(code)}")
        return
    L.add("P6.1", "CreateJobObjectW（非管理员建 Job）", "OK", expect="成功", evidence=f"handle={job}")
    kernel32.SetInformationJobObject.restype = BOOL
    kernel32.SetInformationJobObject.argtypes = [HANDLE, ctypes.c_int, c_void_p, DWORD]
    kernel32.AssignProcessToJobObject.restype = BOOL
    kernel32.AssignProcessToJobObject.argtypes = [HANDLE, HANDLE]
    kernel32.ResumeThread.restype = DWORD

    def set_limits(flags, mem=None, active=None):
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = DWORD(flags)
        if mem:
            info.ProcessMemoryLimit = SIZE_T(mem)
        if active:
            info.BasicLimitInformation.ActiveProcessLimit = DWORD(active)
        if not kernel32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                                 byref(info), DWORD(sizeof(info))):
            return "SetInformationJobObject -> " + win_error(last_error())
        return "set ok flags=0x{:X} mem={} active={}".format(flags, mem, active)

    alloc = "buf=bytearray({}*1024*1024);buf[0]=1;print('ALLOC_OK',len(buf))"

    # --- 基线（不挂 Job）：这条命令本身必须能成功，否则后面的 DENIED 没有意义
    base = job_child(alloc.format(64))
    base_rc, base_out = wait_out(base)
    L.add(
        "P6.2",
        "基线：解释器子进程分配 64MB（Job 外）",
        "OK" if "ALLOC_OK" in base_out else "UNKNOWN",
        expect="成功（这是后面所有 DENIED 判定的阳性对照）",
        evidence="rc={} out={}".format(base_rc, base_out[:120]),
    )

    # --- 内存上限
    st = set_limits(JOB_OBJECT_LIMIT_PROCESS_MEMORY, mem=48 * 1024 * 1024)
    lim = job_child(alloc.format(256))
    ok_a, err_a = assign(job, lim)
    # 时序：Job 的 PID 列表必须在子进程还活着时查。放在 wait_out 之后查，取回来的永远是空表，
    # 于是这条判定自己变成 UNKNOWN——不是机器的事实，是探针的缺陷。
    in_list = pids_in_job(job)
    rc, out = wait_out(lim)
    assigned_in_list = lim.pid in in_list
    L.add(
        "P6.3",
        "SetInformationJobObject 设 ProcessMemoryLimit=48MB + 子进程真的在 Job 的 PID 列表里",
        "OK" if st.startswith("set ok") and ok_a and assigned_in_list
        else "UNAVAILABLE" if not ok_a else "UNKNOWN",
        expect="设限成功、assign 成功，且 QueryInformationJobObject 当场能看到该 PID",
        evidence="{} assign={} err={} 子进程pid={} Job内PID={} 在列表里={}".format(
            st, ok_a, err_a, lim.pid, in_list, assigned_in_list),
        note="assign 返回 True 却没进 PID 列表 = 挂到了别的 Job（本机本进程已在某 Job 内，见 P0.4），"
             "后面所有'DENIED'全是假的——所以这条单独记录并且必须能失败",
    )
    verdict = (
        "UNKNOWN"
        if not ok_a
        else "DENIED"
        if "ALLOC_OK" not in out
        else "OK"
    )
    L.add(
        "P6.4",
        "Job 内存上限真的生效（子进程分配 256MB，上限 48MB）",
        verdict,
        expect="分配失败（没有 ALLOC_OK）",
        evidence="rc={} out={}".format(rc, out[:200]),
        control="P6.2 同命令在 Job 外 rc={} out={}".format(base_rc, base_out[:60]),
        note="verdict=OK 意味着'上限没生效'：那 Docker 之外展示的 memory 限额就是假的，面板必须停止展示",
    )

    # --- 活跃进程数上限（换一套 flags，另起两个 Job）
    kernel32.TerminateJobObject(job, DWORD(0))
    kernel32.CloseHandle(job)
    job2 = kernel32.CreateJobObjectW(None, c_wchar_p("kbgai-m0-job2"))
    if not job2:
        L.add("P6.5", "Job 活跃进程数上限", "UNAVAILABLE", expect="第二个 Job 句柄",
              evidence=win_error(last_error()))
    else:
        def set_limits2(flags, mem=None, active=None):
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = DWORD(flags)
            if active:
                info.BasicLimitInformation.ActiveProcessLimit = DWORD(active)
            return kernel32.SetInformationJobObject(job2, JobObjectExtendedLimitInformation,
                                                     byref(info), DWORD(sizeof(info)))

        set_limits2(JOB_OBJECT_LIMIT_ACTIVE_PROCESS, active=1)
        a = job_child("print('FIRST')", sleep_first=8)  # 占住这 1 个名额
        ok1, e1 = assign(job2, a)
        b = job_child(alloc.format(8), sleep_first=2.0)
        ok2, e2 = assign(job2, b)
        rc_b, out_b = wait_out(b, timeout=20)
        L.add(
            "P6.5",
            "Job 活跃进程数上限=1：第二条子进程进不来（assign 失败或立刻被终止）",
            ("UNKNOWN" if not ok1 else "DENIED" if (not ok2 or "ALLOC_OK" not in out_b) else "OK"),
            expect="第二条被拒或被终止",
            evidence="assign_a={} err_a={} assign_b={} err_b={} b_rc={} b_out={}".format(
                ok1, e1, ok2, e2, rc_b, out_b[:120]),
            control="P6.2/P6.4：同命令在无活跃数限制的 Job 外能正常跑完",
            note="verdict=OK = pids 上限没生效，Docker 之外的 pids 展示值就是假的；"
                 "assign_a 失败时判 UNKNOWN（名额根本没占上，'第二条被挡'无从谈起）",
        )
        kernel32.TerminateJobObject(job2, DWORD(1))
        try:
            a.wait(timeout=15)
        except Exception:
            a.kill()
        kernel32.CloseHandle(job2)

    # --- KILL_ON_JOB_CLOSE：停止任务/关闭沙箱时必须带走整棵树
    job3 = kernel32.CreateJobObjectW(None, c_wchar_p("kbgai-m0-job3"))
    if not job3:
        L.add("P6.6", "KILL_ON_JOB_CLOSE", "UNAVAILABLE", expect="句柄", evidence=win_error(last_error()))
        return
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = DWORD(JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
    kernel32.SetInformationJobObject(job3, JobObjectExtendedLimitInformation, byref(info), DWORD(sizeof(info)))
    sleeper = job_child("print('SURVIVED')", sleep_first=600)
    ok3, e3 = assign(job3, sleeper)
    if not ok3:
        L.add("P6.6", "AssignProcessToJobObject（本机能否往自己的 Job 挂进程）", "UNAVAILABLE",
              expect="成功", evidence=f"{e3} {win_error(e3)}",
              note="失败一般是已在别人的 Job 里且不允许嵌套——P0.4 已记录本进程是否在 Job 内")
        sleeper.kill()
        sleeper.wait(timeout=10)
        kernel32.CloseHandle(job3)
        return
    kernel32.CloseHandle(job3)  # 关句柄 == "沙箱生命周期结束"
    rc_s, out_s = wait_out(sleeper, timeout=12)
    killed = rc_s not in (0, -999) or "SURVIVED" not in out_s
    # 阳性对照：同一命令不挂 Job，句柄关掉后必须还活着（否则上面那条"被杀"没有说服力）
    control = job_child("print('CONTROL_ALIVE')", sleep_first=600)
    time.sleep(1.0)
    alive = control.poll() is None
    control.kill()
    control.wait(timeout=10)
    L.add(
        "P6.7",
        "KILL_ON_JOB_CLOSE：关掉 Job 句柄后 600 秒睡眠的子进程必须死",
        "DENIED" if killed and alive else "OK" if not killed else "UNKNOWN",
        expect="被杀（非 0 退出且没打出 SURVIVED）",
        evidence="rc={} out={}".format(rc_s, out_s[:120] or "-"),
        control="对照：不挂 Job 的同种子进程 1 秒后仍在运行 poll_alive={}".format(alive),
        note="verdict=OK（没杀掉）= '停止任务后沙箱里的进程还在跑'，这是停止/恢复语义的地基；"
             "UNKNOWN 多半是超时(-999)，说明句柄关了却没带走它",
    )


# ------------------------------------------------------------------ 主流程


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-python-copy", action="store_true",
                    help="额外测'把解释器复制进授权目录后再进边界'（慢，M1 才用）")
    ap.add_argument("--json", default=str(PROBE_DIR / "m0-report.json"))
    args = ap.parse_args()

    p0_environment()
    ident = p1_identity()
    root, granted, outside = p2_setup_files()
    ctx = {"sid_ptr": None, "attribute": None, "vehicle": None}
    if ident["sid"]:
        p2_grant(ident["sid"], granted, outside)
        ctx["sid_ptr"] = ident["sid_ptr"]
        found = p3_find_attribute(ident["sid_ptr"], granted, outside)
        ctx["attribute"] = found["attribute"]
        ctx["vehicle"] = found["vehicle"]
        if found["ok"]:
            listener = start_local_listener()
            p4_behaviour(ctx, granted, outside, listener)
        else:
            L.add("P4.0", "AC 行为 A/B 整组", "NOT_SUPPORTED",
                  expect="—", evidence="P3 没能确认'进边界'的属性号（成因见 P3.0/P3.1/P3.2 三条），"
                                        "行为 A/B 无从谈起",
                  note="这条决定 M1 是否只剩'受限令牌 + Job'两条腿")
    else:
        L.add("P3.0", "把进程放进 AC", "NOT_SUPPORTED", expect="—",
              evidence="拿不到 AC SID")
    p5_restricted_token()
    p6_jobobject()
    if args.with_python_copy:
        p7_helper_copy(ctx)

    counts = L.counts()
    print_report(counts)
    report = Path(args.json)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"counts": counts, "rows": L.rows}, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(f"\n[artifact] {report}")
    return 1 if counts["UNKNOWN"] else 0


LISTENER = {}


def start_local_listener() -> int:
    """本机环回监听端口：AC 的出网判定用它做对照，全程不碰外部网络。"""
    import socket
    import threading

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    port = s.getsockname()[1]

    def loop():
        while True:
            try:
                c, _ = s.accept()
                c.sendall(b"LISTENER-OK\n")
                c.close()
            except OSError:
                return


def p7_helper_copy(ctx):
    """M1 的真问题：边界内的进程得是可执行的，而 AC 读不到宿主目录 —— 复制要付多少代价。"""
    if not ctx.get("attribute"):
        L.add("P7.1", "helper copy 可行性", "NOT_SUPPORTED", expect="—", evidence="P3 没拿到属性号")
        return
    src = Path(sys.base_prefix)
    dst = PROBE_DIR / "runner-copy"
    t0 = time.time()
    shutil.copytree(src, dst, dirs_exist_ok=True)
    copy_s = round(time.time() - t0, 2)
    files = sum(1 for _ in dst.rglob("*") if _.is_file())
    sid = MARKER["sid"]
    g0 = time.time()
    g = run([str(SYSTEM32 / "icacls.exe"), str(dst), "/grant",
             "{}:(OI)(CI)(RX)".format("*" + sid), "/T"], timeout=600)
    L.add(
        "P7.2",
        f"把解释器复制进工作区（{files} 个文件）并给 AC 授 RX",
        "OK" if g.returncode == 0 else "UNAVAILABLE",
        expect="复制成功 + icacls rc=0",
        evidence="复制用时={}s 授权用时={}s → {}".format(copy_s, round(time.time() - g0, 2), dst),
        note="生产用这份数据决定 helper copy 的缓存策略（Codex 是每个 CLI 版本一份，见 REA §5）",
    )
    run_cmd = q(dst / "python.exe") + " -c " + "'print(\"IN_AC_PYTHON_OK\")'"
    inside = spawn(run_cmd, attributes=[(ctx["attribute"], cap_attr(ctx["sid_ptr"]))], timeout_ms=25_000)
    close_proc(inside)
    L.add(
        "P7.3",
        "授权后，解释器本身能在 AC 里跑起来（=M1 的通用执行载体）",
        "OK" if "IN_AC_PYTHON_OK" in _text(inside) else
        ("UNKNOWN" if not _launched(inside) else "DENIED"),
        expect="打印 IN_AC_PYTHON_OK",
        evidence=_shape(inside),
        control="同一条命令在宿主上={}".format(_shape(spawn(run_cmd, timeout_ms=15_000))),
        note="DENIED 说明还要额外授权（DLLs/Lib 分目录、site-packages 在工作区外）；"
             "这条决定了 M1 是'复制解释器'还是'只允许授权目录内的脚本'",
    )


def print_report(counts):
    print("=" * 100)
    print("M0 权限现实勘测 —— 这台机器（非管理员 / 无 Docker）上可用的内核隔离原语")
    print("=" * 100)
    for r in L.rows:
        mark = {"OK": "[可用]", "DENIED": "[拦住]", "UNAVAILABLE": "[缺]",
                "NOT_SUPPORTED": "[不支持]", "UNKNOWN": "[?未定]"}[r["verdict"]]
        print(f"{r['id']:<7} {mark:<7} {r['title']}")
        print(f"        期望: {r['expect']}")
        print(f"        证据: {r['evidence']}")
        if r["control"]:
            print(f"        对照: {r['control']}")
        if r["note"]:
            print(f"        注:   {r['note']}")
    print("-" * 100)
    print("计数: " + " ".join(f"{k}={v}" for k, v in counts.items()))
    bad = [r["id"] for r in L.rows if r["verdict"] == "UNKNOWN"]
    print("未定论: " + (", ".join(bad) if bad else "无（每条都有确定结论）"))


if __name__ == "__main__":
    sys.exit(main())
