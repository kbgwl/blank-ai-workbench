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
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

CACHE_TTL_SECONDS = 60.0
# 探针口径版本：判定逻辑或阳性对照改了就必须加一。
# 证据包里没有它，就分不清「这台机器变了」和「量它的尺子变了」——两份读数放在一起
# 会看起来像同一件事。只在语义变化时递增，纯重构不动它。
# 2：CPU 限速从"没做"变成"现场压一次自旋子进程"，见 `_probe_cpu_rate`。
# 3：网络边界从"能进 AppContainer"这种能力推断，改成 AppContainer 内的行为证据
#    （零能力包里外连/私钥都被 PermissionError:13 拒），见 `windows_appcontainer.judge`。
# 4：IO 带宽上限从"设上了但没验"改成现场压一次「对照 vs 16MB/s」的写盘对照，
#    见 `_probe_io_rate`；尺度变了，之前的报告与这份不可直接比。
# 5：磁盘容量配额从"没做"改成现场把一个 64MB 的 VHDX 写满，只认 ENOSPC 与写入停止，
#    见 `_probe_disk_quota`；尺度变了，之前的报告与这份不可直接比。
PROBE_VERSION = 5

# 与 windows_appcontainer.py 的权威常量必须一致：winnt.h 的
# ProcThreadAttributeSecurityCapabilities 是 0x00020009。写成 0x00020008
# 时 CreateProcessW 不认这条属性，AppContainer 能力探针会恒假。
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
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
    # 哪个版本的探针给的这个结论（见 PROBE_VERSION）。默认值让老测试无需改动，
    # 但真实探测一律由 `_measure_once` 显式写当前版本。
    probe_version: int = PROBE_VERSION
    job: bool = False
    job_error: str = ""
    restricted_token: bool = False
    restricted_token_error: str = ""
    token_control: bool = False          # DuplicateTokenEx 阳性对照
    # 能不能把进程塞进 AppContainer（固定包 + 零能力）——这只是**能力**，不是边界。
    appcontainer: bool = False
    appcontainer_error: str = ""
    # AppContainer 内的行为证据（`windows_appcontainer.self_check` 的缓存结论）。
    # 网络边界只认这一层：进程"起得来"不等于它真在包里，更不等于出网/读私钥真被拒。
    appcontainer_file_boundary: bool = False
    appcontainer_network_boundary: bool = False
    appcontainer_check: dict[str, Any] = field(default_factory=dict)
    appcontainer_check_error: str = ""
    carrier_control: bool = False        # 不带属性的同载体必须起得来
    # 文件边界是**行为证据**，不是"能造出受限令牌"这种能力推断：自检在同一个受限子进程里
    # 同时验证「读合成私钥被拒」与「工作区读写成功」，再跑宿主令牌/授权后两条对照。
    file_boundary: bool = False
    file_boundary_error: str = ""
    file_check: dict[str, Any] = field(default_factory=dict)
    # CPU 限速是**行为证据**：Job 设上 CpuRate 不代表内核真压得住。探针跑一次
    # "多进程自旋 vs 限速"的对照，只有限速侧真的落在阈值内才算数。
    cpu_rate: bool = False
    cpu_rate_error: str = ""
    cpu_rate_check: dict[str, Any] = field(default_factory=dict)
    # IO 带宽上限同样是行为证据：`SetInformationJobObject(class=31)` 成功只说明
    # 内核收下了结构体。2026-11-10 上一版就栽在这——字段顺序错一位，调用返回成功、
    # 限速根本没开；只有「限速侧写盘明显慢于对照」才算数。
    io_rate: bool = False
    io_rate_error: str = ""
    io_rate_check: dict[str, Any] = field(default_factory=dict)
    # 磁盘容量配额是行为证据：挂上一个 64MB 的 VHDX，写满后必须拿到 ENOSPC 且继续写不再增长。
    # "VHD 能建"、"卷能挂"都不算数——容量上限要真的让写盘失败才算。
    disk_quota: bool = False
    disk_quota_error: str = ""
    disk_quota_check: dict[str, Any] = field(default_factory=dict)
    measured_at: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def apparatus_ok(self) -> bool:
        """探测装置自己可信吗？两条对照 any-fail 就整体作废。"""
        return self.carrier_control and self.token_control

    @property
    def filesystem_boundary(self) -> bool:
        """文件边界此刻真的被系统强制吗（AC 行为自检 或 受限令牌行为自检）。"""
        return self.apparatus_ok and (self.file_boundary or self.appcontainer_file_boundary)

    @property
    def network_boundary(self) -> bool:
        """网络边界只有 AppContainer 这一条实测路径；受限令牌不拦网络，不许混着报。"""
        return self.apparatus_ok and self.appcontainer_network_boundary

    @property
    def strength(self) -> str:
        if not self.apparatus_ok:
            return "unknown"
        if self.appcontainer_file_boundary and self.appcontainer_network_boundary:
            # 两条行为证据都在同一个 AC 子进程里量到，另有宿主对照；这才叫系统强制。
            return "os_enforced"
        if self.restricted_token and self.job and self.file_boundary:
            # 受限令牌 + Job + 行为自检通过 = 身份、文件与资源一起收窄。
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

