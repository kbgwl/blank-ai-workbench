"""命令执行沙箱。

DockerSandbox 把命令放进一次性容器执行：工作区是唯一可写挂载点，默认无网络，
CPU/内存/进程数/文件大小/时长都有上限，超时后强制删除容器以清干净进程树。
Docker 是可选加固档：探测不到可用容器后端时默认退回 LocalSandbox（Job Object 强制
内存/进程数/进程树，文件与网络边界如实标注未强制）。要求"只准容器"的部署设
BLANK_SANDBOX_ALLOW_LOCAL=0，那时没有容器就拒绝执行。
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from blank_api.services.log_safety import redact
from blank_api.services.sandbox import boundary, docker_probe, windows_job, windows_volume
from blank_api.services.sandbox.policy import SandboxPolicy

if TYPE_CHECKING:
    from blank_api.services.sandbox.windows_restricted import CommandReport

MAX_OUTPUT_CHARS = 20000


def join_for_shell(command: str | list[str]) -> str:
    """把 argv 拼成"这层壳自己看得懂"的一行。

    本机档走的是 `create_subprocess_shell`，Windows 上就是 `cmd.exe`（由 COMSPEC 决定）。
    而 `shlex.join` 用的是 POSIX 引号规则：`E:\\app\\python.exe` 会被包成**单引号**，
    cmd 不认单引号，于是整条命令被当成一个文件名，回一句「文件名、目录名或卷标语法
    不正确」。实测（#145）：真模型跑评测样例 T01，验证命令是
    `python -m pytest -q`，解释器被换成本机 venv 的绝对路径后就撞上这条；
    而 T03 的 `npm test` 两个 token 都不需要引用，反而正常——**同一层壳，一半能跑一半不能**。
    Windows 上必须用 `subprocess.list2cmdline`，它与 CreateProcess 的解析规则一致。
    """
    if isinstance(command, str):
        return command
    if sys.platform == "win32":
        return subprocess.list2cmdline(list(command))
    return shlex.join(command)


class SandboxUnavailable(RuntimeError):
    """The configured isolated execution backend is not available."""


@dataclass(slots=True)
class SandboxResult:
    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    backend: str
    truncated: bool = False
    notes: list[str] = field(default_factory=list)
    # 这一次执行**实际被什么强制**的结构化事实（Job 限额、边界强度）。
    # 界面与证据包读这个字段，不读 notes：notes 是给人看的，会被改文案；
    # 把判据建在文案上，改一句话就能把"未强制"说成"已强制"。
    enforcement: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def output(self) -> str:
        text = self.stdout if not self.stderr else f"{self.stdout}\n{self.stderr}".strip()
        return redact(text)


def _clip(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    return text[-MAX_OUTPUT_CHARS:] + "\n…（输出已截断）", True


def _decode_host(raw: bytes) -> str:
    """宿主（local 后端）子进程输出的解码。

    中文 Windows 上 cmd 与系统消息写的是 GBK：一律按 UTF-8 解码会把"配额不足，无法处理此命令。"
    这类**恰好说明限额被内核拒了**的信息变成一串替换字符——等于把最关键的证据丢掉。
    UTF-8 解不动才退回 GBK，两者都解不动才用替换字符。容器里的输出仍是 UTF-8，不走这里。
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace")


