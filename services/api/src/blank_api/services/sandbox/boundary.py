"""边界强度：本机此刻**真的**能拿到哪种内核强制，由探针现场测出来，不看文档猜。

为什么需要这个模块：主流产品把 sandbox 讲成"开/关"，而 M0 实测证明"开"在很多机器上只是
Job 那一层（`docs/SANDBOX-WINDOWS-DESIGN.md` §2.0：非管理员 build 26300 上进不去 AppContainer、
造不出受限令牌）。把 `job_only` 冒充成"沙箱已启用"就是本立项要消灭的那类谎言。

三条铁律（都是从 M0 踩的坑里长出来的）：
1. **每个判定都要带装置自检**。AC 探针必须同时跑"不带属性的同载体"阳性对照；受限令牌探针必须
   同时跑 `DuplicateTokenEx` 对照。对照失败 ⇒ 结论只能是 `unknown`，绝不许写 `not_supported`——
   第一版探针漏挂 `lpAttributeList` 时，32 个属性号全部"起得来、没效果"，差点被写成"这台机器不支持 AC"。
2. **测不到就降级，不猜**。测不了 Job 就等于连资源上限都没强制，强度必须落到 `none`。
3. **结论带时间戳**。同一台机器换权限形状结论会变，缓存必须写清"这是什么时候测的"。
"""

from __future__ import annotations

import ctypes
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

CACHE_TTL_SECONDS = 60.0

PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020008
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_NO_WINDOW = 0x08000000
ALL_APPLICATION_PACKAGES = "S-1-15-2-1"

_STRENGTH_LABEL = {
    "os_enforced": "内核强制（身份/文件边界由系统拒）",
    "job_only": "仅资源与生命周期强制（文件、网络边界未由系统强制）",
    "none": "无内核强制（宿主直连）",
    "unknown": "探测装置自身不可信，不承诺任何边界",
}


@dataclass(slots=True)
class Capability:
    """一次探测的全部事实。字段就是证据，别在 UI 里重新推导。"""

    platform: str = ""
    job: bool = False
    job_error: str = ""
    restricted_token: bool = False
    restricted_token_error: str = ""
    token_control: bool = False          # DuplicateTokenEx 阳性对照
    appcontainer: bool = False
    appcontainer_error: str = ""
    carrier_control: bool = False        # 不带属性的同载体必须起得来
    measured_at: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def apparatus_ok(self) -> bool:
        """探测装置自己可信吗？两条对照 any-fail 就整体作废。"""
        return self.carrier_control and self.token_control

    @property
    def strength(self) -> str:
        if not self.apparatus_ok:
            return "unknown"
        if self.appcontainer or (self.restricted_token and self.job):
            # 进 AC = 文件边界由 NTFS 拒；受限令牌 + Job 至少能把身份和资源一起收窄。
            # 只拿到受限令牌而没有 Job，说明连挂都挂不上，不算 os_enforced。
            return "os_enforced"
        if self.job:
            return "job_only"
        return "none"

    @property
    def label(self) -> str:
        return _STRENGTH_LABEL[self.strength]

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["apparatus_ok"] = self.apparatus_ok
        out["strength"] = self.strength
        out["strength_label"] = self.label
        out["age_seconds"] = round(time.time() - self.measured_at, 1) if self.measured_at else None
        return out


def strength_label(strength: str) -> str:
    """档位模块也要给 `none` 一个标签；文案只在这张表里定义一次。"""
    return _STRENGTH_LABEL.get(strength, strength)


_CACHE: tuple[float, Capability] | None = None


def measure(*, force: bool = False, ttl: float = CACHE_TTL_SECONDS) -> Capability:
    """带 TTL 的现场探测。缓存命中也要把 `age_seconds` 报出去，别把旧值当现值。"""
    global _CACHE
    now = time.time()
    if _CACHE is not None and not force and now - _CACHE[0] < ttl:
        return _CACHE[1]
    cap = _measure_once()
    _CACHE = (now, cap)
    return cap