# CPU 探针要跑两轮真自旋（约 3 秒），但"这台机器的 Job 压不压得住 CPU"是进程生命周期
# 内的静态事实。它在事件循环里被同步调用时（治理快照每一步都取），重复跑等于每一步
# 把循环冻住几秒——跨进程 worker 的短租约甚至会因此续不上、把自己取消。
# 因此：进程内只探一次；正在探时不阻塞调用方，这一拍如实按"未强制"记账。
_CPU_MEMO: tuple[bool, str, dict[str, Any]] | None = None
_CPU_MEMO_LOCK = threading.Lock()
# IO 探针要写两轮 32MB（限速侧约 2 秒），与 CPU 同理：进程内只探一次，
# 正在探时这一拍如实按未强制记，不阻塞调用方。
_IO_MEMO: tuple[bool, str, dict[str, Any]] | None = None
_IO_MEMO_LOCK = threading.Lock()
# 磁盘容量探针要建卷、格式化、写满 64MB，比 CPU/IO 还重；同理只探一次，
# 正在探时这一拍如实按未强制记，不阻塞调用方。
_DISK_MEMO: tuple[bool, str, dict[str, Any]] | None = None
_DISK_MEMO_LOCK = threading.Lock()


def _cpu_probe_memoized() -> tuple[bool, str, dict[str, Any]]:
    global _CPU_MEMO
    if _CPU_MEMO is not None:
        return _CPU_MEMO
    if not _CPU_MEMO_LOCK.acquire(blocking=False):
        return False, "CPU 限速探测正在进行，本拍按未强制记账", {}
    try:
        if _CPU_MEMO is None:
            _CPU_MEMO = _probe_cpu_rate()
        return _CPU_MEMO
    finally:
        _CPU_MEMO_LOCK.release()


def _io_probe_memoized() -> tuple[bool, str, dict[str, Any]]:
    global _IO_MEMO
    if _IO_MEMO is not None:
        return _IO_MEMO
    if not _IO_MEMO_LOCK.acquire(blocking=False):
        return False, "IO 限速探测正在进行，本拍按未强制记账", {}
    try:
        if _IO_MEMO is None:
            _IO_MEMO = _probe_io_rate()
        return _IO_MEMO
    finally:
        _IO_MEMO_LOCK.release()


def _disk_probe_memoized() -> tuple[bool, str, dict[str, Any]]:
    global _DISK_MEMO
    if _DISK_MEMO is not None:
        return _DISK_MEMO
    if not _DISK_MEMO_LOCK.acquire(blocking=False):
        return False, "磁盘容量探测正在进行，本拍按未强制记账", {}
    try:
        if _DISK_MEMO is None:
            _DISK_MEMO = _probe_disk_quota()
        return _DISK_MEMO
    finally:
        _DISK_MEMO_LOCK.release()