class DockerSandbox:
    name = "docker"

    def __init__(self, policy: SandboxPolicy | None = None) -> None:
        self.policy = policy or SandboxPolicy()

    async def run(
        self,
        command: str | list[str],
        *,
        workspace: str,
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> SandboxResult:
        script = command if isinstance(command, str) else shlex.join(command)
        timeout = timeout_seconds or self.policy.timeout_seconds
        container = f"blank-sandbox-{uuid.uuid4().hex[:8]}"
        flags = self.policy.docker_flags(container, os.fspath(workspace))
        for key, value in self.policy.filter_env(env or {}).items():
            flags += ["--env", f"{key}={value}"]
        flags += [self.policy.image, "sh", "-c", script]

        started = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            *flags,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={key: value for key, value in os.environ.items() if key.startswith("DOCKER_")},
        )
        timed_out = False
        try:
            raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            await self._force_remove(container)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            killed_out, killed_err = await proc.communicate()
            raw_out = killed_out
            raw_err = (killed_err.decode("utf-8", "replace") + "\n命令超过时间限制，容器已被强制删除").encode()
        duration = int((time.perf_counter() - started) * 1000)

        stdout, trunc_out = _clip(raw_out.decode("utf-8", "replace"))
        stderr, trunc_err = _clip(raw_err.decode("utf-8", "replace"))
        notes = [f"沙箱超时上限 {timeout:g}s"] if timed_out else []
        return SandboxResult(
            command=redact(script),
            exit_code=None if timed_out else proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration,
            timed_out=timed_out,
            backend=self.name,
            truncated=trunc_out or trunc_err,
            notes=notes,
        )

    async def _force_remove(self, container: str) -> None:
        await docker_rm(container)

    async def cleanup(self) -> int:
        """删除本工具遗留的沙箱容器，返回清理数量；用于验收「任务结束后不存在残留进程」。"""
        return await cleanup_containers()