def invalidate() -> None:
    global _CACHE
    _CACHE = None


_LIMIT_FIELDS = ("memory", "pids", "tree_kill", "cpu", "disk", "network", "filesystem")


def no_limits() -> dict[str, bool]:
    """一项都不强制时的账：后端起不来、或沙箱关档，都读这个，别各自手写一遍 False。"""
    return dict.fromkeys(_LIMIT_FIELDS, False)


def enforced_fields(backend: str, *, limits_enabled: bool = True) -> dict[str, bool]:
    """当前后端会真的强制哪几项限额。逐字段回答，不给"已启用/未启用"这种含糊话。

    面板上每一个数字都要能回答"这个数字是谁在执行"。容器的答案是 docker 命令行参数——那是
    容器运行时的合同，不是本机实测；`local` 的答案才来自现场探测。两种来源在
    `enforcement_source` 里分开写，混成一句"沙箱已启用"就是本立项要消灭的那类谎。
    后端起不来（`none`）时一项都不强制，配置里的数字再好看也不作数。
    `limits_enabled=False` 对应关档：`get_sandbox` 仍会给出 local，但那条路径不建 Job、不设限，
    所以这里必须跟着说"没强制"——只按后端名推导，面板就会在用户关掉沙箱时继续撒谎。
    """
    if not limits_enabled or backend in ("none", ""):
        return no_limits()
    if backend == "docker":
        return dict.fromkeys(_LIMIT_FIELDS, True)
    strength = measure().strength
    if strength == "unknown":
        # 探测装置自己不可信 ⇒ 一项都不许承诺。宁可全红，不许"默认当成生效"。
        return no_limits()
    job = strength in ("job_only", "os_enforced")
    os_level = strength == "os_enforced"
    return {
        "memory": job, "pids": job, "tree_kill": job,
        # 非管理员拿不到可靠的 job CPU 限速，磁盘配额也拿不到：设计稿 §8 明确本期不做，
        # 所以这里必须是 False，而不是"看起来像 True"。
        "cpu": False, "disk": False,
        "network": os_level, "filesystem": os_level,
    }


def enforcement_source(backend: str, *, limits_enabled: bool = True) -> str:
    """上面那些 True 是靠什么成立的。UI 要说清来源，不能只点亮一个绿点。"""
    if not limits_enabled:
        return "sandbox_disabled"
    if backend in ("none", ""):
        # 后端起不来：没有任何执行发生，"实测过 Job" 这类说法在这里会误导。
        return "no_backend"
    if backend == "docker":
        return "docker_flags"
    cap = measure()
    if not cap.apparatus_ok:
        return "probe_apparatus_failed"
    return {
        "os_enforced": "measured_appcontainer_or_restricted_token",
        "job_only": "measured_job_object",
        "none": "nothing_enforced",
        "unknown": "probe_apparatus_failed",
    }[cap.strength]


def _measure_once() -> Capability:
    cap = Capability(platform=sys.platform, measured_at=time.time())
    cap.job, cap.job_error = _probe_job()
    cap.restricted_token, cap.token_control, cap.restricted_token_error = _probe_restricted_token()
    if sys.platform == "win32":
        cap.appcontainer, cap.carrier_control, cap.appcontainer_error = _probe_appcontainer()
    else:
        # POSIX 上"进 AC"这条路根本不存在；容器隔离由 backend=docker 表达，不在这里冒充。
        cap.appcontainer = False
        cap.carrier_control = True
        cap.appcontainer_error = "非 Windows：没有 AppContainer 这条路"
    return cap


# --------------------------------------------------------------------------- 三条探针


def _probe_job() -> tuple[bool, str]:
    """建一个一次性 Job 并设上三项限额——设得上才算数。"""
    from blank_api.services.sandbox import windows_job as wj

    if not wj.supported():
        # POSIX 上 Job Object 不存在，但这条不是"探测失败"：容器后端走另一本账。
        return False, "本平台无 Job Object"
    job = wj.create_job("blank-boundary-probe", memory_bytes=64 * 1024 * 1024, active_process_limit=128)
    ok = job.ok
    err = "" if ok else job.error
    wj.close_job(job)
    return ok, err