def measure(*, force: bool = False, ttl: float = CACHE_TTL_SECONDS) -> Capability:
    """带 TTL 的现场探测。缓存命中也要把 `age_seconds` 报出去，别把旧值当现值。

    `force=True` 连 CPU 探针也重跑（doctor 的现场读数用）；平时的 TTL 刷新复用进程内
    已有的 CPU 结论——机器不会每分钟换一次内核对 CPU 限速的支持。
    """
    global _CACHE, _CPU_MEMO, _IO_MEMO, _DISK_MEMO
    now = time.time()
    if _CACHE is not None and not force and now - _CACHE[0] < ttl:
        return _CACHE[1]
    if force:
        with _CPU_MEMO_LOCK:
            _CPU_MEMO = None
        with _IO_MEMO_LOCK:
            _IO_MEMO = None
        with _DISK_MEMO_LOCK:
            _DISK_MEMO = None
    cap = _measure_once(
        cpu_probe=_cpu_probe_memoized,
        io_probe=_io_probe_memoized,
        disk_probe=_disk_probe_memoized,
    )
    _CACHE = (now, cap)
    return cap


def invalidate() -> None:
    global _CACHE, _CPU_MEMO, _IO_MEMO, _DISK_MEMO
    _CACHE = None
    with _CPU_MEMO_LOCK:
        _CPU_MEMO = None
    with _IO_MEMO_LOCK:
        _IO_MEMO = None
    with _DISK_MEMO_LOCK:
        _DISK_MEMO = None


def _appcontainer_check():
    """读 AppContainer 行为自检的进程内结论；**不现场探测**。

    那份自检要起子进程（实测约 4 秒），而本模块的 `measure()` 会在事件循环里被同步调用。
    谁要一份新读数谁显式调 `windows_appcontainer.self_check(force=True)`（doctor 就这么做），
    这里只负责把已有事实搬进账本。
    """
    from blank_api.services.sandbox import windows_appcontainer as wac

    return wac.cached()