async def docker_rm(container: str) -> None:
    proc = await asyncio.create_subprocess_exec("docker", "rm", "-f", container,
                                               stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()


async def cleanup_containers() -> int:
    proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "-q", "--filter", "label=blank.sandbox=1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    raw, _ = await proc.communicate()
    ids = [line.strip() for line in raw.decode().splitlines() if line.strip()]
    for container_id in ids:
        await docker_rm(container_id)
    return len(ids)


class LocalSandbox:
    """本机子进程执行：内核强制的部分由 Job Object 提供，其余只是策略级约束。

    M0 探针（`evals/win_isolation_probe.py`，结论见 docs/SANDBOX-WINDOWS-DESIGN.md §2.0）定了两件事：
    - Job 的内存上限、活跃进程数上限、`KILL_ON_JOB_CLOSE` 在非管理员下**真的被内核强制**（都有阳性对照）；
    - AppContainer 入口恒 575、`CreateRestrictedToken` 恒 998、`CreateProcessWithTokenW` 缺特权 →
      **文件与网络边界在这类机器上不可能由系统强制**。
    所以这个类的职责是：把前者做真，把后者说清。它不许在 notes 里写"已隔离"。
    """

    name = "local"

    def __init__(self, policy: SandboxPolicy | None = None) -> None:
        self.policy = policy or SandboxPolicy()

    async def run(
        self,
        command: str | list[str],
        *,
        workspace: str,
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> SandboxResult:
        script = join_for_shell(command)
        timeout = timeout_seconds or self.policy.timeout_seconds
        base = {key: value for key, value in os.environ.items() if key in self.policy.env_allowlist}
        base.update(self.policy.filter_env(env or {}))
        # 临时区放进容量受限卷：第一次要跑 diskpart（几秒），放到线程里，别冻事件循环。
        volume = await asyncio.to_thread(windows_volume.quota_volume().ensure, self.policy.disk_mb)
        scratch = await asyncio.to_thread(windows_volume.quota_volume().new_dir, "blank-run-")
        if scratch:
            base["TEMP"] = base["TMP"] = scratch
            base["BLANK_SCRATCH"] = scratch
        started = time.perf_counter()
        creationflags = 0x00000200 if sys.platform == "win32" else 0

        memory_bytes = windows_job.parse_size(self.policy.memory)
        job = windows_job.create_job(
            "blank-sandbox-" + uuid.uuid4().hex[:8],
            memory_bytes=memory_bytes,
            active_process_limit=self.policy.pids or 0,
            cpu_cpus=self.policy.cpus,
            io_bps=self.policy.io_bps,
        )
        proc = await asyncio.create_subprocess_shell(
            script,
            cwd=workspace,
            env=base,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        # 挂 Job 必须在起进程之后立刻做：这中间它已经派生的进程不算在这条限额里（见 assign_pid 的说明）
        assigned, assign_error = (False, job.error) if not job.ok else windows_job.assign_pid(job, proc.pid)
        # 必须在 close_job 之前算：关句柄会把 job.handle 置 0，之后再读 Job 就永远是"没建成"，
        # 于是这条命令明明被内核限着 48MB，账上却写成"限额未强制"——反向说谎同样是谎。
        # 变异验证：把这一行挪到 close_job 之后，本文件三条 enforcement 断言立刻变红。
        # 走线程：`boundary.measure()` 冷启动要起子进程做行为自检（实测 0.2s 起，机器忙时更久），
        # 直接在事件循环里跑会把同进程的团队轮次/SSE 一起冻住。
        enforcement = await asyncio.to_thread(
            _enforcement, job, assigned, assign_error, volume, scratch
        )
        timed_out = False
        try:
            raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            await kill_process_tree(proc.pid)
            raw_out, raw_err = b"", "命令超过时间限制，进程树已被终止".encode()
        finally:
            # 关句柄 = 沙箱生命周期结束。挂了 KILL_ON_JOB_CLOSE 时，还活着的整棵树一起走；
            # M0 的 P6.7 证明这条在内核层生效，对照（不挂 Job）的同种进程则活得好好的。
            windows_job.close_job(job)
            if scratch:
                windows_volume.quota_volume().drop_dir(scratch)
        duration = int((time.perf_counter() - started) * 1000)
        stdout, trunc_out = _clip(_decode_host(raw_out))
        stderr, trunc_err = _clip(_decode_host(raw_err))
        return SandboxResult(
            command=redact(script),
            exit_code=None if timed_out else proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration,
            timed_out=timed_out,
            backend=self.name,
            truncated=trunc_out or trunc_err,
            notes=_enforcement_notes(enforcement),
            enforcement=enforcement,
        )

    async def cleanup(self) -> int:
        # Job 句柄在每次 run 的 finally 里已经关掉，进程树不会跨任务存活。
        # 这里返回 0 是事实，不是"还没实现"：跨进程残留需要 runner 化之后才谈得上枚举。
        return 0


class RestrictedSandbox:
    """Windows 受限令牌执行：文件边界由 NTFS 真的拒，资源/生命周期由 Job 强制。

    它只在自检全绿时被选中（`windows_restricted.available()`），而自检本身就是准入证：
    同一个受限子进程里必须同时出现「合成私钥读不到」与「工作区读写成功」，外加宿主令牌、
    授权后两条对照。网络没有 AppContainer 就拦不住，账本里必须写 False——拿受限令牌
    冒充网络隔离，就是本立项要消灭的那类谎。
    """

    name = "restricted"

    def __init__(self, policy: SandboxPolicy | None = None) -> None:
        self.policy = policy or SandboxPolicy()

    async def run(
        self,
        command: str | list[str],
        *,
        workspace: str,
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> SandboxResult:
        from blank_api.services.sandbox import rules as rules_module
        from blank_api.services.sandbox import windows_restricted as wr

        script = join_for_shell(command)
        timeout = timeout_seconds or self.policy.timeout_seconds
        base = {key: value for key, value in os.environ.items() if key in self.policy.env_allowlist}
        base.update(self.policy.filter_env(env or {}))
        rules = rules_module.load(self.policy.rules_file or None)
        started = time.perf_counter()
        report = await asyncio.to_thread(
            wr.run_command,
            script,
            workspace=workspace,
            env=base,
            timeout=timeout,
            memory=self.policy.memory,
            pids=self.policy.pids,
            cpus=self.policy.cpus,
            io_bps=self.policy.io_bps,
            disk_mb=self.policy.disk_mb,
            rule_set=rules,
        )
        duration = int((time.perf_counter() - started) * 1000)
        # 同上：探测可能现场起子进程，不能在事件循环里同步等。
        enforcement = await asyncio.to_thread(_enforcement_restricted, report)
        stdout, trunc_out = _clip(_decode_host(report.stdout))
        stderr, trunc_err = _clip(_decode_host(report.stderr))
        if report.error:
            stderr = (stderr + "\n" + report.error).strip() if stderr else report.error
        return SandboxResult(
            command=redact(script),
            exit_code=None if report.timed_out else report.exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration,
            timed_out=report.timed_out,
            backend=self.name,
            truncated=trunc_out or trunc_err,
            notes=_enforcement_notes(enforcement) + list(report.notes),
            enforcement=enforcement,
        )

    async def cleanup(self) -> int:
        # 每次 run 的 finally 都关 Job 句柄（KILL_ON_JOB_CLOSE 带走整棵树），没有跨 run 的残留。
        return 0


class AppContainerSandbox:
    """Windows AppContainer 执行：文件与网络边界都由零能力包在系统层拒。

    只在行为自检全绿时被选中（`windows_appcontainer.available()`）；自检在同一个 AC
    子进程里量「外连被 PermissionError:13 拒」「合成私钥被拒」「工作区读写成功」，
    再跑宿主对照——任何一条对照红掉都判装置不可信。账本里的 filesystem/network 直接
    读那份结论，不从"能不能进 AC"推断。Job 限额与受限令牌后端同一套。
    """

    name = "appcontainer"

    def __init__(self, policy: SandboxPolicy | None = None) -> None:
        self.policy = policy or SandboxPolicy()

    async def run(
        self,
        command: str | list[str],
        *,
        workspace: str,
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        profile_name: str = "",
        extra_read: tuple[str, ...] = (),
    ) -> SandboxResult:
        from blank_api.services.sandbox import rules as rules_module
        from blank_api.services.sandbox import windows_appcontainer as wac

        script = join_for_shell(command)
        timeout = timeout_seconds or self.policy.timeout_seconds
        base = {key: value for key, value in os.environ.items() if key in self.policy.env_allowlist}
        base.update(self.policy.filter_env(env or {}))
        rules = rules_module.load(self.policy.rules_file or None)
        started = time.perf_counter()
        report = await asyncio.to_thread(
            wac.run_command,
            script,
            workspace=workspace,
            env=base,
            timeout=timeout,
            memory=self.policy.memory,
            pids=self.policy.pids,
            cpus=self.policy.cpus,
            io_bps=self.policy.io_bps,
            disk_mb=self.policy.disk_mb,
            rule_set=rules,
            # 任务级隔离：换成这个任务的包，并补上工具链之外的只读根（git 安装与配置）。
            profile_name=profile_name,
            extra_read=extra_read,
        )
        duration = int((time.perf_counter() - started) * 1000)
        enforcement = await asyncio.to_thread(_enforcement_appcontainer, report)
        stdout, trunc_out = _clip(_decode_host(report.stdout))
        stderr, trunc_err = _clip(_decode_host(report.stderr))
        if report.error:
            stderr = (stderr + "\n" + report.error).strip() if stderr else report.error
        return SandboxResult(
            command=redact(script),
            exit_code=None if report.timed_out else report.exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration,
            timed_out=report.timed_out,
            backend=self.name,
            truncated=trunc_out or trunc_err,
            notes=_enforcement_notes(enforcement) + list(report.notes),
            enforcement=enforcement,
        )

    async def cleanup(self) -> int:
        return 0


def _enforcement_appcontainer(report: "CommandReport") -> dict[str, object]:
    """AppContainer 后端这一次执行的账：边界只认行为自检，不从能力推断。"""
    from blank_api.services.sandbox import windows_appcontainer as wac

    cap = boundary.measure()
    check = wac.cached()
    ok = bool(check and check.ok)
    job = report.job
    enforced = bool(job.created and job.assigned)
    strength = "os_enforced" if ok else "unknown"
    return {
        "boundary_strength": strength,
        "boundary_strength_label": boundary.strength_label(strength),
        "boundary_measured_seconds_ago": cap.as_dict().get("age_seconds"),
        "boundary_apparatus_ok": ok,
        "limits_applied": True,
        "job_created": job.created,
        "job_assigned": job.assigned,
        "job_error": job.error,
        "memory_enforced": enforced and "memory_bytes" in job.enforced,
        "memory_bytes": int(job.enforced.get("memory_bytes", 0)),
        "pids_enforced": enforced and "active_process_limit" in job.enforced,
        "pids_limit": int(job.enforced.get("active_process_limit", 0)),
        "tree_kill_enforced": enforced and "kill_on_close" in job.enforced,
        "filesystem_enforced": ok and check.file_boundary_ok,
        "network_enforced": ok and check.network_ok,
        "cpu_enforced": enforced and "cpu_rate" in job.enforced and cap.cpu_rate,
        "io_enforced": enforced and "io_bps" in job.enforced and cap.io_rate,
        "io_bps": int(job.enforced.get("io_bps", 0)),
        "io_error": job.io_error,
        # 临时区落在容量卷上 + 本机探针真的把 64MB 卷写满过，两条同真才算。
        "disk_enforced": bool(cap.disk_quota and report.disk_on_quota),
        "disk_bytes": int(report.disk_bytes) if report.disk_on_quota else 0,
        "disk_error": "" if (cap.disk_quota and report.disk_on_quota) else (
            report.disk_error or cap.disk_quota_error or "容量卷未经行为探针证明"
        ),
    }


def _enforcement_restricted(report: "CommandReport") -> dict[str, object]:
    """受限令牌后端这一次执行的账：Job 事实来自执行本身，文件/网络来自行为自检。"""
    cap = boundary.measure()
    job = report.job
    enforced = bool(job.created and job.assigned)
    return {
        "boundary_strength": cap.strength,
        "boundary_strength_label": cap.label,
        "boundary_measured_seconds_ago": cap.as_dict().get("age_seconds"),
        "boundary_apparatus_ok": cap.apparatus_ok,
        "limits_applied": True,
        "job_created": job.created,
        "job_assigned": job.assigned,
        "job_error": job.error,
        "memory_enforced": enforced and "memory_bytes" in job.enforced,
        "memory_bytes": int(job.enforced.get("memory_bytes", 0)),
        "pids_enforced": enforced and "active_process_limit" in job.enforced,
        "pids_limit": int(job.enforced.get("active_process_limit", 0)),
        "tree_kill_enforced": enforced and "kill_on_close" in job.enforced,
        "filesystem_enforced": cap.filesystem_boundary,
        "network_enforced": cap.network_boundary,
        # Job 接受限速 + 探针证明这台机器压得住，两个都真才算真。
        "cpu_enforced": enforced and "cpu_rate" in job.enforced and cap.cpu_rate,
        "io_enforced": enforced and "io_bps" in job.enforced and cap.io_rate,
        "io_bps": int(job.enforced.get("io_bps", 0)),
        "io_error": job.io_error,
        # 临时区落在容量卷上 + 本机探针真的把 64MB 卷写满过，两条同真才算。
        "disk_enforced": bool(cap.disk_quota and report.disk_on_quota),
        "disk_bytes": int(report.disk_bytes) if report.disk_on_quota else 0,
        "disk_error": "" if (cap.disk_quota and report.disk_on_quota) else (
            report.disk_error or cap.disk_quota_error or "容量卷未经行为探针证明"
        ),
    }


def _local_strength(cap: boundary.Capability) -> str:
    """local 这次执行的真实强度：Job 在就 job_only；机器能造受限令牌也不借来冒充。"""
    if not cap.apparatus_ok:
        return "unknown"
    return "job_only" if cap.job else "none"


def limits_not_applied(reason: str) -> dict[str, object]:
    """沙箱开关为关（命令直接跑在宿主进程上）时的账：每一项都写 False，并写清为什么。

    这条是给 `off` 档用的。没有它，界面只能拿 policy 里的配置值当生效值展示——
    设计稿 §1 里"展示值冒充生效值"那一条就是这么产生的。键集合与 `_enforcement` 保持一致，
    读的一方不用分两种形状。
    """
    cap = boundary.measure()
    strength = "none"
    return {
        "boundary_strength": strength,
        "boundary_strength_label": boundary.strength_label(strength),
        "boundary_measured_seconds_ago": cap.as_dict().get("age_seconds"),
        "boundary_apparatus_ok": cap.apparatus_ok,
        "limits_applied": False,
        "job_created": False,
        "job_assigned": False,
        "job_error": reason,
        "memory_enforced": False,
        "memory_bytes": 0,
        "pids_enforced": False,
        "pids_limit": 0,
        "tree_kill_enforced": False,
        "filesystem_enforced": False,
        "network_enforced": False,
        "cpu_enforced": False,
        "io_enforced": False,
        "io_bps": 0,
        "io_error": reason,
        "disk_enforced": False,
        "disk_bytes": 0,
        "disk_error": reason,
    }


def _enforcement(
    job: windows_job.Job,
    assigned: bool,
    assign_error: str,
    volume: windows_volume.VolumeReport | None = None,
    scratch: str = "",
) -> dict[str, object]:
    """把"哪几条真的被内核强制了"算成结构化事实。测不出来就写 unknown，不许默认成真。"""
    cap = boundary.measure()
    strength = _local_strength(cap)
    enforced = bool(job.ok and assigned)
    disk_ok = bool(cap.disk_quota and volume is not None and volume.ok and scratch)
    if disk_ok:
        disk_error = ""
    elif volume is None:
        disk_error = "本次执行未准备容量卷"
    elif not volume.ok:
        disk_error = volume.error or cap.disk_quota_error or "容量卷挂载失败"
    elif not scratch:
        disk_error = "容量卷已挂载，但临时目录没建在卷上"
    else:
        disk_error = cap.disk_quota_error or "容量卷未经行为探针证明"
    return {
        "boundary_strength": strength,
        "boundary_strength_label": boundary.strength_label(strength),
        "boundary_measured_seconds_ago": cap.as_dict().get("age_seconds"),
        "boundary_apparatus_ok": cap.apparatus_ok,
        "limits_applied": True,
        "job_created": job.ok,
        "job_assigned": assigned,
        "job_error": job.error or assign_error or "",
        "memory_enforced": enforced and "memory_bytes" in job.enforced,
        "memory_bytes": int(job.enforced.get("memory_bytes", 0)),
        "pids_enforced": enforced and "active_process_limit" in job.enforced,
        "pids_limit": int(job.enforced.get("active_process_limit", 0)),
        "tree_kill_enforced": enforced and "kill_on_close" in job.enforced,
        # local 后端以宿主令牌启动命令：机器上"能造出受限令牌"与本次执行无关，不许借来冒充。
        # 文件边界只有在 backend=restricted（见 _enforcement_restricted）成立。
        "filesystem_enforced": False,
        "network_enforced": False,
        # 本次 Job 真接受了限速，且本机探针证明内核压得住；缺一个都不算。
        "cpu_enforced": enforced and "cpu_rate" in job.enforced and cap.cpu_rate,
        "io_enforced": enforced and "io_bps" in job.enforced and cap.io_rate,
        "io_bps": int(job.enforced.get("io_bps", 0)),
        "io_error": job.io_error,
        # 容量卷只在"卷真的挂上 + 临时目录真的落在卷上 + 探针把 64MB 卷写满过"时才算强制；
        # 它约束的是沙箱临时区，与工作区容量无关。
        "disk_enforced": disk_ok,
        "disk_bytes": int(volume.capacity_bytes) if (volume is not None and volume.ok) else 0,
        "disk_error": disk_error,
    }


def _enforcement_notes(enforcement: dict[str, object]) -> list[str]:
    out: list[str] = []
    if enforcement["memory_enforced"] and enforcement["pids_enforced"]:
        out.append("内存上限 {}MB、活跃进程上限 {} 由 Windows Job Object 强制（内核级，非我们数着玩）".format(
            round(int(enforcement["memory_bytes"]) / 1024 / 1024, 1), enforcement["pids_limit"]))
    elif enforcement["job_created"] and not enforcement["job_assigned"]:
        out.append("限额没生效：子进程没能挂进 Job（{}）".format(enforcement["job_error"]))
    else:
        out.append("限额未由内核强制：{}".format(enforcement["job_error"] or "本平台无 Job Object"))
    if enforcement.get("cpu_enforced"):
        out.append("CPU 上限由 Job CPU rate 强制（整棵进程树合计，按整机算力折算）")
    if enforcement.get("io_enforced"):
        out.append("读写带宽上限 {}MB/s 由 Job IO rate 强制（整棵进程树、所有卷合计）".format(
            round(int(enforcement["io_bps"]) / 1024 / 1024, 1)))
    elif enforcement.get("io_bps"):
        out.append("IO 速率上限已设进 Job，但本机探针没能现场证明节流（按未强制记账）：{}".format(
            enforcement.get("io_error") or "探针未通过"))
    if enforcement.get("disk_enforced"):
        out.append("沙箱临时区（TEMP/TMP）容量上限 {}MB 由独立 NTFS 卷强制；工作区写入不受这条上限约束".format(
            round(int(enforcement.get("disk_bytes", 0)) / 1024 / 1024)))
    else:
        out.append("临时区容量上限未由内核强制：{}".format(
            enforcement.get("disk_error") or "未启用容量卷"))
    if enforcement["tree_kill_enforced"]:
        out.append("命令结束时整棵进程树被 Job 带走（KILL_ON_JOB_CLOSE 已生效）")
    if not enforcement["filesystem_enforced"]:
        out.append("文件边界未由系统强制：模型把路径藏在脚本字符串里时 OS 不会拒（边界强度={}）".format(
            enforcement["boundary_strength"]))
    if not enforcement["network_enforced"]:
        out.append("网络边界未由系统强制：默认全部放行，靠审批与审计")
    return out


async def kill_process_tree(pid: int) -> None:
    if sys.platform == "win32":
        proc = await asyncio.create_subprocess_exec(
            "taskkill", "/PID", str(pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
        return
    import signal

    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def get_sandbox(policy: SandboxPolicy | None = None):
    """按策略返回沙箱实现；生产配置不会静默降级到宿主机。

    容器后端能不能用由现场探测决定，不看 PATH 里有没有那个文件：装了 Docker 但守护进程
    没起来时返回 DockerSandbox，只会让每条命令各自撞一次墙（`docs/COST.md` 记的那条）。
    探测不到可用容器时，如果部署打开了 AppContainer 后端（`BLANK_SANDBOX_APPCONTAINER=1`，
    需管理员）且行为自检全绿，就用它（文件与网络都由内核拒）；其次是受限令牌后端
    （`BLANK_SANDBOX_RESTRICTED=1`，文件边界由 NTFS 强制）；都没有才退 LocalSandbox
    （产品默认）。`allow_local=False` 才是"只准隔离后端"的部署选择，那种情况下都没有只能拒绝。
    """
    chosen = policy or SandboxPolicy()
    if not chosen.enabled:
        return LocalSandbox(chosen)
    info = docker_probe.probe_for_decision()
    if info.usable:
        return DockerSandbox(chosen)
    if info.state == docker_probe.STATE_PENDING:
        # 还没探过（进程刚起来的瞬间、或探针没跑到的入口）。退回老口径：PATH 里有 docker
        # 就当容器后端——宁可少拦一次，也不要在没测过的情况下宣称"不可用"而拦住正常机器。
        if shutil_which("docker") is not None:
            return DockerSandbox(chosen)
    if chosen.appcontainer_enabled:
        from blank_api.services.sandbox import windows_appcontainer as wac

        # 只认进程内已有的全绿结论；没有就在后台起探测，本拍退回下一档（收紧方向）。
        if wac.available():
            return AppContainerSandbox(chosen)
    if chosen.restricted_enabled:
        from blank_api.services.sandbox import windows_restricted as wr

        if wr.available():
            return RestrictedSandbox(chosen)
    if chosen.allow_local:
        return LocalSandbox(chosen)
    raise SandboxUnavailable(
        "Docker 沙箱不可用"
        + docker_probe.notice_suffix(info)
        + "；本部署要求隔离后端（容器 / AppContainer / 受限令牌）（BLANK_SANDBOX_ALLOW_LOCAL=0）；"
        "如需本机受限额执行，请设置 BLANK_SANDBOX_ALLOW_LOCAL=1 或删除该变量"
    )


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)