def _probe_restricted_token() -> tuple[bool, bool, str]:
    """`CreateRestrictedToken(flags=0)` 是任何 Windows 都该成功的最小形状。

    对照 `DuplicateTokenEx` 用同一枚句柄：它失败说明我们的令牌句柄就是坏的，
    那时 `CreateRestrictedToken` 的失败不能归因于机器（M0 的 P5.1b 就是为这个存在的）。
    """
    if sys.platform != "win32":
        return False, True, "非 Windows：无内核令牌对象"
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    current = ctypes.c_void_p(-1)  # 伪句柄；按 c_int 取会被 64 位截断成无效句柄
    adv.OpenProcessToken.restype = ctypes.c_bool
    adv.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    tok = ctypes.c_void_p()
    # TOKEN_ASSIGN_PRIMARY|TOKEN_DUPLICATE|TOKEN_QUERY
    if not adv.OpenProcessToken(current, ctypes.c_uint32(0x0001 | 0x0002 | 0x0008), ctypes.byref(tok)):
        return False, False, "OpenProcessToken -> %d" % ctypes.get_last_error()
    dup = ctypes.c_void_p()
    adv.DuplicateTokenEx.restype = ctypes.c_bool
    adv.DuplicateTokenEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                     ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
    control = bool(adv.DuplicateTokenEx(tok, ctypes.c_uint32(0x000F00FF | 0x0002), None, 2, 1,
                                        ctypes.byref(dup)))
    adv.CreateRestrictedToken.restype = ctypes.c_bool
    adv.CreateRestrictedToken.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                          ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                          ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    newtok = ctypes.c_void_p()
    ok = bool(adv.CreateRestrictedToken(tok, ctypes.c_uint32(0), ctypes.c_uint32(0), None,
                                        ctypes.c_uint32(0), None, ctypes.c_uint32(0), None, None,
                                        ctypes.byref(newtok)))
    err = "" if ok else "CreateRestrictedToken -> %d" % ctypes.get_last_error()
    for h in (tok, dup, newtok):
        if h.value:
            k32.CloseHandle(h)
    return ok, control, err


class _SecCaps(ctypes.Structure):
    _fields_ = [("AppContainerSid", ctypes.c_void_p), ("Capabilities", ctypes.c_void_p),
                ("CapabilityCount", ctypes.c_uint32), ("Reserved", ctypes.c_uint32)]


class _StartupInfoExW(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_uint32), ("lpReserved", ctypes.c_wchar_p), ("lpDesktop", ctypes.c_wchar_p),
                ("lpTitle", ctypes.c_wchar_p), ("dwX", ctypes.c_int32), ("dwY", ctypes.c_int32),
                ("dwXSize", ctypes.c_int32), ("dwYSize", ctypes.c_int32), ("dwXCountChars", ctypes.c_int32),
                ("dwYCountChars", ctypes.c_int32), ("dwFillAttribute", ctypes.c_uint32),
                ("dwFlags", ctypes.c_uint32), ("wShowWindow", ctypes.c_uint16), ("cbReserved2", ctypes.c_uint16),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", ctypes.c_void_p), ("hStdOutput", ctypes.c_void_p),
                ("hStdError", ctypes.c_void_p), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p),
                ("dwProcessId", ctypes.c_uint32), ("dwThreadId", ctypes.c_uint32)]