_LIMIT_FIELDS = ("memory", "pids", "tree_kill", "cpu", "io", "disk", "network", "filesystem")


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
        # docker_flags 里有 memory/cpus/pids/fsize，没有带宽/容量上限：这两项不跟着点绿。
        return {**dict.fromkeys(_LIMIT_FIELDS, True), "io": False, "disk": False}
    cap = measure()
    if not cap.apparatus_ok:
        # 探测装置自己不可信 ⇒ 一项都不许承诺。宁可全红，不许"默认当成生效"。
        return no_limits()
    # CPU/IO 限速的载体仍是 Job：探测说能压住，但这条账只在 Job 真的可用时成立。
    cpu = bool(cap.job and cap.cpu_rate)
    io = bool(cap.job and cap.io_rate)
    # 磁盘容量：探针证明这台机器上"写满即 ENOSPC"，且本进程的容量卷已经真的挂上。
    disk = bool(cap.disk_quota and _quota_ready())
    if backend == "appcontainer":
        check = _appcontainer_check()
        ok = bool(check and check.ok)
        return {
            "memory": True, "pids": True, "tree_kill": True,
            "cpu": cpu, "io": io, "disk": disk,
            # 目录不可写、外部不可达都由零能力包在系统层拒；两条都来自同一个子进程的行为自检。
            "network": ok and check.network_ok,
            "filesystem": ok and check.file_boundary_ok,
        }
    if backend == "restricted":
        # 受限令牌后端：文件边界来自行为自检；网络一律不承诺——这台机器上就算 AC 可用，
        # 这次执行也没走 AC，借它的证据来点绿灯就是拿另一个后端的边界给这个后端背书。
        return {
            "memory": True, "pids": True, "tree_kill": True,
            # CPU/IO 限速已由行为探针现场压过；磁盘容量由「探针证明 + 容量卷已挂上」两条同真。
            "cpu": cpu, "io": io, "disk": disk,
            "network": False,
            "filesystem": cap.filesystem_boundary,
        }
    # local：命令真的以宿主令牌执行，机器上"能造出受限令牌"与本次执行无关，不许借来冒充。
    job = cap.job
    return {
        "memory": job, "pids": job, "tree_kill": job,
        "cpu": cpu, "io": io, "disk": disk,
        "network": False, "filesystem": False,
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
    if backend == "appcontainer":
        check = _appcontainer_check()
        return "measured_appcontainer_boundary" if (check and check.ok) else "appcontainer_unverified"
    if backend == "restricted":
        cap = measure()
        if not cap.apparatus_ok:
            return "probe_apparatus_failed"
        return "measured_restricted_token_file_boundary" if cap.filesystem_boundary else "measured_job_object"
    cap = measure()
    if not cap.apparatus_ok:
        return "probe_apparatus_failed"
    # local 只借 Job 这一层：机器上能不能造受限令牌/进 AC 与本次执行无关。
    return "measured_job_object" if cap.job else "nothing_enforced"


def _measure_once(cpu_probe=None, io_probe=None, disk_probe=None) -> Capability:
    cap = Capability(platform=sys.platform, probe_version=PROBE_VERSION, measured_at=time.time())
    cap.job, cap.job_error = _probe_job()
    if cap.job:
        # 默认现探（doctor 与单测直接调本函数时就要真读数）；measure() 传进来的
        # 是进程内复用、探测中不阻塞的那一版。
        probe = cpu_probe or _probe_cpu_rate
        cap.cpu_rate, cap.cpu_rate_error, cap.cpu_rate_check = probe()
        io = io_probe or _probe_io_rate
        cap.io_rate, cap.io_rate_error, cap.io_rate_check = io()
    else:
        cap.cpu_rate_error = "Job 不可用，CPU 限速未测"
        cap.io_rate_error = "Job 不可用，IO 限速未测"
    # 容量卷不依赖 Job：它是一条独立的挂载路径，只要求 Windows + 管理员。
    disk = disk_probe or _probe_disk_quota
    cap.disk_quota, cap.disk_quota_error, cap.disk_quota_check = disk()
    cap.restricted_token, cap.token_control, cap.restricted_token_error = _probe_restricted_token()
    if cap.restricted_token and cap.job:
        cap.file_boundary, cap.file_boundary_error, cap.file_check = _probe_file_boundary()
    else:
        cap.file_boundary_error = "受限令牌或 Job 不可用，文件边界未做行为自检"
    if sys.platform == "win32":
        cap.appcontainer, cap.carrier_control, cap.appcontainer_error = _probe_appcontainer()
        check = _appcontainer_check()
        if check is not None:
            cap.appcontainer_file_boundary = check.file_boundary_ok
            cap.appcontainer_network_boundary = check.network_ok
            cap.appcontainer_check = check.as_dict()
            cap.appcontainer_check_error = check.error
        else:
            cap.appcontainer_check_error = "AppContainer 行为自检未运行（本模块不现场起探测）"
    else:
        # POSIX 上"进 AC"这条路根本不存在；容器隔离由 backend=docker 表达，不在这里冒充。
        cap.appcontainer = False
        cap.carrier_control = True
        cap.appcontainer_error = "非 Windows：没有 AppContainer 这条路"
        cap.appcontainer_check_error = "非 Windows：没有 AppContainer 这条路"
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


# 父进程先睡 0.25s，给 `assign_pid` 留出挂 Job 的窗口；之后 N 个自旋子进程必然在 Job 里。
_SPIN_SCRIPT = """\
import subprocess, sys, time
n = int(sys.argv[1])
seconds = float(sys.argv[2])
time.sleep(0.25)
inner = (
    "import time\\nt=time.time();c=time.process_time()\\n"
    "while time.time()-t<%s: pass\\n"
    "print(time.process_time()-c)" % seconds
)
kids = [subprocess.Popen([sys.executable, "-c", inner], stdout=subprocess.PIPE, text=True)
        for _ in range(n)]
print(sum(float(k.communicate()[0]) for k in kids))
"""


def _run_spin(script: Path, workers: int, seconds: float, job=None) -> tuple[float, str]:
    """跑一轮多进程自旋，返回合计 CPU 秒。`job` 非空时整棵树挂进去。"""
    proc = subprocess.Popen(
        [sys.executable, str(script), str(workers), str(seconds)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if job is not None:
        from blank_api.services.sandbox import windows_job as wj

        assigned, error = wj.assign_pid(job, proc.pid)
        if not assigned:
            proc.kill()
            proc.communicate()
            return 0.0, f"挂进 Job 失败：{error}"
    try:
        out, err = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return 0.0, "自旋探针超时"
    if proc.returncode != 0:
        return 0.0, f"自旋探针退出码 {proc.returncode}：{(err or '')[:200]}"
    try:
        return float(out.strip().splitlines()[-1]), ""
    except (IndexError, ValueError):
        return 0.0, f"自旋探针输出读不懂：{out[:200]!r}"


def _probe_cpu_rate() -> tuple[bool, str, dict[str, Any]]:
    """CPU 限速的行为自检：同一负载跑两轮，限速侧必须明显低于不限速对照。

    只查 `SetInformationJobObject` 返回 True 是不够的：本机实测过它在某些标志下
    "设上了但不生效"。判据必须能失败——对照没跑满就判探针不可信，限速侧没掉下来
    就判机器不支持；单核机器构造不出对照，也照实说"测不了"，不猜成支持。
    """
    from blank_api.services.sandbox import windows_job as wj

    cores = os.cpu_count() or 1
    details: dict[str, Any] = {"logical_cpus": cores}
    if not wj.supported():
        return False, "本平台无 Job Object", details
    if cores < 2:
        return False, "单核机器无法构造「限速 vs 不限速」对照", details
    workers = min(4, cores)
    seconds = 0.5
    details.update({"workers": workers, "spin_seconds": seconds})
    fd, path = tempfile.mkstemp(prefix="blank-cpu-probe-", suffix=".py")
    os.close(fd)
    script = Path(path)
    job = None
    try:
        script.write_text(_SPIN_SCRIPT, encoding="utf-8")
        control, error = _run_spin(script, workers, seconds)
        details["control_cpu_seconds"] = round(control, 3)
        if error:
            return False, "对照跑不起来：" + error, details
        floor = workers * seconds * 0.4
        if control < floor:
            return False, f"对照没跑满（{control:.2f} < {floor:.2f} CPU 秒），探针不可信", details
        job = wj.create_job("blank-cpu-probe", cpu_cpus=1.0)
        details["cpu_rate"] = int(job.enforced.get("cpu_rate", 0))
        if not job.ok:
            return False, f"建 Job 失败：{job.error}", details
        if "cpu_rate" not in job.enforced:
            return False, job.cpu_error or "Job 没接受 CPU 限速", details
        capped, error = _run_spin(script, workers, seconds, job)
        details["capped_cpu_seconds"] = round(capped, 3)
        if error:
            return False, "限速侧跑不起来：" + error, details
        limit = control * 0.7
        details["pass_threshold_cpu_seconds"] = round(limit, 3)
        if capped > limit:
            return False, (
                f"限速未生效：限速侧 {capped:.2f} CPU 秒 > 阈值 {limit:.2f}"
                f"（对照 {control:.2f}）"
            ), details
        return True, "", details
    finally:
        if job is not None:
            wj.close_job(job)
        try:
            script.unlink()
        except OSError:
            pass


# 子进程先睡 0.25s，给父进程 assign 进 Job 留窗口；之后每写 1MB 就 flush+fsync，
# 避免"全在页缓存里"把节流读数洗掉。输出最后一行是实测 MB/s。
_IO_WRITE_SCRIPT = """\
import os, sys, time
path, mb = sys.argv[1], int(sys.argv[2])
time.sleep(0.25)
chunk = b"\\xa5" * (1024 * 1024)
start = time.monotonic()
with open(path, "wb", buffering=0) as fh:
    for _ in range(mb):
        fh.write(chunk)
        fh.flush()
        os.fsync(fh.fileno())
elapsed = time.monotonic() - start
print("%.3f" % (mb / elapsed))
"""


def _run_io_write(script: Path, target: str, mb: int, job=None) -> tuple[float, str]:
    """跑一轮写盘，返回实测 MB/s。`job` 非空时整棵树挂进去受 IO 上限约束。"""
    proc = subprocess.Popen(
        [sys.executable, str(script), target, str(mb)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if job is not None:
        from blank_api.services.sandbox import windows_job as wj

        assigned, error = wj.assign_pid(job, proc.pid)
        if not assigned:
            proc.kill()
            proc.communicate()
            return 0.0, f"挂进 Job 失败：{error}"
    try:
        out, err = proc.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return 0.0, "写盘探针超时"
    if proc.returncode != 0:
        return 0.0, f"写盘探针退出码 {proc.returncode}：{(err or '')[:200]}"
    try:
        return float(out.strip().splitlines()[-1]), ""
    except (IndexError, ValueError):
        return 0.0, f"写盘探针输出读不懂：{out[:200]!r}"


def _probe_io_rate() -> tuple[bool, str, dict[str, Any]]:
    """IO 带宽上限的行为自检：同一次写盘，限速侧必须明显慢于不限速对照。

    只看 `SetInformationJobObject(class=31)` 返回 True 是上一轮踩过的坑：结构体字段
    错一位时调用照样成功、限速根本没开。2026-11-10 本机实测：对照 680.9 MB/s；
    请求 16 MB/s 时限速侧 16.1 MB/s。这里把请求值固定为 16 MB/s、写 32MB，
    限速侧约 2 秒；对照必须至少是它的 2 倍，否则这台机器的磁盘本身慢于限速档，
    构造不出对照——如实写"测不了"，不猜成支持。
    """
    from blank_api.services.sandbox import windows_job as wj

    limit_bps = 16 * 1024 * 1024
    mb = 32
    details: dict[str, Any] = {"write_mb": mb, "limit_mb_per_s": 16}
    if not wj.supported():
        return False, "本平台无 Job Object", details
    fd, raw_path = tempfile.mkstemp(prefix="blank-io-probe-", suffix=".py")
    os.close(fd)
    script = Path(raw_path)
    workdir = tempfile.mkdtemp(prefix="blank-io-work-")
    target = os.path.join(workdir, "probe.bin")
    job = None
    try:
        script.write_text(_IO_WRITE_SCRIPT, encoding="utf-8")
        control, error = _run_io_write(script, target, mb)
        details["control_mb_per_s"] = round(control, 1)
        if error:
            return False, "对照跑不起来：" + error, details
        if control < 2 * (limit_bps / 1024 / 1024):
            return False, (
                f"本机写速 {control:.1f} MB/s 不足限速档 16MB/s 的 2 倍，构造不出对照"
            ), details
        job = wj.create_job("blank-io-probe", io_bps=limit_bps)
        details["io_bps"] = int(job.enforced.get("io_bps", 0))
        if not job.ok:
            return False, f"建 Job 失败：{job.error}", details
        if "io_bps" not in job.enforced:
            return False, job.io_error or "Job 没接受 IO 限速", details
        capped, error = _run_io_write(script, target, mb, job)
        details["limited_mb_per_s"] = round(capped, 1)
        if error:
            return False, "限速侧跑不起来：" + error, details
        limit = control * 0.7
        details["pass_threshold_mb_per_s"] = round(limit, 1)
        if capped > limit:
            return False, (
                f"限速未生效：限速侧 {capped:.1f} MB/s > 阈值 {limit:.1f}"
                f"（对照 {control:.1f}）"
            ), details
        return True, "", details
    finally:
        if job is not None:
            wj.close_job(job)
        try:
            script.unlink()
        except OSError:
            pass
        shutil.rmtree(workdir, ignore_errors=True)


def _quota_ready() -> bool:
    """本进程的容量卷此刻是否真的挂上了（不触发挂载，避免在事件循环里跑 diskpart）。"""
    try:
        from blank_api.services.sandbox import windows_volume

        return windows_volume.quota_volume().ready()
    except Exception:  # noqa: BLE001 - 读不到就是"没有"，不许推断成有
        return False


def _probe_disk_quota() -> tuple[bool, str, dict[str, Any]]:
    """容量上限的行为自检：挂一个 64MB 卷、写满，只认 ENOSPC 与"继续写仍然失败"。

    挂得上、容量读得到都不够：判据是写满后的错误必须是 ENOSPC（errno 28 /
    WinError 112/39），且同样的写再试一次仍然失败。2026-11-10 本机实测：容量 62.93MB，
    写 48MB 后剩 3.14MB，继续写 `OSError errno 28`。
    """
    from blank_api.services.sandbox import windows_volume as wv

    size_mb = 64
    details: dict[str, Any] = {"size_mb": size_mb, "admin": wv.is_admin()}
    if sys.platform != "win32":
        return False, "非 Windows：没有 VHDX 容量卷这条路", details
    if not wv.is_admin():
        return False, "需要管理员权限才能挂载容量受限卷", details
    base = tempfile.mkdtemp(prefix="blank-disk-probe-")
    vhdx = os.path.join(base, "probe.vhdx")
    mount_dir = os.path.join(base, "mnt")
    report = wv.mount(vhdx, mount_dir, size_mb)
    details["capacity_mb"] = round(report.capacity_bytes / 1024 / 1024, 2)
    if not report.ok:
        details["mount_error"] = report.error
        # mount() 自己会 detach 并删挂载点，但探针的临时基目录要在这里收掉。
        shutil.rmtree(base, ignore_errors=True)
        return False, report.error, details
    chunk = b"\xa5" * (4 * 1024 * 1024)
    written = 0
    error: OSError | None = None
    zero_write = False
    try:
        target = os.path.join(mount_dir, "fill.bin")
        try:
            with open(target, "wb", buffering=0) as handle:
                while written < report.capacity_bytes + 16 * 1024 * 1024:
                    try:
                        count = handle.write(chunk)
                    except OSError as exc:
                        error = exc
                        break
                    if not count:
                        zero_write = True
                        break
                    written += int(count)
        except OSError as exc:
            error = exc
        details["written_mb"] = round(written / 1024 / 1024, 2)
        try:
            details["free_mb"] = round(shutil.disk_usage(mount_dir).free / 1024 / 1024, 2)
        except OSError as exc:
            details["free_error"] = str(exc)[:200]
        if zero_write:
            return False, "写盘返回 0 字节且没有错误，这个判据不可信", details
        if error is None:
            return False, "写满容量后仍能继续写：容量上限没有生效", details
        winerror = getattr(error, "winerror", None)
        details["write_error"] = {
            "errno": error.errno,
            "winerror": winerror,
            "strerror": str(error)[:200],
        }
        enospc = error.errno == 28 or winerror in (112, 39)
        details["enospc"] = bool(enospc)
        if not enospc:
            return False, f"写满后的错误不是 ENOSPC：{error}", details
        # 写满后继续写必须继续失败，而不是"报过一次就又能写"。
        again_failed = False
        try:
            with open(target, "ab", buffering=0) as handle:
                handle.write(chunk)
        except OSError:
            again_failed = True
        details["second_write_failed"] = again_failed
        if not again_failed:
            return False, "写满后第二次写入仍然成功：容量上限没拦住", details
        return True, "", details
    finally:
        wv.detach_and_remove(vhdx, mount_dir)
        shutil.rmtree(base, ignore_errors=True)


def _probe_restricted_token() -> tuple[bool, bool, str]:
    """用真正要用的形状造一枚受限令牌，并跑 `DuplicateTokenEx` 阳性对照。

    旧版本在这里手搓 `CreateRestrictedToken` 并多传了一个实参（10 个对 9 个形参），
    于是这个探针恒失败——"机器不支持"其实只是探针自己写错了。现在委托给执行后端，
    探针与真正启动命令用的是同一段代码：能造出来才算数，对照失败就说明句柄本身不可信。
    """
    if sys.platform != "win32":
        return False, True, "非 Windows：无内核令牌对象"
    from blank_api.services.sandbox import windows_restricted as wr

    return wr.token_probe()


def _probe_file_boundary() -> tuple[bool, str, dict[str, Any]]:
    """文件边界的行为自检：读合成私钥被拒 + 工作区读写成功 + 三条对照。

    这是"文件边界"唯一的准入证。能造出限制 SID 不等于命令真的被拒；只有同一个受限
    子进程里同时看到"私钥读不到"和"工作区能写"，边界才算被系统强制。
    """
    from blank_api.services.sandbox import windows_restricted as wr

    check = wr.self_check()
    return check.ok, check.error, check.as_dict()


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
    return False, True, "0x%08X -> win32=%s" % (
        PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
        code,
    )


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