def _probe_appcontainer() -> tuple[bool, bool, str]:
    """能不能把一个进程放进 AppContainer。判据只有"起得来"这一条，见下方警告。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    exe = "C:\\Windows\\System32\\where.exe"
    cmd = ctypes.create_unicode_buffer('"{}" "cmd.exe"'.format(exe))

    base = _startup_spawn(k32, cmd, [])
    carrier_ok, carrier_code = base
    if not carrier_ok:
        # 连不带属性的同载体都起不来 ⇒ 是探针/环境坏了，不是机器不支持 AC
        return False, False, "载体对照失败（win32=%s）：这条不是机器的结论" % carrier_code

    adv.ConvertStringSidToSidW.restype = ctypes.c_bool
    adv.ConvertStringSidToSidW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
    sid = ctypes.c_void_p()
    if not adv.ConvertStringSidToSidW(ALL_APPLICATION_PACKAGES, ctypes.byref(sid)):
        return False, True, "ConvertStringSidToSidW(%s) -> %d" % (ALL_APPLICATION_PACKAGES,
                                                                 ctypes.get_last_error())
    caps = _SecCaps()
    caps.AppContainerSid = sid.value or 0
    caps.Capabilities = None
    caps.CapabilityCount = 0
    caps.Reserved = 0
    ok, code = _startup_spawn(k32, cmd, [(PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                                          ctypes.byref(caps), ctypes.sizeof(caps))])
    k32.LocalFree(sid)
    if ok:
        return True, True, ""
    return False, True, "0x20008 -> win32=%s" % code


def _startup_spawn(k32, cmd, attributes) -> tuple[bool, str]:
    """带属性表的 CreateProcessW。`attributes` 是 (编号, 指针, 字节数) 三元组列表。

    警告（M0 的原话）：这里判的是"进程起得来"。起得来**不等于**它真在 AppContainer 里——
    本机实测有个属性号 0x20014 也起得来但边界毫无变化。因此本函数的 True 只能让强度升到
    `os_enforced` 的"候选"，正式的边界自证由 `evals/win_isolation_probe.py` 的行为判据（P3.2/P4）
    和 M4 的 `blank doctor --sandbox` 负责；这里不做行为级承诺。
    """
    k32.InitializeProcThreadAttributeList.restype = ctypes.c_bool
    k32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                                      ctypes.POINTER(ctypes.c_size_t)]
    size = ctypes.c_size_t(0)
    # 第一次调用**按文档就是失败**（它只回填所需字节数），这里不能当成错误
    k32.InitializeProcThreadAttributeList(None, max(1, len(attributes)), 0, ctypes.byref(size))
    buf = ctypes.create_string_buffer(size.value or 1)
    if not k32.InitializeProcThreadAttributeList(buf, max(1, len(attributes)), 0, ctypes.byref(size)):
        return False, "Initialize -> %d" % ctypes.get_last_error()
    si = _StartupInfoExW()
    si.cb = ctypes.sizeof(si)
    si.lpAttributeList = ctypes.cast(buf, ctypes.c_void_p)
    kept = []
    try:
        k32.UpdateProcThreadAttribute.restype = ctypes.c_bool
        k32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                                  ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                                  ctypes.c_void_p]
        for number, value, nbytes in attributes:
            holder = ctypes.c_void_p(ctypes.cast(value, ctypes.c_void_p).value or 0)
            kept.append(holder)
            if not k32.UpdateProcThreadAttribute(si.lpAttributeList, 0, ctypes.c_void_p(number),
                                                  holder, ctypes.c_size_t(nbytes), None, None):
                return False, "Update(0x%X) -> %d" % (number, ctypes.get_last_error())
        pi = _ProcessInformation()
        flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW
        k32.CreateProcessW.restype = ctypes.c_bool
        k32.CreateProcessW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_bool, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_wchar_p,
                                       ctypes.c_void_p, ctypes.c_void_p]
        ok = bool(k32.CreateProcessW(None, cmd, None, None, False, flags, None, None,
                                      ctypes.byref(si), ctypes.byref(pi)))
        if not ok:
            return False, "%d" % ctypes.get_last_error()
        k32.WaitForSingleObject(pi.hProcess, ctypes.c_uint32(8000))
        k32.CloseHandle(pi.hThread)
        k32.CloseHandle(pi.hProcess)
        return True, ""
    finally:
        k32.DeleteProcThreadAttributeList(buf)
